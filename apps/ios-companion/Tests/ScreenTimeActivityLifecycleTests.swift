import BackgroundTasks
import Foundation
import XCTest

private enum ScreenTimeLifecycleTestError: Error {
    case authorizationFailed
    case collectionFailed
    case exclusionApprovalFailed
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

private enum ScreenTimeLifecycleUploadAction {
    case succeed
    case fail(HealthMesAPIError)
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
    private let state: ScreenTimeCollectionState
    private let stateDelayNanoseconds: UInt64
    private var uploadActions: [ScreenTimeLifecycleUploadAction]
    private var stateCalls = 0
    private var reports: [ScreenTimeActivityReport] = []
    private var statePairings: [Pairing] = []
    private var reportPairings: [Pairing] = []

    init(
        state: ScreenTimeCollectionState,
        uploadActions: [ScreenTimeLifecycleUploadAction] = [],
        stateDelayNanoseconds: UInt64 = 0
    ) {
        self.state = state
        self.uploadActions = uploadActions
        self.stateDelayNanoseconds = stateDelayNanoseconds
    }

    func collectionState(
        pairing: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
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
        if !uploadActions.isEmpty {
            switch uploadActions.removeFirst() {
            case .succeed:
                break
            case .fail(let error):
                throw error
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

    init(state: ScreenTimeCollectionState) {
        self.state = state
    }

    func collectionState(
        pairing _: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        stateCalls += 1
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

private actor ScreenTimeLifecycleMockSyncService:
    ScreenTimeActivitySyncing
{
    private let authorizationResult: ScreenTimeCollectorResult
    private let syncResult: ScreenTimeSyncOutcome
    private let authorizationError: Error?
    private let exclusionApprovalError: Error?
    private var authorizationCalls = 0
    private var approvedExclusions: [Set<String>] = []
    private var syncCalls = 0
    private var syncTriggers: [ScreenTimeSyncTrigger] = []
    private var reconciledPairings: [Pairing?] = []

    init(
        authorizationResult: ScreenTimeCollectorResult,
        syncResult: ScreenTimeSyncOutcome,
        authorizationError: Error? = nil,
        exclusionApprovalError: Error? = nil
    ) {
        self.authorizationResult = authorizationResult
        self.syncResult = syncResult
        self.authorizationError = authorizationError
        self.exclusionApprovalError = exclusionApprovalError
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
        timezone _: TimeZone,
        trigger: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeSyncOutcome {
        syncCalls += 1
        syncTriggers.append(trigger)
        return syncResult
    }

    func reconcilePendingUploads(
        pairing: Pairing?
    ) async throws {
        reconciledPairings.append(pairing)
    }

    func calls() -> (
        authorization: Int,
        approvedExclusions: [Set<String>],
        sync: Int,
        syncTriggers: [ScreenTimeSyncTrigger],
        reconciledPairings: [Pairing?]
    ) {
        (
            authorizationCalls,
            approvedExclusions,
            syncCalls,
            syncTriggers,
            reconciledPairings
        )
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
        let storage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        let now = date("2026-08-16T10:05:00Z")
        let firstPairing = pairing(token: "first")
        let secondPairing = pairing(token: "second")
        _ = try await storage.0.enqueue(
            report: report(),
            pairing: firstPairing,
            now: now
        )
        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: secondPairing
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
            state: collectionState(configRevision: 3)
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
            )
        )
        let entriesAfterRetentionChange =
            await storage.0.allEntries()
        XCTAssertTrue(entriesAfterRetentionChange.isEmpty)
    }

    func testOutboxReconcilePurgesObsoleteDeviceIdentity()
        async throws
    {
        let storage = temporaryOutbox()
        defer {
            try? FileManager.default.removeItem(at: storage.1)
        }
        _ = try await storage.0.enqueue(
            report: report(deviceID: "obsolete-device"),
            pairing: pairing(),
            now: date("2026-08-16T10:05:00Z")
        )

        try await storage.0.reconcile(
            deviceID: "ios-lifecycle-device",
            pairing: pairing()
        )
        let entries = await storage.0.allEntries()

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
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(pendingCount, 0)
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
        XCTAssertEqual(stateCalls, 1)
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

        XCTAssertEqual(stateCalls, 2)
        XCTAssertEqual(counts.collection, 2)
        XCTAssertEqual(reports.count, 2)
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
        try await Task.sleep(nanoseconds: 20_000_000)

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
        XCTAssertEqual(stateCalls, 1)
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
        try await Task.sleep(nanoseconds: 20_000_000)

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
        XCTAssertEqual(stateCalls, 1)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
    }

    func testBackgroundExpirationCancelsServiceOwnedPipeline()
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
                now: date("2026-08-16T10:34:00Z"),
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

        XCTAssertTrue(entries.isEmpty)
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
        XCTAssertEqual(stateCalls, 2)
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
        try await Task.sleep(nanoseconds: 20_000_000)

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
        XCTAssertEqual(stateCalls, 1)
        XCTAssertEqual(counts.collection, 1)
        XCTAssertEqual(reports.count, 1)
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
        try await Task.sleep(nanoseconds: 20_000_000)

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
        ])
        XCTAssertEqual(pairings.report, [secondPairing])
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(counts.collection, 1)
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
        XCTAssertEqual(calls.sync, 1)
        XCTAssertEqual(calls.syncTriggers, [.authorizationChanged])
        XCTAssertTrue(calls.reconciledPairings.isEmpty)
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
            .skipped(reason: "not_paired")
        )
        XCTAssertEqual(calls.authorization, 1)
        XCTAssertEqual(calls.sync, 0)
        XCTAssertEqual(calls.reconciledPairings.count, 1)
        XCTAssertNil(calls.reconciledPairings[0])
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
        await Task.yield()
        XCTAssertEqual(completionValues, [false])
    }

