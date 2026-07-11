import Foundation

/// The one-glance text in the macOS status item. PLACEHOLDER vocabulary
/// (docs/design/WATCH-NOTIFICATIONS.ko.md Q3 decides numbers vs state words;
/// docs/PLAN.md §8.5): the *information* is fixed — score when known, honest
/// "--" when not, an alert marker, and an explicit stale marker so a cached
/// value never impersonates a fresh one.
public enum StatusItemText {
    /// "--" (not paired / no payload), "58", "58•" (recent alerts pending),
    /// "(58•)" (rendered from cache because the instance was unreachable).
    public static func title(payload: GlancePayload?, stale: Bool, isPaired: Bool) -> String {
        guard isPaired, let payload else { return "--" }
        var text = GlanceFormat.scoreText(payload.energy.score)
        if payload.alerts.unresolvedCount > 0 {
            text += "•"
        }
        if stale {
            text = "(\(text))"
        }
        return text
    }
}
