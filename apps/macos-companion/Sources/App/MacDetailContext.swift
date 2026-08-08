import SwiftUI

enum MacDetailContext: Identifiable {
    case energy(GlancePayload)
    case block(GlanceBlock, timezone: String)
    case proposal(ProposalItem, alert: AlertItem?)
    case alert(AlertItem)
    case goal(WeeklyGoalItem)
    case task(TaskItem)
    case event(CalendarEventItem)
    case decision(MacDecisionSummary)
    case report(WeeklyReport)

    var id: String {
        switch self {
        case .energy: return "energy"
        case .block(let block, _): return "block-\(block.start.timeIntervalSince1970)"
        case .proposal(let proposal, _): return "proposal-\(proposal.id)"
        case .alert(let alert): return "alert-\(alert.id)"
        case .goal(let goal): return "goal-\(goal.id)"
        case .task(let task): return "task-\(task.id)"
        case .event(let event): return "event-\(event.id)"
        case .decision(let decision): return "decision-\(decision.id)"
        case .report(let report): return "report-\(report.weekStart)"
        }
    }
}
