import Foundation
import HealthKit

@MainActor
final class HealthKitSyncManager: ObservableObject {
    static let shared = HealthKitSyncManager()

    enum State: Equatable {
        case unavailable
        case notRequested
        case ready
        case syncing
        case failed(String)
    }

    @Published private(set) var state: State
    @Published private(set) var lastUploadAt: Date?

    private let store = HKHealthStore()
    private let api = HealthMesAPI()
    private let defaults = AppGroup.userDefaults
    private let lastUploadKey = "healthmes.healthkit.lastUploadAt"
    private let queryLimit = 1_000
    private var observerQueries: [HKObserverQuery] = []
    private var syncInProgress = false

    private struct QuantitySpec {
        let type: HKQuantityType
        let unit: HKUnit
        let wireUnit: String
        let multiplier: Double
    }

    private init() {
        lastUploadAt = defaults.object(forKey: lastUploadKey) as? Date
        state = HKHealthStore.isHealthDataAvailable() ? .notRequested : .unavailable
    }

    var statusText: String {
        switch state {
        case .unavailable:
            return String(localized: "Unavailable on this device")
        case .notRequested:
            return String(localized: "Permission required")
        case .ready:
            guard let lastUploadAt else {
                return String(localized: "Ready · no upload yet")
            }
            return String(
                localized: "Synced \(lastUploadAt.formatted(.relative(presentation: .named)))"
            )
        case .syncing:
            return String(localized: "Syncing…")
        case .failed:
            return String(localized: "Sync needs attention")
        }
    }

