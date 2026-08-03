import Foundation
import UserNotifications

/// One refresh pipeline shared by the BGAppRefreshTask and foreground
/// activation (so the alert→notification loop is fully exercisable even
/// when iOS grants zero background budget):
///
///   1. `GET /v1/alerts` (24 h window — glance semantics),
///   2. diff against SeenAlertsStore → local notification per NEW alert,
///      with the exact server-correlated schedule proposal when present,
///   3. badge = unresolved count,
///   4. `GET /v1/briefing/glance` (ETag-cheap) → Live Activity sync.
///
/// Every step tolerates failure independently — an unreachable instance
/// must never crash a background task or spam retries (the next poll picks
/// up where this one left off).
actor RefreshCoordinator {
    static let shared = RefreshCoordinator()

    private let api = HealthMesAPI()
    private let glanceClient = GlanceClient()
    private let seenStore = SeenAlertsStore.shared

    /// Returns true when at least one network step succeeded (BG task
    /// success signal).
    @discardableResult
    func sync(isForeground: Bool, now: Date = Date()) async -> Bool {
        guard PairingStore.shared.load() != nil else { return false }
        var anySuccess = false

        // -- Alerts → notifications --------------------------------------
        if let page = try? await api.listAlerts(hours: 24) {
            anySuccess = true
            await notifyNewAlerts(page.data)
            NotificationManager.shared.setBadge(page.pagination.totalCount)
        }

        // -- Glance → Live Activity ---------------------------------------
        if let snapshot = try? await glanceClient.fetch(now: now) {
            anySuccess = true
            await LiveActivityController.shared.sync(
                payload: snapshot.payload, isForeground: isForeground, now: now
            )
        }

        return anySuccess
    }

    private func notifyNewAlerts(_ alerts: [AlertItem]) async {
        let status = await NotificationManager.shared.authorizationStatus()
        guard status == .authorized || status == .provisional else {
            // Not authorized: remember what exists so enabling notifications
            // later never dumps the whole backlog at once.
            seenStore.markSeen(alerts)
            return
        }
        let unseen = seenStore.unseen(from: alerts)
        guard !unseen.isEmpty else { return }

        // Oldest first so notification order matches fired order.
        for alert in unseen.reversed() {
            let content = AlertNotificationContent.from(alert: alert)
            await NotificationManager.shared.post(content: content)
        }
        seenStore.markSeen(unseen)
    }
}
