import Foundation
import HealthKit

@MainActor
final class HealthKitSyncManager: ObservableObject {
    static let shared = HealthKitSyncManager()

    private enum SyncError: Error, LocalizedError {
        case invalidStoredAnchor

        var errorDescription: String? {
            switch self {
            case .invalidStoredAnchor:
                return "A queued HealthKit anchor could not be restored."
            }
        }
    }

    enum State: Equatable {
        case unavailable
        case notRequested
        case ready
        case paused
        case syncing
        case failed(String)
    }

    @Published private(set) var state: State
    @Published private(set) var lastUploadAt: Date?
    @Published private(set) var pendingUploadCount = 0
    @Published private(set) var nextRetryAt: Date?
    @Published private(set) var isPaused = false

    private let store = HKHealthStore()
    private let api = HealthMesAPI()
    private let outbox = HealthKitSyncOutbox.shared
    private let defaults = AppGroup.userDefaults
    private let lastUploadKeyPrefix = "healthmes.healthkit.lastUploadAt"
    private let pauseKeyPrefix = "healthmes.healthkit.paused"
    private let queryLimit = 1_000
    private var observerQueries: [HKObserverQuery] = []
    private var syncInProgress = false
    private var syncRequestedWhileActive = false
    private var forceRetryRequested = false
    private var syncWaiters: [CheckedContinuation<Void, Never>] = []
    private var syncOperationGate = PairingOperationGate()

    private struct QuantitySpec {
        let type: HKQuantityType
        let unit: HKUnit
        let wireUnit: String
        let multiplier: Double
    }

    private init() {
        state = HKHealthStore.isHealthDataAvailable() ? .notRequested : .unavailable
        lastUploadAt = nil
        if let pairing = PairingStore.shared.load() {
            lastUploadAt = defaults.object(
                forKey: lastUploadKey(pairing.cacheFingerprint)
            ) as? Date
            isPaused = defaults.bool(
                forKey: pauseKey(pairing.cacheFingerprint)
            )
        }
        if isPaused, state != .unavailable {
            state = .paused
        }
    }

