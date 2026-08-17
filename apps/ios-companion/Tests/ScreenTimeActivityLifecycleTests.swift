import BackgroundTasks
import Foundation
import XCTest

private enum ScreenTimeLifecycleTestError: Error {
    case authorizationFailed
    case collectionFailed
    case exclusionApprovalFailed
    case identityCleanupFailed
}

private actor ScreenTimeLifecycleTestCounter {
    private var authorizationCalls = 0
    private var collectionCalls = 0

    func recordAuthorization() {
        authorizationCalls += 1
    }

    func recordCollection() {
        collectionCalls += 1
    }

    func values() -> (authorization: Int, collection: Int) {
        (authorizationCalls, collectionCalls)
    }
}

private struct ScreenTimeLifecycleTestCollector:
    ScreenTimeActivityCollecting
{
    let result: ScreenTimeCollectorResult
    let pseudonymKeyID: String?
    let counter: ScreenTimeLifecycleTestCounter
    let collectionDelayNanoseconds: UInt64

    init(
        result: ScreenTimeCollectorResult,
        pseudonymKeyID: String?,
        counter: ScreenTimeLifecycleTestCounter =
            ScreenTimeLifecycleTestCounter(),
        collectionDelayNanoseconds: UInt64 = 0
    ) {
        self.result = result
        self.pseudonymKeyID = pseudonymKeyID
        self.counter = counter
        self.collectionDelayNanoseconds =
            collectionDelayNanoseconds
    }

    @MainActor
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        result
    }

    @MainActor
    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        await counter.recordAuthorization()
        return result
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        await counter.recordCollection()
        if collectionDelayNanoseconds > 0 {
            try await Task.sleep(
                nanoseconds: collectionDelayNanoseconds
            )
        }
        return result
    }
}

private struct ScreenTimeLifecycleThrowingCollector:
    ScreenTimeActivityCollecting
{
    let pseudonymKeyID: String?

    @MainActor
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        ScreenTimeCollectorResult(
            capability: .aggregate,
            permissionStatus: .granted,
            reason: nil,
            samples: []
        )
    }

    @MainActor
    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        throw ScreenTimeLifecycleTestError.authorizationFailed
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        throw ScreenTimeLifecycleTestError.collectionFailed
    }
}

private actor ScreenTimeLifecycleSequencedCollectorState {
    private var authorizationResults: [ScreenTimeCollectorResult]
    private let collectionResult: ScreenTimeCollectorResult
    private var collectionCalls = 0

    init(
        authorizationResults: [ScreenTimeCollectorResult],
        collectionResult: ScreenTimeCollectorResult
    ) {
        self.authorizationResults = authorizationResults
        self.collectionResult = collectionResult
    }

    func nextAuthorizationResult() -> ScreenTimeCollectorResult {
        if authorizationResults.count > 1 {
            return authorizationResults.removeFirst()
        }
        return authorizationResults[0]
    }

    func collect() -> ScreenTimeCollectorResult {
        collectionCalls += 1
        return collectionResult
    }

    func capturedCollectionCalls() -> Int {
        collectionCalls
    }

    func recordCollectionFailure() {
        collectionCalls += 1
    }
}

private struct ScreenTimeLifecycleSequencedCollector:
    ScreenTimeActivityCollecting
{
    let pseudonymKeyID: String?
    let state: ScreenTimeLifecycleSequencedCollectorState

    @MainActor
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        await state.nextAuthorizationResult()
    }

    @MainActor
    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        await state.nextAuthorizationResult()
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        await state.collect()
    }
}

private struct ScreenTimeLifecycleSequencedThrowingCollector:
    ScreenTimeActivityCollecting
{
    let pseudonymKeyID: String?
    let state: ScreenTimeLifecycleSequencedCollectorState

    @MainActor
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        await state.nextAuthorizationResult()
    }

    @MainActor
    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        await state.nextAuthorizationResult()
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        await state.recordCollectionFailure()
        throw ScreenTimeLifecycleTestError.collectionFailed
    }
}

private enum ScreenTimeLifecycleUploadAction {
    case succeed
    case fail(HealthMesAPIError)
    case failUnknown
}

private actor ScreenTimeLifecycleUploadGate {
    private var remainingBlocks: Int
    private var continuations: [CheckedContinuation<Void, Never>] = []

    init(blockCount: Int = 1) {
        remainingBlocks = max(0, blockCount)
    }

    func suspendIfNeeded() async {
        guard remainingBlocks > 0 else { return }
        remainingBlocks -= 1
        await withCheckedContinuation { continuation in
            continuations.append(continuation)
        }
    }

    func releaseNext() {
        guard !continuations.isEmpty else { return }
        continuations.removeFirst().resume()
    }

    func waitingCount() -> Int {
        continuations.count
    }
}

private final class ScreenTimeLifecycleCancellationURLProtocol:
    URLProtocol
{
    static var uploadStarted: (() -> Void)?
    static var uploadStopped: (() -> Void)?

    private var isPendingUpload = false

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(
        for request: URLRequest
    ) -> URLRequest {
        request
    }

    override func startLoading() {
        if request.httpMethod == "POST" {
            isPendingUpload = true
            Self.uploadStarted?()
            return
        }
        let body = Data(
            """
            {
              "device_id": "ios-lifecycle-device",
              "enabled": true,
              "excluded_apps": [],
              "paused_until": null,
              "effective_collecting": true,
              "blocked_reason": null,
              "config_revision": 3,
              "raw_retention_cutoff": null
            }
            """.utf8
        )
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(
            self,
            didReceive: response,
            cacheStoragePolicy: .notAllowed
        )
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {
        guard isPendingUpload else { return }
        isPendingUpload = false
        Self.uploadStopped?()
    }
}

private actor ScreenTimeLifecycleTestTransport:
    ScreenTimeActivityTransport
{
    private let states: [ScreenTimeCollectionState]
    private let stateDelayNanoseconds: UInt64
    private let uploadDelayNanoseconds: UInt64
    private let uploadGate: ScreenTimeLifecycleUploadGate?
    private var uploadActions: [ScreenTimeLifecycleUploadAction]
    private var stateCalls = 0
    private var reports: [ScreenTimeActivityReport] = []
    private var statePairings: [Pairing] = []
    private var reportPairings: [Pairing] = []

    init(
        state: ScreenTimeCollectionState,
        uploadActions: [ScreenTimeLifecycleUploadAction] = [],
        stateDelayNanoseconds: UInt64 = 0,
        uploadDelayNanoseconds: UInt64 = 0,
        uploadGate: ScreenTimeLifecycleUploadGate? = nil
    ) {
        states = [state]
        self.uploadActions = uploadActions
        self.stateDelayNanoseconds = stateDelayNanoseconds
        self.uploadDelayNanoseconds = uploadDelayNanoseconds
        self.uploadGate = uploadGate
    }

    init(
        states: [ScreenTimeCollectionState],
        uploadActions: [ScreenTimeLifecycleUploadAction] = [],
        stateDelayNanoseconds: UInt64 = 0,
        uploadDelayNanoseconds: UInt64 = 0,
        uploadGate: ScreenTimeLifecycleUploadGate? = nil
    ) {
        precondition(!states.isEmpty)
        self.states = states
        self.uploadActions = uploadActions
        self.stateDelayNanoseconds = stateDelayNanoseconds
        self.uploadDelayNanoseconds = uploadDelayNanoseconds
        self.uploadGate = uploadGate
    }

    func collectionState(
        pairing: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        let state = states[min(stateCalls, states.count - 1)]
        stateCalls += 1
        statePairings.append(pairing)
        if stateDelayNanoseconds > 0 {
            try await Task.sleep(
                nanoseconds: stateDelayNanoseconds
            )
        }
        return state
    }

    func upload(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        reports.append(report)
        reportPairings.append(pairing)
        if let uploadGate {
            await uploadGate.suspendIfNeeded()
        }
        if uploadDelayNanoseconds > 0 {
            try await Task.sleep(
                nanoseconds: uploadDelayNanoseconds
            )
        }
        if !uploadActions.isEmpty {
            switch uploadActions.removeFirst() {
            case .succeed:
                break
            case .fail(let error):
                throw error
            case .failUnknown:
                throw ScreenTimeLifecycleTestError.collectionFailed
            }
        }
        return ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: ["2026-08-16"]
        )
    }

    func capturedStateCalls() -> Int {
        stateCalls
    }

    func capturedReports() -> [ScreenTimeActivityReport] {
        reports
    }

    func capturedPairings() -> (
        state: [Pairing],
        report: [Pairing]
    ) {
        (statePairings, reportPairings)
    }
}

private actor ScreenTimeLifecycleBlockingTransport:
    ScreenTimeActivityTransport
{
    private let state: ScreenTimeCollectionState
    private var stateCalls = 0
    private var stateCancellations = 0
    private var reports: [ScreenTimeActivityReport] = []
    private var stateWaiters: [CheckedContinuation<Void, Never>] = []
    private var released = false

    init(state: ScreenTimeCollectionState) {
        self.state = state
    }

    func collectionState(
        pairing _: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        stateCalls += 1
        if released {
            return state
        }
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                stateWaiters.append(continuation)
            }
        } onCancel: {
            Task {
                await self.recordStateCancellation()
            }
        }
        try Task.checkCancellation()
        return state
    }

    func upload(
        pairing _: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        reports.append(report)
        return ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: ["2026-08-16"]
        )
    }

    func releaseStateCalls() {
        released = true
        let waiters = stateWaiters
        stateWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    func capturedStateCalls() -> Int {
        stateCalls
    }

    func capturedStateCancellations() -> Int {
        stateCancellations
    }

    func capturedReports() -> [ScreenTimeActivityReport] {
        reports
    }

    private func recordStateCancellation() {
        stateCancellations += 1
    }
}

private actor ScreenTimeLifecycleOfflineStateTransport:
    ScreenTimeActivityTransport
{
    func collectionState(
        pairing _: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        throw HealthMesAPIError.transport(
            underlying: URLError(.notConnectedToInternet)
        )
    }

    func upload(
        pairing _: Pairing,
        report _: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        throw ScreenTimeLifecycleTestError.collectionFailed
    }
}

private enum ScreenTimeLifecycleEnableAction {
    case succeed
    case conflict
    case conflictWithDisabledInstance
    case fail(HealthMesAPIError)
}

private actor ScreenTimeLifecycleRegistrationTransport:
    ScreenTimeActivityTransport
{
    private let deviceID: String
    private var descriptor: ScreenTimeInputDescriptor
    private var state: ScreenTimeCollectionState
    private var enableActions: [ScreenTimeLifecycleEnableAction]
    private var descriptorError: HealthMesAPIError?
    private var descriptorCalls = 0
    private var enableCalls = 0
    private var collectionStateCalls = 0
    private var reports: [ScreenTimeActivityReport] = []

    init(
        deviceID: String,
        enableActions: [ScreenTimeLifecycleEnableAction] = [
            .succeed
        ],
        descriptorError: HealthMesAPIError? = nil
    ) {
        self.deviceID = deviceID
        descriptor = ScreenTimeInputDescriptor(
            sourceID: ScreenTimeInputDescriptor.sourceID,
            instances: [],
            revision: "sha256:" + String(repeating: "0", count: 64)
        )
        state = ScreenTimeCollectionState(
            deviceID: deviceID,
            enabled: false,
            excludedApps: [],
            pausedUntil: nil,
            effectiveCollecting: false,
            blockedReason: "collection_disabled",
            configRevision: 0,
            rawRetentionCutoff: nil
        )
        self.enableActions = enableActions
        self.descriptorError = descriptorError
    }

    func inputDescriptor(
        pairing _: Pairing
    ) async throws -> ScreenTimeInputDescriptor {
        descriptorCalls += 1
        if let descriptorError {
            throw descriptorError
        }
        return descriptor
    }

    func enableInput(
        pairing _: Pairing,
        deviceID: String,
        revision: String
    ) async throws -> ScreenTimeInputDescriptor {
        enableCalls += 1
        guard revision == descriptor.revision else {
            throw revisionConflict()
        }
        let action = enableActions.isEmpty
            ? .succeed
            : enableActions.removeFirst()
        switch action {
        case .succeed:
            setCentralState(enabled: true, revisionNumber: 1)
            return descriptor
        case .conflict:
            descriptor = ScreenTimeInputDescriptor(
                sourceID: ScreenTimeInputDescriptor.sourceID,
                instances: descriptor.instances,
                revision:
                    "sha256:"
                    + String(
                        repeating: String(enableCalls + 1),
                        count: 64
                    )
            )
            throw revisionConflict()
        case .conflictWithDisabledInstance:
            setCentralState(enabled: false, revisionNumber: 1)
            throw revisionConflict()
        case .fail(let error):
            throw error
        }
    }

    func collectionState(
        pairing _: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        collectionStateCalls += 1
        return state
    }

    func upload(
        pairing _: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        reports.append(report)
        return ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: ["2026-08-16"]
        )
    }

    func disableCentrally() {
        setCentralState(enabled: false, revisionNumber: 2)
    }

    func pauseCentrally(until: Date) {
        setCentralState(
            enabled: true,
            revisionNumber: 2,
            pausedUntil: until
        )
    }

    func captured() -> (
        descriptor: Int,
        enable: Int,
        collectionState: Int,
        reports: [ScreenTimeActivityReport]
    ) {
        (
            descriptorCalls,
            enableCalls,
            collectionStateCalls,
            reports
        )
    }

    private func setCentralState(
        enabled: Bool,
        revisionNumber: Int,
        pausedUntil: Date? = nil
    ) {
        descriptor = ScreenTimeInputDescriptor(
            sourceID: ScreenTimeInputDescriptor.sourceID,
            instances: [
                ScreenTimeInputInstance(
                    instanceID: deviceID,
                    platform: "ios",
                    enabled: enabled,
                    pausedUntil: pausedUntil
                )
            ],
            revision:
                "sha256:"
                + String(
                    repeating: String(revisionNumber),
                    count: 64
                )
        )
        state = ScreenTimeCollectionState(
            deviceID: deviceID,
            enabled: enabled,
            excludedApps: [],
            pausedUntil: pausedUntil,
            effectiveCollecting:
                enabled && pausedUntil == nil,
            blockedReason:
                !enabled
                ? "collection_disabled"
                : pausedUntil == nil
                    ? nil
                    : "collection_paused",
            configRevision: revisionNumber,
            rawRetentionCutoff: nil
        )
    }

    private func revisionConflict() -> HealthMesAPIError {
        HealthMesAPIError.server(
            statusCode: 409,
            code: "input_settings_revision_conflict",
            message: "stale input descriptor",
            detail: nil
        )
    }
}

