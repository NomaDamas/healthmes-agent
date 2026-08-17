import Foundation

struct ScreenTimeCollectorResult: Equatable {
    let capability: ScreenTimeActivityCapability
    let permissionStatus: ScreenTimeActivityPermissionStatus
    let reason: String?
    let samples: [ScreenTimeActivitySample]
    let authoritativeBucketStarts: Set<Date>

    init(
        capability: ScreenTimeActivityCapability,
        permissionStatus: ScreenTimeActivityPermissionStatus,
        reason: String?,
        samples: [ScreenTimeActivitySample],
        authoritativeBucketStarts: Set<Date> = []
    ) {
        self.capability = capability
        self.permissionStatus = permissionStatus
        self.reason = reason
        self.samples = samples
        self.authoritativeBucketStarts = authoritativeBucketStarts
    }
}

protocol ScreenTimeActivityCollecting {
    var pseudonymKeyID: String? { get }

    @MainActor
    func currentAuthorizationStatus() async -> ScreenTimeCollectorResult

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult

    func collect(
        window: ScreenTimeCollectionWindow,
        excludedAppTokens: Set<String>
    ) async throws -> ScreenTimeCollectorResult
}

extension ScreenTimeActivityCollecting {
    var pseudonymKeyID: String? { nil }
}

extension ScreenTimeCollectorResult {
    var permitsAggregateUpload: Bool {
        capability == .aggregate && permissionStatus == .granted
    }
}

enum ScreenTimeSyncOutcome: Equatable {
    case uploaded(ScreenTimeActivityBatchResult)
    case unavailableReported(reason: String)
    case queued(
        reason: String,
        retryAt: Date,
        queueDepth: Int
    )
    case deferred(
        reason: String,
        retryAt: Date,
        queueDepth: Int
    )
    case skipped(reason: String)
}

enum ScreenTimeActivityCollectionError: Error, Equatable {
    case exportFailed
}

enum ScreenTimeActivityCollectionFailure: Equatable {
    case unauthorized
    case unavailable
    case transient
}

enum ScreenTimeActivityCollectionFailurePolicy {
    static func result(
        for failure: ScreenTimeActivityCollectionFailure
    ) throws -> ScreenTimeCollectorResult {
        switch failure {
        case .unauthorized:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .revoked,
                reason: "ios_screen_time_permission_revoked",
                samples: []
            )
        case .unavailable:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .unavailable,
                reason: "ios_screen_time_activity_data_unavailable",
                samples: []
            )
        case .transient:
            throw ScreenTimeActivityCollectionError.exportFailed
        }
    }

    static func authorizationResultAfterUnexpectedFailure(
        current: ScreenTimeCollectorResult
    ) -> ScreenTimeCollectorResult? {
        current.permitsAggregateUpload ? nil : current
    }
}

protocol ScreenTimeActivityTransport {
    func inputDescriptor(
        pairing: Pairing
    ) async throws -> ScreenTimeInputDescriptor

    func enableInput(
        pairing: Pairing,
        deviceID: String,
        revision: String
    ) async throws -> ScreenTimeInputDescriptor

    func collectionState(
        pairing: Pairing,
        deviceID: String
    ) async throws -> ScreenTimeCollectionState

    func upload(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult
}

enum ScreenTimeInputControlError: Error, Equatable {
    case invalidCollectorIdentity
    case invalidDescriptor
    case revisionConflictExhausted
}

extension ScreenTimeActivityTransport {
    func inputDescriptor(
        pairing _: Pairing
    ) async throws -> ScreenTimeInputDescriptor {
        throw ScreenTimeInputControlError.invalidDescriptor
    }

    func enableInput(
        pairing _: Pairing,
        deviceID _: String,
        revision _: String
    ) async throws -> ScreenTimeInputDescriptor {
        throw ScreenTimeInputControlError.invalidDescriptor
    }
}

final class URLSessionScreenTimeActivityTransport:
    ScreenTimeActivityTransport
{
    private let session: URLSession

    init(session: URLSession = GlanceClient.makeSession()) {
        self.session = session
    }

    func inputDescriptor(
        pairing: Pairing
    ) async throws -> ScreenTimeInputDescriptor {
        try await performInputDescriptor(
            ScreenTimeActivityHTTP.inputDescriptorRequest(
                pairing: pairing
            )
        )
    }

    func enableInput(
        pairing: Pairing,
        deviceID: String,
        revision: String
    ) async throws -> ScreenTimeInputDescriptor {
        try await performInputDescriptor(
            ScreenTimeActivityHTTP.inputSettingsRequest(
                pairing: pairing,
                deviceID: deviceID,
                revision: revision
            )
        )
    }

    func collectionState(
        pairing: Pairing,
        deviceID: String
    ) async throws -> ScreenTimeCollectionState {
        try await perform(
            ScreenTimeActivityHTTP.collectionRequest(
                pairing: pairing,
                deviceID: deviceID
            ),
            expecting: ScreenTimeCollectionState.self
        )
    }

    func upload(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        try await perform(
            ScreenTimeActivityHTTP.reportRequest(
                pairing: pairing,
                report: report
            ),
            expecting: ScreenTimeActivityBatchResult.self
        )
    }

    private func perform<Response: Decodable>(
        _ request: URLRequest,
        expecting _: Response.Type
    ) async throws -> Response {
        let (data, http) = try await responseData(request)
        switch http.statusCode {
        case 200...299:
            do {
                return try GlanceJSON.decoder().decode(
                    Response.self,
                    from: data
                )
            } catch {
                throw HealthMesAPIError.decoding(
                    underlying: error
                )
            }
        case 401:
            throw HealthMesAPIError.unauthorized(
                statusCode: http.statusCode
            )
        default:
            throw HealthMesAPI.responseError(
                statusCode: http.statusCode,
                data: data
            )
        }
    }

    private func performInputDescriptor(
        _ request: URLRequest
    ) async throws -> ScreenTimeInputDescriptor {
        let (data, http) = try await responseData(request)
        switch http.statusCode {
        case 200...299:
            let descriptor: ScreenTimeInputDescriptor
            do {
                descriptor = try GlanceJSON.decoder().decode(
                    ScreenTimeInputDescriptor.self,
                    from: data
                )
            } catch {
                throw HealthMesAPIError.decoding(
                    underlying: error
                )
            }
            guard
                descriptor.sourceID
                    == ScreenTimeInputDescriptor.sourceID,
                descriptor.hasValidRevision,
                http.value(forHTTPHeaderField: "ETag")
                    == "\"\(descriptor.revision)\""
            else {
                throw ScreenTimeInputControlError
                    .invalidDescriptor
            }
            return descriptor
        case 401:
            throw HealthMesAPIError.unauthorized(
                statusCode: http.statusCode
            )
        default:
            throw HealthMesAPI.responseError(
                statusCode: http.statusCode,
                data: data
            )
        }
    }

    private func responseData(
        _ request: URLRequest
    ) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            if error is CancellationError
                || Task.isCancelled
                || (error as? URLError)?.code == .cancelled
            {
                throw CancellationError()
            }
            throw HealthMesAPIError.transport(underlying: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw HealthMesAPIError.httpStatus(-1)
        }
        return (data, http)
    }
}

