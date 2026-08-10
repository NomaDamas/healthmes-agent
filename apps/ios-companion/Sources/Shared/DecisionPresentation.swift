import Foundation

public extension AlertItem {
    var hasConsistentProposalIdentity: Bool {
        guard let decisionCard else { return true }
        return proposalId == decisionCard.proposalId
    }

    var correlatedDecisionCard: DecisionCard? {
        hasConsistentProposalIdentity ? decisionCard : nil
    }

    var exactDecisionURL: URL? {
        let raw = correlatedDecisionCard?.decisionUrl ?? decisionUrl
        return raw.flatMap(URL.init(string:))
    }

    var exactDecisionRecordID: UUID? {
        if let decisionCard = correlatedDecisionCard {
            return decisionCard.decisionId
        }
        return exactDecisionURL.flatMap {
            UUID(uuidString: $0.pathComponents.last ?? "")
        }
    }
}

public struct PendingDecision: Identifiable, Equatable {
    public let proposal: ProposalItem
    public let alert: AlertItem?
    public let prompt: String

    public var id: UUID { proposal.id }
    public var card: DecisionCard? { alert?.correlatedDecisionCard }

    public var hasExactDecisionCorrelation: Bool {
        guard let card else { return false }
        return proposal.decisionRecordId == card.decisionId
            && proposal.proposedStart == card.after
            && proposal.proposedEnd == card.endsAt
    }

    public var reason: String? {
        if let observation = card?.observationShort, !observation.isEmpty {
            return observation
        }
        if let summary = alert?.summary, !summary.isEmpty {
            return summary
        }
        return nil
    }

    public var exactWebURL: URL? {
        alert?.exactDecisionURL
    }

    public var primaryActionText: String {
        prompt
    }

    public var watchActionTitle: String {
        if let title = card?.title.trimmingCharacters(in: .whitespacesAndNewlines),
            !title.isEmpty
        {
            return AlertNotificationContent.compactLine(title, limit: 28)
        }
        return String(localized: "Schedule adjustment")
    }

    public var watchReason: String? {
        let value = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value, !value.isEmpty else { return nil }
        return AlertNotificationContent.compactLine(value, limit: 42)
    }

    public var secondaryContextTitle: String? {
        guard
            let title = card?.title.trimmingCharacters(in: .whitespacesAndNewlines),
            !title.isEmpty,
            title.caseInsensitiveCompare(prompt) != .orderedSame
        else { return nil }
        return title
    }

    public static func correlate(
        alerts: [AlertItem],
        proposals: [ProposalItem]
    ) -> [PendingDecision] {
        let alertsByProposal: [UUID: AlertItem] = Dictionary(
            alerts.compactMap { alert -> (UUID, AlertItem)? in
                guard alert.hasConsistentProposalIdentity else { return nil }
                return alert.proposalId.map { ($0, alert) }
            },
            uniquingKeysWith: { first, _ in first }
        )
        return proposals
            .filter(\.isActionable)
            .compactMap { proposal in
                let alert = alertsByProposal[proposal.id]
                guard let prompt = ProposalActionPresentation.exactPrompt(alert: alert) else {
                    return nil
                }
                let decision = PendingDecision(
                    proposal: proposal,
                    alert: alert,
                    prompt: prompt
                )
                return decision.hasExactDecisionCorrelation ? decision : nil
            }
            .sorted { $0.proposal.proposedStart < $1.proposal.proposedStart }
    }
}

public enum ProposalActionPresentation {
    /// Returns only a server-provided action phrase. A proposal id and time
    /// window alone are not enough context to expose Yes/No safely.
    public static func exactPrompt(alert: AlertItem?) -> String? {
        guard alert?.hasConsistentProposalIdentity != false else { return nil }
        if let action = alert?.correlatedDecisionCard?.proposedAction {
            let question = AlertNotificationContent.questionLine(action)
            if !question.isEmpty {
                return question
            }
        }
        if let action = alert?.proposal {
            let question = AlertNotificationContent.questionLine(action)
            if !question.isEmpty {
                return question
            }
        }
        return nil
    }
}

public enum ProposalStatusPresentation {
    public static func label(for status: ProposalStatus) -> String {
        switch status {
        case .proposed:
            return String(localized: "Pending")
        case .accepted:
            return String(localized: "Approved · calendar sync pending")
        case .pushed:
            return String(localized: "Applied to calendar")
        case .declined:
            return String(localized: "Declined · calendar unchanged")
        case .invalidated:
            return String(localized: "Expired · calendar unchanged")
        }
    }

    public static func systemImage(for status: ProposalStatus) -> String {
        switch status {
        case .proposed:
            return "questionmark.circle"
        case .accepted:
            return "clock.badge.checkmark"
        case .pushed:
            return "calendar.badge.checkmark"
        case .declined:
            return "xmark.circle"
        case .invalidated:
            return "clock.badge.xmark"
        }
    }

    public static func detail(for status: ProposalStatus) -> String {
        switch status {
        case .proposed:
            return String(localized: "The server did not confirm the decision. Refresh and try again.")
        case .accepted:
            return String(localized: "Calendar sync will apply the change.")
        case .pushed:
            return String(localized: "The approved change is in your calendar.")
        case .declined:
            return String(localized: "Your calendar stays unchanged.")
        case .invalidated:
            return String(localized: "The decision window closed without a change.")
        }
    }
}