    @MainActor
    func testRuntimeRegisterRestoresOptInAndWiresAuthorizationChanges()
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
            authorization: 1,
            sync: 1,
            from: service
        )
        XCTAssertEqual(backgroundTasks.registrations, 1)
        XCTAssertEqual(backgroundTasks.schedules, 1)

        await observer.emitChange()
        try await waitForLifecycleCalls(
            authorization: 1,
            sync: 2,
            from: service
        )
        XCTAssertEqual(backgroundTasks.schedules, 2)

        _ = await runtime.foregroundCatchUp(
            now: date("2026-08-16T10:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let calls = await service.calls()

        XCTAssertEqual(calls.authorization, 2)
        XCTAssertEqual(calls.sync, 3)
        XCTAssertEqual(
            calls.syncTriggers,
            [
                .authorizationChanged,
                .authorizationChanged,
                .authorizationChanged,
            ]
        )
        XCTAssertEqual(backgroundTasks.schedules, 3)
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
        try await waitForBackgroundCompletion(task)
        await transport.releaseStateCalls()

        XCTAssertEqual(task.completionValues, [false])
        XCTAssertEqual(backgroundTasks.registrations, 1)
        XCTAssertEqual(backgroundTasks.schedules, 1)
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
        let runtime = ScreenTimeActivityRuntime(
            lifecycle: ScreenTimeActivityLifecycleController(
                syncService: service,
                pairingProvider: { self.pairing() }
            ),
            authorizationIntentStore: intentStore,
            backgroundTasks: ScreenTimeLifecycleBackgroundTasks(),
            notificationCenter: NotificationCenter()
        )

        XCTAssertFalse(intentStore.isOptedIn)
        _ = await runtime.requestAuthorizationAndSync()
        XCTAssertTrue(intentStore.isOptedIn)

        runtime.clearAuthorizationOptIn()
        XCTAssertFalse(intentStore.isOptedIn)
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
    }
}