    func requestAuthorizationAndSync() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            state = .unavailable
            return
        }
        do {
            try await store.requestAuthorization(toShare: [], read: readTypes)
            try await enableBackgroundDelivery()
            installObservers()
            state = .ready
            await sync()
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func resume() async {
        guard HKHealthStore.isHealthDataAvailable() else {
            state = .unavailable
            return
        }
        installObservers()
        await sync()
    }

    func sync() async {
        guard !syncInProgress else { return }
        guard PairingStore.shared.load() != nil else { return }
        guard HKHealthStore.isHealthDataAvailable() else {
            state = .unavailable
            return
        }
        syncInProgress = true
        defer { syncInProgress = false }
        state = .syncing
        do {
            while true {
                let batch = try await collectBatch()
                guard batch.hasChanges else { break }
                let ack = try await api.uploadHealthKit(batch.payload)
                guard ack.durable else {
                    throw NSError(
                        domain: "HealthMes.HealthKit",
                        code: 2,
                        userInfo: [
                            NSLocalizedDescriptionKey:
                                "The personal server did not confirm durable HealthKit storage."
                        ]
                    )
                }
                for (key, anchor) in batch.anchors {
                    saveAnchor(anchor, key: key)
                }
                let now = Date()
                defaults.set(now, forKey: lastUploadKey)
                lastUploadAt = now
                if !batch.hasMore {
                    break
                }
            }
            state = .ready
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private var quantitySpecs: [QuantitySpec] {
        [
            quantity(.heartRate, unit: .count().unitDivided(by: .minute()), wire: "count/min"),
            quantity(
                .restingHeartRate,
                unit: .count().unitDivided(by: .minute()),
                wire: "count/min"
            ),
            quantity(.heartRateVariabilitySDNN, unit: .secondUnit(with: .milli), wire: "ms"),
            quantity(
                .respiratoryRate,
                unit: .count().unitDivided(by: .minute()),
                wire: "count/min"
            ),
            quantity(.oxygenSaturation, unit: .percent(), wire: "%", multiplier: 100),
            quantity(.stepCount, unit: .count(), wire: "count"),
            quantity(.activeEnergyBurned, unit: .kilocalorie(), wire: "kcal"),
            quantity(.distanceWalkingRunning, unit: .meter(), wire: "m"),
            quantity(.appleSleepingWristTemperature, unit: .degreeCelsius(), wire: "degC"),
        ].compactMap { $0 }
    }

    private var readTypes: Set<HKObjectType> {
        var types = Set(quantitySpecs.map(\.type) as [HKObjectType])
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) {
            types.insert(sleep)
        }
        types.insert(HKObjectType.workoutType())
        return types
    }

    private func quantity(
        _ identifier: HKQuantityTypeIdentifier,
        unit: HKUnit,
        wire: String,
        multiplier: Double = 1
    ) -> QuantitySpec? {
        guard let type = HKObjectType.quantityType(forIdentifier: identifier) else {
            return nil
        }
        return QuantitySpec(
            type: type,
            unit: unit,
            wireUnit: wire,
            multiplier: multiplier
        )
    }

    private struct Batch {
        let payload: HealthKitIngestPayload
        let anchors: [String: HKQueryAnchor]
        let hasMore: Bool

        var hasChanges: Bool {
            let data = payload.data
            return !data.records.isEmpty
                || !data.sleep.isEmpty
                || !data.workouts.isEmpty
                || !data.deletions.isEmpty
        }
    }

    private func collectBatch() async throws -> Batch {
        var metrics: [HealthKitIngestPayload.Metric] = []
        var sleepRows: [HealthKitIngestPayload.Sleep] = []
        var workouts: [HealthKitIngestPayload.Workout] = []
        var deletions: [HealthKitIngestPayload.Deletion] = []
        var anchors: [String: HKQueryAnchor] = [:]
        var hasMore = false

        for spec in quantitySpecs {
            let key = spec.type.identifier
            let result = try await anchoredSamples(
                type: spec.type,
                anchor: loadAnchor(key: key)
            )
            metrics += result.samples.compactMap { sample in
                guard let quantity = sample as? HKQuantitySample else { return nil }
                return .init(
                    id: quantity.uuid.uuidString.lowercased(),
                    type: spec.type.identifier,
                    startDate: quantity.startDate,
                    endDate: quantity.endDate,
                    value: quantity.quantity.doubleValue(for: spec.unit) * spec.multiplier,
                    unit: spec.wireUnit,
                    zoneOffset: HealthKitWireFormat.zoneOffset(for: quantity.startDate),
                    source: Self.sourceInfo(for: quantity)
                )
            }
            deletions += result.deleted.map {
                .init(id: $0.uuid.uuidString.lowercased(), type: key)
            }
            anchors[key] = result.anchor
            hasMore = hasMore || result.hasMore
        }

        if let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) {
            let key = sleepType.identifier
            let result = try await anchoredSamples(
                type: sleepType,
                anchor: loadAnchor(key: key)
            )
            sleepRows += result.samples.compactMap { sample in
                guard let category = sample as? HKCategorySample else { return nil }
                return .init(
                    id: category.uuid.uuidString.lowercased(),
                    stage: Self.sleepStage(category.value),
                    startDate: category.startDate,
                    endDate: category.endDate,
                    zoneOffset: HealthKitWireFormat.zoneOffset(for: category.startDate),
                    source: Self.sourceInfo(for: category)
                )
            }
            deletions += result.deleted.map {
                .init(id: $0.uuid.uuidString.lowercased(), type: key)
            }
            anchors[key] = result.anchor
            hasMore = hasMore || result.hasMore
        }

        let workoutType = HKObjectType.workoutType()
        let workoutKey = workoutType.identifier
        let workoutResult = try await anchoredSamples(
            type: workoutType,
            anchor: loadAnchor(key: workoutKey)
        )
        workouts += workoutResult.samples.compactMap { sample in
            guard let workout = sample as? HKWorkout else { return nil }
            var values = [
                HealthKitIngestPayload.Statistic(
                    type: "duration",
                    unit: "s",
                    value: workout.duration
                )
            ]
            if let energy = workout.totalEnergyBurned {
                values.append(
                    .init(
                        type: "calories",
                        unit: "kcal",
                        value: energy.doubleValue(for: .kilocalorie())
                    )
                )
            }
            if let distance = workout.totalDistance {
                values.append(
                    .init(
                        type: "distance",
                        unit: "m",
                        value: distance.doubleValue(for: .meter())
                    )
                )
            }
            return .init(
                id: workout.uuid.uuidString.lowercased(),
                type: Self.workoutType(workout.workoutActivityType),
                startDate: workout.startDate,
                endDate: workout.endDate,
                values: values,
                zoneOffset: HealthKitWireFormat.zoneOffset(for: workout.startDate),
                source: Self.sourceInfo(for: workout)
            )
        }
        deletions += workoutResult.deleted.map {
            .init(id: $0.uuid.uuidString.lowercased(), type: workoutKey)
        }
        anchors[workoutKey] = workoutResult.anchor
        hasMore = hasMore || workoutResult.hasMore

        return Batch(
            payload: .init(
                data: .init(
                    records: metrics,
                    sleep: sleepRows,
                    workouts: workouts,
                    deletions: deletions
                )
            ),
            anchors: anchors,
            hasMore: hasMore
        )
    }

    private struct AnchoredResult {
        let samples: [HKSample]
        let deleted: [HKDeletedObject]
        let anchor: HKQueryAnchor
        let hasMore: Bool
    }

    private func anchoredSamples(
        type: HKSampleType,
        anchor: HKQueryAnchor?
    ) async throws -> AnchoredResult {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: type,
                predicate: nil,
                anchor: anchor,
                limit: queryLimit
            ) { _, samples, deleted, newAnchor, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(
                        returning: AnchoredResult(
                            samples: samples ?? [],
                            deleted: deleted ?? [],
                            anchor: newAnchor ?? anchor ?? HKQueryAnchor(fromValue: 0),
                            hasMore: (samples?.count ?? 0) + (deleted?.count ?? 0)
                                >= self.queryLimit
                        )
                    )
                }
            }
            store.execute(query)
        }
    }

    private func enableBackgroundDelivery() async throws {
        for type in readTypes.compactMap({ $0 as? HKSampleType }) {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                store.enableBackgroundDelivery(for: type, frequency: .hourly) {
                    success, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else if success {
                        continuation.resume()
                    } else {
                        continuation.resume(
                            throwing: NSError(
                                domain: "HealthMes.HealthKit",
                                code: 1,
                                userInfo: [
                                    NSLocalizedDescriptionKey:
                                        "HealthKit background delivery was not enabled."
                                ]
                            )
                        )
                    }
                }
            }
        }
    }

    private func installObservers() {
        guard observerQueries.isEmpty else { return }
        for type in readTypes.compactMap({ $0 as? HKSampleType }) {
            let query = HKObserverQuery(sampleType: type, predicate: nil) {
                _, completion, _ in
                Task { @MainActor in
                    await self.sync()
                    completion()
                }
            }
            observerQueries.append(query)
            store.execute(query)
        }
    }

    private func anchorKey(_ key: String) -> String {
        let pairing = PairingStore.shared.load()?.cacheFingerprint ?? "unpaired"
        return "healthmes.healthkit.anchor.\(pairing).\(key)"
    }

    private func loadAnchor(key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: anchorKey(key)) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(
            ofClass: HKQueryAnchor.self,
            from: data
        )
    }

    private func saveAnchor(_ anchor: HKQueryAnchor, key: String) {
        guard
            let data = try? NSKeyedArchiver.archivedData(
                withRootObject: anchor,
                requiringSecureCoding: true
            )
        else { return }
        defaults.set(data, forKey: anchorKey(key))
    }

    private static func sleepStage(_ value: Int) -> String {
        switch value {
        case HKCategoryValueSleepAnalysis.inBed.rawValue: return "in_bed"
        case HKCategoryValueSleepAnalysis.awake.rawValue: return "awake"
        case HKCategoryValueSleepAnalysis.asleepCore.rawValue: return "light"
        case HKCategoryValueSleepAnalysis.asleepDeep.rawValue: return "deep"
        case HKCategoryValueSleepAnalysis.asleepREM.rawValue: return "rem"
        case HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue: return "sleeping"
        default: return "unknown"
        }
    }

    private static func workoutType(_ value: HKWorkoutActivityType) -> String {
        switch value {
        case .walking: return "walking"
        case .running: return "running"
        case .cycling: return "cycling"
        case .hiking: return "hiking"
        case .yoga: return "yoga"
        case .swimming: return "swimming"
        case .functionalStrengthTraining: return "functional_strength_training"
        case .traditionalStrengthTraining: return "strength_training"
        case .highIntensityIntervalTraining: return "hiit"
        case .mindAndBody: return "mind_and_body"
        case .pilates: return "pilates"
        default: return "other"
        }
    }

    private static func sourceInfo(
        for sample: HKSample
    ) -> HealthKitIngestPayload.Source {
        let revision = sample.sourceRevision
        let device = sample.device
        let product = revision.productType
        let classifier = [
            product,
            device?.model,
            device?.name,
        ]
        .compactMap { $0?.lowercased() }
        .joined(separator: " ")
        let deviceType: String
        if classifier.contains("watch") {
            deviceType = "watch"
        } else if classifier.contains("iphone") || classifier.contains("phone") {
            deviceType = "phone"
        } else if classifier.contains("ring") {
            deviceType = "ring"
        } else {
            deviceType = "unknown"
        }
        return .init(
            appId: revision.source.bundleIdentifier,
            name: revision.source.name,
            bundleIdentifier: revision.source.bundleIdentifier,
            version: revision.version,
            productType: product,
            deviceId: device?.localIdentifier,
            deviceName: device?.name,
            deviceManufacturer: device?.manufacturer,
            deviceType: deviceType,
            deviceModel: device?.model,
            deviceHardwareVersion: device?.hardwareVersion,
            deviceSoftwareVersion: device?.softwareVersion
        )
    }
}