private actor ScreenTimeLifecycleBlockingRegistrationService:
    ScreenTimeActivitySyncing
{
    private var registrationWaiters:
        [CheckedContinuation<Void, Never>] = []
    private var registrationStartedWaiters:
        [CheckedContinuation<Void, Never>] = []
    private var authorizationCalls = 0
    private var registrationCalls = 0
    private var registrationCancellations = 0
    private var syncCalls = 0
    private var syncPairings: [Pairing] = []
    private var disableCalls = 0

    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        ScreenTimeCollectorResult(
            capability: .aggregate,
            permissionStatus: .granted,
            reason: nil,
            samples: []
        )
    }

    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        authorizationCalls += 1
        return ScreenTimeCollectorResult(
            capability: .aggregate,
            permissionStatus: .granted,
            reason: nil,
            samples: []
        )
    }

    func authorizationRestorationFence(
        pairing: Pairing,
        now _: Date
    ) async throws -> ScreenTimeAuthorizationRestorationFence {
        ScreenTimeAuthorizationRestorationFence(
            deviceID:
                "ios-collector-v1-" + String(repeating: "1", count: 40),
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            configRevision: 3,
            blockedReason: nil
        )
    }

    func registerAuthorizedCollector(
        pairing _: Pairing
    ) async throws {
        registrationCalls += 1
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                registrationWaiters.append(continuation)
                let startedWaiters =
                    registrationStartedWaiters
                registrationStartedWaiters.removeAll()
                startedWaiters.forEach { $0.resume() }
            }
        } onCancel: {
            Task {
                await self.cancelRegistration()
            }
        }
        try Task.checkCancellation()
    }

    func approveExcludedApps(
        _: Set<String>
    ) async throws {}

    func sync(
        pairing: Pairing,
        now _: Date,
        timezone _: TimeZone,
        trigger _: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeSyncOutcome {
        syncCalls += 1
        syncPairings.append(pairing)
        return .skipped(reason: "unexpected")
    }

    func reconcilePendingUploads(
        pairing _: Pairing?
    ) async throws {}

    func disableAndPurge(now _: Date) async throws {
        disableCalls += 1
    }

    func waitUntilRegistrationStarts() async {
        guard registrationCalls == 0 else { return }
        await withCheckedContinuation { continuation in
            registrationStartedWaiters.append(continuation)
        }
    }

    func completeRegistration() {
        let waiters = registrationWaiters
        registrationWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    func calls() -> (
        authorization: Int,
        registration: Int,
        registrationCancellations: Int,
        sync: Int,
        syncPairings: [Pairing],
        disable: Int
    ) {
        (
            authorizationCalls,
            registrationCalls,
            registrationCancellations,
            syncCalls,
            syncPairings,
            disableCalls
        )
    }

    private func cancelRegistration() {
        registrationCancellations += 1
        let waiters = registrationWaiters
        registrationWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }
}

private actor ScreenTimeLifecycleMockSyncService:
    ScreenTimeActivitySyncing
{
    private let currentAuthorizationResult: ScreenTimeCollectorResult
    private let authorizationResult: ScreenTimeCollectorResult
    private let syncResult: ScreenTimeSyncOutcome
    private let authorizationError: Error?
    private let registrationError: Error?
    private let exclusionApprovalError: Error?
    private var restorationFences:
        [ScreenTimeAuthorizationRestorationFence]
    private var currentAuthorizationCalls = 0
    private var authorizationCalls = 0
    private var restorationFenceCalls = 0
    private var registrationCalls = 0
    private var approvedExclusions: [Set<String>] = []
    private var syncCalls = 0
    private var syncTriggers: [ScreenTimeSyncTrigger] = []
    private var syncTimezones: [String] = []
    private var reconciledPairings: [Pairing?] = []
    private var disableCalls = 0

    init(
        authorizationResult: ScreenTimeCollectorResult,
        syncResult: ScreenTimeSyncOutcome,
        currentAuthorizationResult: ScreenTimeCollectorResult? = nil,
        restorationFences:
            [ScreenTimeAuthorizationRestorationFence] = [],
        authorizationError: Error? = nil,
        registrationError: Error? = nil,
        exclusionApprovalError: Error? = nil
    ) {
        self.currentAuthorizationResult =
            currentAuthorizationResult ?? authorizationResult
        self.authorizationResult = authorizationResult
        self.syncResult = syncResult
        self.restorationFences = restorationFences
        self.authorizationError = authorizationError
        self.registrationError = registrationError
        self.exclusionApprovalError = exclusionApprovalError
    }

    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        currentAuthorizationCalls += 1
        return currentAuthorizationResult
    }

    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        authorizationCalls += 1
        if let authorizationError {
            throw authorizationError
        }
        return authorizationResult
    }

    func authorizationRestorationFence(
        pairing: Pairing,
        now _: Date
    ) async throws -> ScreenTimeAuthorizationRestorationFence {
        restorationFenceCalls += 1
        if restorationFences.count > 1 {
            return restorationFences.removeFirst()
        }
        if let fence = restorationFences.first {
            return fence
        }
        return ScreenTimeAuthorizationRestorationFence(
            deviceID:
                "ios-collector-v1-" + String(repeating: "1", count: 40),
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            configRevision: 3,
            blockedReason: nil
        )
    }

    func registerAuthorizedCollector(
        pairing _: Pairing
    ) async throws {
        registrationCalls += 1
        if let registrationError {
            throw registrationError
        }
    }

    func approveExcludedApps(
        _ excludedAppTokens: Set<String>
    ) async throws {
        if let exclusionApprovalError {
            throw exclusionApprovalError
        }
        approvedExclusions.append(excludedAppTokens)
    }

    func sync(
        pairing _: Pairing,
        now _: Date,
        timezone: TimeZone,
        trigger: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeSyncOutcome {
        syncCalls += 1
        syncTriggers.append(trigger)
        syncTimezones.append(timezone.identifier)
        return syncResult
    }

    func reconcilePendingUploads(
        pairing: Pairing?
    ) async throws {
        reconciledPairings.append(pairing)
    }

    func disableAndPurge(now _: Date) async throws {
        disableCalls += 1
    }

    func calls() -> (
        currentAuthorization: Int,
        authorization: Int,
        restorationFence: Int,
        registration: Int,
        approvedExclusions: [Set<String>],
        sync: Int,
        syncTriggers: [ScreenTimeSyncTrigger],
        syncTimezones: [String],
        reconciledPairings: [Pairing?],
        disable: Int
    ) {
        (
            currentAuthorizationCalls,
            authorizationCalls,
            restorationFenceCalls,
            registrationCalls,
            approvedExclusions,
            syncCalls,
            syncTriggers,
            syncTimezones,
            reconciledPairings,
            disableCalls
        )
    }
}

