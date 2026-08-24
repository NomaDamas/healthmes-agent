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
        guard let pairingSnapshot = PairingStore.shared.load() else {
            return false
        }
        var anySuccess = false

        // -- Alerts → notifications --------------------------------------
        if let page = try? await api.listAlerts(
            pairing: pairingSnapshot,
            hours: 24
        ) {
            guard
                let relayLease = PairingRelayGate.shared.begin(
                    pairing: pairingSnapshot
                )
            else {
                return false
            }
            defer {
                PairingRelayGate.shared.end(relayLease)
            }
            anySuccess = true
            await notifyNewAlerts(page.data, pairing: pairingSnapshot)
            guard PairingStore.shared.load() == pairingSnapshot else {
                return false
            }
            NotificationManager.shared.setBadge(page.pagination.totalCount)
            await DecisionLiveActivityController.shared.sync(
                alerts: page.data,
                pairing: pairingSnapshot,
                isForeground: isForeground,
                now: now
            )
        }

        // -- Glance → Live Activity ---------------------------------------
        if let snapshot = try? await glanceClient.fetch(
            pairing: pairingSnapshot,
            now: now
        ) {
            guard
                let relayLease = PairingRelayGate.shared.begin(
                    pairing: pairingSnapshot
                )
            else {
                return false
            }
            defer {
                PairingRelayGate.shared.end(relayLease)
            }
            anySuccess = true
            await LiveActivityController.shared.sync(
                payload: snapshot.payload, isForeground: isForeground, now: now
            )
        }

        return anySuccess
    }

    private func notifyNewAlerts(
        _ alerts: [AlertItem],
        pairing: Pairing
    ) async {
        let status = await NotificationManager.shared.authorizationStatus()
        guard PairingStore.shared.load() == pairing else { return }
        guard status == .authorized || status == .provisional else {
            // Not authorized: remember what exists so enabling notifications
            // later never dumps the whole backlog at once.
            seenStore.primeWithoutNotifying(alerts)
            return
        }
        let unseen = seenStore.unseenOrPrime(from: alerts)
        guard !unseen.isEmpty else { return }
        guard
            let identity = PairingStore.shared.cacheIdentity(for: pairing)
        else {
            return
        }

        // Oldest first so notification order matches fired order.
        var posted: [AlertItem] = []
        for alert in unseen.reversed() {
            guard PairingStore.shared.load() == pairing else { return }
            let content = AlertNotificationContent.from(
                alert: alert,
                pairingFingerprint: identity.fingerprint,
                pairingGeneration: identity.generation
            )
            if await NotificationManager.shared.post(content: content) {
                posted.append(alert)
            }
        }
        guard PairingStore.shared.load() == pairing else { return }
        seenStore.markSeen(posted)
    }
}
