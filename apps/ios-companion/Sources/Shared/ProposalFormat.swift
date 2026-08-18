import Foundation

enum ProposalFormat {
    static func windowLine(
        _ proposal: ProposalItem,
        timeZone: TimeZone = .autoupdatingCurrent
    ) -> String {
        let day = DateFormatter()
        day.timeZone = timeZone
        day.dateStyle = .medium
        day.timeStyle = .none
        return "\(day.string(from: proposal.proposedStart)) · \(timeRange(proposal, timeZone: timeZone))"
    }

    static func timeRange(
        _ proposal: ProposalItem,
        timeZone: TimeZone = .autoupdatingCurrent
    ) -> String {
        let time = DateFormatter()
        time.timeZone = timeZone
        time.dateStyle = .none
        time.timeStyle = .short
        return "\(time.string(from: proposal.proposedStart))–\(time.string(from: proposal.proposedEnd))"
    }

    static func compactWindowLine(
        _ proposal: ProposalItem,
        now: Date = Date(),
        timeZone: TimeZone = .autoupdatingCurrent
    ) -> String {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
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
            formatter.timeZone = timeZone
            formatter.dateStyle = .short
            formatter.timeStyle = .none
            day = formatter.string(from: proposal.proposedStart)
        }
        return "\(day) · \(timeRange(proposal, timeZone: timeZone))"
    }

    static func watchWindowLine(
        _ proposal: ProposalItem,
        now: Date = Date(),
        timeZone: TimeZone = .autoupdatingCurrent
    ) -> String {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
        if calendar.isDate(proposal.proposedStart, inSameDayAs: now) {
            return timeRange(proposal, timeZone: timeZone)
        }
        return compactWindowLine(proposal, now: now, timeZone: timeZone)
    }
}
