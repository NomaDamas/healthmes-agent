import Foundation

// Codable contract for the schedule-proposal surface
// (healthmes/api/schedule.py): `GET /v1/schedule/proposals` and the
// propose-then-confirm actions `POST …/{id}/accept` / `…/{id}/decline`.
// These are the real endpoints behind the compact No/Yes decision remote
// and the longer in-app proposal controls.

/// Mirror of healthmes.store.enums.ProposalStatus.
public enum ProposalStatus: String, Codable {
    case proposed
    case accepted
    case pushed
    case declined
    case invalidated
}

public struct ProposalItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let taskId: UUID
    public let proposedStart: Date
    public let proposedEnd: Date
    public let status: ProposalStatus
    public let decisionRecordId: UUID?
    public let healthmesKind: String?
    public let decidedAt: Date?
    public let decisionSurface: String?
    public let acceptResolutionToken: String?
    public let declineResolutionToken: String?

    public init(
        id: UUID,
        taskId: UUID,
        proposedStart: Date,
        proposedEnd: Date,
        status: ProposalStatus,
        decisionRecordId: UUID?,
        healthmesKind: String? = nil,
        decidedAt: Date? = nil,
        decisionSurface: String? = nil,
        acceptResolutionToken: String?,
        declineResolutionToken: String?
    ) {
        self.id = id
        self.taskId = taskId
        self.proposedStart = proposedStart
        self.proposedEnd = proposedEnd
        self.status = status
        self.decisionRecordId = decisionRecordId
        self.healthmesKind = healthmesKind
        self.decidedAt = decidedAt
        self.decisionSurface = decisionSurface
        self.acceptResolutionToken = acceptResolutionToken
        self.declineResolutionToken = declineResolutionToken
    }

    enum CodingKeys: String, CodingKey {
        case id
        case taskId = "task_id"
        case proposedStart = "proposed_start"
        case proposedEnd = "proposed_end"
        case status
        case decisionRecordId = "decision_record_id"
        case healthmesKind = "healthmes_kind"
        case decidedAt = "decided_at"
        case decisionSurface = "decision_surface"
        case acceptResolutionToken = "accept_resolution_token"
        case declineResolutionToken = "decline_resolution_token"
    }

    public func resolutionToken(for action: ProposalAction) -> String? {
        switch action {
        case .accept: acceptResolutionToken
        case .decline: declineResolutionToken
        }
    }

    public var isActionable: Bool {
        status == .proposed
            && acceptResolutionToken != nil
            && declineResolutionToken != nil
    }
}

public typealias ProposalsPage = APIPage<ProposalItem>

public enum ProposalAction: String {
    case accept
    case decline
}

public struct ProposalResolutionBody: Codable, Equatable {
    public let resolutionToken: String
    public let surface: String

    public init(resolutionToken: String, surface: String = "ios_app") {
        self.resolutionToken = resolutionToken
        self.surface = surface
    }

    enum CodingKeys: String, CodingKey {
        case resolutionToken = "resolution_token"
        case surface
    }
}