    var statusText: String {
        switch state {
        case .unavailable:
            return String(localized: "Unavailable on this device")
        case .notRequested:
            return String(localized: "Permission required")
        case .ready:
            if pendingUploadCount > 0 {
                return String(
                    localized: "\(pendingUploadCount) upload(s) queued"
                )
            }
            guard let lastUploadAt else {
                return String(localized: "Ready · no upload yet")
            }
            return String(
                localized: "Synced \(lastUploadAt.formatted(.relative(presentation: .named)))"
            )
        case .paused:
            return pendingUploadCount == 0
                ? String(localized: "Paused")
                : String(
                    localized:
                        "Paused · \(pendingUploadCount) upload(s) queued"
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
            setPaused(false, for: PairingStore.shared.load())
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
        await refreshStatus()
        guard !isPaused else {
            state = .paused
            return
        }
        installObservers()
        await sync()
    }

    func refreshStatus() async {
        for _ in 0..<2 {
            guard let pairing = PairingStore.shared.load() else {
                resetUnpairedState()
                return
            }
            loadLocalState(for: pairing)
            guard await refreshQueueStatus(for: pairing) else {
                if PairingStore.shared.load() != pairing {
                    continue
                }
                return
            }
            guard state != .unavailable else { return }
            if isPaused {
                state = .paused
            } else if state == .syncing
                || state == .ready
                || lastUploadAt != nil
                || pendingUploadCount > 0
            {
                state = .ready
            }
            return
        }
        if PairingStore.shared.load() == nil {
            resetUnpairedState()
        }
    }

    func pairingDidChange() async {
        syncOperationGate.invalidate()
        await refreshStatus()
    }

    func pauseSync() async {
        guard let pairing = PairingStore.shared.load() else { return }
        setPaused(true, for: pairing)
        syncOperationGate.invalidate()
        guard await refreshQueueStatus(for: pairing) else { return }
        state = .paused
    }

    func resumeSync() async {
        guard let pairing = PairingStore.shared.load() else { return }
        setPaused(false, for: pairing)
        state = .ready
        await retryPendingUploads()
    }

    func retryPendingUploads() async {
        forceRetryRequested = true
        await sync()
    }

    func backgroundSync() async -> Bool {
        await sync()
        switch state {
        case .ready, .paused:
            return true
        case .unavailable, .notRequested, .syncing, .failed:
            return false
        }
    }

    func deletePendingUploads() async {
        guard let pairing = PairingStore.shared.load() else { return }
        do {
            setPaused(true, for: pairing)
            syncOperationGate.invalidate()
            _ = try await outbox.purge(
                destinationFingerprint: pairing.cacheFingerprint
            )
            guard await refreshQueueStatus(for: pairing) else { return }
            state = .paused
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func prepareForUnpair(_ pairing: Pairing) async throws {
        setPaused(true, for: pairing)
        syncOperationGate.invalidate()
        await sync()
        _ = try await outbox.purge(
            destinationFingerprint: pairing.cacheFingerprint
        )
        clearPersistedState(for: pairing)
        pendingUploadCount = 0
        nextRetryAt = nil
        lastUploadAt = nil
    }

    func sync() async {
        if syncInProgress {
            syncRequestedWhileActive = true
            await withCheckedContinuation { continuation in
                syncWaiters.append(continuation)
            }
            return
        }
        syncInProgress = true
        repeat {
            syncRequestedWhileActive = false
            let forcePending = forceRetryRequested
            forceRetryRequested = false
            await performSyncPass(forcePending: forcePending)
        } while syncRequestedWhileActive
        syncInProgress = false
        let completedWaiters = syncWaiters
        syncWaiters.removeAll(keepingCapacity: true)
        for waiter in completedWaiters {
            waiter.resume()
        }
        await settleStateAfterSyncIfNeeded()
    }

    private enum PendingDrainResult: Equatable {
        case drained
        case deferred
    }

    private func performSyncPass(forcePending: Bool) async {
        guard let pairingSnapshot = PairingStore.shared.load() else {
            await refreshStatus()
            return
        }
        guard HKHealthStore.isHealthDataAvailable() else {
            state = .unavailable
            return
        }
        loadLocalState(for: pairingSnapshot)
        guard !isPaused else {
            guard await refreshQueueStatus(for: pairingSnapshot) else {
                return
            }
            state = .paused
            return
        }
        let syncOperation = syncOperationGate.begin(pairing: pairingSnapshot)
        state = .syncing
        do {
            let drainResult = try await drainPendingUploads(
                pairing: pairingSnapshot,
                operation: syncOperation,
                forceRetry: forcePending
            )
            guard drainResult == .drained else {
                state = .ready
                return
            }

            while true {
                guard isCurrent(syncOperation) else { return }
                let batch = try await collectBatch(
                    pairingFingerprint: pairingSnapshot.cacheFingerprint
                )
                guard isCurrent(syncOperation) else { return }
                guard batch.hasChanges else { break }
                let body = try HealthMesAPI.healthKitUploadBody(
                    batch.payload
                )
                let anchors = try archiveAnchors(batch.anchors)
                _ = try await outbox.enqueue(
                    body: body,
                    anchors: anchors,
                    destinationFingerprint:
                        pairingSnapshot.cacheFingerprint
                )
                guard await refreshQueueStatus(for: pairingSnapshot) else {
                    return
                }
                _ = try await drainPendingUploads(
                    pairing: pairingSnapshot,
                    operation: syncOperation,
                    forceRetry: true
                )
                guard isCurrent(syncOperation) else { return }
                if !batch.hasMore {
                    break
                }
            }
            guard await refreshQueueStatus(for: pairingSnapshot) else {
                return
            }
            state = .ready
        } catch {
            await refreshQueueStatus(for: pairingSnapshot)
            if isCurrent(syncOperation) {
                state = .failed(error.localizedDescription)
            }
        }
    }

    private func drainPendingUploads(
        pairing: Pairing,
        operation: PairingOperationToken,
        forceRetry: Bool
    ) async throws -> PendingDrainResult {
        let fingerprint = pairing.cacheFingerprint
        while true {
            guard isCurrent(operation) else { return .deferred }
            let entries = try await outbox.pendingEntries(
                destinationFingerprint: fingerprint
            )
            pendingUploadCount = entries.count
            nextRetryAt = entries.first?.nextAttemptAt
            guard let entry = entries.first else {
                nextRetryAt = nil
                return .drained
            }
            guard forceRetry || entry.isDue(at: Date()) else {
                return .deferred
            }

            do {
                _ = try await api.uploadHealthKit(
                    body: entry.body,
                    idempotencyKey: entry.idempotencyKey,
                    pairing: pairing
                )
            } catch {
                _ = try? await outbox.markFailed(
                    idempotencyKey: entry.idempotencyKey,
                    destinationFingerprint: fingerprint
                )
                await refreshQueueStatus(for: pairing)
                throw error
            }

            guard isCurrent(operation) else {
                return .deferred
            }
            try commitAnchors(
                entry.anchors,
                pairingFingerprint: fingerprint
            )
            try await outbox.markSucceeded(
                idempotencyKey: entry.idempotencyKey,
                destinationFingerprint: fingerprint
            )
            recordSuccessfulUpload(for: fingerprint)
            guard await refreshQueueStatus(for: pairing) else {
                return .deferred
            }
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

    private func collectBatch(pairingFingerprint: String) async throws -> Batch {
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
                anchor: loadAnchor(key: key, pairingFingerprint: pairingFingerprint)
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
                anchor: loadAnchor(key: key, pairingFingerprint: pairingFingerprint)
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
            anchor: loadAnchor(
                key: workoutKey,
                pairingFingerprint: pairingFingerprint
            )
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

    private func isCurrent(_ operation: PairingOperationToken) -> Bool {
        syncOperationGate.isCurrent(
            operation,
            pairing: PairingStore.shared.load()
        )
    }

    private func loadLocalState(for pairing: Pairing) {
        let fingerprint = pairing.cacheFingerprint
        lastUploadAt = defaults.object(
            forKey: lastUploadKey(fingerprint)
        ) as? Date
        isPaused = defaults.bool(forKey: pauseKey(fingerprint))
    }

    private func setPaused(_ paused: Bool, for pairing: Pairing?) {
        guard let pairing else {
            isPaused = false
            return
        }
        let key = pauseKey(pairing.cacheFingerprint)
        if paused {
            defaults.set(true, forKey: key)
        } else {
            defaults.removeObject(forKey: key)
        }
        isPaused = paused
    }

    @discardableResult
    private func refreshQueueStatus(for pairing: Pairing) async -> Bool {
        do {
            let entries = try await outbox.pendingEntries(
                destinationFingerprint: pairing.cacheFingerprint
            )
            guard PairingStore.shared.load() == pairing else {
                return false
            }
            pendingUploadCount = entries.count
            nextRetryAt = entries.first?.nextAttemptAt
            return true
        } catch {
            if PairingStore.shared.load() == pairing {
                pendingUploadCount = 0
                nextRetryAt = nil
                state = .failed(error.localizedDescription)
            }
            return false
        }
    }

    private func settleStateAfterSyncIfNeeded() async {
        guard state == .syncing else { return }
        await refreshStatus()
    }

    private func resetUnpairedState() {
        syncOperationGate.invalidate()
        lastUploadAt = nil
        pendingUploadCount = 0
        nextRetryAt = nil
        isPaused = false
        if state != .unavailable {
            state = .notRequested
        }
    }

    private func clearPersistedState(for pairing: Pairing) {
        let fingerprint = pairing.cacheFingerprint
        let anchorPrefix = "healthmes.healthkit.anchor.\(fingerprint)."
        defaults.removeObject(forKey: lastUploadKey(fingerprint))
        defaults.removeObject(forKey: pauseKey(fingerprint))
        for key in defaults.dictionaryRepresentation().keys
        where key.hasPrefix(anchorPrefix) {
            defaults.removeObject(forKey: key)
        }
        isPaused = false
    }

    private func recordSuccessfulUpload(for pairingFingerprint: String) {
        let now = Date()
        defaults.set(
            now,
            forKey: lastUploadKey(pairingFingerprint)
        )
        lastUploadAt = now
    }

    private func archiveAnchors(
        _ anchors: [String: HKQueryAnchor]
    ) throws -> [String: Data] {
        try anchors.mapValues { anchor in
            try NSKeyedArchiver.archivedData(
                withRootObject: anchor,
                requiringSecureCoding: true
            )
        }
    }

    private func commitAnchors(
        _ anchors: [String: Data],
        pairingFingerprint: String
    ) throws {
        for data in anchors.values {
            guard
                try NSKeyedUnarchiver.unarchivedObject(
                    ofClass: HKQueryAnchor.self,
                    from: data
                ) != nil
            else {
                throw SyncError.invalidStoredAnchor
            }
        }
        for (key, data) in anchors {
            defaults.set(
                data,
                forKey: anchorKey(
                    key,
                    pairingFingerprint: pairingFingerprint
                )
            )
        }
    }

    private func lastUploadKey(_ pairingFingerprint: String) -> String {
        "\(lastUploadKeyPrefix).\(pairingFingerprint)"
    }

    private func pauseKey(_ pairingFingerprint: String) -> String {
        "\(pauseKeyPrefix).\(pairingFingerprint)"
    }

    private func anchorKey(_ key: String, pairingFingerprint: String) -> String {
        "healthmes.healthkit.anchor.\(pairingFingerprint).\(key)"
    }

    private func loadAnchor(
        key: String,
        pairingFingerprint: String
    ) -> HKQueryAnchor? {
        guard
            let data = defaults.data(
                forKey: anchorKey(key, pairingFingerprint: pairingFingerprint)
            )
        else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(
            ofClass: HKQueryAnchor.self,
            from: data
        )
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
