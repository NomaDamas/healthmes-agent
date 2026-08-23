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

struct ScreenTimeAuthorizationRestorationFence: Equatable, Sendable {
    let deviceID: String
    let destinationID: String
    let configRevision: Int
    let blockedReason: String?
}

struct ScreenTimeAuthorizationRestorationContext: Equatable {
    let pairing: Pairing
    let fence: ScreenTimeAuthorizationRestorationFence
}

enum ScreenTimeAuthorizationRestorationPreparation: Equatable {
    case ready(ScreenTimeAuthorizationRestorationContext)
    case skipped(reason: String)
    case failed(reason: String)
}

protocol ScreenTimeActivitySyncing: Sendable {
    func currentAuthorizationStatus() async -> ScreenTimeCollectorResult

    func requestAuthorization() async throws -> ScreenTimeCollectorResult

    func authorizationRestorationFence(
        pairing: Pairing,
        now: Date
    ) async throws -> ScreenTimeAuthorizationRestorationFence

    func registerAuthorizedCollector(
        pairing: Pairing
    ) async throws

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

    func disableAndPurge(now: Date) async throws
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
    private static let cleanupPendingKey =
        "healthmes.screen-time.privacy-cleanup-pending.v1"
    private static let activeDeviceIDKey =
        "healthmes.screen-time.active-device-id.v1"
    private static let legacyFallbackDeviceIDKey =
        "healthmes.screen-time.fallback-device-id.v1"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.userDefaults) {
        self.defaults = defaults
    }

    var isOptedIn: Bool {
        defaults.bool(forKey: Self.optedInKey)
    }

    var isPrivacyCleanupPending: Bool {
        defaults.bool(forKey: Self.cleanupPendingKey)
    }

    var activeDeviceID: String? {
        defaults.string(forKey: Self.activeDeviceIDKey)
            .flatMap(Self.normalizedDeviceID)
    }

    var legacyFallbackDeviceID: String? {
        defaults.string(forKey: Self.legacyFallbackDeviceIDKey)
            .flatMap(Self.normalizedDeviceID)
    }

    var privacyCleanupDeviceIDs: Set<String> {
        Set([activeDeviceID, legacyFallbackDeviceID].compactMap { $0 })
    }

    func setOptedIn(_ optedIn: Bool) {
        defaults.set(optedIn, forKey: Self.optedInKey)
    }

    func rememberActiveDeviceID(_ deviceID: String) {
        guard
            isOptedIn,
            !isPrivacyCleanupPending,
            let normalized = Self.normalizedDeviceID(deviceID)
        else {
            return
        }
        defaults.set(normalized, forKey: Self.activeDeviceIDKey)
    }

    func beginPrivacyCleanup() {
        defaults.set(false, forKey: Self.optedInKey)
        defaults.set(true, forKey: Self.cleanupPendingKey)
    }

    func completePrivacyCleanup() {
        defaults.removeObject(forKey: Self.cleanupPendingKey)
        defaults.removeObject(forKey: Self.activeDeviceIDKey)
        defaults.removeObject(forKey: Self.legacyFallbackDeviceIDKey)
    }

    private static func normalizedDeviceID(
        _ value: String
    ) -> String? {
        let normalized = value.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        return normalized.isEmpty ? nil : normalized
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
            sync: await syncAfterExplicitAuthorization(
                authorization,
                now: now,
                timezone: timezone
            )
        )
    }

    func requestAuthorization() async -> ScreenTimeAuthorizationAttempt {
        guard pairingProvider() != nil else {
            return ScreenTimeAuthorizationAttempt(
                authorization: nil,
                failureReason: "not_paired"
            )
        }
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

    func currentAuthorizationStatus() async -> ScreenTimeCollectorResult {
        await syncService.currentAuthorizationStatus()
    }

    func prepareAuthorizationRestoration(
        now: Date = Date()
    ) async -> ScreenTimeAuthorizationRestorationPreparation {
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
            let fence =
                try await syncService.authorizationRestorationFence(
                    pairing: pairing,
                    now: now
                )
            if let blockedReason = fence.blockedReason {
                return .skipped(reason: blockedReason)
            }
            return .ready(
                ScreenTimeAuthorizationRestorationContext(
                    pairing: pairing,
                    fence: fence
                )
            )
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback:
                        "ios_screen_time_authorization_preflight_failed"
                )
            )
        }
    }

    func syncAfterAuthorizationRestoration(
        _ authorization: ScreenTimeCollectorResult,
        context: ScreenTimeAuthorizationRestorationContext,
        now: Date = Date(),
        timezone: TimeZone = .current,
        trigger: ScreenTimeSyncTrigger = .authorizationChanged
    ) async -> ScreenTimeActivityLifecycleResult {
        guard authorization.permitsAggregateUpload else {
            return .skipped(
                reason: "ios_screen_time_reauthorization_required"
            )
        }
        do {
            try Task.checkCancellation()
            guard pairingProvider() == context.pairing else {
                return .failed(reason: "pairing_changed")
            }
            let refreshedFence =
                try await syncService.authorizationRestorationFence(
                    pairing: context.pairing,
                    now: now
                )
            if let blockedReason = refreshedFence.blockedReason {
                return .skipped(reason: blockedReason)
            }
            guard refreshedFence == context.fence else {
                return .skipped(
                    reason:
                        "ios_screen_time_collection_configuration_changed"
                )
            }
            try Task.checkCancellation()
            guard pairingProvider() == context.pairing else {
                return .failed(reason: "pairing_changed")
            }
            return .completed(
                try await syncService.sync(
                    pairing: context.pairing,
                    now: now,
                    timezone: timezone,
                    trigger: trigger
                )
            )
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback:
                        "ios_screen_time_authorization_restoration_failed"
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

    func syncAfterExplicitAuthorization(
        _ authorization: ScreenTimeCollectorResult,
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        guard authorization.permitsAggregateUpload else {
            return await catchUp(
                now: now,
                timezone: timezone,
                trigger: .authorizationChanged
            )
        }
        guard let pairing = pairingProvider() else {
            return await catchUp(
                now: now,
                timezone: timezone,
                trigger: .authorizationChanged
            )
        }
        do {
            try await syncService.registerAuthorizedCollector(
                pairing: pairing
            )
            try Task.checkCancellation()
            guard pairingProvider() == pairing else {
                return .failed(reason: "pairing_changed")
            }
            return .completed(
                try await syncService.sync(
                    pairing: pairing,
                    now: now,
                    timezone: timezone,
                    trigger: .authorizationChanged
                )
            )
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback:
                        "ios_screen_time_registration_failed"
                )
            )
        }
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

    func disableAndPurge(
        now: Date = Date()
    ) async -> ScreenTimeActivityLifecycleResult {
        do {
            try await syncService.disableAndPurge(now: now)
            return .skipped(reason: "ios_screen_time_disabled")
        } catch {
            return .failed(
                reason: Self.failureReason(
                    for: error,
                    fallback:
                        "ios_screen_time_disable_cleanup_failed"
                )
            )
        }
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
        if let error = error as? ScreenTimeInputControlError {
            switch error {
            case .invalidCollectorIdentity:
                return "ios_screen_time_invalid_collector_identity"
            case .invalidDescriptor:
                return "invalid_server_response"
            case .revisionConflictExhausted:
                return "input_settings_revision_conflict"
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
