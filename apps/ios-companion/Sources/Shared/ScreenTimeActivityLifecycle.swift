import Foundation

enum ScreenTimeSyncTrigger: Equatable, Sendable {
    case routine
    case backgroundRefresh
    case authorizationChanged
    case inputConfigurationChanged

    var requiresFreshRun: Bool {
        switch self {
        case .authorizationChanged, .inputConfigurationChanged:
            return true
        case .routine, .backgroundRefresh:
            return false
        }
    }

    var cancellationLease: ScreenTimeSyncCancellationLease {
        self == .backgroundRefresh ? .background : .foreground
    }
}

enum ScreenTimeSyncCancellationLease: Equatable, Sendable {
    case foreground
    case background
}

protocol ScreenTimeActivitySyncing: Sendable {
    func requestAuthorization() async throws -> ScreenTimeCollectorResult

    func approveExcludedApps(
        _ excludedAppTokens: Set<String>
    ) async throws

    func sync(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone,
        trigger: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeSyncOutcome

    func reconcilePendingUploads(
        pairing: Pairing?
    ) async throws
}

@MainActor
protocol ScreenTimeAuthorizationChangeObserving: AnyObject {
    func start(
        onChange: @escaping @MainActor @Sendable () async -> Void
    )
}

struct ScreenTimeAuthorizationIntentStore {
    private static let optedInKey =
        "healthmes.screen-time.authorization-opt-in.v1"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.userDefaults) {
        self.defaults = defaults
    }

    var isOptedIn: Bool {
        defaults.bool(forKey: Self.optedInKey)
    }

    func setOptedIn(_ optedIn: Bool) {
        defaults.set(optedIn, forKey: Self.optedInKey)
    }
}

extension ScreenTimeActivitySyncing {
    func sync(
        pairing: Pairing,
        now: Date,
        timezone: TimeZone
    ) async throws -> ScreenTimeSyncOutcome {
        try await sync(
            pairing: pairing,
            now: now,
            timezone: timezone,
            trigger: .routine
        )
    }
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

struct ScreenTimeAuthorizationAttempt: Equatable {
    let authorization: ScreenTimeCollectorResult?
    let failureReason: String?
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
        let attempt = await requestAuthorization()
        guard let authorization = attempt.authorization else {
            return ScreenTimeAuthorizationSyncResult(
                authorization: nil,
                sync: .failed(
                    reason: attempt.failureReason
                        ?? "ios_screen_time_authorization_failed"
                )
            )
        }
        return ScreenTimeAuthorizationSyncResult(
            authorization: authorization,
            sync: await catchUp(
                now: now,
                timezone: timezone,
                trigger: .authorizationChanged
            )
        )
    }

    func requestAuthorization() async -> ScreenTimeAuthorizationAttempt {
        do {
            return ScreenTimeAuthorizationAttempt(
                authorization:
                    try await syncService.requestAuthorization(),
                failureReason: nil
            )
        } catch {
            return ScreenTimeAuthorizationAttempt(
                authorization: nil,
                failureReason: Self.failureReason(
                    for: error,
                    fallback: "ios_screen_time_authorization_failed"
                )
            )
        }
    }

    func authorizationDidChange(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        await catchUp(
            now: now,
            timezone: timezone,
            trigger: .authorizationChanged
        )
    }

    func catchUp(
        now: Date = Date(),
        timezone: TimeZone = .current,
        trigger: ScreenTimeSyncTrigger = .routine
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
                    timezone: timezone,
                    trigger: trigger
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
        await catchUp(
            now: now,
            timezone: timezone,
            trigger: .inputConfigurationChanged
        )
    }

    func configurationDidChange(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        await catchUp(
            now: now,
            timezone: timezone,
            trigger: .inputConfigurationChanged
        )
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
        return await catchUp(
            now: now,
            timezone: timezone,
            trigger: .inputConfigurationChanged
        )
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
        if let error = error as? ScreenTimeActivityCollectionError {
            switch error {
            case .exportFailed:
                return "ios_screen_time_activity_export_failed"
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
