import Foundation

enum ProposalFormat {
    /// Formats proposal instants in the current device locale and timezone.
    static func windowLine(_ proposal: ProposalItem) -> String {
        let day = DateFormatter()
        day.dateStyle = .medium
        day.timeStyle = .none
        return "\(day.string(from: proposal.proposedStart)) · \(timeRange(proposal))"
    }

    static func timeRange(_ proposal: ProposalItem) -> String {
        let time = DateFormatter()
        time.dateStyle = .none
        time.timeStyle = .short
        return "\(time.string(from: proposal.proposedStart))–\(time.string(from: proposal.proposedEnd))"
    }

    static func compactWindowLine(
        _ proposal: ProposalItem,
        now: Date = Date(),
        calendar: Calendar = .autoupdatingCurrent
    ) -> String {
        let day: String
        if calendar.isDate(proposal.proposedStart, inSameDayAs: now) {
            day = String(localized: "Today")
        } else if
            let tomorrow = calendar.date(byAdding: .day, value: 1, to: now),
            calendar.isDate(proposal.proposedStart, inSameDayAs: tomorrow)
        {
            day = String(localized: "Tomorrow")
        } else {
            let formatter = DateFormatter()
            formatter.dateStyle = .short
            formatter.timeStyle = .none
            day = formatter.string(from: proposal.proposedStart)
        }
        return "\(day) · \(timeRange(proposal))"
    }
}