private struct ScreenTimeUploadAttemptFailure: Error {
    let report: ScreenTimeActivityReport
    let underlying: Error
}

private enum ScreenTimeSyncControl: Error {
    case collectionConfigurationChanged
    case pipelineSuperseded
}

private struct ScreenTimeUploadFence {
    let state: ScreenTimeCollectionState
    let authorization: ScreenTimeCollectorResult
    let controlEpoch: UInt64
}

private struct ScreenTimeSyncRequest {
    let pairing: Pairing
    let destinationID: String
    let now: Date
    let timezone: TimeZone
    let trigger: ScreenTimeSyncTrigger

    func requiresFreshRun(after active: ScreenTimeSyncRequest) -> Bool {
        if trigger.requiresFreshRun {
            return true
        }
        if timezone.identifier != active.timezone.identifier {
            return true
        }
        return Self.localHourStart(now, timezone: timezone)
            > Self.localHourStart(
                active.now,
                timezone: active.timezone
            )
    }

    func coalescing(with newer: ScreenTimeSyncRequest)
        -> ScreenTimeSyncRequest
    {
        let useNewerClock =
            newer.now >= now
            || newer.timezone.identifier != timezone.identifier
        return ScreenTimeSyncRequest(
            pairing: newer.pairing,
            destinationID: newer.destinationID,
            now: max(now, newer.now),
            timezone: useNewerClock ? newer.timezone : timezone,
            trigger: trigger.requiresFreshRun
                ? trigger
                : newer.trigger
        )
    }

    private static func localHourStart(
        _ date: Date,
        timezone: TimeZone
    ) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        return calendar.dateInterval(of: .hour, for: date)?.start
            ?? date
    }
}

private struct ScreenTimeActiveSync {
    let id: UUID
    let request: ScreenTimeSyncRequest
    let task: Task<ScreenTimeSyncOutcome, Error>
    var waiterLeases: [UUID: ScreenTimeSyncCancellationLease]
}

private struct ScreenTimePendingSync {
    let id: UUID
    var request: ScreenTimeSyncRequest
    let task: Task<ScreenTimeSyncOutcome, Error>
    var waiterLeases: [UUID: ScreenTimeSyncCancellationLease]
}

private final class ScreenTimeSyncWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation:
        CheckedContinuation<ScreenTimeSyncOutcome, Error>?
    private var pendingResult:
        Result<ScreenTimeSyncOutcome, Error>?
    private var cancelled = false

    func install(
        _ continuation:
            CheckedContinuation<ScreenTimeSyncOutcome, Error>
    ) {
        lock.lock()
        if cancelled {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        if let pendingResult {
            lock.unlock()
            continuation.resume(with: pendingResult)
            return
        }
        self.continuation = continuation
        lock.unlock()
    }

    func complete(
        _ result: Result<ScreenTimeSyncOutcome, Error>
    ) {
        lock.lock()
        guard !cancelled else {
            lock.unlock()
            return
        }
        if let continuation {
            self.continuation = nil
            lock.unlock()
            continuation.resume(with: result)
            return
        }
        pendingResult = result
        lock.unlock()
    }

    func cancel() {
        lock.lock()
        guard !cancelled, pendingResult == nil else {
            lock.unlock()
            return
        }
        cancelled = true
        let continuation = continuation
        self.continuation = nil
        lock.unlock()
        continuation?.resume(throwing: CancellationError())
    }
}

private enum ScreenTimeUploadFailurePolicy {
    static func isRetryable(_ error: Error) -> Bool {
        if error is CancellationError {
            return false
        }
        guard let error = error as? HealthMesAPIError else {
            return false
        }
        switch error {
        case .unauthorized, .transport, .decoding:
            return true
        case .httpStatus(let statusCode):
            return statusCode == 408
                || statusCode == 425
                || statusCode == 429
                || statusCode >= 500
        case .server(let statusCode, let code, _, _):
            return statusCode == 408
                || statusCode == 425
                || statusCode == 429
                || statusCode >= 500
                || (
                    statusCode == 409
                        && code == "activity_write_conflict"
                )
        case .notPaired:
            return false
        }
    }

    static func shouldQuarantine(_ error: Error) -> Bool {
        if error is CancellationError
            || error is ScreenTimeSyncControl
            || requiresCollectionRefresh(error)
            || isRetryable(error)
        {
            return false
        }
        // A POST was started and the failure is neither transient nor a
        // collection-policy refresh. Quarantine every remaining response so
        // malformed 4xx responses and unknown transport implementations
        // cannot be replayed immediately forever.
        return true
    }

