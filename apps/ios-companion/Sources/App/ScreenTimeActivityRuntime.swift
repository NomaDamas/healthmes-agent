import BackgroundTasks
import Foundation

/// App-lifecycle facade for the UI-neutral Screen Time engine.
///
/// Device UI code should call `requestAuthorizationAndSync()` after an
/// explicit user opt-in. The rest of the collection lifecycle is automatic:
/// foreground catch-up, pairing changes, and best-effort BGAppRefreshTask all
/// enter the same single-flight sync service and persistent outbox.
@MainActor
final class ScreenTimeActivityRuntime {
    static let shared = ScreenTimeActivityRuntime()

    /// Must match `BGTaskSchedulerPermittedIdentifiers` in project.yml.
    static let taskIdentifier =
        "com.healthmes.companion.screen-time-refresh"
    /// Source data is bucketed hourly. iOS may run substantially later.
    static let minimumInterval: TimeInterval = 60 * 60

    private let lifecycle: ScreenTimeActivityLifecycleController
    private let authorizationObserver:
        any ScreenTimeAuthorizationChangeObserving
    private var registered = false
    private var pairingObserver: NSObjectProtocol?

    private init() {
        lifecycle = ScreenTimeActivityLifecycleController(
            syncService: ScreenTimeActivitySyncService.live(),
            pairingProvider: { PairingStore.shared.load() }
        )
        authorizationObserver =
            ScreenTimeAuthorizationChangeObserverFactory.make()
    }

    init(
        lifecycle: ScreenTimeActivityLifecycleController,
        authorizationObserver:
            (any ScreenTimeAuthorizationChangeObserving)? = nil
    ) {
        self.lifecycle = lifecycle
        self.authorizationObserver =
            authorizationObserver
            ?? ScreenTimeAuthorizationChangeObserverFactory.make()
    }

    func register() {
        guard !registered else { return }
        registered = true
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.taskIdentifier,
            using: nil
        ) { [weak self] task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            Task { @MainActor [weak self] in
                self?.handle(refreshTask)
            }
        }
        pairingObserver = NotificationCenter.default.addObserver(
            forName: Notification.Name("healthmes.pairing.changed"),
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                _ = await self.lifecycle.pairingDidChange()
                self.schedule()
            }
        }
        authorizationObserver.start { [weak self] in
            guard let self else { return }
            _ = await self.lifecycle.authorizationDidChange()
            self.schedule()
        }
    }

    /// Callable seam for the future device-team settings UI.
    ///
    /// This method does not imply entitlement approval. Unsupported builds
    /// return the unavailable collector result and remain fail-closed.
    func requestAuthorizationAndSync(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeAuthorizationSyncResult {
        let result =
            await lifecycle.requestAuthorizationAndSync(
                now: now,
                timezone: timezone
            )
        schedule()
        return result
    }

    @discardableResult
    func foregroundCatchUp(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        let result = await lifecycle.catchUp(
            now: now,
            timezone: timezone
        )
        schedule()
        return result
    }

    /// UI seam for confirming the exact opaque exclusion set after a
    /// pseudonym-key or input-configuration change.
    @discardableResult
    func approveExcludedAppsAndSync(
        _ excludedAppTokens: Set<String>,
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        let result =
            await lifecycle.approveExcludedAppsAndSync(
                excludedAppTokens,
                now: now,
                timezone: timezone
            )
        schedule()
        return result
    }

    /// UI-neutral seam for a saved input-setting or retention revision.
    @discardableResult
    func inputConfigurationDidChange(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        let result = await lifecycle.configurationDidChange(
            now: now,
            timezone: timezone
        )
        schedule()
        return result
    }

    func schedule() {
        guard PairingStore.shared.load() != nil else { return }
        let request = BGAppRefreshTaskRequest(
            identifier: Self.taskIdentifier
        )
        request.earliestBeginDate = Date(
            timeIntervalSinceNow: Self.minimumInterval
        )
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // The simulator, user settings, battery policy, or the OS may
            // reject background work. Foreground activation remains the
            // deterministic catch-up opportunity.
        }
    }

    private func handle(_ task: BGAppRefreshTask) {
        schedule()
        let runner = ScreenTimeBackgroundRefreshRunner(
            operation: { [weak self] in
                guard let self else { return false }
                let result = await self.lifecycle.catchUp(
                    trigger: .backgroundRefresh
                )
                return result.completedWithoutError
            },
            completion: { success in
                task.setTaskCompleted(success: success)
            }
        )
        task.expirationHandler = {
            Task { @MainActor in
                runner.expire()
            }
        }
        runner.start()
    }
}
