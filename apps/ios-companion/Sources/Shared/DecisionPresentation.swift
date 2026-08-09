import Foundation

public extension AlertItem {
    var exactDecisionURL: URL? {
        let raw = decisionCard?.decisionUrl ?? decisionUrl
        return raw.flatMap(URL.init(string:))
    }

    var exactDecisionRecordID: UUID? {
        if let decisionCard {
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
    public var card: DecisionCard? { alert?.decisionCard }

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
        let alertsByProposal = Dictionary(
            alerts.compactMap { alert in
                alert.proposalId.map { ($0, alert) }
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
                return PendingDecision(proposal: proposal, alert: alert, prompt: prompt)
            }
            .sorted { $0.proposal.proposedStart < $1.proposal.proposedStart }
    }
}

public enum ProposalActionPresentation {
    /// Returns only a server-provided action phrase. A proposal id and time
    /// window alone are not enough context to expose Yes/No safely.
    public static func exactPrompt(alert: AlertItem?) -> String? {
        if let action = alert?.decisionCard?.proposedAction {
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
