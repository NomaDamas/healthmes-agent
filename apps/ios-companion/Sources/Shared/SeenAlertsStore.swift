import Foundation

/// Remembers which alert ids already produced a local notification, so the
/// polling loop (BGAppRefreshTask + foreground sync) notifies each pushed
/// alert exactly once. App Group defaults — the state survives relaunches
/// and is honest across processes.
///
/// The server is the real noise gate (quiet hours / cooldown / daily budget,
/// PLAN §11 — enforced before an alert ever reaches `GET /v1/alerts`); this
/// store only prevents the CLIENT from re-announcing what it already showed.
public final class SeenAlertsStore {
    public static let shared = SeenAlertsStore()

    static let defaultsKey = "healthmes.alerts.notified-ids"
    /// Alert history is budget-capped server-side (≤8/day), so a small cap
    /// covers weeks while keeping the defaults payload tiny.
    static let capacity = 200

    private let defaults: UserDefaults

    public init(defaults: UserDefaults = AppGroup.userDefaults) {
        self.defaults = defaults
    }

    public func seenIDs() -> Set<String> {
        Set(defaults.stringArray(forKey: Self.defaultsKey) ?? [])
    }

    /// Alerts (newest first, as the endpoint returns them) not yet notified.
    public func unseen(from alerts: [AlertItem]) -> [AlertItem] {
        let seen = seenIDs()
        return alerts.filter { !seen.contains($0.id.uuidString.lowercased()) }
    }

    /// Record ids as notified, newest kept when the cap trims.
    public func markSeen(_ alerts: [AlertItem]) {
        guard !alerts.isEmpty else { return }
        var ordered = defaults.stringArray(forKey: Self.defaultsKey) ?? []
        for alert in alerts {
            let id = alert.id.uuidString.lowercased()
            if let index = ordered.firstIndex(of: id) {
                ordered.remove(at: index)
            }
            ordered.insert(id, at: 0)
        }
        if ordered.count > Self.capacity {
            ordered.removeLast(ordered.count - Self.capacity)
        }
        defaults.set(ordered, forKey: Self.defaultsKey)
    }

    /// First launch with an already-populated history must not fire a
    /// notification storm: mark everything current as seen without
    /// notifying. Called once when notifications are first enabled.
    public func primeWithoutNotifying(_ alerts: [AlertItem]) {
        markSeen(alerts)
    }

    public func clear() {
        defaults.removeObject(forKey: Self.defaultsKey)
    }
}
