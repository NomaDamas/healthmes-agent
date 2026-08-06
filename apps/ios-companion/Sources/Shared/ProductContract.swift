import Foundation

// Read/write contracts for the unified product surfaces in issue #108.
// These mirror the existing REST APIs without adding an iOS-only envelope.

public struct WeeklyGoalItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let weekStart: String
    public let title: String
    public let priority: Int
    public let status: String

    enum CodingKeys: String, CodingKey {
        case id
        case weekStart = "week_start"
        case title
        case priority
        case status
    }
}

public struct WeeklyGoalCreateBody: Codable, Equatable {
    public let weekStart: String
    public let title: String
    public let priority: Int
    public let status: String

    public init(
        weekStart: String,
        title: String,
        priority: Int = 0,
        status: String = "active"
    ) {
        self.weekStart = weekStart
        self.title = title
        self.priority = priority
        self.status = status
    }

    enum CodingKeys: String, CodingKey {
        case weekStart = "week_start"
        case title
        case priority
        case status
    }
}

public struct TaskItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let title: String
    public let goalId: UUID?
    public let estimatedMinutes: Int?
    public let deadline: Date?
    public let energyDemand: String
    public let status: String
    public let source: String

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case goalId = "goal_id"
        case estimatedMinutes = "est_minutes"
        case deadline
        case energyDemand = "energy_demand"
        case status
        case source
    }

    public var isOpen: Bool {
        status != "done" && status != "cancelled"
    }
}

public struct TaskCreateBody: Codable, Equatable {
    public let title: String
    public let goalId: UUID?
    public let estimatedMinutes: Int?
    public let deadline: Date?
    public let energyDemand: String
    public let source: String

    public init(
        title: String,
        goalId: UUID? = nil,
        estimatedMinutes: Int? = nil,
        deadline: Date? = nil,
        energyDemand: String = "med",
        source: String = "user"
    ) {
        self.title = title
        self.goalId = goalId
        self.estimatedMinutes = estimatedMinutes
        self.deadline = deadline
        self.energyDemand = energyDemand
        self.source = source
    }

    enum CodingKeys: String, CodingKey {
        case title
        case goalId = "goal_id"
        case estimatedMinutes = "est_minutes"
        case deadline
        case energyDemand = "energy_demand"
        case source
    }
}

public struct CalendarEventItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let externalId: String
    public let calendarSource: String
    public let summary: String?
    public let startAt: Date
    public let endAt: Date
    public let isAgentCreated: Bool
    public let agentTaskId: UUID?

    enum CodingKeys: String, CodingKey {
        case id
        case externalId = "external_id"
        case calendarSource = "calendar_source"
        case summary
        case startAt = "start_at"
        case endAt = "end_at"
        case isAgentCreated = "is_agent_created"
        case agentTaskId = "agent_task_id"
    }
}

public typealias WeeklyGoalsPage = APIPage<WeeklyGoalItem>
public typealias TasksPage = APIPage<TaskItem>
public typealias CalendarEventsPage = APIPage<CalendarEventItem>

public enum ProductDecisionKind: String, Codable {
    case scheduleChange = "schedule_change"
    case alert
    case insight
    case capture
}

public struct ProductDecisionSummary: Codable, Equatable, Identifiable {
    public let id: UUID
    public let kind: ProductDecisionKind
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

public typealias ProductDecisionsPage = APIPage<ProductDecisionSummary>

public enum ProductDateFormat {
    public static func weekStart(containing date: Date, calendar: Calendar = .autoupdatingCurrent)
        -> String
    {
        var calendar = calendar
        calendar.firstWeekday = 2
        let start = calendar.dateInterval(of: .weekOfYear, for: date)?.start ?? date
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: start)
    }
}

public struct LatestRefreshGate {
    private var generation: UInt = 0

    public init() {}

    public mutating func begin() -> UInt {
        generation &+= 1
        return generation
    }

    public func isCurrent(_ candidate: UInt) -> Bool {
        candidate == generation
    }
}

public func productRefreshResult<Value>(
    _ operation: () async throws -> Value
) async -> Result<Value, Error> {
    do {
        return .success(try await operation())
    } catch {
        return .failure(error)
    }
}
