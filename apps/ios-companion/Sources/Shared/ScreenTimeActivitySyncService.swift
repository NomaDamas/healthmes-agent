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
    func requestAuthorization() async throws -> ScreenTimeCollectorResult

    func collect(
        window: ScreenTimeCollectionWindow,
        excludedAppTokens: Set<String>
    ) async throws -> ScreenTimeCollectorResult
}

extension ScreenTimeActivityCollecting {
    var pseudonymKeyID: String? { nil }
}

enum ScreenTimeSyncOutcome: Equatable {
    case uploaded(ScreenTimeActivityBatchResult)
    case unavailableReported(reason: String)
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

actor ScreenTimeActivitySyncService {
    private let deviceID: String
    private let collector: any ScreenTimeActivityCollecting
    private let transport: any ScreenTimeActivityTransport
    private let stateStore: ScreenTimeSyncStateStore

    init(
        deviceID: String,
        collector: any ScreenTimeActivityCollecting,
        transport: any ScreenTimeActivityTransport,
        stateStore: ScreenTimeSyncStateStore = .shared
    ) {
        self.deviceID = deviceID
        self.collector = collector
        self.transport = transport
        self.stateStore = stateStore
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

    func sync(
        pairing: Pairing,
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async throws -> ScreenTimeSyncOutcome {
        let state = try await transport.collectionState(
            pairing: pairing,
            deviceID: deviceID
        )
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
        let result = try await collector.collect(
            window: window,
            excludedAppTokens: Set(state.excludedApps)
        )
        guard
            result.capability == .aggregate,
            result.permissionStatus == .granted,
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
            _ = try await transport.upload(
                pairing: pairing,
                report: .unavailable(
                    deviceID: deviceID,
                    timezone: timezone.identifier,
                    permissionStatus: result.permissionStatus,
                    reason: reason,
                    collectedAt: now,
                    collectionRevision: state.configRevision,
                    collectionGeneration: generation
                )
            )
            return .unavailableReported(reason: reason)
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

        do {
            let response = try await transport.upload(
                pairing: pairing,
                report: report
            )
            return .uploaded(response)
        } catch let error as HealthMesAPIError
            where ScreenTimeSyncPlanner.shouldResetSnapshotFence(error)
        {
            let response = try await transport.upload(
                pairing: pairing,
                report: report.resettingSnapshotFence()
            )
            return .uploaded(response)
        }
    }
}
