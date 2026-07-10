import Foundation

/// Tiny, deliberately plain text renderers over the glance payload.
///
/// NOTE (issue #7): these strings are PLACEHOLDER plumbing. What a glance
/// surface should actually say — wording, urgency, thresholds, when to stay
/// silent — is healthcare-domain UX reserved for the domain expert
/// (docs/design/WATCH-NOTIFICATIONS.ko.md; grammar: docs/PLAN.md §8.5).
public enum GlanceFormat {
    public static func scoreText(_ score: Int?) -> String {
        score.map(String.init) ?? "--"
    }

    /// "Energy 58 · high"
    public static func energyLine(_ payload: GlancePayload) -> String {
        "Energy \(scoreText(payload.energy.score)) · \(payload.energy.confidence.rawValue)"
    }

    /// "14:00 Deep work block [high]" — times rendered in the *server's*
    /// user timezone so phone/watch clock drift never lies about the plan.
    public static func nextBlockLine(_ payload: GlancePayload) -> String? {
        payload.nextBlocks.first.map { blockLine($0, timezone: payload.timezone) }
    }

    /// Same rendering for any single block (systemLarge lists all ≤3 blocks).
    public static func blockLine(_ block: GlanceBlock, timezone: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        formatter.timeZone = TimeZone(identifier: timezone) ?? .current
        let title = block.title ?? (block.source == .proposal ? "Proposed block" : "Untitled block")
        var line = "\(formatter.string(from: block.start)) \(title)"
        if let demand = block.energyDemand {
            line += " [\(demand.rawValue)]"
        }
        return line
    }

    /// "2 alerts · Stress 82 vs baseline 55" / "No recent alerts"
    public static func alertsLine(_ payload: GlancePayload) -> String {
        let count = payload.alerts.unresolvedCount
        guard count > 0 else { return "No recent alerts" }
        let noun = count == 1 ? "alert" : "alerts"
        if let top = payload.alerts.top, !top.summary.isEmpty {
            return "\(count) \(noun) · \(top.summary)"
        }
        return "\(count) \(noun)"
    }

    /// Single-line variant for accessoryInline: "HM 58 · 2!"
    public static func inlineLine(_ payload: GlancePayload) -> String {
        var line = "HM \(scoreText(payload.energy.score))"
        if payload.alerts.unresolvedCount > 0 {
            line += " · \(payload.alerts.unresolvedCount)!"
        }
        return line
    }
}
