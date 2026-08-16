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

protocol ScreenTimeActivityTransport {
    func collectionState(
        pairing: Pairing,
        deviceID: String
    ) async throws -> ScreenTimeCollectionState

    func upload(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult
}

final class URLSessionScreenTimeActivityTransport:
    ScreenTimeActivityTransport
{
    private let session: URLSession

    init(session: URLSession = GlanceClient.makeSession()) {
        self.session = session
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
        switch http.statusCode {
        case 200...299:
            do {
                return try GlanceJSON.decoder().decode(Response.self, from: data)
            } catch {
                throw HealthMesAPIError.decoding(underlying: error)
            }
        case 401:
            throw HealthMesAPIError.unauthorized(statusCode: http.statusCode)
        default:
            throw HealthMesAPI.responseError(
                statusCode: http.statusCode,
                data: data
            )
        }
    }
}

private struct ScreenTimeUploadAttemptFailure: Error {
    let report: ScreenTimeActivityReport
    let underlying: Error
}

private struct ScreenTimeActiveSync {
    let id: UUID
    let destinationID: String
    let task: Task<ScreenTimeSyncOutcome, Error>
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
                || code == "activity_write_conflict"
        case .notPaired:
            return false
        }
    }

    static func shouldDiscard(_ error: Error) -> Bool {
        guard
            case .server(_, let code, _, _)? =
                error as? HealthMesAPIError
        else {
            return false
        }
        return [
            "activity_collection_blocked",
            "activity_future_data",
            "activity_outside_retention",
            "activity_source_conflict",
            "activity_source_mode_conflict",
            "ios_exclusion_reapproval_required",
            "snapshot_retry_response_unavailable",
            "stale_collection_revision",
        ].contains(code)
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
    private var activeSync: ScreenTimeActiveSync?

    init(
        deviceID: String,
        collector: any ScreenTimeActivityCollecting,
        transport: any ScreenTimeActivityTransport,
        stateStore: ScreenTimeSyncStateStore = .shared,
        outbox: ScreenTimeActivityOutbox = .shared
    ) {
        self.deviceID = deviceID
        self.collector = collector
        self.transport = transport
        self.stateStore = stateStore
        self.outbox = outbox
    }

    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        try await collector.requestAuthorization()
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
        await cancelActiveSyncIfDestinationChanged(
            pairing.map(
                ScreenTimeActivityReportIdentity.destinationID
            )
        )
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing
        )
    }

    func sync(
        pairing: Pairing,
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async throws -> ScreenTimeSyncOutcome {
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        await cancelActiveSyncIfDestinationChanged(destinationID)
        if let activeSync {
            return try await valuePropagatingCancellation(
                from: activeSync.task
            )
        }
        let id = UUID()
        let task = Task {
            try await self.performSync(
                pairing: pairing,
                now: now,
                timezone: timezone
            )
        }
        activeSync = ScreenTimeActiveSync(
            id: id,
            destinationID: destinationID,
            task: task
        )
        defer {
            if activeSync?.id == id {
                activeSync = nil
            }
        }
        return try await valuePropagatingCancellation(from: task)
    }

    private func valuePropagatingCancellation(
        from task: Task<ScreenTimeSyncOutcome, Error>
    ) async throws -> ScreenTimeSyncOutcome {
        try await withTaskCancellationHandler {
            try await task.value
        } onCancel: {
            task.cancel()
        }
    }

    private func cancelActiveSyncIfDestinationChanged(
        _ destinationID: String?
    ) async {
        guard
            let activeSync,
            activeSync.destinationID != destinationID
        else {
            return
        }
        activeSync.task.cancel()
        _ = try? await activeSync.task.value
        if self.activeSync?.id == activeSync.id {
            self.activeSync = nil
        }
    }

    private func performSync(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone
    ) async throws -> ScreenTimeSyncOutcome {
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing
        )
        let state = try await transport.collectionState(
            pairing: pairing,
            deviceID: deviceID
        )
        let authorization =
            await collector.currentAuthorizationStatus()
        try await outbox.reconcile(
            deviceID: deviceID,
            pairing: pairing,
            state: state,
            authorization: authorization
        )
        if let reason = ScreenTimeSyncPlanner.skipReason(
            state: state,
            now: now
        ) {
            return .skipped(reason: reason)
        }

        if let pendingOutcome = try await flushPendingUploads(
            pairing: pairing,
            now: now
        ) {
            return pendingOutcome
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
            result = ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .unavailable,
                reason: "ios_screen_time_activity_data_unavailable",
                samples: []
            )
        }
        if result.permitsAggregateUpload {
            let currentAuthorization =
                await collector.currentAuthorizationStatus()
            if !currentAuthorization.permitsAggregateUpload {
                result = currentAuthorization
            }
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
                collectionRevision: state.configRevision,
                collectionGeneration: generation
            )
            return try await deliver(
                pairing: pairing,
                report: report,
                now: now,
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
            collectionRevision: state.configRevision,
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
            success: ScreenTimeSyncOutcome.uploaded
        )
    }

    private func flushPendingUploads(
        pairing: Pairing,
        now: Date
    ) async throws -> ScreenTimeSyncOutcome? {
        while true {
            let authorization =
                await collector.currentAuthorizationStatus()
            try await outbox.reconcile(
                deviceID: deviceID,
                pairing: pairing,
                authorization: authorization
            )
            guard
                let entry = await outbox.oldest(
                    deviceID: deviceID,
                    pairing: pairing
                )
            else {
                return nil
            }
            let queueDepth = await outbox.pendingCount(
                deviceID: deviceID,
                pairing: pairing
            )
            guard entry.nextAttemptAt <= now else {
                return .deferred(
                    reason: "retry_backoff",
                    retryAt: entry.nextAttemptAt,
                    queueDepth: queueDepth
                )
            }
            do {
                _ = try await uploadWithFenceRecovery(
                    pairing: pairing,
                    report: entry.report
                )
                try await outbox.markSucceeded(id: entry.id)
            } catch let failure as ScreenTimeUploadAttemptFailure {
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
                    return .queued(
                        reason: ScreenTimeUploadFailurePolicy.reason(
                            failure.underlying
                        ),
                        retryAt: retryAt,
                        queueDepth: await outbox.pendingCount(
                            deviceID: deviceID,
                            pairing: pairing
                        )
                    )
                }
                if ScreenTimeUploadFailurePolicy.shouldDiscard(
                    failure.underlying
                ) {
                    try await outbox.markSucceeded(id: entry.id)
                    return .skipped(
                        reason: ScreenTimeUploadFailurePolicy.reason(
                            failure.underlying
                        )
                    )
                }
                throw failure.underlying
            }
        }
    }

    private func deliver(
        pairing: Pairing,
        report: ScreenTimeActivityReport,
        now: Date,
        success: (ScreenTimeActivityBatchResult) -> ScreenTimeSyncOutcome
    ) async throws -> ScreenTimeSyncOutcome {
        do {
            return success(
                try await uploadWithFenceRecovery(
                    pairing: pairing,
                    report: report
                )
            )
        } catch let failure as ScreenTimeUploadAttemptFailure {
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
            if ScreenTimeUploadFailurePolicy.shouldDiscard(
                failure.underlying
            ) {
                return .skipped(
                    reason: ScreenTimeUploadFailurePolicy.reason(
                        failure.underlying
                    )
                )
            }
            throw failure.underlying
        }
    }

    private func uploadWithFenceRecovery(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        do {
            return try await transport.upload(
                pairing: pairing,
                report: report
            )
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as HealthMesAPIError
            where ScreenTimeSyncPlanner.shouldResetSnapshotFence(error)
        {
            let resetReport = report.resettingSnapshotFence()
            do {
                return try await transport.upload(
                    pairing: pairing,
                    report: resetReport
                )
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                throw ScreenTimeUploadAttemptFailure(
                    report: resetReport,
                    underlying: error
                )
            }
        } catch {
            throw ScreenTimeUploadAttemptFailure(
                report: report,
                underlying: error
            )
        }
    }
}