    static func requiresCollectionRefresh(_ error: Error) -> Bool {
        guard let error = error as? HealthMesAPIError else {
            return false
        }
        guard case .server(_, let code, _, _) = error else {
            return false
        }
        return [
            "activity_collection_blocked",
            "activity_outside_retention",
            "ios_exclusion_reapproval_required",
            "stale_collection_revision",
        ].contains(code)
    }

    static func statusCode(_ error: Error) -> Int? {
        guard let error = error as? HealthMesAPIError else {
            return nil
        }
        switch error {
        case .server(let statusCode, _, _, _):
            return statusCode
        case .httpStatus(let statusCode),
            .unauthorized(let statusCode):
            return statusCode
        case .notPaired, .transport, .decoding:
            return nil
        }
    }

    static func reason(_ error: Error) -> String {
        if error is CancellationError {
            return "cancelled"
        }
        guard let error = error as? HealthMesAPIError else {
            return "ios_screen_time_upload_failed"
        }
        switch error {
        case .notPaired:
            return "not_paired"
        case .unauthorized:
            return "pairing_unauthorized"
        case .transport:
            return "network_unavailable"
        case .decoding:
            return "invalid_server_response"
        case .httpStatus(let statusCode):
            return "http_\(statusCode)"
        case .server(_, let code, _, _):
            return code
        }
    }
}

