import BackgroundTasks
import Foundation

/// BGAppRefreshTask plumbing for the native alert loop (issue #10).
///
/// HONESTY (documented in README.md): iOS decides when — and whether — a
/// refresh task runs. Real-world cadence lands anywhere between "every 15
/// minutes" and "a few times a day", tied to usage patterns and battery.
/// Combined with the server's 5-minute cache this makes native
/// notifications a best-effort convenience mirror; Telegram remains the
/// guaranteed-delivery alert channel until a push relay exists (deliberately
/// out of scope — local-first).
final class BackgroundRefreshManager {
    static let shared = BackgroundRefreshManager()

    /// Must match BGTaskSchedulerPermittedIdentifiers in project.yml.
    static let taskIdentifier = "com.healthmes.companion.refresh"
    /// The endpoint caches for 5 minutes; 15 minutes matches the WidgetKit
    /// floor used across the companions (never sooner, per the glance
    /// budget policy).
    static let minimumInterval: TimeInterval = 15 * 60

    private init() {}

    /// Call before the app finishes launching (App.init).
    func register() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.taskIdentifier,
            using: nil
        ) { [weak self] task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self?.handle(refreshTask)
        }
    }

    /// Schedule the next run; safe to call repeatedly (one pending request
    /// per identifier). Errors are expected on simulators (BGTaskScheduler
    /// is unavailable there) and simply mean "no background polling here".
    func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: Self.taskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Self.minimumInterval)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // Simulator / Low Power Mode / user disabled Background App
            // Refresh: polling continues on foreground activation only.
        }
    }

    private func handle(_ task: BGAppRefreshTask) {
        // Always keep the chain alive first — even if this run fails.
        schedule()

        let work = Task {
            let success = await RefreshCoordinator.shared.sync(isForeground: false)
            task.setTaskCompleted(success: success)
        }
        task.expirationHandler = {
            work.cancel()
            task.setTaskCompleted(success: false)
        }
    }
}
