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
    func cancel()
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

    func cancel() {
        BGTaskScheduler.shared.cancel(
            taskRequestWithIdentifier:
                ScreenTimeActivityRuntime.taskIdentifier
        )
    }
}

private struct ScreenTimeAuthorizationRefresh {
    let id: UUID
    let task: Task<ScreenTimeAuthorizationAttempt, Never>
    var waiterLeases: [UUID: ScreenTimeSyncCancellationLease]
}

private final class ScreenTimeAuthorizationWaiter:
    @unchecked Sendable
{
    private let lock = NSLock()
    private var continuation:
        CheckedContinuation<ScreenTimeAuthorizationAttempt, Error>?
    private var pendingResult: ScreenTimeAuthorizationAttempt?
    private var cancelled = false

    func install(
        _ continuation:
            CheckedContinuation<ScreenTimeAuthorizationAttempt, Error>
    ) {
        lock.lock()
        if cancelled {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        if let pendingResult {
            lock.unlock()
            continuation.resume(returning: pendingResult)
            return
        }
        self.continuation = continuation
        lock.unlock()
    }

    func complete(_ result: ScreenTimeAuthorizationAttempt) {
        lock.lock()
        guard !cancelled else {
            lock.unlock()
            return
        }
        if let continuation {
            self.continuation = nil
            lock.unlock()
            continuation.resume(returning: result)
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

    private var lifecycle: ScreenTimeActivityLifecycleController?
    private let lifecycleFactory:
        @MainActor () -> ScreenTimeActivityLifecycleController
    private let identityReset: @MainActor () throws -> Void
    private let authorizationObserver:
        any ScreenTimeAuthorizationChangeObserving
    private let authorizationIntentStore:
        ScreenTimeAuthorizationIntentStore
    private let backgroundTasks: any ScreenTimeBackgroundTaskManaging
    private let notificationCenter: NotificationCenter
    private let nowProvider: @MainActor () -> Date
    private let timezoneProvider: @MainActor () -> TimeZone
    private var registered = false
    private var pairingObserver: NSObjectProtocol?
    private var timezoneObserver: NSObjectProtocol?
    private var authorizationRefresh: ScreenTimeAuthorizationRefresh?
    private var explicitAuthorizationTasks:
        [UUID: Task<ScreenTimeAuthorizationSyncResult, Never>] = [:]
    private var automaticTasks: [UUID: Task<Void, Never>] = [:]
    private var backgroundRunners:
        [UUID: ScreenTimeBackgroundRefreshRunner] = [:]

    private init() {
        let intentStore = ScreenTimeAuthorizationIntentStore()
        lifecycle = nil
        lifecycleFactory = {
            ScreenTimeActivityLifecycleController(
                syncService: ScreenTimeActivitySyncService.live(
                    authorizationIntentStore: intentStore
                ),
                pairingProvider: { PairingStore.shared.load() }
            )
        }
        identityReset = {
            try ScreenTimePseudonymKeyStore().delete()
        }
        authorizationObserver =
            ScreenTimeAuthorizationChangeObserverFactory.make()
        authorizationIntentStore = intentStore
        backgroundTasks = LiveScreenTimeBackgroundTaskManager()
        notificationCenter = .default
        nowProvider = Date.init
        timezoneProvider = { .current }
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
        notificationCenter: NotificationCenter = .default,
        nowProvider: @escaping @MainActor () -> Date = Date.init,
        timezoneProvider:
            @escaping @MainActor () -> TimeZone = { .current },
        identityReset:
            @escaping @MainActor () throws -> Void = {}
    ) {
        self.lifecycle = lifecycle
        lifecycleFactory = { lifecycle }
        self.identityReset = identityReset
        self.authorizationObserver =
            authorizationObserver
            ?? ScreenTimeAuthorizationChangeObserverFactory.make()
        self.authorizationIntentStore = authorizationIntentStore
        self.backgroundTasks =
            backgroundTasks
            ?? LiveScreenTimeBackgroundTaskManager()
        self.notificationCenter = notificationCenter
        self.nowProvider = nowProvider
        self.timezoneProvider = timezoneProvider
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
                await self.cancelAndWaitForExplicitAuthorizationWork()
                await self.runAutomaticCatchUp(
                    trigger: .inputConfigurationChanged
                )
            }
        }
        timezoneObserver = notificationCenter.addObserver(
            forName: Notification.Name.NSSystemTimeZoneDidChange,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                await self.runAutomaticCatchUp(
                    trigger: .inputConfigurationChanged
                )
            }
        }
        authorizationObserver.start { [weak self] in
            guard let self else { return }
            await self.runAutomaticCatchUp(
                trigger: .authorizationChanged
            )
        }
        if authorizationIntentStore.isOptedIn {
            Task { @MainActor [weak self] in
                guard let self else { return }
                await self.runAutomaticCatchUp(
                    trigger: .authorizationChanged
                )
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
        if authorizationIntentStore.isPrivacyCleanupPending {
            let cleanup = await completePrivacyCleanup(now: now)
            if case .failed = cleanup {
                return ScreenTimeAuthorizationSyncResult(
                    authorization: nil,
                    sync: cleanup
                )
            }
        }
        authorizationIntentStore.setOptedIn(true)
        let taskID = UUID()
        let task = Task { @MainActor [weak self] in
            guard let self else {
                return ScreenTimeAuthorizationSyncResult(
                    authorization: nil,
                    sync: .failed(reason: "cancelled")
                )
            }
            return await self.authorizationAndSync(
                now: now,
                timezone: timezone,
                trigger: .authorizationChanged
            )
        }
        explicitAuthorizationTasks[taskID] = task
        let result = await withTaskCancellationHandler {
            await task.value
        } onCancel: {
            task.cancel()
        }
        explicitAuthorizationTasks.removeValue(forKey: taskID)
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

    /// Device-team seam for applying the local privacy boundary after the
    /// user disables this input. This does not revoke Apple's system grant.
    @discardableResult
    func clearAuthorizationOptIn(
        now: Date = Date()
    ) async -> ScreenTimeActivityLifecycleResult {
        // Resolve the opted-in cleanup identity before setting the persistent
        // pending fence. This never creates identity for a user who has not
        // opted in because the live factory remains fail-closed in that case.
        let cleanupLifecycle = lifecycleController()
        // Persist intent first so every entry point rejects new work while
        // in-flight pipelines are being detached and purged.
        authorizationIntentStore.beginPrivacyCleanup()
        backgroundTasks.cancel()

        let tasks = Array(automaticTasks.values)
        automaticTasks.removeAll()
        tasks.forEach { $0.cancel() }

        let runners = Array(backgroundRunners.values)
        backgroundRunners.removeAll()
        runners.forEach { $0.expire() }

        await cancelAndWaitForExplicitAuthorizationWork()
        for task in tasks {
            _ = await task.result
        }
        for runner in runners {
            await runner.cancelAndWait()
        }
        return await completePrivacyCleanup(
            now: now,
            using: cleanupLifecycle
        )
    }

    private func completePrivacyCleanup(
        now: Date,
        using cleanupLifecycle:
            ScreenTimeActivityLifecycleController? = nil
    ) async -> ScreenTimeActivityLifecycleResult {
        let result =
            await (cleanupLifecycle ?? lifecycleController())
                .disableAndPurge(now: now)
        var identityCleanupFailed = false
        do {
            try identityReset()
        } catch {
            identityCleanupFailed = true
        }
        lifecycle = nil
        if case .failed = result {
            return result
        }
        if identityCleanupFailed {
            return .failed(
                reason: "ios_screen_time_identity_cleanup_failed"
            )
        }
        authorizationIntentStore.completePrivacyCleanup()
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
        guard authorizationIntentStore.isOptedIn else {
            return .skipped(reason: "ios_screen_time_not_opted_in")
        }
        let result =
            await lifecycleController()
                .approveExcludedAppsAndSync(
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
        guard authorizationIntentStore.isOptedIn else {
            return .skipped(reason: "ios_screen_time_not_opted_in")
        }
        let result = await lifecycleController().configurationDidChange(
            now: now,
            timezone: timezone
        )
        schedule()
        return result
    }

    func schedule() {
        guard authorizationIntentStore.isOptedIn else { return }
        backgroundTasks.schedule()
    }

    private func handle(
        _ task: any ScreenTimeBackgroundRefreshTask
    ) {
        guard authorizationIntentStore.isOptedIn else {
            task.setTaskCompleted(success: true)
            return
        }
        schedule()
        let runnerID = UUID()
        let runner = ScreenTimeBackgroundRefreshRunner(
            operation: { [weak self] in
                guard let self else { return false }
                let result = await self.automaticCatchUp(
                    now: self.nowProvider(),
                    timezone: self.timezoneProvider(),
                    trigger: .backgroundRefresh
                )
                return result.completedWithoutError
            },
            completion: { [weak self] success in
                task.setTaskCompleted(success: success)
                self?.backgroundRunners.removeValue(
                    forKey: runnerID
                )
            }
        )
        backgroundRunners[runnerID] = runner
        task.expirationHandler = {
            Task { @MainActor in
                runner.expire()
            }
        }
        runner.start()
    }

    private func runAutomaticCatchUp(
        trigger: ScreenTimeSyncTrigger
    ) async {
        guard authorizationIntentStore.isOptedIn else {
            return
        }
        let taskID = UUID()
        let now = nowProvider()
        let timezone = timezoneProvider()
        let task = Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                self.automaticTasks.removeValue(forKey: taskID)
            }
            _ = await self.automaticCatchUp(
                now: now,
                timezone: timezone,
                trigger: trigger
            )
            guard
                !Task.isCancelled,
                self.authorizationIntentStore.isOptedIn
            else {
                return
            }
            self.schedule()
        }
        automaticTasks[taskID] = task
        _ = await task.result
    }

    func waitForAutomaticWork() async {
        while !automaticTasks.isEmpty {
            let tasks = Array(automaticTasks.values)
            for task in tasks {
                _ = await task.result
            }
        }
    }

    private func automaticCatchUp(
        now: Date = Date(),
        timezone: TimeZone = .current,
        trigger: ScreenTimeSyncTrigger = .routine
    ) async -> ScreenTimeActivityLifecycleResult {
        guard authorizationIntentStore.isOptedIn else {
            return .skipped(
                reason: "ios_screen_time_not_opted_in"
            )
        }
        return await lifecycleController().catchUp(
            now: now,
            timezone: timezone,
            trigger: trigger
        )
    }

    private func cancelAndWaitForExplicitAuthorizationWork() async {
        let explicitTasks = Array(
            explicitAuthorizationTasks.values
        )
        explicitAuthorizationTasks.removeAll()
        explicitTasks.forEach { $0.cancel() }

        let authorizationTask = authorizationRefresh?.task
        authorizationRefresh = nil
        authorizationTask?.cancel()

        if let authorizationTask {
            _ = await authorizationTask.value
        }
        for task in explicitTasks {
            _ = await task.value
        }
    }

    private func authorizationAndSync(
        now: Date,
        timezone: TimeZone,
        trigger: ScreenTimeSyncTrigger
    ) async -> ScreenTimeAuthorizationSyncResult {
        let attempt: ScreenTimeAuthorizationAttempt
        do {
            attempt = try await authorizationAttempt(trigger: trigger)
        } catch {
            return ScreenTimeAuthorizationSyncResult(
                authorization: nil,
                sync: .failed(reason: "cancelled")
            )
        }
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
        guard authorizationIntentStore.isOptedIn else {
            return ScreenTimeAuthorizationSyncResult(
                authorization: attempt.authorization,
                sync: .skipped(
                    reason: "ios_screen_time_not_opted_in"
                )
            )
        }
        return ScreenTimeAuthorizationSyncResult(
            authorization: authorization,
            sync: await lifecycleController()
                .syncAfterExplicitAuthorization(
                    authorization,
                    now: now,
                    timezone: timezone
                )
        )
    }

    private func authorizationAttempt(
        trigger: ScreenTimeSyncTrigger
    ) async throws -> ScreenTimeAuthorizationAttempt {
        let waiterID = UUID()
        let refreshID: UUID
        let task: Task<ScreenTimeAuthorizationAttempt, Never>
        if var authorizationRefresh {
            authorizationRefresh.waiterLeases[waiterID] =
                trigger.cancellationLease
            self.authorizationRefresh = authorizationRefresh
            refreshID = authorizationRefresh.id
            task = authorizationRefresh.task
        } else {
            let id = UUID()
            let lifecycle = lifecycleController()
            let newTask = Task { @MainActor in
                await lifecycle.requestAuthorization()
            }
            authorizationRefresh = ScreenTimeAuthorizationRefresh(
                id: id,
                task: newTask,
                waiterLeases: [
                    waiterID: trigger.cancellationLease
                ]
            )
            refreshID = id
            task = newTask
        }

        do {
            let result =
                try await valueIsolatingAuthorizationCancellation(
                    from: task
                )
            removeAuthorizationWaiter(
                refreshID: refreshID,
                waiterID: waiterID,
                completed: true
            )
            return result
        } catch {
            let cancelledTask = removeAuthorizationWaiter(
                refreshID: refreshID,
                waiterID: waiterID,
                cancelledByCaller: Task.isCancelled
            )
            if let cancelledTask {
                _ = await cancelledTask.value
            }
            throw error
        }
    }

    private func valueIsolatingAuthorizationCancellation(
        from task: Task<ScreenTimeAuthorizationAttempt, Never>
    ) async throws -> ScreenTimeAuthorizationAttempt {
        let waiter = ScreenTimeAuthorizationWaiter()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                continuation in
                waiter.install(continuation)
                Task {
                    waiter.complete(await task.value)
                }
            }
        } onCancel: {
            waiter.cancel()
        }
    }

    @discardableResult
    private func removeAuthorizationWaiter(
        refreshID: UUID,
        waiterID: UUID,
        cancelledByCaller: Bool = false,
        completed: Bool = false
    ) -> Task<ScreenTimeAuthorizationAttempt, Never>? {
        guard var authorizationRefresh,
            authorizationRefresh.id == refreshID,
            let lease =
                authorizationRefresh.waiterLeases.removeValue(
                    forKey: waiterID
                )
        else {
            return nil
        }
        if cancelledByCaller,
            lease == .background,
            authorizationRefresh.waiterLeases.isEmpty
        {
            self.authorizationRefresh = nil
            authorizationRefresh.task.cancel()
            return authorizationRefresh.task
        }
        if authorizationRefresh.waiterLeases.isEmpty, completed {
            self.authorizationRefresh = nil
        } else {
            self.authorizationRefresh = authorizationRefresh
        }
        return nil
    }

    private func lifecycleController()
        -> ScreenTimeActivityLifecycleController
    {
        if let lifecycle {
            return lifecycle
        }
        let created = lifecycleFactory()
        lifecycle = created
        return created
    }
}
