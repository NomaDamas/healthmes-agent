import Foundation

public enum AlertNotificationActionID {
    public static let yes = "HEALTHMES_YES"
    public static let no = "HEALTHMES_NO"
    public static let speak = "HEALTHMES_SPEAK"
    // Keep handling already-delivered notifications from older builds.
    public static let legacyAlternative = "HEALTHMES_ALTERNATIVE"
}

public enum SpeakCommandSyncKeys {
    // Keep the wire key stable so phones and watches on adjacent builds
    // continue to exchange dictated instructions.
    public static let command = "healthmes_alternative_command"
}

public enum SpeakCommand {
    public static func compose(
        userText: String,
        proposalID: UUID?,
        title: String?,
        proposedAction: String?
    ) -> String {
        let cleanText = userText.trimmingCharacters(in: .whitespacesAndNewlines)
        var context: [String] = []
        if let proposalID {
            context.append("proposal:\(proposalID.uuidString.lowercased())")
        }
        if let title {
            let clean = title.trimmingCharacters(in: .whitespacesAndNewlines)
            if !clean.isEmpty { context.append("title:\(clean)") }
        }
        if let proposedAction {
            let clean = proposedAction.trimmingCharacters(in: .whitespacesAndNewlines)
            if !clean.isEmpty { context.append("action:\(clean)") }
        }

        let header = context.isEmpty
            ? "Spoken instruction"
            : "Spoken instruction [\(context.joined(separator: " | "))]"
        return cleanText.isEmpty ? header : "\(header)\n\(cleanText)"
    }
}

