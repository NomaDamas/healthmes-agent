import Foundation

enum MacSceneTimeZone {
    static func resolve(
        payloadTimezone: String?,
        reportTimezone: String?,
        fallback: TimeZone = .autoupdatingCurrent
    ) -> TimeZone {
        payloadTimezone.flatMap(TimeZone.init(identifier:))
            ?? reportTimezone.flatMap(TimeZone.init(identifier:))
            ?? fallback
    }
}

/// Decisions are the only full-window Mac contract not yet in the shared
/// Apple product layer. Goals, tasks and calendar events use ProductContract.
public enum MacDecisionKind: String, Codable, CaseIterable {
    case scheduleChange = "schedule_change"
    case alert
    case insight
    case capture
}

public struct MacDecisionSummary: Codable, Equatable, Identifiable {
    public let id: UUID
    public let kind: MacDecisionKind
    public let summary: String
    public let model: String?
    public let tokens: Int?
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case summary
        case model = "llm_model"
        case tokens
        case createdAt = "created_at"
    }
}

public typealias MacDecisionsPage = APIPage<MacDecisionSummary>