actor ScreenTimeActivitySyncService: ScreenTimeActivitySyncing {
    private let deviceID: String
    private let collector: any ScreenTimeActivityCollecting
    private let transport: any ScreenTimeActivityTransport
    private let stateStore: ScreenTimeSyncStateStore
    private let outbox: ScreenTimeActivityOutbox
    private let cleanupDeviceIDs: Set<String>
    private var activeSync: ScreenTimeActiveSync?
    private var pendingSync: ScreenTimePendingSync?
    private var controlEpoch: UInt64 = 0

    init(
        deviceID: String,
        collector: any ScreenTimeActivityCollecting,
        transport: any ScreenTimeActivityTransport,
        stateStore: ScreenTimeSyncStateStore = .shared,
        outbox: ScreenTimeActivityOutbox = .shared,
        cleanupDeviceIDs: Set<String> = []
    ) {
        self.deviceID = deviceID
        self.collector = collector
        self.transport = transport
        self.stateStore = stateStore
        self.outbox = outbox
        self.cleanupDeviceIDs = cleanupDeviceIDs.union([deviceID])
    }

    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        try await collector.requestAuthorization()
    }

    func registerAuthorizedCollector(
        pairing: Pairing
    ) async throws {
        try Task.checkCancellation()
        guard ScreenTimeDeviceIdentity.isStableCollectorID(deviceID) else {
            throw ScreenTimeInputControlError.invalidCollectorIdentity
        }

        for _ in 0..<3 {
            try Task.checkCancellation()
            let descriptor = try await transport.inputDescriptor(
                pairing: pairing
            )
            try Task.checkCancellation()
            try validateInputDescriptor(descriptor)
            if descriptor.instance(deviceID: deviceID) != nil {
                return
            }
            do {
                let updated = try await transport.enableInput(
                    pairing: pairing,
                    deviceID: deviceID,
                    revision: descriptor.revision
                )
                try Task.checkCancellation()
                try validateInputDescriptor(updated)
                guard
                    let instance = updated.instance(
                        deviceID: deviceID
                    ),
                    instance.enabled
                else {
                    throw ScreenTimeInputControlError
                        .invalidDescriptor
                }
                return
            } catch {
                guard Self.isInputRevisionConflict(error) else {
                    throw error
                }
            }
        }

        try Task.checkCancellation()
        let latest = try await transport.inputDescriptor(
            pairing: pairing
        )
        try Task.checkCancellation()
        try validateInputDescriptor(latest)
        guard latest.instance(deviceID: deviceID) != nil else {
            throw ScreenTimeInputControlError
                .revisionConflictExhausted
        }
    }

    func approveExcludedApps(
        _ excludedAppTokens: Set<String>
    ) async throws {
        guard let pseudonymKeyID = collector.pseudonymKeyID else {
            throw ScreenTimePseudonymBoundaryError
                .pseudonymKeyUnavailable
        }
        try await stateStore.approveExcludedApps(
            deviceID: deviceID,
            pseudonymKeyID: pseudonymKeyID,
            excludedAppTokens: excludedAppTokens
        )
    }

    func reconcilePendingUploads(
        pairing: Pairing?
    ) async throws {
        await cancelSyncsIfDestinationChanged(
            pairing.map(
                ScreenTimeActivityReportIdentity.destinationID
            )
        )
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing,
            now: Date()
        )
    }

    func disableAndPurge(now: Date) async throws {
        controlEpoch &+= 1
        let activeTask = activeSync?.task
        let pendingTask = pendingSync?.task
        activeSync = nil
        pendingSync = nil
        pendingTask?.cancel()
        activeTask?.cancel()

        if let pendingTask {
            _ = await pendingTask.result
        }
        if let activeTask {
            _ = await activeTask.result
        }

        var persistenceError: Error?
        do {
            // The outbox is dedicated to this Screen Time input. Purge every
            // historical pseudonym identity, including entries written before
            // a Keychain reset changed the current device ID.
            try await outbox.purgeAll()
        } catch {
            persistenceError = error
        }
        for cleanupDeviceID in cleanupDeviceIDs {
            await stateStore.resetAfterOptOut(
                deviceID: cleanupDeviceID,
                now: now
            )
        }
        if let persistenceError {
            throw persistenceError
        }
    }

    private func validateInputDescriptor(
        _ descriptor: ScreenTimeInputDescriptor
    ) throws {
        guard
            descriptor.sourceID == ScreenTimeInputDescriptor.sourceID,
            descriptor.hasValidRevision,
            Set(descriptor.instances.map(\.instanceID)).count
                == descriptor.instances.count,
            descriptor.instances.allSatisfy({
                $0.platform == "ios"
            })
        else {
            throw ScreenTimeInputControlError.invalidDescriptor
        }
    }

    private static func isInputRevisionConflict(
        _ error: Error
    ) -> Bool {
        guard
            let apiError = error as? HealthMesAPIError,
            case .server(
                409,
                "input_settings_revision_conflict",
                _,
                _
            ) = apiError
        else {
            return false
        }
        return true
    }

    func sync(
        pairing: Pairing,
        now: Date = Date(),
        timezone: TimeZone = .current,
        trigger: ScreenTimeSyncTrigger = .routine
    ) async throws -> ScreenTimeSyncOutcome {
        try Task.checkCancellation()
        if trigger.requiresFreshRun {
            controlEpoch &+= 1
        }
        let request = ScreenTimeSyncRequest(
            pairing: pairing,
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            now: now,
            timezone: timezone,
            trigger: trigger
        )
        await cancelSyncsIfDestinationChanged(request.destinationID)
        let waiterID = UUID()
        let syncID: UUID
        let task: Task<ScreenTimeSyncOutcome, Error>
        if var pendingSync {
            pendingSync.request = pendingSync.request.coalescing(
                with: request
            )
            pendingSync.waiterLeases[waiterID] =
                trigger.cancellationLease
            self.pendingSync = pendingSync
            syncID = pendingSync.id
            task = pendingSync.task
        } else if var activeSync {
            if request.requiresFreshRun(after: activeSync.request) {
                let pending = queuePendingSync(
                    request,
                    after: activeSync,
                    waiterID: waiterID
                )
                syncID = pending.id
                task = pending.task
            } else {
                activeSync.waiterLeases[waiterID] =
                    trigger.cancellationLease
                self.activeSync = activeSync
                syncID = activeSync.id
                task = activeSync.task
            }
        } else {
            let active = startSync(request, waiterID: waiterID)
            syncID = active.id
            task = active.task
        }

        do {
            let outcome = try await valueIsolatingWaiterCancellation(
                from: task
            )
            removeWaiter(syncID: syncID, waiterID: waiterID)
            return outcome
        } catch {
            let cancelledPipeline = removeWaiter(
                syncID: syncID,
                waiterID: waiterID,
                cancelledByCaller: Task.isCancelled
            )
            if let cancelledPipeline {
                _ = await cancelledPipeline.result
            }
            throw error
        }
    }

    private func startSync(
        _ request: ScreenTimeSyncRequest,
        waiterID: UUID
    ) -> ScreenTimeActiveSync {
        let id = UUID()
        let task = Task { [weak self] in
            guard let self else {
                throw CancellationError()
            }
            return try await self.executeSync(
                id: id,
                request: request
            )
        }
        let active = ScreenTimeActiveSync(
            id: id,
            request: request,
            task: task,
            waiterLeases: [
                waiterID: request.trigger.cancellationLease
            ]
        )
        activeSync = active
        return active
    }

    private func queuePendingSync(
        _ request: ScreenTimeSyncRequest,
        after active: ScreenTimeActiveSync,
        waiterID: UUID
    ) -> ScreenTimePendingSync {
        let id = UUID()
        let predecessor = active.task
        let task = Task { [weak self] in
            _ = await predecessor.result
            try Task.checkCancellation()
            guard let self else {
                throw CancellationError()
            }
            return try await self.executePendingSync(id: id)
        }
        let pending = ScreenTimePendingSync(
            id: id,
            request: request,
            task: task,
            waiterLeases: [
                waiterID: request.trigger.cancellationLease
            ]
        )
        pendingSync = pending
        return pending
    }

    private func executePendingSync(
        id: UUID
    ) async throws -> ScreenTimeSyncOutcome {
        try Task.checkCancellation()
        guard let pendingSync, pendingSync.id == id else {
            throw CancellationError()
        }
        self.pendingSync = nil
        activeSync = ScreenTimeActiveSync(
            id: id,
            request: pendingSync.request,
            task: pendingSync.task,
            waiterLeases: pendingSync.waiterLeases
        )
        return try await executeSync(
            id: id,
            request: pendingSync.request
        )
    }

    private func executeSync(
        id: UUID,
        request: ScreenTimeSyncRequest
    ) async throws -> ScreenTimeSyncOutcome {
        do {
            let outcome = try await performSync(
                pairing: request.pairing,
                now: request.now,
                timezone: request.timezone
            )
            clearActiveSync(id: id)
            return outcome
        } catch {
            clearActiveSync(id: id)
            throw error
        }
    }

    /// The sync pipeline is service-owned, with explicit cancellation leases.
    ///
    /// Cancelling a foreground waiter does not abandon an idempotent upload or
    /// retry write. BGTask expiration cancels the shared pipeline only when no
    /// foreground lease remains. A pairing destination change is the global
    /// cancellation boundary for this app-lifetime service.
    private func valueIsolatingWaiterCancellation(
        from task: Task<ScreenTimeSyncOutcome, Error>
    ) async throws -> ScreenTimeSyncOutcome {
        let waiter = ScreenTimeSyncWaiter()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                waiter.install(continuation)
                Task {
                    waiter.complete(await task.result)
                }
            }
        } onCancel: {
            waiter.cancel()
        }
    }

    private func clearActiveSync(id: UUID) {
        if activeSync?.id == id {
            activeSync = nil
        }
    }

    @discardableResult
    private func removeWaiter(
        syncID: UUID,
        waiterID: UUID,
        cancelledByCaller: Bool = false
    ) -> Task<ScreenTimeSyncOutcome, Error>? {
        if var pendingSync, pendingSync.id == syncID {
            guard
                let lease = pendingSync.waiterLeases.removeValue(
                    forKey: waiterID
                )
            else {
                return nil
            }
            if cancelledByCaller,
                lease == .background,
                pendingSync.waiterLeases.isEmpty
            {
                // The pending task may still be suspended on its foreground
                // predecessor. Detach it before cancelling so BGTask
                // expiration can complete without waiting for unrelated work.
                pendingSync.task.cancel()
                self.pendingSync = nil
                return nil
            }
            self.pendingSync = pendingSync
            return nil
        }

        guard var activeSync, activeSync.id == syncID else {
            return nil
        }
        guard
            let lease = activeSync.waiterLeases.removeValue(
                forKey: waiterID
            )
        else {
            return nil
        }
        if cancelledByCaller,
            lease == .background,
            activeSync.waiterLeases.isEmpty
        {
            // Detach before cancellation can suspend. A foreground caller
            // arriving while the old pipeline unwinds must start fresh
            // rather than inherit the cancelled task.
            self.activeSync = nil
            activeSync.task.cancel()
            return activeSync.task
        }
        self.activeSync = activeSync
        return nil
    }

    func waiterCounts() -> (active: Int, pending: Int) {
        (
            activeSync?.waiterLeases.count ?? 0,
            pendingSync?.waiterLeases.count ?? 0
        )
    }

    private func cancelSyncsIfDestinationChanged(
        _ destinationID: String?
    ) async {
        if let pendingSync,
            pendingSync.request.destinationID != destinationID
        {
            pendingSync.task.cancel()
            self.pendingSync = nil
        }
        if let activeSync,
            activeSync.request.destinationID != destinationID
        {
            activeSync.task.cancel()
            _ = try? await activeSync.task.value
            if self.activeSync?.id == activeSync.id {
                self.activeSync = nil
            }
        }
    }

    private func performSync(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone
    ) async throws -> ScreenTimeSyncOutcome {
        for attempt in 0..<3 {
            let expectedControlEpoch = controlEpoch
            do {
                return try await performSyncAttempt(
                    pairing: pairing,
                    now: now,
                    timezone: timezone,
                    expectedControlEpoch: expectedControlEpoch
                )
            } catch ScreenTimeSyncControl.collectionConfigurationChanged {
                if attempt == 2 {
                    return .skipped(
                        reason:
                            "ios_screen_time_collection_configuration_changed"
                    )
                }
            } catch ScreenTimeSyncControl.pipelineSuperseded {
                return .skipped(
                    reason: "ios_screen_time_sync_superseded"
                )
            }
        }
        return .skipped(
            reason: "ios_screen_time_collection_configuration_changed"
        )
    }

    private func performSyncAttempt(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone,
        expectedControlEpoch: UInt64
    ) async throws -> ScreenTimeSyncOutcome {
        try requireCurrentControlEpoch(expectedControlEpoch)
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing,
            now: now
        )
        try requireCurrentControlEpoch(expectedControlEpoch)
        let initialFence = try await refreshedUploadFence(
            pairing: pairing,
            now: now,
            expectedControlEpoch: expectedControlEpoch
        )
        let state = initialFence.state
        if let reason = ScreenTimeSyncPlanner.skipReason(
            state: state,
            now: now
        ) {
            return .skipped(reason: reason)
        }
        let pseudonymBoundaryAccepted =
            await stateStore.preparePseudonymBoundary(
                deviceID: deviceID,
                pseudonymKeyID: collector.pseudonymKeyID,
                excludedAppTokens: Set(state.excludedApps),
                now: now
            )
        guard !pseudonymBoundaryAccepted.requiresExclusionReapproval else {
            return .skipped(
                reason: pseudonymBoundaryAccepted.reason
                    ?? "ios_screen_time_exclusions_require_reapproval"
            )
        }
        let earliestCollectionStart =
            try await stateStore.proposedTimezoneBoundary(
                deviceID: deviceID,
                timezone: timezone,
                now: now
            )

        let window = try ScreenTimeSyncPlanner.completedHourWindow(
            now: now,
            timezone: timezone,
            retentionCutoff: state.rawRetentionCutoff,
            earliestCollectionStart: earliestCollectionStart
        )
        var result: ScreenTimeCollectorResult
        do {
            result = try await collector.collect(
                window: window,
                excludedAppTokens: Set(state.excludedApps)
            )
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            if Task.isCancelled {
                throw CancellationError()
            }
            let currentAuthorization =
                await collector.currentAuthorizationStatus()
            if let authorization =
                ScreenTimeActivityCollectionFailurePolicy
                    .authorizationResultAfterUnexpectedFailure(
                        current: currentAuthorization
                    )
            {
                result = authorization
            } else {
                // A transient export failure must not starve reports already
                // durably queued. Re-check authorization first so a grant
                // revoked during export cannot leak the older aggregate.
                let fence = try await refreshedUploadFence(
                    pairing: pairing,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
                if collectionContractChanged(
                    from: state,
                    to: fence.state
                ) {
                    throw ScreenTimeSyncControl
                        .collectionConfigurationChanged
                }
                let pending = try await flushPendingUploads(
                    pairing: pairing,
                    initialFence: fence,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
                if let pendingOutcome = pending.outcome {
                    return pendingOutcome
                }
                throw ScreenTimeActivityCollectionError.exportFailed
            }
        }
        if result.permitsAggregateUpload {
            let currentAuthorization =
                await collector.currentAuthorizationStatus()
            if !currentAuthorization.permitsAggregateUpload {
                result = currentAuthorization
            }
        }
        var uploadFence = try await refreshedUploadFence(
            pairing: pairing,
            now: now,
            expectedControlEpoch: expectedControlEpoch
        )
        result = try validatedCollectionResult(
            result,
            initialState: state,
            window: window,
            uploadFence: uploadFence
        )
        if let reason = ScreenTimeSyncPlanner.skipReason(
            state: uploadFence.state,
            now: now
        ) {
            return .skipped(reason: reason)
        }
        let pending = try await flushPendingUploads(
            pairing: pairing,
            initialFence: uploadFence,
            now: now,
            expectedControlEpoch: expectedControlEpoch
        )
        if let pendingOutcome = pending.outcome {
            return pendingOutcome
        }
        uploadFence = pending.fence
        result = try validatedCollectionResult(
            result,
            initialState: state,
            window: window,
            uploadFence: uploadFence
        )
        if let reason = ScreenTimeSyncPlanner.skipReason(
            state: uploadFence.state,
            now: now
        ) {
            return .skipped(reason: reason)
        }
        guard
            result.permitsAggregateUpload,
            let pseudonymKeyID = collector.pseudonymKeyID
        else {
            let generation = await stateStore.collectionGeneration(
                deviceID: deviceID,
                permissionStatus: result.permissionStatus,
                now: now
            )
            let reason = result.reason
                ?? (
                    collector.pseudonymKeyID == nil
                        ? "ios_screen_time_pseudonym_key_unavailable"
                        : "ios_screen_time_unavailable"
                )
            let report = ScreenTimeActivityReport.unavailable(
                deviceID: deviceID,
                timezone: timezone.identifier,
                permissionStatus: result.permissionStatus,
                reason: reason,
                collectedAt: now,
                collectionRevision: uploadFence.state.configRevision,
                collectionGeneration: generation
            )
            return try await deliver(
                pairing: pairing,
                report: report,
                now: now,
                expectedControlEpoch: expectedControlEpoch,
                success: { _ in
                    .unavailableReported(reason: reason)
                }
            )
        }

        _ = try await stateStore.acceptTimezoneBoundary(
            deviceID: deviceID,
            timezone: timezone,
            now: now
        )
        let generation = await stateStore.collectionGeneration(
            deviceID: deviceID,
            permissionStatus: result.permissionStatus,
            now: now
        )
        let sequence = await stateStore.allocateSnapshotSequence(
            deviceID: deviceID
        )
        let report = ScreenTimeActivityReport.aggregate(
            deviceID: deviceID,
            timezone: timezone.identifier,
            pseudonymKeyID: pseudonymKeyID,
            collectedAt: now,
            collectionRevision: uploadFence.state.configRevision,
            collectionGeneration: generation,
            snapshotSequence: sequence,
            snapshotStart: window.start,
            snapshotEnd: window.end,
            authoritativeBucketStarts:
                result.authoritativeBucketStarts,
            samples: result.samples
        )

        return try await deliver(
            pairing: pairing,
            report: report,
            now: now,
            expectedControlEpoch: expectedControlEpoch,
            success: ScreenTimeSyncOutcome.uploaded
        )
    }

    private func flushPendingUploads(
        pairing: Pairing,
        initialFence: ScreenTimeUploadFence,
        now: Date,
        expectedControlEpoch: UInt64
    ) async throws -> (
        outcome: ScreenTimeSyncOutcome?,
        fence: ScreenTimeUploadFence
    ) {
        var fence = initialFence
        while true {
            try requireCurrentControlEpoch(expectedControlEpoch)
            try await outbox.reconcile(
                deviceID: deviceID,
                pairing: pairing,
                state: fence.state,
                authorization: fence.authorization,
                now: now
            )
            if let reason = ScreenTimeSyncPlanner.skipReason(
                state: fence.state,
                now: now
            ) {
                return (.skipped(reason: reason), fence)
            }
            guard
                let entry = await outbox.oldest(
                    deviceID: deviceID,
                    pairing: pairing
                )
            else {
                return (nil, fence)
            }
            let queueDepth = await outbox.pendingCount(
                deviceID: deviceID,
                pairing: pairing
            )
            guard entry.nextAttemptAt <= now else {
                return (
                    .deferred(
                        reason: "retry_backoff",
                        retryAt: entry.nextAttemptAt,
                        queueDepth: queueDepth
                    ),
                    fence
                )
            }
            do {
                _ = try await uploadWithFenceRecovery(
                    pairing: pairing,
                    report: entry.report,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
                try await outbox.markSucceeded(id: entry.id)
                // Once a POST starts, settle that exact durable item before
                // allowing a newer control epoch to stop this pipeline.
                try requireCurrentControlEpoch(expectedControlEpoch)
                fence = try await refreshedUploadFence(
                    pairing: pairing,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
            } catch let failure as ScreenTimeUploadAttemptFailure {
                if failure.underlying is CancellationError {
                    _ = try await markInterruptedUpload(
                        entry: entry,
                        report: failure.report,
                        pairing: pairing,
                        now: now
                    )
                    throw CancellationError()
                }
                if let control =
                    failure.underlying as? ScreenTimeSyncControl
                {
                    _ = try await markInterruptedUpload(
                        entry: entry,
                        report: failure.report,
                        pairing: pairing,
                        now: now
                    )
                    throw control
                }
                if ScreenTimeUploadFailurePolicy
                    .requiresCollectionRefresh(failure.underlying)
                {
                    try await outbox.markSucceeded(id: entry.id)
                    try requireCurrentControlEpoch(
                        expectedControlEpoch
                    )
                    throw ScreenTimeSyncControl
                        .collectionConfigurationChanged
                }
                if ScreenTimeUploadFailurePolicy.isRetryable(
                    failure.underlying
                ) {
                    let failedEntry: ScreenTimeActivityOutboxEntry?
                    if failure.report == entry.report {
                        failedEntry = try await outbox.markFailed(
                            id: entry.id,
                            now: now
                        )
                    } else {
                        failedEntry =
                            try await outbox.replaceAndMarkFailed(
                                id: entry.id,
                                with: failure.report,
                                pairing: pairing,
                                now: now
                            )
                    }
                    let retryAt = failedEntry?.nextAttemptAt
                        ?? now.addingTimeInterval(60)
                    let outcome = ScreenTimeSyncOutcome.queued(
                        reason: ScreenTimeUploadFailurePolicy.reason(
                            failure.underlying
                        ),
                        retryAt: retryAt,
                        queueDepth: await outbox.pendingCount(
                            deviceID: deviceID,
                            pairing: pairing
                        )
                    )
                    try requireCurrentControlEpoch(
                        expectedControlEpoch
                    )
                    return (
                        outcome,
                        fence
                    )
                }
                if ScreenTimeUploadFailurePolicy.shouldQuarantine(
                    failure.underlying
                ) {
                    _ = try await outbox.markTerminal(
                        id: entry.id,
                        report: failure.report,
                        reason: ScreenTimeUploadFailurePolicy.reason(
                            failure.underlying
                        ),
                        statusCode:
                            ScreenTimeUploadFailurePolicy.statusCode(
                                failure.underlying
                            ),
                        now: now
                    )
                    try requireCurrentControlEpoch(
                        expectedControlEpoch
                    )
                    // A malformed or permanently stale item must not starve
                    // newer snapshots in the oldest-first queue.
                    fence = try await refreshedUploadFence(
                        pairing: pairing,
                        now: now,
                        expectedControlEpoch: expectedControlEpoch
                    )
                    continue
                }
                throw failure.underlying
            }
        }
    }

    @discardableResult
    private func markInterruptedUpload(
        entry: ScreenTimeActivityOutboxEntry,
        report: ScreenTimeActivityReport,
        pairing: Pairing,
        now: Date
    ) async throws -> ScreenTimeActivityOutboxEntry? {
        if report == entry.report {
            return try await outbox.markFailed(
                id: entry.id,
                now: now
            )
        }
        return try await outbox.replaceAndMarkFailed(
            id: entry.id,
            with: report,
            pairing: pairing,
            now: now
        )
    }

    private func refreshedUploadFence(
        pairing: Pairing,
        now: Date,
        expectedControlEpoch: UInt64
    ) async throws -> ScreenTimeUploadFence {
        try requireCurrentControlEpoch(expectedControlEpoch)
        let state = try await transport.collectionState(
            pairing: pairing,
            deviceID: deviceID
        )
        try requireCurrentControlEpoch(expectedControlEpoch)
        let authorization =
            await collector.currentAuthorizationStatus()
        try requireCurrentControlEpoch(expectedControlEpoch)
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing,
            state: state,
            authorization: authorization,
            now: now
        )
        try requireCurrentControlEpoch(expectedControlEpoch)
        return ScreenTimeUploadFence(
            state: state,
            authorization: authorization,
            controlEpoch: expectedControlEpoch
        )
    }

    private func deliver(
        pairing: Pairing,
        report: ScreenTimeActivityReport,
        now: Date,
        expectedControlEpoch: UInt64,
        success: (ScreenTimeActivityBatchResult) -> ScreenTimeSyncOutcome
    ) async throws -> ScreenTimeSyncOutcome {
        do {
            return success(
                try await uploadWithFenceRecovery(
                    pairing: pairing,
                    report: report,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
            )
        } catch let failure as ScreenTimeUploadAttemptFailure {
            if failure.underlying is CancellationError {
                _ = try await enqueueInterruptedUpload(
                    pairing: pairing,
                    report: failure.report,
                    now: now
                )
                throw CancellationError()
            }
            if let control =
                failure.underlying as? ScreenTimeSyncControl
            {
                _ = try await enqueueInterruptedUpload(
                    pairing: pairing,
                    report: failure.report,
                    now: now
                )
                throw control
            }
            if ScreenTimeUploadFailurePolicy
                .requiresCollectionRefresh(failure.underlying)
            {
                throw ScreenTimeSyncControl
                    .collectionConfigurationChanged
            }
            if ScreenTimeUploadFailurePolicy.isRetryable(
                failure.underlying
            ) {
                let entry = try await outbox.enqueue(
                    report: failure.report,
                    pairing: pairing,
                    now: now
                )
                let failedEntry = try await outbox.markFailed(
                    id: entry.id,
                    now: now
                )
                return .queued(
                    reason: ScreenTimeUploadFailurePolicy.reason(
                        failure.underlying
                    ),
                    retryAt: failedEntry?.nextAttemptAt
                        ?? now.addingTimeInterval(60),
                    queueDepth: await outbox.pendingCount(
                        deviceID: deviceID,
                        pairing: pairing
                    )
                )
            }
            if ScreenTimeUploadFailurePolicy.shouldQuarantine(
                failure.underlying
            ) {
                let entry = try await outbox.enqueue(
                    report: failure.report,
                    pairing: pairing,
                    now: now
                )
                _ = try await outbox.markTerminal(
                    id: entry.id,
                    report: failure.report,
                    reason: ScreenTimeUploadFailurePolicy.reason(
                        failure.underlying
                    ),
                    statusCode:
                        ScreenTimeUploadFailurePolicy.statusCode(
                            failure.underlying
                        ),
                    now: now
                )
                return .skipped(
                    reason: ScreenTimeUploadFailurePolicy.reason(
                        failure.underlying
                    )
                )
            }
            throw failure.underlying
        }
    }

    @discardableResult
    private func enqueueInterruptedUpload(
        pairing: Pairing,
        report: ScreenTimeActivityReport,
        now: Date
    ) async throws -> ScreenTimeActivityOutboxEntry? {
        let entry = try await outbox.enqueue(
            report: report,
            pairing: pairing,
            now: now
        )
        return try await outbox.markFailed(
            id: entry.id,
            now: now
        )
    }

    private func uploadWithFenceRecovery(
        pairing: Pairing,
        report: ScreenTimeActivityReport,
        now: Date,
        expectedControlEpoch: UInt64
    ) async throws -> ScreenTimeActivityBatchResult {
        do {
            return try await uploadAfterRefreshingFence(
                pairing: pairing,
                report: report,
                now: now,
                expectedControlEpoch: expectedControlEpoch
            )
        } catch let failure as ScreenTimeUploadAttemptFailure {
            guard
                let apiError = failure.underlying as? HealthMesAPIError,
                ScreenTimeSyncPlanner.shouldResetSnapshotFence(apiError)
            else {
                throw failure
            }
            let resetReport = report.resettingSnapshotFence()
            do {
                return try await uploadAfterRefreshingFence(
                    pairing: pairing,
                    report: resetReport,
                    now: now,
                    expectedControlEpoch: expectedControlEpoch
                )
            } catch let retryFailure
                as ScreenTimeUploadAttemptFailure
            {
                throw retryFailure
            } catch {
                // The first POST received a definitive reset response. Even
                // if cancellation or supersession happens while refreshing
                // the second fence, persist the replacement report so the
                // original snapshot cannot be replayed.
                throw ScreenTimeUploadAttemptFailure(
                    report: resetReport,
                    underlying: error
                )
            }
        }
    }

    private func uploadAfterRefreshingFence(
        pairing: Pairing,
        report: ScreenTimeActivityReport,
        now: Date,
        expectedControlEpoch: UInt64
    ) async throws -> ScreenTimeActivityBatchResult {
        let fence = try await refreshedUploadFence(
            pairing: pairing,
            now: now,
            expectedControlEpoch: expectedControlEpoch
        )
        guard reportCanUpload(
            report,
            through: fence,
            now: now
        ) else {
            throw ScreenTimeSyncControl
                .collectionConfigurationChanged
        }
        try requireCurrentControlEpoch(expectedControlEpoch)
        do {
            return try await transport.upload(
                pairing: pairing,
                report: report
            )
        } catch {
            throw ScreenTimeUploadAttemptFailure(
                report: report,
                underlying: error
            )
        }
    }

    private func requireCurrentControlEpoch(
        _ expectedControlEpoch: UInt64
    ) throws {
        guard controlEpoch == expectedControlEpoch else {
            throw ScreenTimeSyncControl.pipelineSuperseded
        }
    }

    private func collectionContractChanged(
        from previous: ScreenTimeCollectionState,
        to current: ScreenTimeCollectionState
    ) -> Bool {
        previous.deviceID != current.deviceID
            || previous.enabled != current.enabled
            || Set(previous.excludedApps) != Set(current.excludedApps)
            || previous.pausedUntil != current.pausedUntil
            || previous.effectiveCollecting
                != current.effectiveCollecting
            || previous.blockedReason != current.blockedReason
            || previous.configRevision != current.configRevision
    }

    private func validatedCollectionResult(
        _ collected: ScreenTimeCollectorResult,
        initialState: ScreenTimeCollectionState,
        window: ScreenTimeCollectionWindow,
        uploadFence: ScreenTimeUploadFence
    ) throws -> ScreenTimeCollectorResult {
        if collectionContractChanged(
            from: initialState,
            to: uploadFence.state
        ) {
            throw ScreenTimeSyncControl.collectionConfigurationChanged
        }
        if collected.permitsAggregateUpload,
            retentionInvalidates(
                window: window,
                result: collected,
                cutoff: uploadFence.state.rawRetentionCutoff
            )
        {
            throw ScreenTimeSyncControl.collectionConfigurationChanged
        }
        if collected.permitsAggregateUpload {
            return uploadFence.authorization.permitsAggregateUpload
                ? collected
                : uploadFence.authorization
        }
        if uploadFence.authorization.permitsAggregateUpload {
            return collected
        }
        return uploadFence.authorization
    }

    private func retentionInvalidates(
        window: ScreenTimeCollectionWindow,
        result: ScreenTimeCollectorResult,
        cutoff: Date?
    ) -> Bool {
        guard let cutoff else { return false }
        if window.start <= cutoff {
            return true
        }
        if result.samples.contains(where: { $0.bucketStart <= cutoff }) {
            return true
        }
        return result.authoritativeBucketStarts.contains(where: {
            $0 <= cutoff
        })
    }

    private func reportCanUpload(
        _ report: ScreenTimeActivityReport,
        through fence: ScreenTimeUploadFence,
        now: Date
    ) -> Bool {
        guard fence.controlEpoch == controlEpoch else {
            return false
        }
        guard
            ScreenTimeSyncPlanner.skipReason(
                state: fence.state,
                now: now
            ) == nil,
            report.collectionRevision == fence.state.configRevision
        else {
            return false
        }
        if let cutoff = fence.state.rawRetentionCutoff {
            if let snapshotStart = report.snapshotStart,
                snapshotStart <= cutoff
            {
                return false
            }
            if report.samples.contains(where: {
                $0.bucketStart <= cutoff
            }) {
                return false
            }
            if report.authoritativeBucketStarts.contains(where: {
                $0 <= cutoff
            }) {
                return false
            }
        }
        switch report.capability {
        case .aggregate:
            return report.permissionStatus == .granted
                && fence.authorization.permitsAggregateUpload
        case .unavailable:
            if fence.authorization.permitsAggregateUpload {
                return report.permissionStatus == .granted
                    || report.permissionStatus == .unavailable
                    || report.permissionStatus == .unknown
            }
            return report.permissionStatus
                == fence.authorization.permissionStatus
        }
    }
}