// The docs/PLAN.md §8.5 notification grammar, as data (parity with the
// Android companion's NotificationGrammar.kt):
//
//   [decision, 1 line]      -> notification title
//   [result, 1 line]        -> notification body
//   [buttons]  No / Yes / Speak -> UNNotificationActions
//   [details]  Why / change -> userInfo for expanded or tapped surfaces
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
    /// Category with No / Yes / Speak actions — used only when
    /// a pending schedule proposal is attached, so every button maps to a
    /// REAL endpoint call instead of a stub.
    public static let actionableCategoryID = "HEALTHMES_ALERT_ACTIONABLE"
    /// Category without proposal actions (nothing pending to act on).
    public static let infoCategoryID = "HEALTHMES_ALERT_INFO"

    public static let userInfoAlertID = "healthmes_alert_id"
    public static let userInfoDecisionURL = "healthmes_decision_url"
    public static let userInfoProposalID = "healthmes_proposal_id"
    public static let userInfoDecisionTitle = "healthmes_decision_title"
    public static let userInfoDecisionObservation = "healthmes_decision_observation"
    public static let userInfoDecisionEvidence = "healthmes_decision_evidence"
    public static let userInfoDecisionAction = "healthmes_decision_action"
    public static let userInfoDecisionCompactPrompt = "healthmes_decision_compact_prompt"
    public static let userInfoDecisionBefore = "healthmes_decision_before"
    public static let userInfoDecisionAfter = "healthmes_decision_after"
    public static let userInfoDecisionEndsAt = "healthmes_decision_ends_at"
    public static let userInfoDecisionExpiresAt = "healthmes_decision_expires_at"

    /// The complete decision question. Compact system banners may truncate it,
    /// but expanded iPhone and Watch surfaces must retain the original text.
    public let title: String
    /// One short health reason shown directly below the decision.
    public let subtitle: String
    /// The immediate result of saying Yes, also constrained to one line.
    public let body: String
    public let categoryID: String
    /// Stable per-rule thread so repeat firings of one rule group together.
    public let threadID: String
    /// Routing payload: alert id, optional decision link, optional pending
    /// proposal id (string values only — plist-safe).
    public let userInfo: [String: String]

    /// The system-owned compact notification cannot render category buttons.
    /// Keep its copy short enough that the explicit expansion affordance is
    /// visible; the complete action remains in `title` and `userInfo`.
    public var systemTitle: String {
        userInfo[Self.userInfoDecisionCompactPrompt] ?? title
    }

    public var systemBody: String {
        guard categoryID == Self.actionableCategoryID else { return body }
        let hint = String(localized: "Hold to decide · No / Yes / Speak")
        return body.isEmpty ? hint : "\(body)\n\(hint)"
    }

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

    /// Copy budget for the smallest supported watch. This is a defensive
    /// fallback for legacy alerts without a structured decision card.
    public static func compactLine(_ text: String, limit: Int = 32) -> String {
        let singleLine = text
            .split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard singleLine.count > limit else { return singleLine }
        return String(singleLine.prefix(max(1, limit - 1))) + "…"
    }

    public static func decisionPrompt(for card: DecisionCard) -> String {
        let serverAction = questionLine(card.proposedAction, limit: 34)
        if !serverAction.isEmpty {
            return serverAction
        }
        let title = compactLine(card.title, limit: 22)
        switch card.kind {
        case "schedule_change":
            return String(format: String(localized: "Move %@?"), title)
        case "planned_sleep":
            return String(format: String(localized: "Schedule %@?"), title)
        case "actual_sleep":
            return String(format: String(localized: "Review %@?"), title)
        default:
            return String(format: String(localized: "Review %@?"), title)
        }
    }

    public static func questionLine(_ text: String, limit: Int = 34) -> String {
        let singleLine = text
            .split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !singleLine.isEmpty else { return "" }
        if singleLine.hasSuffix("?") {
            return compactLine(singleLine, limit: limit)
        }
        let stem = singleLine.trimmingCharacters(
            in: CharacterSet(charactersIn: ".!。！？? ")
        )
        guard !stem.isEmpty else { return "" }
        return compactLine(stem, limit: max(1, limit - 1)) + "?"
    }

    public static func fullQuestionLine(_ text: String) -> String {
        let singleLine = text
            .split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !singleLine.isEmpty else { return "" }
        if singleLine.hasSuffix("?") {
            return singleLine
        }
        let stem = singleLine.trimmingCharacters(
            in: CharacterSet(charactersIn: ".!。！？? ")
        )
        return stem.isEmpty ? "" : stem + "?"
    }

    public init(
        title: String,
        subtitle: String = "",
        body: String,
        categoryID: String,
        threadID: String,
        userInfo: [String: String]
    ) {
        self.title = title
        self.subtitle = subtitle
        self.body = body
        self.categoryID = categoryID
        self.threadID = threadID
        self.userInfo = userInfo
    }

    public static func targetLine(after: Date) -> String {
        let day = DateFormatter()
        day.locale = .autoupdatingCurrent
        day.setLocalizedDateFormatFromTemplate("EEE")

        let time = DateFormatter()
        time.locale = .autoupdatingCurrent
        time.timeStyle = .short
        time.dateStyle = .none

        return String(
            format: String(localized: "→ %@ · %@"),
            day.string(from: after),
            time.string(from: after)
        )
    }

    /// Build notification content from one alert-history item.
    /// The server-correlated `alert.proposalId` upgrades the category to the
    /// actionable one. `pendingProposalID` remains an explicit test/preview
    /// override.
    public static func from(
        alert: AlertItem,
        pendingProposalID: UUID? = nil
    ) -> AlertNotificationContent {
        let identityIsSafe = alert.hasConsistentProposalIdentity
        let correlatedCard = identityIsSafe ? alert.correlatedDecisionCard : nil
        let exactProposalID = identityIsSafe ? (pendingProposalID ?? alert.proposalId) : nil
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
        if let decisionUrl = correlatedCard?.decisionUrl ?? alert.decisionUrl {
            userInfo[userInfoDecisionURL] = decisionUrl
        }
        if let exactProposalID {
            userInfo[userInfoProposalID] = exactProposalID.uuidString.lowercased()
        }
        if let card = correlatedCard {
            let formatter = ISO8601DateFormatter()
            userInfo[userInfoDecisionTitle] = card.title
            userInfo[userInfoDecisionObservation] = card.observationShort
            if let evidence = card.evidenceShort {
                userInfo[userInfoDecisionEvidence] = evidence
            }
            userInfo[userInfoDecisionAction] = card.proposedAction
            userInfo[userInfoDecisionCompactPrompt] = decisionPrompt(for: card)
            if let before = card.before {
                userInfo[userInfoDecisionBefore] = formatter.string(from: before)
            }
            userInfo[userInfoDecisionAfter] = formatter.string(from: card.after)
            userInfo[userInfoDecisionEndsAt] = formatter.string(from: card.endsAt)
            userInfo[userInfoDecisionExpiresAt] = formatter.string(from: card.expiresAt)
        }

        let actionText =
            correlatedCard?.proposedAction
            ?? alert.proposal
        let actionPrompt = actionText.flatMap {
            let prompt = fullQuestionLine($0)
            return prompt.isEmpty ? nil : prompt
        }
        // A proposal id is not enough for safe Yes/No controls. The user must
        // also see the concrete mutation those controls will resolve.
        let isActionable = exactProposalID != nil && actionPrompt != nil
        let title: String
        let subtitle: String
        let body: String
        if let card = correlatedCard, let actionPrompt, isActionable {
            title = actionPrompt
            subtitle = card.observationShort
            body = targetLine(after: card.after)
        } else if let actionPrompt, isActionable {
            title = actionPrompt
            subtitle = alert.summary
            body = evidenceLine(alert.evidence) ?? ""
        } else {
            title = alert.summary
            subtitle = ""
            body = bodyLines.joined(separator: " ")
        }

        return AlertNotificationContent(
            title: title,
            subtitle: subtitle,
            body: body,
            categoryID: isActionable ? actionableCategoryID : infoCategoryID,
            threadID: alert.ruleId,
            userInfo: userInfo
        )
    }
}
