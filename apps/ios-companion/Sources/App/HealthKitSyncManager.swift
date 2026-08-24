import Foundation
import HealthKit
import WidgetKit

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
    @Published private(set) var terminalFailureCount = 0
    @Published private(set) var latestTerminalFailure: String?
    @Published private(set) var nextRetryAt: Date?
    @Published private(set) var queueStatusError: String?
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
    private var quiescenceWaiters: [CheckedContinuation<Void, Never>] = []
    private var pairingTransitionInProgress = false
    private var syncOperationGate = PairingOperationGate()
    private var activeUpload: ActiveUpload?

    private struct ActiveUpload {
        let id: UUID
        let task: Task<HealthKitIngestAck, Error>
    }

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
            if terminalFailureCount > 0 {
                return String(
                    localized:
                        "\(terminalFailureCount) upload(s) need attention"
                )
            }
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
        guard await recoverInterruptedPairingTransition() else { return }
        guard !pairingTransitionInProgress else { return }
        guard HKHealthStore.isHealthDataAvailable() else {
            state = .unavailable
            return
        }
        do {
            try await store.requestAuthorization(toShare: [], read: readTypes)
            guard !pairingTransitionInProgress else { return }
            try await enableBackgroundDelivery()
            guard !pairingTransitionInProgress else { return }
            installObservers()
            setPaused(false, for: PairingStore.shared.load())
            await sync()
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func resume() async {
        guard await recoverInterruptedPairingTransition() else { return }
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
        guard await recoverInterruptedPairingTransition() else { return }
        PairingRelayGate.shared.reopenIfStable()
        await refreshStatus()
    }

    func replacePairing(with candidate: Pairing) async throws -> Pairing {
        guard !pairingTransitionInProgress else {
            throw PairingError.transitionInProgress
        }
        guard await recoverInterruptedPairingTransition() else {
            throw PairingError.transitionInProgress
        }
        let current = PairingStore.shared.load()
        guard current != candidate else {
            let saved = try savePairing(candidate)
            await pairingDidChange()
            return saved
        }

        let wasPaused = current.map {
            defaults.bool(forKey: pauseKey($0.cacheFingerprint))
        } ?? false
        await beginPairingTransition()
        do {
            let transition = try PairingStore.shared.beginReplacement(
                with: candidate
            )
            try await clearAccountSurfaces()
            try PairingStore.shared
                .markPendingTransitionCleanupStarted()
            try await removeLocalHealthData(
                fingerprint: transition.previousFingerprint,
                userDirectedRemoval: false
            )
            prepareClientState(for: transition.kind)
            guard
                let saved = try PairingStore.shared
                    .commitPendingTransition()
            else {
                throw PairingError.credentialStorageFailed
            }
            endPairingTransition()
            await refreshStatus()
            return saved
        } catch {
            if PairingStore.shared.hasPendingTransition {
                // Cleanup may already have changed the old queue. Keep every
                // process fenced and let the next lifecycle pass resume the
                // durable journal instead of exposing partial old state.
                pairingTransitionInProgress = false
                state = .failed(error.localizedDescription)
                throw error
            }
            if let current {
                setPaused(wasPaused, for: current)
            }
            endPairingTransition()
            await refreshStatus()
            throw error
        }
    }

    func unpair() async throws {
        guard !pairingTransitionInProgress else {
            throw PairingError.transitionInProgress
        }
        guard await recoverInterruptedPairingTransition() else {
            throw PairingError.transitionInProgress
        }
        let pairing = PairingStore.shared.load()
        guard
            pairing != nil
                || PairingStore.shared.hasPersistedPairingState
        else {
            resetUnpairedState()
            return
        }

        let wasPaused = pairing.map {
            defaults.bool(forKey: pauseKey($0.cacheFingerprint))
        } ?? false
        await beginPairingTransition()
        do {
            let transition = try PairingStore.shared.beginUnpair()
            try await clearAccountSurfaces()
            try PairingStore.shared
                .markPendingTransitionCleanupStarted()
            try await removeLocalHealthData(
                fingerprint: transition.previousFingerprint,
                userDirectedRemoval: true
            )
            prepareClientState(for: transition.kind)
            _ = try PairingStore.shared.commitPendingTransition()
            endPairingTransition()
            resetUnpairedState()
        } catch {
            if PairingStore.shared.hasPendingTransition {
                pairingTransitionInProgress = false
                state = .failed(error.localizedDescription)
                throw error
            }
            if let pairing {
                setPaused(wasPaused, for: pairing)
            }
            endPairingTransition()
            await refreshStatus()
            throw error
        }
    }

    func pauseSync() async {
        guard
            !pairingTransitionInProgress,
            !PairingStore.shared.hasPendingTransition
        else { return }
        guard let pairing = PairingStore.shared.load() else { return }
        setPaused(true, for: pairing)
        syncOperationGate.invalidate()
        guard await refreshQueueStatus(for: pairing) else { return }
        state = .paused
    }

    func resumeSync() async {
        guard !pairingTransitionInProgress else { return }
        guard let pairing = PairingStore.shared.load() else { return }
        setPaused(false, for: pairing)
        state = .ready
        await sync()
    }

    func retryPendingUploads() async {
        guard !pairingTransitionInProgress else { return }
        forceRetryRequested = true
        await sync()
    }

    func backgroundSync() async -> Bool {
        guard await recoverInterruptedPairingTransition() else {
            return false
        }
        await sync()
        guard !Task.isCancelled else { return false }
        switch state {
        case .ready, .paused:
            return true
        case .unavailable, .notRequested, .syncing, .failed:
            return false
        }
    }

    func deletePendingUploads() async {
        guard let pairing = PairingStore.shared.load() else { return }
        guard !pairingTransitionInProgress else { return }
        let wasPaused = defaults.bool(
            forKey: pauseKey(pairing.cacheFingerprint)
        )
        setPaused(true, for: pairing)
        await beginPairingTransition()
        do {
            _ = try await outbox.purgeForUserRemoval(
                destinationFingerprint: pairing.cacheFingerprint
            )
            let refreshed = await refreshQueueStatus(for: pairing)
            endPairingTransition()
            guard refreshed else { return }
            state = .paused
        } catch {
            setPaused(wasPaused, for: pairing)
            endPairingTransition()
            state = .failed(error.localizedDescription)
        }
    }

    private func beginPairingTransition() async {
        pairingTransitionInProgress = true
        await PairingRelayGate.shared.fenceAndWait()
        syncOperationGate.invalidate()
        syncRequestedWhileActive = false
        forceRetryRequested = false
        activeUpload?.task.cancel()
        await waitForSyncToQuiesce()
    }

    private func endPairingTransition() {
        pairingTransitionInProgress = false
        syncOperationGate.invalidate()
        PairingRelayGate.shared.reopenIfStable()
    }

    private func savePairing(_ pairing: Pairing) throws -> Pairing {
        try PairingStore.shared.save(
            baseURLString: pairing.baseURL.absoluteString,
            token: pairing.token ?? ""
        )
    }

    private func removeLocalHealthData(
        fingerprint: String?,
        userDirectedRemoval: Bool
    ) async throws {
        switch HealthKitSyncRemovalPolicy.scope(
            fingerprint: fingerprint,
            userDirectedRemoval: userDirectedRemoval
        ) {
        case .destination(let fingerprint):
            if userDirectedRemoval {
                _ = try await outbox.purgeForUserRemoval(
                    destinationFingerprint: fingerprint
                )
            } else {
                _ = try await outbox.purge(
                    destinationFingerprint: fingerprint
                )
            }
            HealthKitSyncLocalState.clear(
                fingerprint: fingerprint,
                defaults: defaults
            )
        case .all:
            _ = try await outbox.purgeAllForUserRemoval()
            HealthKitSyncLocalState.clearAll(defaults: defaults)
        case .none:
            return
        }
        pendingUploadCount = 0
        terminalFailureCount = 0
        latestTerminalFailure = nil
        nextRetryAt = nil
        queueStatusError = nil
        lastUploadAt = nil
    }

    private func prepareClientState(
        for transition: PairingTransitionKind
    ) {
        GlanceSnapshotCache.shared.clear()
        if transition == .replacement {
            SeenAlertsStore.shared.resetForPairingChange()
        } else {
            SeenAlertsStore.shared.clear()
        }
    }

    private func clearAccountSurfaces() async throws {
        guard await NotificationManager.shared.clearAccountSurfaces() else {
            throw PairingError.transitionInProgress
        }
        await DecisionLiveActivityController.shared.endAll()
        await LiveActivityController.shared.endAll()
    }

    private func recoverInterruptedPairingTransition() async -> Bool {
        guard PairingStore.shared.hasPendingTransition else {
            return true
        }
        guard !pairingTransitionInProgress else { return false }
        await beginPairingTransition()
        guard let transition = PairingStore.shared.pendingTransition() else {
            if
                let recovery =
                    PairingStore.shared.abortStagingTransitionIfSafe()
            {
                endPairingTransition()
                publishRecoveredPairing(recovery.pairing)
                await refreshStatus()
                return true
            }
            // A malformed journal cannot prove whether local cleanup began.
            // Keep every process fail-closed until the user retries or
            // reinstalls instead of exposing either destination.
            state = .failed(
                PairingError.transitionInProgress.localizedDescription
            )
            pairingTransitionInProgress = false
            return false
        }
        do {
            try await clearAccountSurfaces()
            try PairingStore.shared
                .markPendingTransitionCleanupStarted()
            try await removeLocalHealthData(
                fingerprint: transition.previousFingerprint,
                userDirectedRemoval: transition.kind == .unpair
            )
            prepareClientState(for: transition.kind)
            let pairing = try PairingStore.shared
                .commitPendingTransition()
            endPairingTransition()
            publishRecoveredPairing(pairing)
            await refreshStatus()
            return true
        } catch {
            // Keep the persisted fence. A later foreground/background pass
            // retries recovery without exposing either destination.
            pairingTransitionInProgress = false
            state = .failed(error.localizedDescription)
            return false
        }
    }

    private func publishRecoveredPairing(_ pairing: Pairing?) {
        if let pairing {
            PhoneWatchSync.shared.pushPairing(
                baseURL: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
        } else {
            PhoneWatchSync.shared.pushUnpair()
        }
        WidgetCenter.shared.reloadAllTimelines()
        NotificationCenter.default.post(
            name: .healthmesPairingChanged,
            object: nil
        )
    }

    func sync() async {
        guard
            !Task.isCancelled,
            !pairingTransitionInProgress,
            !PairingStore.shared.hasPendingTransition
        else { return }
        if syncInProgress {
            syncRequestedWhileActive = true
            await withCheckedContinuation { continuation in
                syncWaiters.append(continuation)
            }
            return
        }
        syncInProgress = true
        repeat {
            guard !Task.isCancelled else { break }
            syncRequestedWhileActive = false
            let forcePending = forceRetryRequested
            forceRetryRequested = false
            await performSyncPass(forcePending: forcePending)
        } while syncRequestedWhileActive && !pairingTransitionInProgress
        syncInProgress = false
        let completedWaiters = syncWaiters
        syncWaiters.removeAll(keepingCapacity: true)
        for waiter in completedWaiters {
            waiter.resume()
        }
        let completedQuiescenceWaiters = quiescenceWaiters
        quiescenceWaiters.removeAll(keepingCapacity: true)
        for waiter in completedQuiescenceWaiters {
            waiter.resume()
        }
        if !pairingTransitionInProgress {
            await settleStateAfterSyncIfNeeded()
        }
    }

    private enum PendingDrainResult: Equatable {
        case drained
        case deferred(blockedLaneKeys: Set<String>)
    }

    private enum PendingUploadResult {
        case completed
        case failed
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
            let initialDrain = try await drainPendingUploads(
                pairing: pairingSnapshot,
                operation: syncOperation,
                forceRetry: forcePending,
                includeTerminalFailures: forcePending
            )
            var blockedLaneKeys: Set<String>
            switch initialDrain {
            case .drained:
                blockedLaneKeys = []
            case .deferred(let blocked):
                blockedLaneKeys = blocked
            }

            while true {
                guard isCurrent(syncOperation) else { return }
                let batch = try await collectBatch(
                    pairingFingerprint: pairingSnapshot.cacheFingerprint,
                    excludingLaneKeys: blockedLaneKeys
                )
                guard isCurrent(syncOperation) else { return }
                guard batch.hasChanges else { break }
                for lane in batch.lanes where lane.hasChanges {
                    let body = try HealthMesAPI.healthKitUploadBody(
                        lane.payload
                    )
                    let anchors = try archiveAnchors(lane.anchors)
                    _ = try await outbox.enqueue(
                        body: body,
                        anchors: anchors,
                        destinationFingerprint:
                            pairingSnapshot.cacheFingerprint
                    )
                }
                guard await refreshQueueStatus(for: pairingSnapshot) else {
                    return
                }
                let drainResult = try await drainPendingUploads(
                    pairing: pairingSnapshot,
                    operation: syncOperation,
                    forceRetry: false,
                    includeTerminalFailures: false
                )
                switch drainResult {
                case .drained:
                    blockedLaneKeys = []
                case .deferred(let blocked):
                    blockedLaneKeys = blocked
                }
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
        forceRetry: Bool,
        includeTerminalFailures: Bool
    ) async throws -> PendingDrainResult {
        let fingerprint = pairing.cacheFingerprint
        var attemptedTerminalKeys = Set<String>()
        while true {
            guard isCurrent(operation) else {
                return .deferred(blockedLaneKeys: [])
            }
            _ = try await outbox.migrateLegacyTerminalEntries(
                destinationFingerprint: fingerprint
            )
            let entries = try await outbox.pendingEntries(
                destinationFingerprint: fingerprint
            )
            applyQueueStatus(entries)
            switch HealthKitSyncQueuePolicy.select(
                from: entries,
                forceRetry: forceRetry,
                includeTerminalFailures: includeTerminalFailures,
                attemptedTerminalKeys: attemptedTerminalKeys,
                now: Date()
            ) {
            case .drained:
                return .drained
            case .deferred:
                return .deferred(
                    blockedLaneKeys:
                        HealthKitSyncQueuePolicy.blockedLaneKeys(
                            in: entries
                        )
                )
            case .finalize(let entry):
                let result = try await finalizeAcceptedEntry(
                    entry,
                    pairing: pairing,
                    operation: operation
                )
                if case .deferred = result {
                    return result
                }
            case .upload(let entry):
                let result = try await uploadPendingEntry(
                    entry,
                    pairing: pairing,
                    operation: operation,
                    includeTerminalFailures: includeTerminalFailures
                )
                switch result {
                case .completed:
                    continue
                case .failed:
                    attemptedTerminalKeys.insert(entry.idempotencyKey)
                case .deferred:
                    return .deferred(
                        blockedLaneKeys: entry.laneKeys
                    )
                }
            }
        }
    }

    private func uploadPendingEntry(
        _ entry: HealthKitSyncOutboxEntry,
        pairing: Pairing,
        operation: PairingOperationToken,
        includeTerminalFailures: Bool
    ) async throws -> PendingUploadResult {
        let fingerprint = pairing.cacheFingerprint
        let uploadID = UUID()
        let uploadTask = Task {
            try await api.uploadHealthKit(
                body: entry.body,
                idempotencyKey: entry.idempotencyKey,
                pairing: pairing
            )
        }
        activeUpload = ActiveUpload(
            id: uploadID,
            task: uploadTask
        )
        do {
            _ = try await uploadTask.value
        } catch {
            if activeUpload?.id == uploadID {
                activeUpload = nil
            }
            guard isCurrent(operation) else {
                return .deferred
            }
            if error is CancellationError {
                return .deferred
            }
            switch HealthKitUploadFailureDisposition.classify(error) {
            case .retryable:
                _ = try await outbox.markFailed(
                    idempotencyKey: entry.idempotencyKey,
                    destinationFingerprint: fingerprint,
                    countsTowardAutomaticQuarantine: false,
                    preserveTerminalFailure:
                        entry.terminalFailure != nil
                )
                if entry.terminalFailure != nil {
                    try await markPermanentFailure(
                        entry,
                        reason:
                            entry.terminalFailure
                            ?? "Manual retry did not complete.",
                        pairingFingerprint: fingerprint
                    )
                }
            case .terminal(let reason):
                try await markPermanentFailure(
                    entry,
                    reason: reason,
                    pairingFingerprint: fingerprint
                )
            }
            await refreshQueueStatus(for: pairing)
            return .failed
        }
        if activeUpload?.id == uploadID {
            activeUpload = nil
        }

        guard isCurrent(operation) else {
            return .deferred
        }
        guard
            let accepted = try await outbox.markUploadAccepted(
                idempotencyKey: entry.idempotencyKey,
                destinationFingerprint: fingerprint
            )
        else {
            return .deferred
        }
        let finalized = try await finalizeAcceptedEntry(
            accepted,
            pairing: pairing,
            operation: operation
        )
        switch finalized {
        case .drained:
            return .completed
        case .deferred:
            return .deferred
        }
    }

    private func finalizeAcceptedEntry(
        _ entry: HealthKitSyncOutboxEntry,
        pairing: Pairing,
        operation: PairingOperationToken
    ) async throws -> PendingDrainResult {
        let fingerprint = pairing.cacheFingerprint
        guard isCurrent(operation) else {
            return .deferred(blockedLaneKeys: entry.laneKeys)
        }
        try commitAnchors(
            entry.anchors,
            pairingFingerprint: fingerprint
        )
        guard isCurrent(operation) else {
            return .deferred(blockedLaneKeys: entry.laneKeys)
        }
        try await outbox.markSucceeded(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: fingerprint
        )
        recordSuccessfulUpload(for: fingerprint)
        guard await refreshQueueStatus(for: pairing) else {
            return .deferred(blockedLaneKeys: entry.laneKeys)
        }
        return .drained
    }

    private func markPermanentFailure(
        _ entry: HealthKitSyncOutboxEntry,
        reason: String,
        pairingFingerprint: String
    ) async throws {
        _ = try await outbox.markTerminal(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: pairingFingerprint,
            reason: reason,
            anchorsCommitted: false,
            anchorCommitPending: false
        )
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

    private struct BatchLane {
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

    private struct Batch {
        let lanes: [BatchLane]

        var hasChanges: Bool {
            lanes.contains(where: \.hasChanges)
        }

        var hasMore: Bool {
            lanes.contains(where: \.hasMore)
        }
    }

    private func collectBatch(
        pairingFingerprint: String,
        excludingLaneKeys: Set<String>
    ) async throws -> Batch {
        if excludingLaneKeys.contains(
            HealthKitSyncQueuePolicy.legacyGlobalLane
        ) {
            return Batch(lanes: [])
        }
        var lanes: [BatchLane] = []

        for spec in quantitySpecs {
            let key = spec.type.identifier
            guard !excludingLaneKeys.contains(key) else { continue }
            let result = try await anchoredSamples(
                type: spec.type,
                anchor: loadAnchor(key: key, pairingFingerprint: pairingFingerprint)
            )
            let metrics: [HealthKitIngestPayload.Metric] =
                result.samples.compactMap { sample in
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
            let deletions: [HealthKitIngestPayload.Deletion] =
                result.deleted.map {
                .init(id: $0.uuid.uuidString.lowercased(), type: key)
            }
            lanes.append(
                BatchLane(
                    payload: .init(
                        data: .init(
                            records: metrics,
                            deletions: deletions
                        )
                    ),
                    anchors: [key: result.anchor],
                    hasMore: result.hasMore
                )
            )
        }

        if let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) {
            let key = sleepType.identifier
            if !excludingLaneKeys.contains(key) {
                let result = try await anchoredSamples(
                    type: sleepType,
                    anchor: loadAnchor(
                        key: key,
                        pairingFingerprint: pairingFingerprint
                    )
                )
                let sleepRows: [HealthKitIngestPayload.Sleep] =
                    result.samples.compactMap { sample in
                    guard
                        let category = sample as? HKCategorySample
                    else {
                        return nil
                    }
                    return .init(
                        id: category.uuid.uuidString.lowercased(),
                        stage: Self.sleepStage(category.value),
                        startDate: category.startDate,
                        endDate: category.endDate,
                        zoneOffset:
                            HealthKitWireFormat.zoneOffset(
                                for: category.startDate
                            ),
                        source: Self.sourceInfo(for: category)
                    )
                }
                let deletions: [HealthKitIngestPayload.Deletion] =
                    result.deleted.map {
                    .init(
                        id: $0.uuid.uuidString.lowercased(),
                        type: key
                    )
                }
                lanes.append(
                    BatchLane(
                        payload: .init(
                            data: .init(
                                sleep: sleepRows,
                                deletions: deletions
                            )
                        ),
                        anchors: [key: result.anchor],
                        hasMore: result.hasMore
                    )
                )
            }
        }

        let workoutType = HKObjectType.workoutType()
        let workoutKey = workoutType.identifier
        if !excludingLaneKeys.contains(workoutKey) {
            let workoutResult = try await anchoredSamples(
                type: workoutType,
                anchor: loadAnchor(
                    key: workoutKey,
                    pairingFingerprint: pairingFingerprint
                )
            )
            let workouts: [HealthKitIngestPayload.Workout] =
                workoutResult.samples.compactMap { sample in
                guard let workout = sample as? HKWorkout else {
                    return nil
                }
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
                            value:
                                energy.doubleValue(
                                    for: .kilocalorie()
                                )
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
                    type:
                        Self.workoutType(
                            workout.workoutActivityType
                        ),
                    startDate: workout.startDate,
                    endDate: workout.endDate,
                    values: values,
                    zoneOffset:
                        HealthKitWireFormat.zoneOffset(
                            for: workout.startDate
                        ),
                    source: Self.sourceInfo(for: workout)
                )
            }
            let deletions: [HealthKitIngestPayload.Deletion] =
                workoutResult.deleted.map {
                .init(
                    id: $0.uuid.uuidString.lowercased(),
                    type: workoutKey
                )
            }
            lanes.append(
                BatchLane(
                    payload: .init(
                        data: .init(
                            workouts: workouts,
                            deletions: deletions
                        )
                    ),
                    anchors: [workoutKey: workoutResult.anchor],
                    hasMore: workoutResult.hasMore
                )
            )
        }

        return Batch(lanes: lanes)
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
                    await HealthKitObserverLifecycle
                        .synchronizeAndAcknowledge(
                            completion: completion,
                            synchronize: {
                                await self.sync()
                            }
                        )
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
            applyQueueStatus(entries)
            queueStatusError = nil
            return true
        } catch {
            if PairingStore.shared.load() == pairing {
                pendingUploadCount = 0
                terminalFailureCount = 0
                latestTerminalFailure = nil
                nextRetryAt = nil
                queueStatusError = error.localizedDescription
                state = .failed(error.localizedDescription)
            }
            return false
        }
    }

    private func applyQueueStatus(
        _ entries: [HealthKitSyncOutboxEntry]
    ) {
        let summary = HealthKitSyncQueueSummary(entries: entries)
        pendingUploadCount = summary.pendingCount
        terminalFailureCount = summary.terminalFailureCount
        latestTerminalFailure = summary.latestTerminalFailure
        nextRetryAt = summary.nextRetryAt
    }

    private func waitForSyncToQuiesce() async {
        guard syncInProgress else { return }
        await withCheckedContinuation { continuation in
            quiescenceWaiters.append(continuation)
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
        terminalFailureCount = 0
        latestTerminalFailure = nil
        nextRetryAt = nil
        queueStatusError = nil
        isPaused = false
        if state != .unavailable {
            state = .notRequested
        }
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
