import Foundation

// Codable contract for `GET /v1/alerts` (healthmes/api/alerts.py, issue #10).
// Same discipline as GlanceContract.swift: field names pinned verbatim to
// the server schema, strict decoding, and a fixture (Tests/Fixtures/
// alerts.json) mirroring the server-side seeded test (tests/api/
// test_alerts.py). Semantics: "unresolved == recently pushed" — exactly the
// glance `alerts` block policy, so this list never disagrees with a widget.

/// Standard list envelope `{"data": [...], "pagination": {...}}`
/// (healthmes/api/pagination.py) shared by every list endpoint.
public struct APIPage<Item: Codable & Equatable>: Codable, Equatable {
    public let data: [Item]
    public let pagination: APIPageMeta
}

public struct APIPageMeta: Codable, Equatable {
    public let totalCount: Int
    public let limit: Int
    public let offset: Int
    public let hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case totalCount = "total_count"
        case limit
        case offset
        case hasMore = "has_more"
    }
}

/// One pushed alert, shaped after the PLAN §8.5 notification grammar:
/// `summary` is the observation line, `evidence` the evidence facts (the
/// client renders the line), `proposal` the proposal line, `decision_url`
/// the "why this?" viewer deep link (derived read-only token embedded
/// server-side).
public struct AlertItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let ruleId: String
    public let firedAt: Date
    public let summary: String
    public let proposal: String?
    public let evidence: [String: JSONValue]?
    public let decisionUrl: String?
    public let proposalId: UUID?
    public let decisionCard: DecisionCard?

    public init(
        id: UUID,
        ruleId: String,
        firedAt: Date,
        summary: String,
        proposal: String?,
        evidence: [String: JSONValue]?,
        decisionUrl: String?,
        proposalId: UUID?,
        decisionCard: DecisionCard? = nil
    ) {
        self.id = id
        self.ruleId = ruleId
        self.firedAt = firedAt
        self.summary = summary
        self.proposal = proposal
        self.evidence = evidence
        self.decisionUrl = decisionUrl
        self.proposalId = proposalId
        self.decisionCard = decisionCard
    }

    enum CodingKeys: String, CodingKey {
        case id
        case ruleId = "rule_id"
        case firedAt = "fired_at"
        case summary
        case proposal
        case evidence
        case decisionUrl = "decision_url"
        case proposalId = "proposal_id"
        case decisionCard = "decision_card"
    }
}

public struct DecisionCard: Codable, Equatable {
    public let decisionId: UUID
    public let proposalId: UUID
    public let kind: String
    public let severity: String
    public let title: String
    public let observationShort: String
    public let evidenceShort: String?
    public let proposedAction: String
    public let before: Date?
    public let after: Date
    public let endsAt: Date
    public let expiresAt: Date
    public let decisionUrl: String?

    enum CodingKeys: String, CodingKey {
        case decisionId = "decision_id"
        case proposalId = "proposal_id"
        case kind
        case severity
        case title
        case observationShort = "observation_short"
        case evidenceShort = "evidence_short"
        case proposedAction = "proposed_action"
        case before
        case after
        case endsAt = "ends_at"
        case expiresAt = "expires_at"
        case decisionUrl = "decision_url"
    }
}

public typealias AlertsPage = APIPage<AlertItem>
