import Foundation

/// Remembers which alert revisions already produced a local notification, so the
/// polling loop (BGAppRefreshTask + foreground sync) notifies each pushed
/// alert once per actionable revision. App Group defaults — the state survives relaunches
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
        let seen = migrateLegacyIDs(using: alerts)
        return alerts.filter {
            let id = $0.id.uuidString.lowercased()
            let revision = revisionKey(for: $0)
            if $0.proposalId == nil {
                return !seen.contains(where: { $0.hasPrefix("\(id):") })
            }
            return !seen.contains(revision)
        }
    }

    /// Record revisions as notified, newest kept when the cap trims.
    public func markSeen(_ alerts: [AlertItem]) {
        guard !alerts.isEmpty else { return }
        var ordered = defaults.stringArray(forKey: Self.defaultsKey) ?? []
        for alert in alerts {
            let legacyID = alert.id.uuidString.lowercased()
            let revision = revisionKey(for: alert)
            ordered.removeAll { $0 == legacyID }
            if let index = ordered.firstIndex(of: revision) {
                ordered.remove(at: index)
            }
            ordered.insert(revision, at: 0)
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

    private func revisionKey(for alert: AlertItem) -> String {
        let id = alert.id.uuidString.lowercased()
        let proposal = alert.proposalId?.uuidString.lowercased() ?? "informational"
        return "\(id):\(proposal)"
    }

    private func migrateLegacyIDs(using alerts: [AlertItem]) -> Set<String> {
        var ordered = defaults.stringArray(forKey: Self.defaultsKey) ?? []
        let current = Dictionary(uniqueKeysWithValues: alerts.map {
            let id = $0.id.uuidString.lowercased()
            return (id, "\(id):informational")
        })
        var changed = false
        ordered = ordered.map { stored in
            guard !stored.contains(":"), let revision = current[stored] else {
                return stored
            }
            changed = true
            return revision
        }
        if changed {
            var unique: [String] = []
            for revision in ordered where !unique.contains(revision) {
                unique.append(revision)
            }
            ordered = unique
            defaults.set(ordered, forKey: Self.defaultsKey)
        }
        return Set(ordered)
    }
}