private actor ScreenTimeLifecycleBlockingAuthorizationService:
    ScreenTimeActivitySyncing
{
    private let currentAuthorizationResult: ScreenTimeCollectorResult
    private let authorizationResult: ScreenTimeCollectorResult
    private let syncResult: ScreenTimeSyncOutcome
    private var restorationFences:
        [ScreenTimeAuthorizationRestorationFence]
    private var currentAuthorizationCalls = 0
    private var authorizationCalls = 0
    private var authorizationCancellations = 0
    private var restorationFenceCalls = 0
    private var authorizationWaiters:
        [CheckedContinuation<Void, Never>] = []
    private var registrationCalls = 0
    private var syncCalls = 0
    private var disableCalls = 0

    init(
        authorizationResult: ScreenTimeCollectorResult,
        syncResult: ScreenTimeSyncOutcome,
        currentAuthorizationResult: ScreenTimeCollectorResult? = nil,
        restorationFences:
            [ScreenTimeAuthorizationRestorationFence] = []
    ) {
        self.currentAuthorizationResult =
            currentAuthorizationResult ?? authorizationResult
        self.authorizationResult = authorizationResult
        self.syncResult = syncResult
        self.restorationFences = restorationFences
    }

    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        currentAuthorizationCalls += 1
        return currentAuthorizationResult
    }

    func requestAuthorization() async throws
        -> ScreenTimeCollectorResult
    {
        authorizationCalls += 1
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                authorizationWaiters.append(continuation)
            }
        } onCancel: {
            Task {
                await self.cancelAuthorization()
            }
        }
        try Task.checkCancellation()
        return authorizationResult
    }

    func authorizationRestorationFence(
        pairing: Pairing,
        now _: Date
    ) async throws -> ScreenTimeAuthorizationRestorationFence {
        restorationFenceCalls += 1
        if restorationFences.count > 1 {
            return restorationFences.removeFirst()
        }
        if let fence = restorationFences.first {
            return fence
        }
        return ScreenTimeAuthorizationRestorationFence(
            deviceID:
                "ios-collector-v1-" + String(repeating: "1", count: 40),
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            configRevision: 3,
            blockedReason: nil
        )
    }

    func registerAuthorizedCollector(
        pairing _: Pairing
    ) async throws {
        registrationCalls += 1
    }

    func approveExcludedApps(
        _: Set<String>
    ) async throws {}

    func sync(
        pairing _: Pairing,
        now _: Date,
        timezone _: TimeZone,
        trigger _: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeSyncOutcome {
        syncCalls += 1
        return syncResult
    }

    func reconcilePendingUploads(
        pairing _: Pairing?
    ) async throws {}

    func disableAndPurge(now _: Date) async throws {
        disableCalls += 1
    }

    func releaseAuthorizationCalls() {
        let waiters = authorizationWaiters
        authorizationWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    func calls() -> (
        currentAuthorization: Int,
        authorization: Int,
        cancellations: Int,
        restorationFence: Int,
        registration: Int,
        sync: Int,
        disable: Int
    ) {
        (
            currentAuthorizationCalls,
            authorizationCalls,
            authorizationCancellations,
            restorationFenceCalls,
            registrationCalls,
            syncCalls,
            disableCalls
        )
    }

    private func cancelAuthorization() {
        authorizationCancellations += 1
        let waiters = authorizationWaiters
        authorizationWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }
}

@MainActor
private final class ScreenTimeLifecycleAuthorizationObserver:
    ScreenTimeAuthorizationChangeObserving
{
    private var onChange:
        (@MainActor @Sendable () async -> Void)?

    func start(
        onChange: @escaping @MainActor @Sendable () async -> Void
    ) {
        self.onChange = onChange
    }

    func emitChange() async {
        await onChange?()
    }
}

@MainActor
private final class ScreenTimeLifecycleBackgroundTasks:
    ScreenTimeBackgroundTaskManaging
{
    private(set) var registrations = 0
    private(set) var schedules = 0
    private(set) var cancellations = 0
    private var handler:
        (
            @MainActor
            (any ScreenTimeBackgroundRefreshTask) -> Void
        )?

    func register(
        handler:
            @escaping @MainActor
            (any ScreenTimeBackgroundRefreshTask) -> Void
    ) {
        registrations += 1
        self.handler = handler
    }

    func schedule() {
        schedules += 1
    }

    func cancel() {
        cancellations += 1
    }

    func launch() -> ScreenTimeLifecycleBackgroundTask {
        let task = ScreenTimeLifecycleBackgroundTask()
        handler?(task)
        return task
    }
}

@MainActor
private final class ScreenTimeLifecycleBackgroundTask:
    ScreenTimeBackgroundRefreshTask
{
    var expirationHandler: (() -> Void)?
    private(set) var completionValues: [Bool] = []

    func setTaskCompleted(success: Bool) {
        completionValues.append(success)
    }

    func expire() {
        expirationHandler?()
    }
}

final class ScreenTimeActivityLifecycleTests: XCTestCase {
    private struct GrantedSupersedeRaceResult {
        let activeOutcome: ScreenTimeSyncOutcome
        let freshOutcome: ScreenTimeSyncOutcome
        let reports: [ScreenTimeActivityReport]
        let entries: [ScreenTimeActivityOutboxEntry]
    }

    private func date(_ value: String) -> Date {
        ISO8601DateFormatter().date(from: value)!
    }

    private func pairing(
        token: String = "screen-time-test-token",
        baseURL: String = "https://healthmes.test.example"
    ) -> Pairing {
        Pairing(
            baseURL: URL(string: baseURL)!,
            token: token
        )
    }

    private func collectionState(
        enabled: Bool = true,
        pausedUntil: Date? = nil,
        configRevision: Int = 3,
        rawRetentionCutoff: Date? = nil
    ) -> ScreenTimeCollectionState {
        ScreenTimeCollectionState(
            deviceID: "ios-lifecycle-device",
            enabled: enabled,
            excludedApps: [],
            pausedUntil: pausedUntil,
            effectiveCollecting:
                enabled && pausedUntil == nil,
            blockedReason: nil,
            configRevision: configRevision,
            rawRetentionCutoff: rawRetentionCutoff
        )
    }

    private func collectorResult(
        capability: ScreenTimeActivityCapability = .aggregate,
        permission:
            ScreenTimeActivityPermissionStatus = .granted,
        reason: String? = nil
    ) -> ScreenTimeCollectorResult {
        ScreenTimeCollectorResult(
            capability: capability,
            permissionStatus: permission,
            reason: reason,
            samples: []
        )
    }

    private func authorizationRestorationFence(
        pairing: Pairing? = nil,
        deviceID: String =
            "ios-collector-v1-" + String(repeating: "1", count: 40),
        configRevision: Int = 3,
        blockedReason: String? = nil
    ) -> ScreenTimeAuthorizationRestorationFence {
        let pairing = pairing ?? self.pairing()
        return ScreenTimeAuthorizationRestorationFence(
            deviceID: deviceID,
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            configRevision: configRevision,
            blockedReason: blockedReason
        )
    }

    private func report(
        deviceID: String = "ios-lifecycle-device",
        revision: Int = 3,
        generation: Int = 100,
        sequence: Int = 1,
        start: Date? = nil
    ) -> ScreenTimeActivityReport {
        let snapshotStart =
            start ?? date("2026-08-16T09:00:00Z")
        return .aggregate(
            deviceID: deviceID,
            timezone: "UTC",
            pseudonymKeyID:
                "ios-key-" + String(repeating: "1", count: 40),
            collectedAt: snapshotStart.addingTimeInterval(3_700),
            collectionRevision: revision,
            collectionGeneration: generation,
            snapshotSequence: sequence,
            snapshotStart: snapshotStart,
            snapshotEnd:
                snapshotStart.addingTimeInterval(3_600),
            authoritativeBucketStarts: [snapshotStart],
            samples: [
                ScreenTimeActivitySample(
                    sourceRecordID: "opaque-record-\(sequence)",
                    bucketStart: snapshotStart,
                    foregroundSeconds: 600,
                    category: "productivity",
                    opaqueAppToken:
                        "ios-app-v2-"
                        + String(repeating: "1", count: 40)
                        + "-"
                        + String(repeating: "a", count: 40),
                    coverageSeconds: 3_600
                )
            ]
        )
    }

    private func temporaryOutbox(
        maximumEntries: Int = 8,
        maximumBytes: Int = 16 * 1_024 * 1_024,
        retryPolicy: ScreenTimeActivityRetryPolicy = .default,
        retentionInterval: TimeInterval =
            ScreenTimeActivityOutbox.defaultRetentionInterval,
        now: Date = Date()
    ) -> (ScreenTimeActivityOutbox, URL) {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "healthmes-screen-time-tests-\(UUID().uuidString)",
                isDirectory: true
            )
        return (
            ScreenTimeActivityOutbox(
                fileURL: directory.appendingPathComponent(
                    "outbox.json"
                ),
                maximumEntries: maximumEntries,
                maximumBytes: maximumBytes,
                retryPolicy: retryPolicy,
                retentionInterval: retentionInterval,
                now: now
            ),
            directory
        )
    }

    private func waitForStateCalls(
        _ expectedCount: Int,
        from transport: ScreenTimeLifecycleTestTransport
    ) async throws {
        for _ in 0..<400 {
            if await transport.capturedStateCalls() >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for Screen Time state call")
    }

    private func waitForReports(
        _ expectedCount: Int,
        from transport: ScreenTimeLifecycleTestTransport
    ) async throws {
        for _ in 0..<400 {
            if await transport.capturedReports().count >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for Screen Time upload")
    }

    private func waitForBlockedUploads(
        _ expectedCount: Int,
        at gate: ScreenTimeLifecycleUploadGate
    ) async throws {
        for _ in 0..<400 {
            if await gate.waitingCount() >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for blocked Screen Time upload")
    }

    private func runGrantedAuthorizationSupersedeRace(
        firstAction: ScreenTimeLifecycleUploadAction,
        retryPolicy: ScreenTimeActivityRetryPolicy = .default
    ) async throws -> GrantedSupersedeRaceResult {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(
            retryPolicy: retryPolicy,
            now: now
        )
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(generation: 100),
            pairing: pairing(),
            now: now.addingTimeInterval(-60)
        )
        let gate = ScreenTimeLifecycleUploadGate()
        let granted = collectorResult()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [firstAction, .succeed],
            uploadGate: gate
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: granted,
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let active = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForBlockedUploads(1, at: gate)
        let fresh = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .authorizationChanged
            )
        }
        try await waitForWaiterCounts(
            active: 1,
            pending: 1,
            from: service
        )
        await gate.releaseNext()

        let activeOutcome = try await active.value
        let freshOutcome = try await fresh.value
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()
        return GrantedSupersedeRaceResult(
            activeOutcome: activeOutcome,
            freshOutcome: freshOutcome,
            reports: reports,
            entries: entries
        )
    }

    private func waitForStateCalls(
        _ expectedCount: Int,
        from transport: ScreenTimeLifecycleBlockingTransport
    ) async throws {
        for _ in 0..<400 {
            if await transport.capturedStateCalls() >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for blocked Screen Time state call")
    }

    private func waitForStateCancellations(
        _ expectedCount: Int,
        from transport: ScreenTimeLifecycleBlockingTransport
    ) async throws {
        for _ in 0..<400 {
            if await transport.capturedStateCancellations()
                >= expectedCount
            {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for Screen Time cancellation")
    }

    private func waitForWaiterCounts(
        active expectedActive: Int,
        pending expectedPending: Int = 0,
        from service: ScreenTimeActivitySyncService
    ) async throws {
        for _ in 0..<400 {
            let counts = await service.waiterCounts()
            if counts.active == expectedActive,
                counts.pending == expectedPending
            {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for Screen Time waiters")
    }

    private func waitForLifecycleCalls(
        authorization expectedAuthorization: Int,
        sync expectedSync: Int,
        from service: ScreenTimeLifecycleMockSyncService
    ) async throws {
        for _ in 0..<400 {
            let calls = await service.calls()
            if calls.authorization >= expectedAuthorization,
                calls.sync >= expectedSync
            {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for Screen Time lifecycle calls")
    }

    private func waitForAuthorizationCalls(
        _ expectedCount: Int,
        from service:
            ScreenTimeLifecycleBlockingAuthorizationService
    ) async throws {
        for _ in 0..<400 {
            if await service.calls().authorization >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for authorization call")
    }

    private func waitForAuthorizationCancellations(
        _ expectedCount: Int,
        from service:
            ScreenTimeLifecycleBlockingAuthorizationService
    ) async throws {
        for _ in 0..<400 {
            if await service.calls().cancellations >= expectedCount {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for authorization cancellation")
    }

    @MainActor
    private func waitForBackgroundCompletion(
        _ task: ScreenTimeLifecycleBackgroundTask
    ) async throws {
        for _ in 0..<400 {
            if !task.completionValues.isEmpty {
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("timed out waiting for background task completion")
    }

    private func isolatedStateStore()
        -> (ScreenTimeSyncStateStore, UserDefaults, String)
    {
        let suite = "screen-time-lifecycle-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return (
            ScreenTimeSyncStateStore(defaults: defaults),
            defaults,
            suite
        )
    }

    func testReportRequestCarriesStableIdempotencyKeyAndNoRawActivity()
        throws
    {
        let report = report()
        let first = try ScreenTimeActivityHTTP.reportRequest(
            pairing: pairing(),
            report: report
        )
        let second = try ScreenTimeActivityHTTP.reportRequest(
            pairing: pairing(),
            report: report
        )
        let body = try XCTUnwrap(first.httpBody)
        let bodyText = String(decoding: body, as: UTF8.self)
            .lowercased()

        XCTAssertEqual(
            first.value(forHTTPHeaderField: "Idempotency-Key"),
            second.value(forHTTPHeaderField: "Idempotency-Key")
        )
        XCTAssertTrue(
            first.value(forHTTPHeaderField: "Idempotency-Key")?
                .hasPrefix("hm-ios-st-v1-") == true
        )
        XCTAssertFalse(bodyText.contains("com.example"))
        XCTAssertFalse(bodyText.contains("bundle"))
        XCTAssertFalse(bodyText.contains("pickup"))
        XCTAssertFalse(bodyText.contains("screenshot"))
        XCTAssertFalse(bodyText.contains("\"url\""))
        XCTAssertFalse(bodyText.contains("\"tap\""))
    }

    func testOutboxPersistsDeduplicatesAndDoesNotStorePairingSecret()
        async throws
    {
        let storage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let now = date("2026-08-16T10:05:00Z")
        let pairing = pairing(token: "never-persist-this-token")
        let report = report()

        let first = try await storage.0.enqueue(
            report: report,
            pairing: pairing,
            now: now
        )
        let duplicate = try await storage.0.enqueue(
            report: report,
            pairing: pairing,
            now: now.addingTimeInterval(1)
        )
        let reloaded = ScreenTimeActivityOutbox(
            fileURL: storage.1.appendingPathComponent("outbox.json"),
            now: now.addingTimeInterval(1)
        )
        let entries = await reloaded.allEntries()
        let stored = try String(
            contentsOf:
                storage.1.appendingPathComponent("outbox.json"),
            encoding: .utf8
        )

        XCTAssertEqual(first.id, duplicate.id)
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].report, report)
        XCTAssertFalse(stored.contains("never-persist-this-token"))
        XCTAssertFalse(stored.contains("healthmes.test.example"))
    }

    func testOutboxIsBoundedAndKeepsNewestEntries() async throws {
        let storage = temporaryOutbox(maximumEntries: 2)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let pairing = pairing()
        let base = date("2026-08-16T10:00:00Z")

        for offset in 0..<3 {
            let report = ScreenTimeActivityReport.unavailable(
                deviceID: "ios-lifecycle-device",
                timezone: "UTC",
                permissionStatus: .denied,
                reason: "denied-\(offset)",
                collectedAt:
                    base.addingTimeInterval(Double(offset)),
                collectionRevision: 3,
                collectionGeneration: 100
            )
            _ = try await storage.0.enqueue(
                report: report,
                pairing: pairing,
                now: base.addingTimeInterval(Double(offset))
            )
        }

        let entries = await storage.0.allEntries()
        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries.map(\.report.reason), [
            "denied-1",
            "denied-2",
        ])
    }

    func testOutboxRejectsPersistedQueueBeyondReloadBound()
        async throws
    {
        let storage = temporaryOutbox(maximumEntries: 3)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let base = date("2026-08-16T10:00:00Z")
        for offset in 0..<3 {
            _ = try await storage.0.enqueue(
                report: .unavailable(
                    deviceID: "ios-lifecycle-device",
                    timezone: "UTC",
                    permissionStatus: .denied,
                    reason: "bounded-\(offset)",
                    collectedAt:
                        base.addingTimeInterval(Double(offset)),
                    collectionRevision: 3,
                    collectionGeneration: 100
                ),
                pairing: pairing(),
                now: base.addingTimeInterval(Double(offset))
            )
        }
        let fileURL = storage.1.appendingPathComponent(
            "outbox.json"
        )

        let reloaded = ScreenTimeActivityOutbox(
            fileURL: fileURL,
            maximumEntries: 2,
            now: base.addingTimeInterval(2)
        )
        let entries = await reloaded.allEntries()

        XCTAssertTrue(entries.isEmpty)
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: fileURL.path)
        )
    }

    func testOutboxBackoffPersistsAcrossRecreation() async throws {
        let policy = ScreenTimeActivityRetryPolicy(
            initialDelay: 10,
            maximumDelay: 60
        )
        let storage = temporaryOutbox(retryPolicy: policy)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let now = date("2026-08-16T10:00:00Z")
        let pairing = pairing()
        let entry = try await storage.0.enqueue(
            report: report(),
            pairing: pairing,
            now: now
        )
        let firstFailure = try await storage.0.markFailed(
            id: entry.id,
            now: now
        )
        let reloaded = ScreenTimeActivityOutbox(
            fileURL: storage.1.appendingPathComponent("outbox.json"),
            retryPolicy: policy,
            now: now
        )
        let persisted = await reloaded.allEntries()
        let secondFailure = try await reloaded.markFailed(
            id: entry.id,
            now: now.addingTimeInterval(10)
        )

        XCTAssertEqual(firstFailure?.failedAttempts, 1)
        XCTAssertEqual(
            firstFailure?.nextAttemptAt,
            now.addingTimeInterval(10)
        )
        XCTAssertEqual(persisted.first?.failedAttempts, 1)
        XCTAssertEqual(secondFailure?.failedAttempts, 2)
        XCTAssertEqual(
            secondFailure?.nextAttemptAt,
            now.addingTimeInterval(30)
        )
    }

    func testOutboxPurgesExpiredEntriesDuringAppRestart()
        async throws
    {
        let retention = 14 * 24 * 60 * 60.0
        let base = date("2026-07-01T10:00:00Z")
        let storage = temporaryOutbox(
            retentionInterval: retention,
            now: base
        )
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        _ = try await storage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: base
        )
        let fileURL = storage.1.appendingPathComponent("outbox.json")

        let reloaded = ScreenTimeActivityOutbox(
            fileURL: fileURL,
            retentionInterval: retention,
            now: base.addingTimeInterval(retention)
        )
        let entries = await reloaded.allEntries()
        let persisted = try String(
            contentsOf: fileURL,
            encoding: .utf8
        )

        XCTAssertTrue(entries.isEmpty)
        XCTAssertFalse(persisted.contains("opaque-record"))
    }

    func testOutboxPurgesExpiredEntriesBeforeOfflineStateFetch()
        async throws
    {
        let retention = 14 * 24 * 60 * 60.0
        let base = date("2026-07-01T10:00:00Z")
        let outboxStorage = temporaryOutbox(
            retentionInterval: retention,
            now: base
        )
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: base
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: ScreenTimeLifecycleOfflineStateTransport(),
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        do {
            _ = try await service.sync(
                pairing: pairing(),
                now: base.addingTimeInterval(retention + 1),
                timezone: TimeZone(secondsFromGMT: 0)!
            )
            XCTFail("expected the offline state fetch to fail")
        } catch {
            // The local TTL purge must happen before this network failure.
        }
        let entries = await outboxStorage.0.allEntries()

        XCTAssertTrue(entries.isEmpty)
    }

    func testOutboxFileIsExcludedFromBackupAfterWriteAndReload()
        async throws
    {
        let now = date("2026-08-16T10:00:00Z")
        let storage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        _ = try await storage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: now
        )
        let fileURL = storage.1.appendingPathComponent("outbox.json")
        let keys: Set<URLResourceKey> = [.isExcludedFromBackupKey]

        XCTAssertEqual(
            try storage.1.resourceValues(forKeys: keys)
                .isExcludedFromBackup,
            true
        )
        XCTAssertEqual(
            try fileURL.resourceValues(forKeys: keys)
                .isExcludedFromBackup,
            true
        )

        var restoredFileURL = fileURL
        var restoredValues = URLResourceValues()
        restoredValues.isExcludedFromBackup = false
        try restoredFileURL.setResourceValues(restoredValues)
        XCTAssertEqual(
            try fileURL.resourceValues(forKeys: keys)
                .isExcludedFromBackup,
            false
        )

        _ = ScreenTimeActivityOutbox(
            fileURL: fileURL,
            now: now
        )

        XCTAssertEqual(
            try fileURL.resourceValues(forKeys: keys)
                .isExcludedFromBackup,
            true
        )
    }

    func testOutboxReconcileDropsWrongPairingRevisionAndRetention()
        async throws
    {
        let now = date("2026-08-16T10:05:00Z")
        let storage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let firstPairing = pairing(token: "first")
        let secondPairing = pairing(token: "second")
        _ = try await storage.0.enqueue(
            report: report(),
            pairing: firstPairing,
            now: now
        )
        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: secondPairing,
            now: now
        )
        let entriesAfterPairingChange =
            await storage.0.allEntries()
        XCTAssertTrue(entriesAfterPairingChange.isEmpty)

        _ = try await storage.0.enqueue(
            report: report(revision: 2),
            pairing: secondPairing,
            now: now
        )
        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: secondPairing,
            state: collectionState(configRevision: 3),
            now: now
        )
        let entriesAfterRevisionChange =
            await storage.0.allEntries()
        XCTAssertTrue(entriesAfterRevisionChange.isEmpty)

        _ = try await storage.0.enqueue(
            report: report(
                start: date("2026-08-16T09:00:00Z")
            ),
            pairing: secondPairing,
            now: now
        )
        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: secondPairing,
            state: collectionState(
                rawRetentionCutoff:
                    date("2026-08-16T09:30:00Z")
            ),
            now: now
        )
        let entriesAfterRetentionChange =
            await storage.0.allEntries()
        XCTAssertTrue(entriesAfterRetentionChange.isEmpty)
    }

    func testOutboxReconcilePurgesObsoleteDeviceIdentity()
        async throws
    {
        let now = date("2026-08-16T10:05:00Z")
        let storage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        _ = try await storage.0.enqueue(
            report: report(deviceID: "obsolete-device"),
            pairing: pairing(),
            now: now
        )

        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: pairing(),
            now: now
        )
        let entries = await storage.0.allEntries()

        XCTAssertTrue(entries.isEmpty)
    }

    func testOutboxPrivacyPurgeRemovesEveryHistoricalDeviceIdentity()
        async throws
    {
        let now = date("2026-08-16T10:05:00Z")
        let storage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        _ = try await storage.0.enqueue(
            report: report(deviceID: "ios-lifecycle-device"),
            pairing: pairing(),
            now: now
        )
        _ = try await storage.0.enqueue(
            report: report(deviceID: "other-device"),
            pairing: pairing(),
            now: now
        )

        let removed = try await storage.0.purgeAll()
        let entries = await storage.0.allEntries()

        XCTAssertEqual(removed, 2)
        XCTAssertTrue(entries.isEmpty)
    }

    func testOfflineReportIsRetriedExactlyBeforeFreshSnapshot()
        async throws
    {
        let outboxStorage = temporaryOutbox(
            retryPolicy: ScreenTimeActivityRetryPolicy(
                initialDelay: 10,
                maximumDelay: 60
            )
        )
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let collector = ScreenTimeLifecycleTestCollector(
            result: collectorResult(),
            pseudonymKeyID:
                "ios-key-" + String(repeating: "1", count: 40),
            counter: collectorCounter
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .transport(
                        underlying: URLError(
                            .notConnectedToInternet
                        )
                    )
                ),
                .succeed,
                .succeed,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: collector,
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")

        let offline = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let deferred = try await service.sync(
            pairing: pairing(),
            now: now.addingTimeInterval(5),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let recovered = try await service.sync(
            pairing: pairing(),
            now: now.addingTimeInterval(11),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await collectorCounter.values()
        let pendingCount = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )

        guard case .queued(_, let retryAt, 1) = offline else {
            return XCTFail("expected offline report to be queued")
        }
        XCTAssertEqual(retryAt, now.addingTimeInterval(10))
        XCTAssertEqual(
            deferred,
            .deferred(
                reason: "retry_backoff",
                retryAt: now.addingTimeInterval(10),
                queueDepth: 1
            )
        )
        guard case .uploaded = recovered else {
            return XCTFail("expected catch-up upload")
        }
        XCTAssertEqual(reports.count, 3)
        XCTAssertEqual(reports[0], reports[1])
        XCTAssertNotEqual(
            reports[1].snapshotSequence,
            reports[2].snapshotSequence
        )
        XCTAssertEqual(counts.collection, 3)
        XCTAssertEqual(pendingCount, 0)
    }

    func testPermanent422IsQuarantinedAndDoesNotBlockNextSnapshot()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 422,
                        code: "activity_write_conflict",
                        message: "permanent invalid snapshot",
                        detail: nil
                    )
                ),
                .succeed,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let terminal = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let recovered = try await service.sync(
            pairing: pairing(),
            now: now.addingTimeInterval(1),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let quarantined =
            await outboxStorage.0.quarantinedEntries()
        let pending = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(
            terminal,
            .skipped(reason: "activity_write_conflict")
        )
        guard case .uploaded = recovered else {
            return XCTFail("expected later snapshot to upload")
        }
        XCTAssertEqual(reports.count, 2)
        XCTAssertEqual(quarantined.count, 1)
        XCTAssertEqual(
            quarantined[0].terminalReason,
            "activity_write_conflict"
        )
        XCTAssertEqual(quarantined[0].terminalStatusCode, 422)
        XCTAssertEqual(pending, 0)
    }

    func testUnclassified409IsQuarantinedAndDoesNotBlockNextSnapshot()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code: "future_server_fence",
                        message: "permanent fence",
                        detail: nil
                    )
                ),
                .succeed,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let terminal = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let recovered = try await service.sync(
            pairing: pairing(),
            now: now.addingTimeInterval(1),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let quarantined =
            await outboxStorage.0.quarantinedEntries()
        let pending = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )

        XCTAssertEqual(
            terminal,
            .skipped(reason: "future_server_fence")
        )
        guard case .uploaded = recovered else {
            return XCTFail("expected later snapshot to upload")
        }
        XCTAssertEqual(quarantined.count, 1)
        XCTAssertEqual(
            quarantined[0].terminalReason,
            "future_server_fence"
        )
        XCTAssertEqual(quarantined[0].terminalStatusCode, 409)
        XCTAssertEqual(pending, 0)
    }

    func testUnclassifiedHTTPClientErrorsAreQuarantined()
        async throws
    {
        for statusCode in [400, 403, 404] {
            let now = date("2026-08-16T10:34:00Z")
            let outboxStorage = temporaryOutbox(now: now)
            let stateStorage = isolatedStateStore()
            let transport = ScreenTimeLifecycleTestTransport(
                state: collectionState(),
                uploadActions: [
                    .fail(.httpStatus(statusCode))
                ]
            )
            let service = ScreenTimeActivitySyncService(
                deviceID: "ios-lifecycle-device",
                collector: ScreenTimeLifecycleTestCollector(
                    result: collectorResult(),
                    pseudonymKeyID:
                        "ios-key-" + String(repeating: "1", count: 40)
                ),
                transport: transport,
                stateStore: stateStorage.0,
                outbox: outboxStorage.0
            )

            let outcome = try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
            let quarantined =
                await outboxStorage.0.quarantinedEntries()

            XCTAssertEqual(
                outcome,
                .skipped(reason: "http_\(statusCode)")
            )
            XCTAssertEqual(quarantined.count, 1)
            XCTAssertEqual(
                quarantined[0].terminalStatusCode,
                statusCode
            )
            XCTAssertEqual(quarantined[0].failedAttempts, 0)

            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
    }

    func testUnknownCompletedPostFailureIsQuarantined()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [.failUnknown]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let quarantined =
            await outboxStorage.0.quarantinedEntries()

        XCTAssertEqual(
            outcome,
            .skipped(reason: "ios_screen_time_upload_failed")
        )
        XCTAssertEqual(quarantined.count, 1)
        XCTAssertNil(quarantined[0].terminalStatusCode)
    }

    func testFenceResetFailureQuarantinesResetReport() async throws {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(at: outboxStorage.1)
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code: "activity_snapshot_fence_reset_required",
                        message: "reset required",
                        detail: nil
                    )
                ),
                .fail(
                    .server(
                        statusCode: 409,
                        code: "future_server_fence",
                        message: "permanent reset rejection",
                        detail: nil
                    )
                ),
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let quarantined =
            await outboxStorage.0.quarantinedEntries()

        XCTAssertEqual(
            outcome,
            .skipped(reason: "future_server_fence")
        )
        XCTAssertEqual(quarantined.count, 1)
        XCTAssertTrue(quarantined[0].report.resetSnapshotFence)
    }

    func testRetryable409WriteConflictRemainsPending() async throws {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code: "activity_write_conflict",
                        message: "retry transaction race",
                        detail: nil
                    )
                )
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let pending = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )
        let quarantined = await outboxStorage.0.quarantinedCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )

        guard case .queued(
            reason: "activity_write_conflict",
            retryAt: _,
            queueDepth: 1
        ) = outcome else {
            return XCTFail("expected write conflict to remain retryable")
        }
        XCTAssertEqual(pending, 1)
        XCTAssertEqual(quarantined, 0)
    }

    func testQueuedReportDrainsBeforeTransientCollectionFailure()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let queued = report(
            sequence: 7,
            start: date("2026-08-16T08:00:00Z")
        )
        _ = try await outboxStorage.0.enqueue(
            report: queued,
            pairing: pairing(),
            now: now
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [.succeed]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleThrowingCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        do {
            _ = try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
            XCTFail("expected transient export failure")
        } catch let error as ScreenTimeActivityCollectionError {
            XCTAssertEqual(error, .exportFailed)
        }
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()

        XCTAssertEqual(reports, [queued])
        XCTAssertTrue(entries.isEmpty)
    }

    func testPermissionRevocationDropsQueuedAggregateBeforeUpload()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: date("2026-08-16T10:00:00Z")
        )
        let denied = collectorResult(
            capability: .unavailable,
            permission: .denied,
            reason: "ios_screen_time_permission_denied"
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: denied,
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_denied"
            )
        )
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .unavailable)
        XCTAssertEqual(reports[0].permissionStatus, .denied)
        XCTAssertFalse(
            reports.contains(where: { $0.capability == .aggregate })
        )
        XCTAssertTrue(entries.isEmpty)
    }

    func testExportDetectedRevocationFencesQueuedAggregate()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: date("2026-08-16T10:00:00Z")
        )
        let granted = collectorResult()
        let revoked = collectorResult(
            capability: .unavailable,
            permission: .revoked,
            reason: "ios_screen_time_permission_revoked"
        )
        let collectorState =
            ScreenTimeLifecycleSequencedCollectorState(
                authorizationResults: [granted, revoked],
                collectionResult: revoked
            )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleSequencedCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                state: collectorState
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()
        let collectionCalls =
            await collectorState.capturedCollectionCalls()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_revoked"
            )
        )
        XCTAssertEqual(collectionCalls, 1)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .unavailable)
        XCTAssertEqual(reports[0].permissionStatus, .revoked)
        XCTAssertFalse(
            reports.contains(where: { $0.capability == .aggregate })
        )
        XCTAssertTrue(entries.isEmpty)
    }

    func testTransientExportDetectedRevocationFencesQueuedAggregate()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: date("2026-08-16T10:00:00Z")
        )
        let granted = collectorResult()
        let revoked = collectorResult(
            capability: .unavailable,
            permission: .revoked,
            reason: "ios_screen_time_permission_revoked"
        )
        let collectorState =
            ScreenTimeLifecycleSequencedCollectorState(
                authorizationResults: [granted, revoked],
                collectionResult: granted
            )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleSequencedThrowingCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                state: collectorState
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()
        let collectionCalls =
            await collectorState.capturedCollectionCalls()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_revoked"
            )
        )
        XCTAssertEqual(collectionCalls, 1)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .unavailable)
        XCTAssertEqual(reports[0].permissionStatus, .revoked)
        XCTAssertFalse(
            reports.contains(where: { $0.capability == .aggregate })
        )
        XCTAssertTrue(entries.isEmpty)
    }

    func testGrantedPermissionDropsBackedOffUnavailableBeforeFirstSync()
        async throws
    {
        let retryPolicy = ScreenTimeActivityRetryPolicy(
            initialDelay: 60,
            maximumDelay: 60
        )
        let outboxStorage = temporaryOutbox(
            retryPolicy: retryPolicy
        )
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let queuedAt = date("2026-08-16T10:00:00Z")
        let queued = try await outboxStorage.0.enqueue(
            report: .unavailable(
                deviceID: "ios-lifecycle-device",
                timezone: "UTC",
                permissionStatus: .denied,
                reason: "ios_screen_time_permission_denied",
                collectedAt: queuedAt,
                collectionRevision: 3,
                collectionGeneration: 99
            ),
            pairing: pairing(),
            now: queuedAt
        )
        _ = try await outboxStorage.0.markFailed(
            id: queued.id,
            now: queuedAt
        )
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: queuedAt.addingTimeInterval(5),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let entries = await outboxStorage.0.allEntries()

        guard case .uploaded = outcome else {
            return XCTFail("expected newly authorized first sync")
        }
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .aggregate)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertTrue(entries.isEmpty)
    }

    func testConcurrentSyncCallsShareOneInFlightPipeline()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let collector = ScreenTimeLifecycleTestCollector(
            result: collectorResult(),
            pseudonymKeyID:
                "ios-key-" + String(repeating: "1", count: 40),
            counter: collectorCounter
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 100_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: collector,
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")

        async let first = service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        async let second = service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let outcomes = try await [first, second]
        let counts = await collectorCounter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        XCTAssertEqual(outcomes[0], outcomes[1])
        XCTAssertEqual(stateCalls, 3)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(counts.collection, 1)
    }

    func testAuthorizationAndConfigurationTriggersCoalesceIntoOneRerun()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 150_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let first = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(1, from: transport)

        let authorization = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .authorizationChanged
            )
        }
        let configuration = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .inputConfigurationChanged
            )
        }

        _ = try await [first.value, authorization.value, configuration.value]
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        XCTAssertEqual(stateCalls, 4)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testTimezoneChangeQueuesFreshRunWithLatestTimezone()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 150_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let utc = TimeZone(secondsFromGMT: 0)!
        let seoul = TimeZone(secondsFromGMT: 9 * 60 * 60)!
        let first = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: utc
            )
        }
        try await waitForStateCalls(1, from: transport)

        let second = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: seoul
            )
        }

        _ = try await [first.value, second.value]
        let reports = await transport.capturedReports()

        XCTAssertEqual(reports.count, 2)
        XCTAssertEqual(reports[0].timezone, utc.identifier)
        XCTAssertEqual(reports[1].timezone, seoul.identifier)
    }

    func testSoleWaiterCancellationLeavesServiceOwnedPipelineRunning()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 100_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let work = Task {
            try await service.sync(
                pairing: pairing(),
                now: date("2026-08-16T10:34:00Z"),
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(1, from: transport)

        work.cancel()

        do {
            _ = try await work.value
            XCTFail("expected the cancelled waiter to stop waiting")
        } catch is CancellationError {
            // Expected.
        }
        let replacement = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        guard case .uploaded = replacement else {
            return XCTFail("expected replacement waiter to join the pipeline")
        }
        XCTAssertEqual(stateCalls, 3)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testCancellingOneOfTwoWaitersDoesNotCancelTheOther()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 100_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let first = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        let second = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForWaiterCounts(
            active: 2,
            from: service
        )

        second.cancel()

        do {
            _ = try await second.value
            XCTFail("expected only the second waiter to cancel")
        } catch is CancellationError {
            // Expected.
        }
        let firstOutcome = try await first.value
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        guard case .uploaded = firstOutcome else {
            return XCTFail("expected the first waiter to complete")
        }
        XCTAssertEqual(stateCalls, 3)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testBackgroundExpirationCancelsServiceOwnedPipeline()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let queuedReport = report(
            sequence: 7,
            start: date("2026-08-16T08:00:00Z")
        )
        let queuedEntry = try await outboxStorage.0.enqueue(
            report: queuedReport,
            pairing: pairing(),
            now: now.addingTimeInterval(-60)
        )
        let uploadStarted = expectation(
            description: "background upload started"
        )
        let uploadStopped = expectation(
            description: "background upload stopped"
        )
        ScreenTimeLifecycleCancellationURLProtocol.uploadStarted = {
            uploadStarted.fulfill()
        }
        ScreenTimeLifecycleCancellationURLProtocol.uploadStopped = {
            uploadStopped.fulfill()
        }
        defer {
            ScreenTimeLifecycleCancellationURLProtocol.uploadStarted = nil
            ScreenTimeLifecycleCancellationURLProtocol.uploadStopped = nil
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [
            ScreenTimeLifecycleCancellationURLProtocol.self
        ]
        let session = URLSession(configuration: configuration)
        defer {
            session.invalidateAndCancel()
        }
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: URLSessionScreenTimeActivityTransport(
                session: session
            ),
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let work = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .backgroundRefresh
            )
        }
        await fulfillment(of: [uploadStarted], timeout: 2)

        work.cancel()

        do {
            _ = try await work.value
            XCTFail("expected background expiration cancellation")
        } catch is CancellationError {
            // Expected.
        }
        await fulfillment(of: [uploadStopped], timeout: 2)
        let entries = await outboxStorage.0.allEntries()

        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].id, queuedEntry.id)
        XCTAssertEqual(entries[0].report, queuedReport)
        XCTAssertEqual(entries[0].failedAttempts, 1)
        XCTAssertEqual(
            entries[0].nextAttemptAt,
            now.addingTimeInterval(60)
        )
    }

    func testBackgroundExpirationDetachesBeforeForegroundReplacement()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleBlockingTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let background = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .backgroundRefresh
            )
        }
        try await waitForStateCalls(1, from: transport)

        background.cancel()
        try await waitForStateCancellations(1, from: transport)

        let foreground = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(2, from: transport)
        await transport.releaseStateCalls()

        do {
            _ = try await background.value
            XCTFail("expected expired background waiter to cancel")
        } catch is CancellationError {
            // Expected.
        }
        let foregroundOutcome = try await foreground.value
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        guard case .uploaded = foregroundOutcome else {
            return XCTFail("expected a fresh foreground pipeline")
        }
        XCTAssertEqual(stateCalls, 4)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testBackgroundExpirationPreservesSharedForegroundWaiter()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 150_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let background = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .backgroundRefresh
            )
        }
        try await waitForStateCalls(1, from: transport)
        let foreground = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForWaiterCounts(
            active: 2,
            from: service
        )

        background.cancel()

        do {
            _ = try await background.value
            XCTFail("expected the background waiter to cancel")
        } catch is CancellationError {
            // Expected.
        }
        let foregroundOutcome = try await foreground.value
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        guard case .uploaded = foregroundOutcome else {
            return XCTFail("expected foreground waiter to complete")
        }
        XCTAssertEqual(stateCalls, 3)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testPendingBackgroundExpirationDoesNotWaitForForegroundPredecessor()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleBlockingTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let now = date("2026-08-16T10:34:00Z")
        let foreground = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(1, from: transport)
        let background = Task {
            try await service.sync(
                pairing: pairing(),
                now: now.addingTimeInterval(3_600),
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .backgroundRefresh
            )
        }
        try await waitForWaiterCounts(
            active: 1,
            pending: 1,
            from: service
        )
        let cancelled = expectation(
            description: "pending background waiter detached"
        )

        background.cancel()
        Task {
            do {
                _ = try await background.value
                XCTFail("expected pending background cancellation")
            } catch is CancellationError {
                cancelled.fulfill()
            } catch {
                XCTFail("unexpected error: \(error)")
            }
        }

        await fulfillment(of: [cancelled], timeout: 2)
        let stateCalls = await transport.capturedStateCalls()
        XCTAssertEqual(stateCalls, 1)

        await transport.releaseStateCalls()
        _ = try await foreground.value
    }

    func testDestinationRemovalCancelsURLSessionWithoutRetryWork()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let uploadStarted = expectation(
            description: "URLSession upload started"
        )
        let uploadStopped = expectation(
            description: "URLSession upload stopped"
        )
        ScreenTimeLifecycleCancellationURLProtocol.uploadStarted = {
            uploadStarted.fulfill()
        }
        ScreenTimeLifecycleCancellationURLProtocol.uploadStopped = {
            uploadStopped.fulfill()
        }
        defer {
            ScreenTimeLifecycleCancellationURLProtocol.uploadStarted = nil
            ScreenTimeLifecycleCancellationURLProtocol.uploadStopped = nil
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [
            ScreenTimeLifecycleCancellationURLProtocol.self
        ]
        let session = URLSession(configuration: configuration)
        defer {
            session.invalidateAndCancel()
        }
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: URLSessionScreenTimeActivityTransport(
                session: session
            ),
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let work = Task {
            try await service.sync(
                pairing: pairing(),
                now: date("2026-08-16T10:34:00Z"),
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        await fulfillment(of: [uploadStarted], timeout: 2)

        try await service.reconcilePendingUploads(pairing: nil)

        do {
            _ = try await work.value
            XCTFail("expected destination removal to cancel upload")
        } catch is CancellationError {
            // Expected.
        }
        await fulfillment(of: [uploadStopped], timeout: 2)
        let entries = await outboxStorage.0.allEntries()

        XCTAssertTrue(entries.isEmpty)
    }

    func testPairingChangeCancelsOldPipelineBeforeUpload()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let collector = ScreenTimeLifecycleTestCollector(
            result: collectorResult(),
            pseudonymKeyID:
                "ios-key-" + String(repeating: "1", count: 40),
            counter: counter
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 200_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: collector,
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let firstPairing = pairing(
            token: "first",
            baseURL: "https://first.healthmes.test"
        )
        let secondPairing = pairing(
            token: "second",
            baseURL: "https://second.healthmes.test"
        )
        let now = date("2026-08-16T10:34:00Z")
        let first = Task {
            try await service.sync(
                pairing: firstPairing,
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(1, from: transport)

        let second = try await service.sync(
            pairing: secondPairing,
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        do {
            _ = try await first.value
            XCTFail("expected the old destination sync to cancel")
        } catch is CancellationError {
            // Expected: changed pairing must stop the old destination.
        }
        let pairings = await transport.capturedPairings()
        let reports = await transport.capturedReports()
        let counts = await counter.values()

        guard case .uploaded = second else {
            return XCTFail("expected the new pairing to upload")
        }
        XCTAssertEqual(pairings.state, [
            firstPairing,
            secondPairing,
            secondPairing,
            secondPairing,
        ])
        XCTAssertEqual(pairings.report, [secondPairing])
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(counts.collection, 1)
    }

    func testRevisionChangeDuringCollectionPurgesOldOutboxAndRecollects()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(revision: 3),
            pairing: pairing(),
            now: now
        )
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let entries = await outboxStorage.0.allEntries()

        guard case .uploaded = outcome else {
            return XCTFail("expected revision 4 recollection to upload")
        }
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].collectionRevision, 4)
        XCTAssertTrue(entries.isEmpty)
    }

    func testDisableObservedAfterCollectionPreventsStaleUpload()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let disabled = collectionState(
            enabled: false,
            configRevision: 4
        )
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                disabled,
                disabled,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()

        XCTAssertEqual(
            outcome,
            .skipped(reason: "collection_disabled")
        )
        XCTAssertEqual(counts.collection, 1)
        XCTAssertTrue(reports.isEmpty)
    }

    func testPauseObservedAfterCollectionPreventsStaleUpload()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let paused = collectionState(
            pausedUntil: date("2026-08-16T12:00:00Z"),
            configRevision: 4
        )
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                paused,
                paused,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()

        XCTAssertEqual(
            outcome,
            .skipped(reason: "collection_paused")
        )
        XCTAssertEqual(counts.collection, 1)
        XCTAssertTrue(reports.isEmpty)
    }

    func testRetentionChangeDuringCollectionRecollectsWithinNewCutoff()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let retained = collectionState(
            configRevision: 4,
            rawRetentionCutoff: date("2026-08-16T07:30:00Z")
        )
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                retained,
                retained,
                retained,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()

        guard case .uploaded = outcome else {
            return XCTFail("expected recollection within new retention")
        }
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].collectionRevision, 4)
        XCTAssertEqual(
            reports[0].snapshotStart,
            date("2026-08-16T09:00:00Z")
        )
    }

    func testMovingRetentionCutoffDoesNotMasqueradeAsConfigChange()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(
                    rawRetentionCutoff:
                        date("2026-08-15T06:00:00Z")
                ),
                collectionState(
                    rawRetentionCutoff:
                        date("2026-08-15T06:00:01Z")
                ),
                collectionState(
                    rawRetentionCutoff:
                        date("2026-08-15T06:00:02Z")
                ),
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()

        guard case .uploaded = outcome else {
            return XCTFail("expected moving cutoff to remain uploadable")
        }
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(stateCalls, 3)
        XCTAssertEqual(reports.count, 1)
    }

    func testAuthorizationRevokedAfterFinalFenceBlocksAggregatePost()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let granted = collectorResult()
        let revoked = collectorResult(
            capability: .unavailable,
            permission: .revoked,
            reason: "ios_screen_time_permission_revoked"
        )
        let collectorState =
            ScreenTimeLifecycleSequencedCollectorState(
                authorizationResults: [
                    granted,
                    granted,
                    granted,
                    revoked,
                ],
                collectionResult: granted
            )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleSequencedCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                state: collectorState
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let collectionCalls =
            await collectorState.capturedCollectionCalls()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_revoked"
            )
        )
        XCTAssertEqual(collectionCalls, 2)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .unavailable)
        XCTAssertEqual(reports[0].permissionStatus, .revoked)
        XCTAssertFalse(
            reports.contains(where: { $0.capability == .aggregate })
        )
    }

    func testGrantedSupersedeSettlesSuccessfulQueuedPost()
        async throws
    {
        let result = try await runGrantedAuthorizationSupersedeRace(
            firstAction: .succeed
        )

        XCTAssertEqual(
            result.activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        guard case .uploaded = result.freshOutcome else {
            return XCTFail("expected the fresh pipeline to upload")
        }
        XCTAssertEqual(
            result.reports.filter {
                $0.collectionGeneration == 100
            }.count,
            1
        )
        XCTAssertTrue(result.entries.isEmpty)
    }

    func testGrantedSupersedeBacksOffRetryableQueuedPost()
        async throws
    {
        let retryPolicy = ScreenTimeActivityRetryPolicy(
            initialDelay: 10,
            maximumDelay: 10
        )
        let result = try await runGrantedAuthorizationSupersedeRace(
            firstAction: .fail(
                .transport(
                    underlying:
                        ScreenTimeLifecycleTestError.collectionFailed
                )
            ),
            retryPolicy: retryPolicy
        )

        XCTAssertEqual(
            result.activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        XCTAssertEqual(
            result.freshOutcome,
            .deferred(
                reason: "retry_backoff",
                retryAt:
                    date("2026-08-16T10:34:00Z")
                    .addingTimeInterval(10),
                queueDepth: 1
            )
        )
        XCTAssertEqual(result.reports.count, 1)
        XCTAssertEqual(result.entries.count, 1)
        XCTAssertEqual(result.entries[0].failedAttempts, 1)
        XCTAssertNil(result.entries[0].terminalReason)
    }

    func testGrantedSupersedeDeletesCollectionRefreshRejection()
        async throws
    {
        let result = try await runGrantedAuthorizationSupersedeRace(
            firstAction: .fail(
                .server(
                    statusCode: 422,
                    code: "activity_outside_retention",
                    message: "collection window expired",
                    detail: nil
                )
            )
        )

        XCTAssertEqual(
            result.activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        guard case .uploaded = result.freshOutcome else {
            return XCTFail("expected fresh collection after rejection")
        }
        XCTAssertEqual(
            result.reports.filter {
                $0.collectionGeneration == 100
            }.count,
            1
        )
        XCTAssertTrue(result.entries.isEmpty)
    }

    func testGrantedSupersedeQuarantinesTerminalQueuedPost()
        async throws
    {
        let result = try await runGrantedAuthorizationSupersedeRace(
            firstAction: .fail(
                .server(
                    statusCode: 422,
                    code: "activity_source_conflict",
                    message: "terminal aggregate",
                    detail: nil
                )
            )
        )

        XCTAssertEqual(
            result.activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        guard case .uploaded = result.freshOutcome else {
            return XCTFail("expected fresh collection after quarantine")
        }
        XCTAssertEqual(
            result.reports.filter {
                $0.collectionGeneration == 100
            }.count,
            1
        )
        XCTAssertEqual(result.entries.count, 1)
        XCTAssertEqual(
            result.entries[0].terminalReason,
            "activity_source_conflict"
        )
    }

    func testFenceResetSupersessionPersistsResetReportWithBackoff()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let retryPolicy = ScreenTimeActivityRetryPolicy(
            initialDelay: 10,
            maximumDelay: 10
        )
        let outboxStorage = temporaryOutbox(
            retryPolicy: retryPolicy,
            now: now
        )
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let original = report(generation: 100)
        _ = try await outboxStorage.0.enqueue(
            report: original,
            pairing: pairing(),
            now: now.addingTimeInterval(-60)
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code:
                            "activity_snapshot_fence_reset_required",
                        message: "reset required",
                        detail: nil
                    )
                ),
                .succeed,
            ],
            stateDelayNanoseconds: 100_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let active = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForReports(1, from: transport)
        try await waitForStateCalls(4, from: transport)
        let fresh = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .authorizationChanged
            )
        }

        let activeOutcome = try await active.value
        let freshOutcome = try await fresh.value
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()

        XCTAssertEqual(
            activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        XCTAssertEqual(
            freshOutcome,
            .deferred(
                reason: "retry_backoff",
                retryAt: now.addingTimeInterval(10),
                queueDepth: 1
            )
        )
        XCTAssertEqual(reports, [original])
        XCTAssertEqual(entries.count, 1)
        XCTAssertTrue(entries[0].report.resetSnapshotFence)
        XCTAssertNotEqual(entries[0].report, original)
        XCTAssertEqual(entries[0].failedAttempts, 1)
        XCTAssertEqual(
            entries[0].nextAttemptAt,
            now.addingTimeInterval(10)
        )
        XCTAssertNil(entries[0].terminalReason)
    }

    func testAuthorizationTriggerDuringTerminalUploadFencesNextQueuedItem()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(generation: 100),
            pairing: pairing(),
            now: now.addingTimeInterval(-120)
        )
        _ = try await outboxStorage.0.enqueue(
            report: report(generation: 101),
            pairing: pairing(),
            now: now.addingTimeInterval(-60)
        )
        let granted = collectorResult()
        let revoked = collectorResult(
            capability: .unavailable,
            permission: .revoked,
            reason: "ios_screen_time_permission_revoked"
        )
        let collectorState =
            ScreenTimeLifecycleSequencedCollectorState(
                authorizationResults: [
                    granted,
                    granted,
                    granted,
                    granted,
                    revoked,
                ],
                collectionResult: granted
            )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 422,
                        code: "activity_source_conflict",
                        message: "terminal aggregate",
                        detail: nil
                    )
                ),
                .succeed,
            ],
            uploadDelayNanoseconds: 300_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleSequencedCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                state: collectorState
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let active = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForReports(1, from: transport)
        let authorizationChange = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!,
                trigger: .authorizationChanged
            )
        }

        let activeOutcome = try await active.value
        let freshOutcome = try await authorizationChange.value
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()
        let aggregateReports = reports.filter {
            $0.capability == .aggregate
        }

        XCTAssertEqual(
            activeOutcome,
            .skipped(reason: "ios_screen_time_sync_superseded")
        )
        XCTAssertEqual(
            freshOutcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_revoked"
            )
        )
        XCTAssertEqual(aggregateReports.count, 1)
        XCTAssertEqual(reports.last?.capability, .unavailable)
        XCTAssertTrue(entries.isEmpty)
    }

    func testPendingStaleRevisionIsDeletedAndRecollected()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(revision: 3),
            pairing: pairing(),
            now: now.addingTimeInterval(-60)
        )
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                collectionState(configRevision: 3),
                collectionState(configRevision: 3),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
            ],
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code: "stale_collection_revision",
                        message: "stale queued aggregate",
                        detail: nil
                    )
                ),
                .succeed,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let entries = await outboxStorage.0.allEntries()

        guard case .uploaded = outcome else {
            return XCTFail("expected stale queued report recovery")
        }
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(
            reports.map(\.collectionRevision),
            [3, 4]
        )
        XCTAssertTrue(entries.isEmpty)
    }

    func testCollectorUnavailableIsReportedWhileAuthorizationIsGranted()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let granted = collectorResult()
        let unavailable = collectorResult(
            capability: .unavailable,
            permission: .granted,
            reason: "ios_screen_time_snapshot_exceeds_upload_limit"
        )
        let collectorState =
            ScreenTimeLifecycleSequencedCollectorState(
                authorizationResults: [granted],
                collectionResult: unavailable
            )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleSequencedCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                state: collectorState
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let collectionCalls =
            await collectorState.capturedCollectionCalls()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_snapshot_exceeds_upload_limit"
            )
        )
        XCTAssertEqual(collectionCalls, 1)
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].capability, .unavailable)
        XCTAssertEqual(reports[0].permissionStatus, .granted)
    }

    func testControlPlaneRejectionsNeverQuarantinePrivatePayload()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let codes = [
            "activity_outside_retention",
            "activity_collection_blocked",
            "ios_exclusion_reapproval_required",
        ]

        for code in codes {
            let outboxStorage = temporaryOutbox(now: now)
            defer {
                try? FileManager.default.removeItem(
                    at: outboxStorage.1
                )
            }
            let stateStorage = isolatedStateStore()
            defer {
                stateStorage.1.removePersistentDomain(
                    forName: stateStorage.2
                )
            }
            let counter = ScreenTimeLifecycleTestCounter()
            let rejection = ScreenTimeLifecycleUploadAction.fail(
                .server(
                    statusCode: 422,
                    code: code,
                    message: "control plane changed",
                    detail: nil
                )
            )
            let transport = ScreenTimeLifecycleTestTransport(
                state: collectionState(),
                uploadActions: [
                    rejection,
                    rejection,
                    rejection,
                ]
            )
            let service = ScreenTimeActivitySyncService(
                deviceID: "ios-lifecycle-device",
                collector: ScreenTimeLifecycleTestCollector(
                    result: collectorResult(),
                    pseudonymKeyID:
                        "ios-key-" + String(repeating: "1", count: 40),
                    counter: counter
                ),
                transport: transport,
                stateStore: stateStorage.0,
                outbox: outboxStorage.0
            )

            let outcome = try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
            let entries = await outboxStorage.0.allEntries()
            let counts = await counter.values()
            let reports = await transport.capturedReports()

            XCTAssertEqual(
                outcome,
                .skipped(
                    reason:
                        "ios_screen_time_collection_configuration_changed"
                ),
                code
            )
            XCTAssertEqual(counts.collection, 3, code)
            XCTAssertEqual(reports.count, 3, code)
            XCTAssertTrue(entries.isEmpty, code)
        }
    }

    func testServerStaleRevisionTriggersFreshCollectionInsteadOfQuarantine()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 3),
                collectionState(configRevision: 3),
                collectionState(configRevision: 3),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
                collectionState(configRevision: 4),
            ],
            uploadActions: [
                .fail(
                    .server(
                        statusCode: 409,
                        code: "stale_collection_revision",
                        message: "collection settings changed",
                        detail: nil
                    )
                ),
                .succeed,
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let pending = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )
        let quarantined = await outboxStorage.0.quarantinedCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )

        guard case .uploaded = outcome else {
            return XCTFail("expected fresh revision to recover")
        }
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(
            reports.map(\.collectionRevision),
            [3, 4]
        )
        XCTAssertEqual(pending, 0)
        XCTAssertEqual(quarantined, 0)
    }

    func testRepeatedCollectionChangesStopAfterThreeFreshAttempts()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            states: [
                collectionState(configRevision: 1),
                collectionState(configRevision: 2),
                collectionState(configRevision: 2),
                collectionState(configRevision: 3),
                collectionState(configRevision: 3),
                collectionState(configRevision: 4),
            ]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: now,
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let counts = await counter.values()
        let stateCalls = await transport.capturedStateCalls()

        XCTAssertEqual(
            outcome,
            .skipped(
                reason:
                    "ios_screen_time_collection_configuration_changed"
            )
        )
        XCTAssertEqual(counts.collection, 3)
        XCTAssertEqual(stateCalls, 6)
        XCTAssertTrue(reports.isEmpty)
    }

    func testDisabledPurgesPendingDataWithoutCollectingOrUploading()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: date("2026-08-16T10:00:00Z")
        )
        let counter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(enabled: false)
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: counter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let counts = await counter.values()
        let reports = await transport.capturedReports()
        let entries = await outboxStorage.0.allEntries()

        XCTAssertEqual(
            outcome,
            .skipped(reason: "collection_disabled")
        )
        XCTAssertEqual(counts.collection, 0)
        XCTAssertTrue(reports.isEmpty)
        XCTAssertTrue(entries.isEmpty)
    }

    func testPausedKeepsPendingDataButDoesNotUpload() async throws {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: date("2026-08-16T10:00:00Z")
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(
                pausedUntil: date("2026-08-16T12:00:00Z")
            )
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        let outcome = try await service.sync(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()
        let pendingCount = await outboxStorage.0.pendingCount(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )

        XCTAssertEqual(
            outcome,
            .skipped(reason: "collection_paused")
        )
        XCTAssertTrue(reports.isEmpty)
        XCTAssertEqual(pendingCount, 1)
    }

    func testCollectorFailurePreservesAuthorizationGenerationAndBoundary()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState()
        )
        let timezone = TimeZone(secondsFromGMT: 0)!
        let initialNow = date("2026-08-16T09:34:00Z")
        let initialGeneration =
            await stateStorage.0.collectionGeneration(
                deviceID: "ios-lifecycle-device",
                permissionStatus: .granted,
                now: initialNow
            )
        let initialBoundary =
            try await stateStorage.0.acceptTimezoneBoundary(
                deviceID: "ios-lifecycle-device",
                timezone: timezone,
                now: initialNow
            )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleThrowingCollector(
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )

        do {
            _ = try await service.sync(
                pairing: pairing(),
                now: date("2026-08-16T10:34:00Z"),
                timezone: timezone
            )
            XCTFail("expected transient export failure")
        } catch let error as ScreenTimeActivityCollectionError {
            XCTAssertEqual(error, .exportFailed)
        }
        let reports = await transport.capturedReports()
        let generationAfterFailure =
            await stateStorage.0.collectionGeneration(
                deviceID: "ios-lifecycle-device",
                permissionStatus: .granted,
                now: date("2026-08-16T10:35:00Z")
            )
        let boundaryAfterFailure =
            try await stateStorage.0.proposedTimezoneBoundary(
                deviceID: "ios-lifecycle-device",
                timezone: timezone,
                now: date("2026-08-16T10:35:00Z")
            )

        XCTAssertTrue(reports.isEmpty)
        XCTAssertEqual(generationAfterFailure, initialGeneration)
        XCTAssertEqual(boundaryAfterFailure, initialBoundary)
    }

    func testOptOutResetAdvancesGenerationAndClearsCollectionBoundaries()
        async throws
    {
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let deviceID = "ios-lifecycle-device"
        let timezone = TimeZone(secondsFromGMT: 0)!
        let initialNow = date("2026-08-16T09:34:00Z")
        let resetNow = date("2026-08-16T12:34:00Z")
        let pseudonymKeyID =
            "ios-key-" + String(repeating: "1", count: 40)
        let excludedToken =
            "ios-app-v2-"
            + String(repeating: "1", count: 40)
            + "-"
            + String(repeating: "a", count: 40)
        let initialGeneration =
            await stateStorage.0.collectionGeneration(
                deviceID: deviceID,
                permissionStatus: .granted,
                now: initialNow
            )
        _ = try await stateStorage.0.acceptTimezoneBoundary(
            deviceID: deviceID,
            timezone: timezone,
            now: initialNow
        )
        _ = await stateStorage.0.preparePseudonymBoundary(
            deviceID: deviceID,
            pseudonymKeyID: pseudonymKeyID,
            excludedAppTokens: [],
            now: initialNow
        )
        _ = await stateStorage.0.allocateSnapshotSequence(
            deviceID: deviceID
        )

        await stateStorage.0.resetAfterOptOut(
            deviceID: deviceID,
            now: resetNow
        )

        let generationAfterReset =
            await stateStorage.0.collectionGeneration(
                deviceID: deviceID,
                permissionStatus: .granted,
                now: resetNow
            )
        let boundaryAfterReset =
            try await stateStorage.0.proposedTimezoneBoundary(
                deviceID: deviceID,
                timezone: timezone,
                now: resetNow
            )
        let pseudonymBoundary =
            await stateStorage.0.preparePseudonymBoundary(
                deviceID: deviceID,
                pseudonymKeyID: pseudonymKeyID,
                excludedAppTokens: [excludedToken],
                now: resetNow
            )
        let sequenceAfterReset =
            await stateStorage.0.allocateSnapshotSequence(
                deviceID: deviceID
            )

        XCTAssertGreaterThan(
            generationAfterReset,
            initialGeneration
        )
        XCTAssertEqual(
            boundaryAfterReset,
            date("2026-08-16T11:00:00Z")
        )
        XCTAssertTrue(
            pseudonymBoundary.requiresExclusionReapproval
        )
        XCTAssertEqual(sequenceAfterReset, 1)
    }

    func testRestartedPrivacyCleanupTargetsRememberedDeviceWithoutKeyLoad()
        async throws
    {
        let now = date("2026-08-16T12:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let deviceID = "ios-key-derived-device"
        let legacyDeviceID = "ios-legacy-fallback-device"
        _ = await stateStorage.0.allocateSnapshotSequence(
            deviceID: deviceID
        )
        _ = await stateStorage.0.allocateSnapshotSequence(
            deviceID: deviceID
        )
        _ = await stateStorage.0.allocateSnapshotSequence(
            deviceID: legacyDeviceID
        )
        _ = await stateStorage.0.allocateSnapshotSequence(
            deviceID: legacyDeviceID
        )

        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: stateStorage.1
        )
        intentStore.setOptedIn(true)
        intentStore.rememberActiveDeviceID(deviceID)
        stateStorage.1.set(
            legacyDeviceID,
            forKey: "healthmes.screen-time.fallback-device-id.v1"
        )
        intentStore.beginPrivacyCleanup()
        XCTAssertEqual(
            intentStore.privacyCleanupDeviceIDs,
            [deviceID, legacyDeviceID]
        )
        var keyLoadCount = 0
        let service = ScreenTimeActivitySyncService.live(
            transport: ScreenTimeLifecycleTestTransport(
                state: collectionState()
            ),
            stateStore: stateStorage.0,
            outbox: outboxStorage.0,
            pseudonymKeyLoader: {
                keyLoadCount += 1
                return Data(repeating: 0x11, count: 32)
            },
            authorizationIntentStore: intentStore
        )

        try await service.disableAndPurge(now: now)
        let sequenceAfterCleanup =
            await stateStorage.0.allocateSnapshotSequence(
                deviceID: deviceID
            )
        let legacySequenceAfterCleanup =
            await stateStorage.0.allocateSnapshotSequence(
                deviceID: legacyDeviceID
            )

        XCTAssertEqual(keyLoadCount, 0)
        XCTAssertEqual(sequenceAfterCleanup, 1)
        XCTAssertEqual(legacySequenceAfterCleanup, 1)
        XCTAssertEqual(intentStore.activeDeviceID, deviceID)
        XCTAssertEqual(
            intentStore.legacyFallbackDeviceID,
            legacyDeviceID
        )
    }

    func testServiceDisableCancelsPipelinePurgesOutboxAndResetsState()
        async throws
    {
        let now = date("2026-08-16T10:34:00Z")
        let outboxStorage = temporaryOutbox(now: now)
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        _ = try await outboxStorage.0.enqueue(
            report: report(),
            pairing: pairing(),
            now: now
        )
        let transport = ScreenTimeLifecycleTestTransport(
            state: collectionState(),
            stateDelayNanoseconds: 5_000_000_000
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let work = Task {
            try await service.sync(
                pairing: pairing(),
                now: now,
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        }
        try await waitForStateCalls(1, from: transport)

        try await service.disableAndPurge(
            now: now.addingTimeInterval(1)
        )

        do {
            _ = try await work.value
            XCTFail("expected opt-out to cancel the active pipeline")
        } catch is CancellationError {
            // Expected.
        }
        let entries = await outboxStorage.0.allEntries()
        let counts = await service.waiterCounts()

        XCTAssertTrue(entries.isEmpty)
        XCTAssertEqual(counts.active, 0)
        XCTAssertEqual(counts.pending, 0)
    }

    func testAuthorizationRestorationFenceReadsCentralRevisionAndIdentity()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "a", count: 40)
        let state = ScreenTimeCollectionState(
            deviceID: deviceID,
            enabled: true,
            excludedApps: [],
            pausedUntil: nil,
            effectiveCollecting: true,
            blockedReason: nil,
            configRevision: 17,
            rawRetentionCutoff: nil
        )
        let transport = ScreenTimeLifecycleTestTransport(state: state)
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport
        )

        let fence = try await service.authorizationRestorationFence(
            pairing: pairing(),
            now: date("2026-08-16T10:34:00Z")
        )
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()

        XCTAssertEqual(
            fence,
            authorizationRestorationFence(
                deviceID: deviceID,
                configRevision: 17
            )
        )
        XCTAssertEqual(stateCalls, 1)
        XCTAssertTrue(reports.isEmpty)
    }

    func testAuthorizationRestorationFenceRejectsMismatchedDevice()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "a", count: 40)
        let transport = ScreenTimeLifecycleTestTransport(
            state: ScreenTimeCollectionState(
                deviceID:
                    "ios-collector-v1-"
                    + String(repeating: "b", count: 40),
                enabled: true,
                excludedApps: [],
                pausedUntil: nil,
                effectiveCollecting: true,
                blockedReason: nil,
                configRevision: 17,
                rawRetentionCutoff: nil
            )
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport
        )

        do {
            _ = try await service.authorizationRestorationFence(
                pairing: pairing(),
                now: date("2026-08-16T10:34:00Z")
            )
            XCTFail("expected mismatched central device identity")
        } catch let error as ScreenTimeInputControlError {
            XCTAssertEqual(error, .invalidDescriptor)
        }
        let stateCalls = await transport.capturedStateCalls()
        let reports = await transport.capturedReports()
        XCTAssertEqual(stateCalls, 1)
        XCTAssertTrue(reports.isEmpty)
    }

    @MainActor
    func testAuthorizationSuccessImmediatelyUsesSyncPipeline()
        async
    {
        let batch = ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: []
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .uploaded(batch)
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result =
            await controller.requestAuthorizationAndSync(
                now: date("2026-08-16T10:34:00Z"),
                timezone: TimeZone(secondsFromGMT: 0)!
            )
        let calls = await service.calls()

        XCTAssertEqual(result.authorization, collectorResult())
        XCTAssertEqual(result.sync, .completed(.uploaded(batch)))
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.registration, 1)
        XCTAssertEqual(calls.sync, 1)
        XCTAssertEqual(calls.syncTriggers, [.authorizationChanged])
        XCTAssertTrue(calls.reconciledPairings.isEmpty)
    }

    @MainActor
    func testExplicitAuthorizationRegistersFirstSyncButLaterDisableWins()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "a", count: 40)
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: deviceID
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )
        let now = date("2026-08-16T10:34:00Z")
        let timezone = TimeZone(secondsFromGMT: 0)!

        let first = await controller.requestAuthorizationAndSync(
            now: now,
            timezone: timezone
        )

        guard case .completed(.uploaded) = first.sync else {
            return XCTFail(
                "expected authorization bootstrap to upload first sync"
            )
        }
        var transportCalls = await transport.captured()
        var collectorCalls = await collectorCounter.values()
        XCTAssertEqual(transportCalls.descriptor, 1)
        XCTAssertEqual(transportCalls.enable, 1)
        XCTAssertEqual(transportCalls.reports.count, 1)
        XCTAssertEqual(collectorCalls.authorization, 1)
        XCTAssertEqual(collectorCalls.collection, 1)

        await transport.pauseCentrally(
            until: now.addingTimeInterval(3_600)
        )
        let foreground = await controller.catchUp(
            now: now.addingTimeInterval(60),
            timezone: timezone
        )
        await transport.disableCentrally()
        let background = await controller.catchUp(
            now: now.addingTimeInterval(120),
            timezone: timezone,
            trigger: .backgroundRefresh
        )
        let authorizationObservation =
            await controller.authorizationDidChange(
                now: now.addingTimeInterval(180),
                timezone: timezone
            )

        XCTAssertEqual(
            foreground,
            .completed(.skipped(reason: "collection_paused"))
        )
        XCTAssertEqual(
            background,
            .completed(.skipped(reason: "collection_disabled"))
        )
        XCTAssertEqual(
            authorizationObservation,
            .completed(.skipped(reason: "collection_disabled"))
        )
        transportCalls = await transport.captured()
        collectorCalls = await collectorCounter.values()
        XCTAssertEqual(transportCalls.descriptor, 1)
        XCTAssertEqual(transportCalls.enable, 1)
        XCTAssertEqual(transportCalls.reports.count, 1)
        XCTAssertEqual(collectorCalls.authorization, 1)
        XCTAssertEqual(collectorCalls.collection, 1)
    }

    @MainActor
    func testAuthorizationCASConflictRereadsThenRegisters()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "d", count: 40)
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: deviceID,
            enableActions: [.conflict, .succeed]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result = await controller.requestAuthorizationAndSync(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let transportCalls = await transport.captured()

        guard case .completed(.uploaded) = result.sync else {
            return XCTFail("expected retry to register and upload")
        }
        XCTAssertEqual(transportCalls.descriptor, 2)
        XCTAssertEqual(transportCalls.enable, 2)
        XCTAssertEqual(transportCalls.reports.count, 1)
    }

    @MainActor
    func testAuthorizationCASConflictExhaustionFailsClosed()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "e", count: 40)
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: deviceID,
            enableActions: [.conflict, .conflict, .conflict]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result = await controller.requestAuthorizationAndSync()
        let transportCalls = await transport.captured()
        let collectorCalls = await collectorCounter.values()

        XCTAssertEqual(
            result.sync,
            .failed(reason: "input_settings_revision_conflict")
        )
        XCTAssertEqual(transportCalls.descriptor, 4)
        XCTAssertEqual(transportCalls.enable, 3)
        XCTAssertEqual(transportCalls.collectionState, 0)
        XCTAssertTrue(transportCalls.reports.isEmpty)
        XCTAssertEqual(collectorCalls.collection, 0)
    }

    @MainActor
    func testAuthorizationRejectsUnstableCollectorBeforeNetwork()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: "iphone-unstable"
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "iphone-unstable",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result = await controller.requestAuthorizationAndSync()
        let transportCalls = await transport.captured()
        let collectorCalls = await collectorCounter.values()

        XCTAssertEqual(
            result.sync,
            .failed(
                reason: "ios_screen_time_invalid_collector_identity"
            )
        )
        XCTAssertEqual(transportCalls.descriptor, 0)
        XCTAssertEqual(transportCalls.enable, 0)
        XCTAssertEqual(transportCalls.collectionState, 0)
        XCTAssertTrue(transportCalls.reports.isEmpty)
        XCTAssertEqual(collectorCalls.collection, 0)
    }

    @MainActor
    func testAuthorizationCASConflictKeepsConcurrentCentralDisable()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "b", count: 40)
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: deviceID,
            enableActions: [.conflictWithDisabledInstance]
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result = await controller.requestAuthorizationAndSync(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let transportCalls = await transport.captured()
        let collectorCalls = await collectorCounter.values()

        XCTAssertEqual(
            result.sync,
            .completed(.skipped(reason: "collection_disabled"))
        )
        XCTAssertEqual(transportCalls.descriptor, 2)
        XCTAssertEqual(transportCalls.enable, 1)
        XCTAssertTrue(transportCalls.reports.isEmpty)
        XCTAssertEqual(collectorCalls.authorization, 1)
        XCTAssertEqual(collectorCalls.collection, 0)
    }

    @MainActor
    func testPairingChangeDuringRegistrationPreventsStaleFirstSync()
        async
    {
        let service =
            ScreenTimeLifecycleBlockingRegistrationService()
        let firstPairing = pairing(
            token: "first",
            baseURL: "https://first.healthmes.test"
        )
        let secondPairing = pairing(
            token: "second",
            baseURL: "https://second.healthmes.test"
        )
        var currentPairing: Pairing? = firstPairing
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { currentPairing }
        )
        let authorizationTask = Task { @MainActor in
            await controller.requestAuthorizationAndSync()
        }
        await service.waitUntilRegistrationStarts()

        currentPairing = secondPairing
        await service.completeRegistration()
        let result = await authorizationTask.value
        let calls = await service.calls()

        XCTAssertEqual(
            result.sync,
            .failed(reason: "pairing_changed")
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.registration, 1)
        XCTAssertEqual(calls.registrationCancellations, 0)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertTrue(calls.syncPairings.isEmpty)
    }

    @MainActor
    func testAuthorizationRegistrationServerErrorFailsClosed()
        async throws
    {
        let deviceID =
            "ios-collector-v1-" + String(repeating: "c", count: 40)
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let collectorCounter = ScreenTimeLifecycleTestCounter()
        let transport = ScreenTimeLifecycleRegistrationTransport(
            deviceID: deviceID,
            descriptorError: .server(
                statusCode: 503,
                code: "input_control_unavailable",
                message: "input control unavailable",
                detail: nil
            )
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: deviceID,
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40),
                counter: collectorCounter
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result = await controller.requestAuthorizationAndSync()
        let transportCalls = await transport.captured()
        let collectorCalls = await collectorCounter.values()

        XCTAssertEqual(
            result.sync,
            .failed(reason: "input_control_unavailable")
        )
        XCTAssertEqual(transportCalls.descriptor, 1)
        XCTAssertEqual(transportCalls.enable, 0)
        XCTAssertEqual(transportCalls.collectionState, 0)
        XCTAssertTrue(transportCalls.reports.isEmpty)
        XCTAssertEqual(collectorCalls.authorization, 1)
        XCTAssertEqual(collectorCalls.collection, 0)
    }

    @MainActor
    func testLifecyclePassesBackgroundConfigurationAndAuthorizationTriggers()
        async
    {
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        _ = await controller.catchUp(trigger: .backgroundRefresh)
        _ = await controller.configurationDidChange()
        _ = await controller.authorizationDidChange()
        let calls = await service.calls()

        XCTAssertEqual(
            calls.syncTriggers,
            [
                .backgroundRefresh,
                .inputConfigurationChanged,
                .authorizationChanged,
            ]
        )
    }

    @MainActor
    func testUnpairedAuthorizationFailsClosedAndClearsOutbox()
        async
    {
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused")
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { nil }
        )

        let result =
            await controller.requestAuthorizationAndSync()
        let calls = await service.calls()

        XCTAssertEqual(
            result.sync,
            .failed(reason: "not_paired")
        )
        XCTAssertNil(result.authorization)
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertTrue(calls.reconciledPairings.isEmpty)
    }

    @MainActor
    func testAuthorizationFailureDoesNotStartSync() async {
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            authorizationError:
                ScreenTimeLifecycleTestError.authorizationFailed
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result =
            await controller.requestAuthorizationAndSync()
        let calls = await service.calls()

        XCTAssertNil(result.authorization)
        XCTAssertEqual(
            result.sync,
            .failed(
                reason: "ios_screen_time_authorization_failed"
            )
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
    }

    @MainActor
    func testEveryAuthorizationResultImmediatelyUsesSyncPipeline() async {
        let statuses: [
            (
                ScreenTimeActivityPermissionStatus,
                String
            )
        ] = [
            (.denied, "ios_screen_time_permission_denied"),
            (.restricted, "ios_screen_time_data_access_not_approved"),
            (.unavailable, "ios_screen_time_authorization_failed"),
            (.unknown, "ios_screen_time_permission_not_determined"),
        ]

        for (status, reason) in statuses {
            let authorization = collectorResult(
                capability: .unavailable,
                permission: status,
                reason: reason
            )
            let syncOutcome =
                ScreenTimeSyncOutcome.unavailableReported(
                    reason: reason
                )
            let service = ScreenTimeLifecycleMockSyncService(
                authorizationResult: authorization,
                syncResult: syncOutcome
            )
            let controller = ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            )

            let result =
                await controller.requestAuthorizationAndSync()
            let calls = await service.calls()

            XCTAssertEqual(result.authorization, authorization)
            XCTAssertEqual(result.sync, .completed(syncOutcome))
            XCTAssertEqual(calls.authorization, 1)
            XCTAssertEqual(calls.registration, 0)
            XCTAssertEqual(calls.sync, 1)
            XCTAssertEqual(
                calls.syncTriggers,
                [.authorizationChanged]
            )
        }
    }

    @MainActor
    func testExclusionApprovalUsesCallableSyncInterface() async {
        let batch = ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: []
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .uploaded(batch)
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )
        let exclusions: Set<String> = [
            "ios-app-v2-"
                + String(repeating: "1", count: 40)
                + "-"
                + String(repeating: "a", count: 40)
        ]

        let result =
            await controller.approveExcludedAppsAndSync(
                exclusions
            )
        let calls = await service.calls()

        XCTAssertEqual(result, .completed(.uploaded(batch)))
        XCTAssertEqual(calls.approvedExclusions, [exclusions])
        XCTAssertEqual(calls.sync, 1)
        XCTAssertEqual(
            calls.syncTriggers,
            [.inputConfigurationChanged]
        )
    }

    @MainActor
    func testExclusionApprovalFailureDoesNotStartSync() async {
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            exclusionApprovalError:
                ScreenTimeLifecycleTestError.exclusionApprovalFailed
        )
        let controller = ScreenTimeActivityLifecycleController(
            syncService: service,
            pairingProvider: { self.pairing() }
        )

        let result =
            await controller.approveExcludedAppsAndSync([])
        let calls = await service.calls()

        XCTAssertEqual(
            result,
            .failed(
                reason:
                    "ios_screen_time_exclusion_approval_failed"
            )
        )
        XCTAssertTrue(calls.approvedExclusions.isEmpty)
        XCTAssertEqual(calls.sync, 0)
    }

    @MainActor
    func testBackgroundRefreshRunnerExpirationCancelsAndCompletesOnce()
        async
    {
        let started = expectation(
            description: "background runner started"
        )
        let cancelled = expectation(
            description: "background runner cancelled"
        )
        let completed = expectation(
            description: "background task completed"
        )
        var completionValues: [Bool] = []
        let runner = ScreenTimeBackgroundRefreshRunner(
            operation: {
                started.fulfill()
                do {
                    try await Task.sleep(
                        nanoseconds: 5_000_000_000
                    )
                    return true
                } catch is CancellationError {
                    cancelled.fulfill()
                    return true
                } catch {
                    return false
                }
            },
            completion: { success in
                completionValues.append(success)
                completed.fulfill()
            }
        )
        runner.start()
        await fulfillment(of: [started], timeout: 2)

        runner.expire()
        runner.expire()

        await fulfillment(
            of: [cancelled, completed],
            timeout: 2
        )
        XCTAssertEqual(completionValues, [false])
    }

    @MainActor
    func testRuntimeRegisterRestoresOptInAndStartsColdLaunchCatchUp()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.runtime-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let observer = ScreenTimeLifecycleAuthorizationObserver()
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationObserver: observer,
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: NotificationCenter()
        )

        runtime.register()
        try await waitForLifecycleCalls(
            authorization: 0,
            sync: 1,
            from: service
        )
        var calls = await service.calls()
        XCTAssertEqual(backgroundTasks.registrations, 1)
        XCTAssertEqual(backgroundTasks.schedules, 1)
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.sync, 1)

        await observer.emitChange()
        try await waitForLifecycleCalls(
            authorization: 0,
            sync: 2,
            from: service
        )
        XCTAssertEqual(backgroundTasks.schedules, 2)

        _ = await runtime.foregroundCatchUp(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        calls = await service.calls()

        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.sync, 3)
        XCTAssertEqual(
            calls.syncTriggers,
            [
                .authorizationChanged,
                .authorizationChanged,
                .routine,
            ]
        )
        XCTAssertEqual(backgroundTasks.schedules, 3)
    }

    @MainActor
    func testRuntimeForegroundRestoresPersistedAuthorizationBeforeSync()
        async
    {
        let suiteName =
            "healthmes.screen-time.restore-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let notDetermined = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test"),
            currentAuthorizationResult: notDetermined
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        runtime.register()
        await runtime.waitForAutomaticWork()
        var calls = await service.calls()
        XCTAssertEqual(calls.currentAuthorization, 1)
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.restorationFence, 1)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)

        let foreground = await runtime.foregroundCatchUp(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        calls = await service.calls()

        XCTAssertEqual(
            foreground,
            .completed(.skipped(reason: "test"))
        )
        XCTAssertEqual(calls.currentAuthorization, 2)
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.restorationFence, 3)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 1)
        XCTAssertEqual(calls.syncTriggers, [.authorizationChanged])
    }

    @MainActor
    func testRuntimeColdLaunchAndBackgroundNeverPromptWhenRestorationNeeded()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.noninteractive-tests."
            + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let notDetermined = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            currentAuthorizationResult: notDetermined
        )
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: NotificationCenter()
        )

        runtime.register()
        await runtime.waitForAutomaticWork()
        let backgroundTask = backgroundTasks.launch()
        try await waitForBackgroundCompletion(backgroundTask)
        let calls = await service.calls()

        XCTAssertEqual(backgroundTask.completionValues, [true])
        XCTAssertEqual(calls.currentAuthorization, 2)
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.restorationFence, 2)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
    }

    @MainActor
    func testRuntimeForegroundDefersWhenAuthorizationCannotBeRestored()
        async
    {
        for permission:
            ScreenTimeActivityPermissionStatus
            in [.unknown, .revoked, .denied, .restricted]
        {
            let suiteName =
                "healthmes.screen-time.reauth-tests."
                + permission.rawValue + "." + UUID().uuidString
            let defaults = UserDefaults(suiteName: suiteName)!
            defer {
                defaults.removePersistentDomain(forName: suiteName)
            }
            let intentStore = ScreenTimeAuthorizationIntentStore(
                defaults: defaults
            )
            intentStore.setOptedIn(true)
            let unresolved = collectorResult(
                capability: .unavailable,
                permission: permission,
                reason: "authorization_not_restored"
            )
            let service = ScreenTimeLifecycleMockSyncService(
                authorizationResult: unresolved,
                syncResult: .skipped(reason: "unused"),
                currentAuthorizationResult: unresolved
            )
            let runtime = ScreenTimeActivityRuntime(
                lifecycle: ScreenTimeActivityLifecycleController(
                    syncService: service,
                    pairingProvider: { self.pairing() }
                ),
                authorizationIntentStore: intentStore,
                backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
                notificationCenter: NotificationCenter()
            )

            let result = await runtime.foregroundCatchUp()
            let calls = await service.calls()

            XCTAssertEqual(
                result,
                .skipped(
                    reason:
                        "ios_screen_time_reauthorization_required"
                ),
                "permission=\(permission.rawValue)"
            )
            XCTAssertEqual(calls.authorization, 1)
            XCTAssertEqual(calls.restorationFence, 1)
            XCTAssertEqual(calls.registration, 0)
            XCTAssertEqual(calls.sync, 0)
        }
    }

    @MainActor
    func testRuntimeRestorationRespectsCentralDisableAndPause()
        async
    {
        for reason in ["collection_disabled", "collection_paused"] {
            let suiteName =
                "healthmes.screen-time.central-block-tests."
                + reason + "." + UUID().uuidString
            let defaults = UserDefaults(suiteName: suiteName)!
            defer {
                defaults.removePersistentDomain(forName: suiteName)
            }
            let intentStore = ScreenTimeAuthorizationIntentStore(
                defaults: defaults
            )
            intentStore.setOptedIn(true)
            let unknown = collectorResult(
                capability: .unavailable,
                permission: .unknown,
                reason: "ios_screen_time_permission_not_determined"
            )
            let service = ScreenTimeLifecycleMockSyncService(
                authorizationResult: collectorResult(),
                syncResult: .skipped(reason: "unused"),
                currentAuthorizationResult: unknown,
                restorationFences: [
                    authorizationRestorationFence(
                        blockedReason: reason
                    )
                ]
            )
            let runtime = ScreenTimeActivityRuntime(
                lifecycle: ScreenTimeActivityLifecycleController(
                    syncService: service,
                    pairingProvider: { self.pairing() }
                ),
                authorizationIntentStore: intentStore,
                backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
                notificationCenter: NotificationCenter()
            )

            let result = await runtime.foregroundCatchUp()
            let calls = await service.calls()

            XCTAssertEqual(
                result,
                .skipped(reason: reason)
            )
            XCTAssertEqual(calls.authorization, 0)
            XCTAssertEqual(calls.restorationFence, 1)
            XCTAssertEqual(calls.registration, 0)
            XCTAssertEqual(calls.sync, 0)
        }
    }

    @MainActor
    func testRuntimeRestorationRejectsCentralRevisionChange()
        async
    {
        let suiteName =
            "healthmes.screen-time.revision-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            currentAuthorizationResult: unknown,
            restorationFences: [
                authorizationRestorationFence(configRevision: 3),
                authorizationRestorationFence(configRevision: 4),
            ]
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        let result = await runtime.foregroundCatchUp()
        let calls = await service.calls()

        XCTAssertEqual(
            result,
            .skipped(
                reason:
                    "ios_screen_time_collection_configuration_changed"
            )
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.restorationFence, 2)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
    }

    @MainActor
    func testRuntimeRestorationStopsWhenCentralStateDisablesAfterGrant()
        async
    {
        let suiteName =
            "healthmes.screen-time.disable-race-tests."
            + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            currentAuthorizationResult: unknown,
            restorationFences: [
                authorizationRestorationFence(configRevision: 3),
                authorizationRestorationFence(
                    configRevision: 4,
                    blockedReason: "collection_disabled"
                ),
            ]
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        let result = await runtime.foregroundCatchUp()
        let calls = await service.calls()

        XCTAssertEqual(
            result,
            .skipped(reason: "collection_disabled")
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.restorationFence, 2)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
    }

    @MainActor
    func testRuntimeRestorationRechecksPairingAndRememberedIdentity()
        async throws
    {
        let firstPairing = pairing(
            token: "first",
            baseURL: "https://first.healthmes.test"
        )
        let secondPairing = pairing(
            token: "second",
            baseURL: "https://second.healthmes.test"
        )
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )

        do {
            let suiteName =
                "healthmes.screen-time.identity-fence-tests."
                + UUID().uuidString
            let defaults = UserDefaults(suiteName: suiteName)!
            defer {
                defaults.removePersistentDomain(forName: suiteName)
            }
            let intentStore = ScreenTimeAuthorizationIntentStore(
                defaults: defaults
            )
            intentStore.setOptedIn(true)
            intentStore.rememberActiveDeviceID(
                "ios-collector-v1-" + String(repeating: "2", count: 40)
            )
            let service = ScreenTimeLifecycleMockSyncService(
                authorizationResult: collectorResult(),
                syncResult: .skipped(reason: "unused"),
                currentAuthorizationResult: unknown
            )
            let runtime = ScreenTimeActivityRuntime(
                lifecycle: ScreenTimeActivityLifecycleController(
                    syncService: service,
                    pairingProvider: { firstPairing }
                ),
                authorizationIntentStore: intentStore,
                backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
                notificationCenter: NotificationCenter()
            )

            let result = await runtime.foregroundCatchUp()
            let calls = await service.calls()

            XCTAssertEqual(
                result,
                .failed(
                    reason:
                        "ios_screen_time_invalid_collector_identity"
                )
            )
            XCTAssertEqual(calls.authorization, 0)
            XCTAssertEqual(calls.restorationFence, 1)
            XCTAssertEqual(calls.sync, 0)
        }

        do {
            let suiteName =
                "healthmes.screen-time.pairing-fence-tests."
                + UUID().uuidString
            let defaults = UserDefaults(suiteName: suiteName)!
            defer {
                defaults.removePersistentDomain(forName: suiteName)
            }
            let intentStore = ScreenTimeAuthorizationIntentStore(
                defaults: defaults
            )
            intentStore.setOptedIn(true)
            var currentPairing: Pairing? = firstPairing
            let service = ScreenTimeLifecycleBlockingAuthorizationService(
                authorizationResult: collectorResult(),
                syncResult: .skipped(reason: "unused"),
                currentAuthorizationResult: unknown,
                restorationFences: [
                    authorizationRestorationFence(
                        pairing: firstPairing
                    )
                ]
            )
            let runtime = ScreenTimeActivityRuntime(
                lifecycle: ScreenTimeActivityLifecycleController(
                    syncService: service,
                    pairingProvider: { currentPairing }
                ),
                authorizationIntentStore: intentStore,
                backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
                notificationCenter: NotificationCenter()
            )
            let restoration = Task { @MainActor in
                await runtime.foregroundCatchUp()
            }
            try await waitForAuthorizationCalls(1, from: service)

            currentPairing = secondPairing
            await service.releaseAuthorizationCalls()
            let result = await restoration.value
            let calls = await service.calls()

            XCTAssertEqual(
                result,
                .failed(reason: "pairing_changed")
            )
            XCTAssertEqual(calls.authorization, 1)
            XCTAssertEqual(calls.restorationFence, 1)
            XCTAssertEqual(calls.sync, 0)
        }
    }

    @MainActor
    func testRuntimeConcurrentForegroundRestorationIsSingleFlight()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.single-flight-tests."
            + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleBlockingAuthorizationService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test"),
            currentAuthorizationResult: unknown
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        let first = Task { @MainActor in
            await runtime.foregroundCatchUp()
        }
        let second = Task { @MainActor in
            await runtime.foregroundCatchUp()
        }
        try await waitForAuthorizationCalls(1, from: service)
        let callsWhileBlocked = await service.calls()
        XCTAssertEqual(callsWhileBlocked.authorization, 1)

        await service.releaseAuthorizationCalls()
        let results = await [first.value, second.value]
        let calls = await service.calls()

        XCTAssertEqual(
            results,
            [
                .completed(.skipped(reason: "test")),
                .completed(.skipped(reason: "test")),
            ]
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.cancellations, 0)
        XCTAssertEqual(calls.restorationFence, 4)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 2)
    }

    @MainActor
    func testRuntimeForegroundRestorationCancellationKeepsSharedWork()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.restore-cancel-tests."
            + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleBlockingAuthorizationService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test"),
            currentAuthorizationResult: unknown
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        let cancelled = Task { @MainActor in
            await runtime.foregroundCatchUp()
        }
        let survivor = Task { @MainActor in
            await runtime.foregroundCatchUp()
        }
        try await waitForAuthorizationCalls(1, from: service)
        cancelled.cancel()
        let cancelledResult = await cancelled.value

        XCTAssertEqual(
            cancelledResult,
            .failed(reason: "cancelled")
        )
        let callsAfterCancellation = await service.calls()
        XCTAssertEqual(callsAfterCancellation.cancellations, 0)

        await service.releaseAuthorizationCalls()
        let survivorResult = await survivor.value
        let calls = await service.calls()

        XCTAssertEqual(
            survivorResult,
            .completed(.skipped(reason: "test"))
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.cancellations, 0)
        XCTAssertEqual(calls.sync, 1)
    }

    @MainActor
    func testRuntimeOptOutCancelsForegroundAuthorizationRestoration()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.restore-optout-tests."
            + UUID().uuidString
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let unknown = collectorResult(
            capability: .unavailable,
            permission: .unknown,
            reason: "ios_screen_time_permission_not_determined"
        )
        let service = ScreenTimeLifecycleBlockingAuthorizationService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused"),
            currentAuthorizationResult: unknown
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )
        let restoration = Task { @MainActor in
            await runtime.foregroundCatchUp()
        }
        try await waitForAuthorizationCalls(1, from: service)

        let disabled = await runtime.clearAuthorizationOptIn()
        let restorationResult = await restoration.value
        let calls = await service.calls()

        XCTAssertEqual(
            disabled,
            .skipped(reason: "ios_screen_time_disabled")
        )
        XCTAssertEqual(
            restorationResult,
            .skipped(reason: "ios_screen_time_not_opted_in")
        )
        XCTAssertFalse(intentStore.isOptedIn)
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.cancellations, 1)
        XCTAssertEqual(calls.registration, 0)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertEqual(calls.disable, 1)
    }

    @MainActor
    func testRuntimeTimezoneAndPairingObserversUseCurrentConfiguration()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.observer-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        intentStore.setOptedIn(true)
        let notificationCenter = NotificationCenter()
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        var timezone = TimeZone(secondsFromGMT: 0)!
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationObserver:
                ScreenTimeLifecycleAuthorizationObserver(),
            authorizationIntentStore: intentStore,
            backgroundTasks:
                ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: notificationCenter,
            nowProvider: {
                self.date("2026-08-16T10:34:00Z")
            },
            timezoneProvider: { timezone }
        )
        runtime.register()
        try await waitForLifecycleCalls(
            authorization: 0,
            sync: 1,
            from: service
        )

        timezone = TimeZone(secondsFromGMT: 9 * 60 * 60)!
        notificationCenter.post(
            name: Notification.Name.NSSystemTimeZoneDidChange,
            object: nil
        )
        try await waitForLifecycleCalls(
            authorization: 0,
            sync: 2,
            from: service
        )

        notificationCenter.post(
            name: Notification.Name("healthmes.pairing.changed"),
            object: nil
        )
        try await waitForLifecycleCalls(
            authorization: 0,
            sync: 3,
            from: service
        )
        await runtime.waitForAutomaticWork()
        let calls = await service.calls()

        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(
            calls.syncTriggers,
            [
                .authorizationChanged,
                .inputConfigurationChanged,
                .inputConfigurationChanged,
            ]
        )
        XCTAssertEqual(
            calls.syncTimezones,
            [
                TimeZone(secondsFromGMT: 0)!.identifier,
                timezone.identifier,
                timezone.identifier,
            ]
        )
    }

    @MainActor
    func testRuntimeOptOutGatesEveryAutomaticCollectionEntryPoint()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.opt-out-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let observer = ScreenTimeLifecycleAuthorizationObserver()
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let notificationCenter = NotificationCenter()
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "unused")
        )
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationObserver: observer,
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: notificationCenter
        )

        runtime.register()
        let foreground = await runtime.foregroundCatchUp()
        await observer.emitChange()
        let configuration =
            await runtime.inputConfigurationDidChange()
        let exclusions =
            await runtime.approveExcludedAppsAndSync([])
        let backgroundTask = backgroundTasks.launch()
        let calls = await service.calls()

        XCTAssertEqual(
            foreground,
            .skipped(reason: "ios_screen_time_not_opted_in")
        )
        XCTAssertEqual(
            configuration,
            .skipped(reason: "ios_screen_time_not_opted_in")
        )
        XCTAssertEqual(
            exclusions,
            .skipped(reason: "ios_screen_time_not_opted_in")
        )
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertTrue(calls.approvedExclusions.isEmpty)
        XCTAssertEqual(backgroundTasks.registrations, 1)
        XCTAssertEqual(backgroundTasks.schedules, 0)
        XCTAssertEqual(backgroundTask.completionValues, [true])
    }

    @MainActor
    func testRuntimeBackgroundExpirationCancelsOptedInPipeline()
        async throws
    {
        let outboxStorage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(
                at: outboxStorage.1
            )
        }
        let stateStorage = isolatedStateStore()
        defer {
            stateStorage.1.removePersistentDomain(
                forName: stateStorage.2
            )
        }
        let suiteName =
            "healthmes.screen-time.runtime-bg-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let transport = ScreenTimeLifecycleBlockingTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-lifecycle-device",
            collector: ScreenTimeLifecycleTestCollector(
                result: collectorResult(),
                pseudonymKeyID:
                    "ios-key-" + String(repeating: "1", count: 40)
            ),
            transport: transport,
            stateStore: stateStorage.0,
            outbox: outboxStorage.0
        )
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationObserver:
                ScreenTimeLifecycleAuthorizationObserver(),
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: NotificationCenter()
        )
        runtime.register()
        intentStore.setOptedIn(true)

        let task = backgroundTasks.launch()
        try await waitForStateCalls(1, from: transport)
        task.expire()
        try await waitForStateCancellations(1, from: transport)
        XCTAssertTrue(task.completionValues.isEmpty)
        await transport.releaseStateCalls()
        try await waitForBackgroundCompletion(task)

        XCTAssertEqual(task.completionValues, [false])
        XCTAssertEqual(backgroundTasks.registrations, 1)
        XCTAssertEqual(backgroundTasks.schedules, 1)
    }

    @MainActor
    func testRuntimeBackgroundAndForegroundNeverRequestAuthorization()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.auth-bg-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let service = ScreenTimeLifecycleBlockingAuthorizationService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationObserver:
                ScreenTimeLifecycleAuthorizationObserver(),
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: NotificationCenter()
        )
        runtime.register()
        intentStore.setOptedIn(true)

        let backgroundTask = backgroundTasks.launch()
        try await waitForBackgroundCompletion(backgroundTask)

        let foregroundResult = await runtime.foregroundCatchUp(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let calls = await service.calls()

        XCTAssertEqual(backgroundTask.completionValues, [true])
        XCTAssertEqual(
            foregroundResult,
            .completed(.skipped(reason: "test"))
        )
        XCTAssertEqual(calls.authorization, 0)
        XCTAssertEqual(calls.cancellations, 0)
        XCTAssertEqual(calls.sync, 2)
    }

    @MainActor
    func testExplicitAuthorizationPersistsOptInUntilDeviceTeamClearsIt()
        async
    {
        let suiteName =
            "healthmes.screen-time.intent-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        let backgroundTasks = ScreenTimeLifecycleBackgroundTasks()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: backgroundTasks,
            notificationCenter: NotificationCenter()
        )

        XCTAssertFalse(intentStore.isOptedIn)
        _ = await runtime.requestAuthorizationAndSync()
        XCTAssertTrue(intentStore.isOptedIn)
        XCTAssertFalse(intentStore.isPrivacyCleanupPending)
        intentStore.rememberActiveDeviceID("ios-lifecycle-device")
        defaults.set(
            "ios-legacy-fallback-device",
            forKey: "healthmes.screen-time.fallback-device-id.v1"
        )
        XCTAssertEqual(
            intentStore.activeDeviceID,
            "ios-lifecycle-device"
        )

        let disabled = await runtime.clearAuthorizationOptIn(
            now: date("2026-08-16T10:34:00Z")
        )
        XCTAssertFalse(intentStore.isOptedIn)
        XCTAssertFalse(intentStore.isPrivacyCleanupPending)
        XCTAssertNil(intentStore.activeDeviceID)
        XCTAssertNil(intentStore.legacyFallbackDeviceID)
        let foreground = await runtime.foregroundCatchUp(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        XCTAssertEqual(
            foreground,
            .skipped(reason: "ios_screen_time_not_opted_in")
        )
        XCTAssertEqual(backgroundTasks.cancellations, 1)
        XCTAssertEqual(
            disabled,
            .skipped(reason: "ios_screen_time_disabled")
        )
        let calls = await service.calls()
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.disable, 1)
    }

    @MainActor
    func testOptOutCancelsInFlightAuthorizationBootstrapBeforeSync()
        async
    {
        let suiteName =
            "healthmes.screen-time.bootstrap-race.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let service =
            ScreenTimeLifecycleBlockingRegistrationService()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )
        let authorizationTask = Task { @MainActor in
            await runtime.requestAuthorizationAndSync()
        }
        await service.waitUntilRegistrationStarts()

        let disabled = await runtime.clearAuthorizationOptIn()
        let authorization = await authorizationTask.value
        let calls = await service.calls()

        XCTAssertEqual(
            authorization.sync,
            .failed(reason: "cancelled")
        )
        XCTAssertEqual(
            disabled,
            .skipped(reason: "ios_screen_time_disabled")
        )
        XCTAssertFalse(intentStore.isOptedIn)
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.registration, 1)
        XCTAssertEqual(calls.registrationCancellations, 1)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertEqual(calls.disable, 1)
    }

    @MainActor
    func testPairingNotificationCancelsInFlightAuthorizationBootstrap()
        async throws
    {
        let suiteName =
            "healthmes.screen-time.pairing-race.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let service =
            ScreenTimeLifecycleBlockingRegistrationService()
        let firstPairing = pairing(
            token: "first",
            baseURL: "https://first.healthmes.test"
        )
        let secondPairing = pairing(
            token: "second",
            baseURL: "https://second.healthmes.test"
        )
        var currentPairing: Pairing? = firstPairing
        let notificationCenter = NotificationCenter()
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { currentPairing }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: notificationCenter
        )
        runtime.register()
        let authorizationTask = Task { @MainActor in
            await runtime.requestAuthorizationAndSync()
        }
        await service.waitUntilRegistrationStarts()

        currentPairing = secondPairing
        notificationCenter.post(
            name: Notification.Name("healthmes.pairing.changed"),
            object: nil
        )
        let authorization = await authorizationTask.value
        for _ in 0..<400 {
            let calls = await service.calls()
            if calls.sync >= 1 {
                break
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        await runtime.waitForAutomaticWork()
        let calls = await service.calls()

        XCTAssertEqual(
            authorization.sync,
            .failed(reason: "cancelled")
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.registration, 1)
        XCTAssertEqual(calls.registrationCancellations, 1)
        XCTAssertEqual(calls.sync, 1)
        XCTAssertEqual(calls.syncPairings, [secondPairing])
    }

    @MainActor
    func testIdentityCleanupFailureBlocksReuseUntilCleanupSucceeds()
        async
    {
        let suiteName =
            "healthmes.screen-time.cleanup-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: defaults
        )
        let service = ScreenTimeLifecycleMockSyncService(
            authorizationResult: collectorResult(),
            syncResult: .skipped(reason: "test")
        )
        var shouldFailCleanup = true
        var cleanupAttempts = 0
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter(),
            identityReset: {
                cleanupAttempts += 1
                if shouldFailCleanup {
                    throw ScreenTimeLifecycleTestError
                        .identityCleanupFailed
                }
            }
        )

        _ = await runtime.requestAuthorizationAndSync()
        intentStore.rememberActiveDeviceID("ios-lifecycle-device")
        let firstOptOut = await runtime.clearAuthorizationOptIn()
        let blockedOptIn = await runtime.requestAuthorizationAndSync()
        var calls = await service.calls()

        XCTAssertEqual(
            firstOptOut,
            .failed(
                reason: "ios_screen_time_identity_cleanup_failed"
            )
        )
        XCTAssertEqual(
            blockedOptIn.sync,
            .failed(
                reason: "ios_screen_time_identity_cleanup_failed"
            )
        )
        XCTAssertNil(blockedOptIn.authorization)
        XCTAssertFalse(intentStore.isOptedIn)
        XCTAssertTrue(intentStore.isPrivacyCleanupPending)
        XCTAssertEqual(
            intentStore.activeDeviceID,
            "ios-lifecycle-device"
        )
        XCTAssertEqual(cleanupAttempts, 2)
        XCTAssertEqual(calls.authorization, 1)

        shouldFailCleanup = false
        let resumed = await runtime.requestAuthorizationAndSync()
        calls = await service.calls()

        XCTAssertEqual(resumed.authorization, collectorResult())
        XCTAssertTrue(intentStore.isOptedIn)
        XCTAssertFalse(intentStore.isPrivacyCleanupPending)
        XCTAssertNil(intentStore.activeDeviceID)
        XCTAssertEqual(cleanupAttempts, 3)
        XCTAssertEqual(calls.authorization, 2)
        XCTAssertEqual(calls.disable, 3)
    }

    @MainActor
    func testBuildCollectorFailClosedReasonMatchesCapability()
        async throws
    {
        #if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
        throw XCTSkip(
            "The normal-build fail-closed assertion is not for the "
                + "SDK-capable Screen Time opt-in configuration."
        )
        #else
        let collector = ScreenTimeActivityCollectorFactory.make(
            pseudonymKeyData: Data(repeating: 0x11, count: 32)
        )
        let result = try await collector.requestAuthorization()
        #if HEALTHMES_SCREENTIME_OPT_IN_REQUESTED
        let expectedReason =
            "ios_screen_time_export_sdk_unavailable"
        #else
        let expectedReason =
            "ios_screen_time_normal_build_unavailable"
        #endif

        XCTAssertEqual(result.capability, .unavailable)
        XCTAssertEqual(result.permissionStatus, .unavailable)
        XCTAssertEqual(result.reason, expectedReason)
        XCTAssertTrue(result.samples.isEmpty)
        #endif
    }

    func testAuthorizationFailurePolicyPreservesObservableDenial() {
        let denied = collectorResult(
            capability: .unavailable,
            permission: .denied,
            reason: "ios_screen_time_permission_denied"
        )

        XCTAssertEqual(
            ScreenTimeAuthorizationFailurePolicy.reportableResult(
                current: denied
            ),
            denied
        )
    }

    func testAuthorizationFailurePolicyFailsClosedForAmbiguousStatus() {
        for status in [
            ScreenTimeActivityPermissionStatus.granted,
            .unknown,
        ] {
            let result =
                ScreenTimeAuthorizationFailurePolicy.reportableResult(
                    current: collectorResult(
                        capability: status == .granted
                            ? .aggregate
                            : .unavailable,
                        permission: status
                    )
                )

            XCTAssertEqual(result.capability, .unavailable)
            XCTAssertEqual(result.permissionStatus, .unavailable)
            XCTAssertEqual(
                result.reason,
                "ios_screen_time_authorization_failed"
            )
            XCTAssertTrue(result.samples.isEmpty)
        }
    }

    func testCollectionFailurePolicySeparatesAuthorizationAndExport()
        throws
    {
        let unauthorized =
            try ScreenTimeActivityCollectionFailurePolicy.result(
                for: .unauthorized
            )
        let unavailable =
            try ScreenTimeActivityCollectionFailurePolicy.result(
                for: .unavailable
            )

        XCTAssertEqual(unauthorized.permissionStatus, .revoked)
        XCTAssertEqual(
            unauthorized.reason,
            "ios_screen_time_permission_revoked"
        )
        XCTAssertEqual(unavailable.permissionStatus, .unavailable)
        XCTAssertEqual(
            unavailable.reason,
            "ios_screen_time_activity_data_unavailable"
        )
        XCTAssertThrowsError(
            try ScreenTimeActivityCollectionFailurePolicy.result(
                for: .transient
            )
        ) { error in
            XCTAssertEqual(
                error as? ScreenTimeActivityCollectionError,
                .exportFailed
            )
        }
        let revoked = collectorResult(
            capability: .unavailable,
            permission: .revoked,
            reason: "ios_screen_time_permission_revoked"
        )
        XCTAssertEqual(
            ScreenTimeActivityCollectionFailurePolicy
                .authorizationResultAfterUnexpectedFailure(
                    current: revoked
                ),
            revoked
        )
        XCTAssertNil(
            ScreenTimeActivityCollectionFailurePolicy
                .authorizationResultAfterUnexpectedFailure(
                    current: collectorResult()
                )
        )
    }
}
