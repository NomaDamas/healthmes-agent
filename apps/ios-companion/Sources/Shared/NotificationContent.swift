import Foundation

// The docs/PLAN.md §8.5 notification grammar, as data (parity with the
// Android companion's NotificationGrammar.kt):
//
//   [observation, 1 line]   -> notification title
//   [evidence, 1 line]      -> body line 1
//   [proposal, 1 line]      -> body line 2
//   [buttons]  ✅ Apply / ✏️ Adjust / ❌ Keep as-is   -> UNNotificationActions
//   [link]     Why this? -> decision-viewer deep link -> userInfo route
//
// Pure Foundation so the mapping from a `GET /v1/alerts` item is unit-
// testable and reusable on macOS. Surfaces may DROP whole lines when space
// runs out, but never reorder or invent lines (WATCH-NOTIFICATIONS.ko.md
// §1.1).
//
// PLACEHOLDER WORDING: the evidence-line rendering (sorted "key value"
// pairs) and every fallback string are engineering placeholders proving the
// plumbing. The real copy — urgency ladders, vocabulary, when to stay
// silent — is the healthcare domain expert's deliverable
// (docs/design/WATCH-NOTIFICATIONS.ko.md Q2/Q3/Q5).
public struct AlertNotificationContent: Equatable {
    /// Category with ✅ Apply / ✏️ Adjust / ❌ Keep actions — used only when
    /// a pending schedule proposal is attached, so every button maps to a
    /// REAL endpoint call instead of a stub.
    public static let actionableCategoryID = "HEALTHMES_ALERT_ACTIONABLE"
    /// Category without proposal actions (nothing pending to act on).
    public static let infoCategoryID = "HEALTHMES_ALERT_INFO"

    public static let userInfoAlertID = "healthmes_alert_id"
    public static let userInfoDecisionURL = "healthmes_decision_url"
    public static let userInfoProposalID = "healthmes_proposal_id"

    /// Observation line (§8.5 line 1).
    public let title: String
    /// Evidence + proposal lines joined by a newline (either may be absent).
    public let body: String
    public let categoryID: String
    /// Stable per-rule thread so repeat firings of one rule group together.
    public let threadID: String
    /// Routing payload: alert id, optional decision link, optional pending
    /// proposal id (string values only — plist-safe).
    public let userInfo: [String: String]

    /// Deterministic placeholder rendering of the evidence facts: keys
    /// sorted, "key value" pairs joined with " · ". Never invents data.
    public static func evidenceLine(_ evidence: [String: JSONValue]?) -> String? {
        guard let evidence, !evidence.isEmpty else { return nil }
        return
            evidence
            .sorted { $0.key < $1.key }
            .map { "\($0.key) \($0.value.displayText)" }
            .joined(separator: " · ")
    }

    /// Build notification content from one alert-history item.
    /// `pendingProposalID` (when the refresh loop found a proposal awaiting
    /// confirmation) upgrades the category to the actionable one.
    public static func from(
        alert: AlertItem,
        pendingProposalID: UUID? = nil
    ) -> AlertNotificationContent {
        var bodyLines: [String] = []
        if let evidence = evidenceLine(alert.evidence) {
            bodyLines.append(evidence)
        }
        if let proposal = alert.proposal, !proposal.isEmpty {
            bodyLines.append(proposal)
        }

        var userInfo: [String: String] = [
            userInfoAlertID: alert.id.uuidString.lowercased()
        ]
        if let decisionUrl = alert.decisionUrl {
            userInfo[userInfoDecisionURL] = decisionUrl
        }
        if let pendingProposalID {
            userInfo[userInfoProposalID] = pendingProposalID.uuidString.lowercased()
        }

        return AlertNotificationContent(
            title: alert.summary,
            body: bodyLines.joined(separator: "\n"),
            categoryID: pendingProposalID != nil ? actionableCategoryID : infoCategoryID,
            threadID: alert.ruleId,
            userInfo: userInfo
        )
    }
}
