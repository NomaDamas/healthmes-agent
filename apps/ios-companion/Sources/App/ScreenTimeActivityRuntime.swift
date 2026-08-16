import BackgroundTasks
import Foundation

@MainActor
protocol ScreenTimeBackgroundRefreshTask: AnyObject {
    var expirationHandler: (() -> Void)? { get set }
    func setTaskCompleted(success: Bool)
}

@MainActor
protocol ScreenTimeBackgroundTaskManaging: AnyObject {
    func register(
        handler:
            @escaping @MainActor
            (any ScreenTimeBackgroundRefreshTask) -> Void
    )
    func schedule()
}

@MainActor
private final class LiveScreenTimeBackgroundRefreshTask:
    ScreenTimeBackgroundRefreshTask
{
    private let task: BGAppRefreshTask

    init(task: BGAppRefreshTask) {
        self.task = task
    }

    var expirationHandler: (() -> Void)? {
        get { task.expirationHandler }
        set { task.expirationHandler = newValue }
    }

    func setTaskCompleted(success: Bool) {
        task.setTaskCompleted(success: success)
    }
}

@MainActor
private final class LiveScreenTimeBackgroundTaskManager:
    ScreenTimeBackgroundTaskManaging
{
    func register(
        handler:
            @escaping @MainActor
            (any ScreenTimeBackgroundRefreshTask) -> Void
    ) {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier:
                ScreenTimeActivityRuntime.taskIdentifier,
            using: nil
        ) { task in
            Task { @MainActor in
                guard let refreshTask = task as? BGAppRefreshTask else {
                    task.setTaskCompleted(success: false)
                    return
                }
                handler(
                    LiveScreenTimeBackgroundRefreshTask(
                        task: refreshTask
                    )
                )
            }
        }
    }

    func schedule() {
        guard PairingStore.shared.load() != nil else { return }
        let request = BGAppRefreshTaskRequest(
            identifier: ScreenTimeActivityRuntime.taskIdentifier
        )
        request.earliestBeginDate = Date(
            timeIntervalSinceNow:
                ScreenTimeActivityRuntime.minimumInterval
        )
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // Foreground activation remains the deterministic catch-up
            // opportunity when the OS declines background work.
        }
    }
}

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
    private let authorizationIntentStore:
        ScreenTimeAuthorizationIntentStore
    private let backgroundTasks: any ScreenTimeBackgroundTaskManaging
    private let notificationCenter: NotificationCenter
    private var registered = false
    private var pairingObserver: NSObjectProtocol?
    private var authorizationRefresh:
        (
            id: UUID,
            task: Task<ScreenTimeAuthorizationAttempt, Never>
        )?

    private init() {
        lifecycle = ScreenTimeActivityLifecycleController(
            syncService: ScreenTimeActivitySyncService.live(),
            pairingProvider: { PairingStore.shared.load() }
        )
        authorizationObserver =
            ScreenTimeAuthorizationChangeObserverFactory.make()
        authorizationIntentStore =
            ScreenTimeAuthorizationIntentStore()
        backgroundTasks = LiveScreenTimeBackgroundTaskManager()
        notificationCenter = .default
    }

    init(
        lifecycle: ScreenTimeActivityLifecycleController,
        authorizationObserver:
            (any ScreenTimeAuthorizationChangeObserving)? = nil,
        authorizationIntentStore:
            ScreenTimeAuthorizationIntentStore =
                ScreenTimeAuthorizationIntentStore(),
        backgroundTasks:
            (any ScreenTimeBackgroundTaskManaging)? = nil,
        notificationCenter: NotificationCenter = .default
    ) {
        self.lifecycle = lifecycle
        self.authorizationObserver =
            authorizationObserver
            ?? ScreenTimeAuthorizationChangeObserverFactory.make()
        self.authorizationIntentStore = authorizationIntentStore
        self.backgroundTasks =
            backgroundTasks
            ?? LiveScreenTimeBackgroundTaskManager()
        self.notificationCenter = notificationCenter
    }

    func register() {
        guard !registered else { return }
        registered = true
        backgroundTasks.register { [weak self] task in
            self?.handle(task)
        }
        pairingObserver = notificationCenter.addObserver(
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
        if authorizationIntentStore.isOptedIn {
            Task { @MainActor [weak self] in
                guard let self else { return }
                _ = await self.automaticCatchUp()
                self.schedule()
            }
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
        authorizationIntentStore.setOptedIn(true)
        let result =
            await authorizationAndSync(
                now: now,
                timezone: timezone,
                trigger: .authorizationChanged
            )
        schedule()
        return result
    }

    @discardableResult
    func foregroundCatchUp(
        now: Date = Date(),
        timezone: TimeZone = .current
    ) async -> ScreenTimeActivityLifecycleResult {
        let result = await automaticCatchUp(
            now: now,
            timezone: timezone,
            trigger: .routine
        )
        schedule()
        return result
    }

    /// Device-team seam for stopping automatic status restoration after the
    /// user disables this input. This does not revoke Apple's system grant.
    func clearAuthorizationOptIn() {
        authorizationIntentStore.setOptedIn(false)
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
        backgroundTasks.schedule()
    }

    private func handle(
        _ task: any ScreenTimeBackgroundRefreshTask
    ) {
        schedule()
        let runner = ScreenTimeBackgroundRefreshRunner(
            operation: { [weak self] in
                guard let self else { return false }
                let result = await self.automaticCatchUp(
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

    private func automaticCatchUp(
        now: Date = Date(),
        timezone: TimeZone = .current,
        trigger: ScreenTimeSyncTrigger = .routine
    ) async -> ScreenTimeActivityLifecycleResult {
        guard authorizationIntentStore.isOptedIn else {
            return await lifecycle.catchUp(
                now: now,
                timezone: timezone,
                trigger: trigger
            )
        }
        let syncTrigger: ScreenTimeSyncTrigger =
            trigger == .backgroundRefresh
            ? .backgroundRefresh
            : .authorizationChanged
        return await authorizationAndSync(
            now: now,
            timezone: timezone,
            trigger: syncTrigger
        ).sync
    }

    private func authorizationAndSync(
        now: Date,
        timezone: TimeZone,
        trigger: ScreenTimeSyncTrigger
    ) async -> ScreenTimeAuthorizationSyncResult {
        let attempt = await authorizationAttempt()
        guard !Task.isCancelled else {
            return ScreenTimeAuthorizationSyncResult(
                authorization: attempt.authorization,
                sync: .failed(reason: "cancelled")
            )
        }
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
            sync: await lifecycle.catchUp(
                now: now,
                timezone: timezone,
                trigger: trigger
            )
        )
    }

    private func authorizationAttempt()
        async -> ScreenTimeAuthorizationAttempt
    {
        if let authorizationRefresh {
            return await authorizationRefresh.task.value
        }
        let id = UUID()
        let lifecycle = lifecycle
        let task = Task { @MainActor in
            await lifecycle.requestAuthorization()
        }
        authorizationRefresh = (id: id, task: task)
        let result = await task.value
        if authorizationRefresh?.id == id {
            authorizationRefresh = nil
        }
        return result
    }
}
