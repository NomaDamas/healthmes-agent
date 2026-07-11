import Foundation

// Codable contract for the schedule-proposal surface
// (healthmes/api/schedule.py): `GET /v1/schedule/proposals` and the
// propose-then-confirm actions `POST …/{id}/accept` / `…/{id}/decline`.
// These are the REAL endpoints behind the §8.5 alert buttons
// (✅ Apply → accept, ❌ Keep as-is → decline, ✏️ Adjust → open the
// proposal in-app).

/// Mirror of healthmes.store.enums.ProposalStatus.
public enum ProposalStatus: String, Codable {
    case proposed
    case accepted
    case pushed
    case declined
}

public struct ProposalItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let taskId: UUID
    public let proposedStart: Date
    public let proposedEnd: Date
    public let status: ProposalStatus
    public let decisionRecordId: UUID?

    public init(
        id: UUID,
        taskId: UUID,
        proposedStart: Date,
        proposedEnd: Date,
        status: ProposalStatus,
        decisionRecordId: UUID?
    ) {
        self.id = id
        self.taskId = taskId
        self.proposedStart = proposedStart
        self.proposedEnd = proposedEnd
        self.status = status
        self.decisionRecordId = decisionRecordId
    }

    enum CodingKeys: String, CodingKey {
        case id
        case taskId = "task_id"
        case proposedStart = "proposed_start"
        case proposedEnd = "proposed_end"
        case status
        case decisionRecordId = "decision_record_id"
    }
}

public typealias ProposalsPage = APIPage<ProposalItem>

public enum ProposalAction: String {
    case accept
    case decline
}
