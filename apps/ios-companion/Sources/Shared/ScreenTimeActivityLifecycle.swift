import Foundation

protocol ScreenTimeActivitySyncing: Sendable {
    func requestAuthorization() async throws -> ScreenTimeCollectorResult

    func approveExcludedApps(
        _ excludedAppTokens: Set<String>
    ) async throws

    func sync(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone
    ) async throws -> ScreenTimeSyncOutcome

    func reconcilePendingUploads(
        pairing: Pairing?
    ) async throws
}

enum ScreenTimeActivityLifecycleResult: Equatable {
    case completed(ScreenTimeSyncOutcome)
    case skipped(reason: String)
    case failed(reason: String)

    var completedWithoutError: Bool {
        if case .failed = self {
            return false
        }
        return true
    }
}

struct ScreenTimeAuthorizationSyncResult: Equatable {
    let authorization: ScreenTimeCollectorResult?
    let sync: ScreenTimeActivityLifecycleResult
}

/// UI-neutral entry point for the future device settings surface.
///
/// A device UI should call `requestAuthorizationAndSync()` after explicit
/// user opt-in. A successful Apple authorization immediately enters the same
/// idempotent pipeline used by foreground and background catch-up.
@MainActor
final class ScreenTimeActivityLifecycleController {
    private let syncService: any ScreenTimeActivitySyncing
    private let pairingProvider: @MainActor () -> Pairing?

    init(
        syncService: any ScreenTimeActivitySyncing,
        pairingProvider: @escaping @MainActor () -> Pairing?
    ) {
        self.syncService = syncService
        self.pairingProvider = pairingProvider
    }

    func requestAuthorizationAndSync(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeAuthorizationSyncResult {
        let authorization: ScreenTimeCollectorResult
        do {
            authorization =
                try await syncService.requestAuthorization()
        } catch {
            return ScreenTimeAuthorizationSyncResult(
                authorization: nil,
                sync: .failed(
                    reason: Self.failureReason(
                        for: error,
                        fallback: "ios_screen_time_authorization_failed"
                    )
                )
            )
        }
        guard authorization.permitsAggregateUpload else {
            return ScreenTimeAuthorizationSyncResult(
                authorization: authorization,
                sync: .skipped(
                    reason: authorization.reason
                        ?? "ios_screen_time_authorization_not_granted"
                )
            )
        }
        return ScreenTimeAuthorizationSyncResult(
            authorization: authorization,
            sync: await catchUp(now: now, timezone: timezone)
        )
    }

    func catchUp(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        guard let pairing = pairingProvider() else {
            do {
                try await syncService.reconcilePendingUploads(
                    pairing: nil
                )
                return .skipped(reason: "not_paired")
            } catch {
                return .failed(
                    reason: Self.failureReason(
                        for: error,
                        fallback:
                            "ios_screen_time_outbox_reconciliation_failed"
                    )
                )
            }
        }
        do {
            return .completed(
                try await syncService.sync(
                    pairing: pairing,
                    now: now,
                    timezone: timezone
                )
            )
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback: "ios_screen_time_sync_failed"
                )
            )
        }
    }

    func pairingDidChange(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        await catchUp(now: now, timezone: timezone)
    }

    func approveExcludedAppsAndSync(
        _ excludedAppTokens: Set<String>,
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        do {
            try await syncService.approveExcludedApps(
                excludedAppTokens
            )
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback:
                        "ios_screen_time_exclusion_approval_failed"
                )
            )
        }
        return await catchUp(now: now, timezone: timezone)
    }

    private static func failureReason(
        for error: Error,
        fallback: String
    ) -> String {
        if error is CancellationError {
            return "cancelled"
        }
        if let error = error as? ScreenTimeActivityOutboxError {
            switch error {
            case .itemTooLarge:
                return "ios_screen_time_outbox_item_too_large"
            case .persistenceFailed:
                return "ios_screen_time_outbox_persistence_failed"
            }
        }
        if let error = error as? ScreenTimePseudonymBoundaryError {
            switch error {
            case .pseudonymKeyUnavailable:
                return "ios_screen_time_pseudonym_key_unavailable"
            case .pseudonymKeyChanged:
                return "ios_screen_time_pseudonym_key_changed"
            case .invalidExcludedAppToken:
                return "ios_screen_time_invalid_excluded_app_token"
            }
        }
        guard let error = error as? HealthMesAPIError else {
            return fallback
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
