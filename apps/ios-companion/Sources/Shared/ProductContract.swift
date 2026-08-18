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
    public let isAllDay: Bool
    public let isLocked: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case externalId = "external_id"
        case calendarSource = "calendar_source"
        case summary
        case startAt = "start_at"
        case endAt = "end_at"
        case isAgentCreated = "is_agent_created"
        case agentTaskId = "agent_task_id"
        case isAllDay = "is_all_day"
        case isLocked = "is_locked"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        externalId = try container.decode(String.self, forKey: .externalId)
        calendarSource = try container.decode(String.self, forKey: .calendarSource)
        summary = try container.decodeIfPresent(String.self, forKey: .summary)
        startAt = try container.decode(Date.self, forKey: .startAt)
        endAt = try container.decode(Date.self, forKey: .endAt)
        isAgentCreated = try container.decode(Bool.self, forKey: .isAgentCreated)
        agentTaskId = try container.decodeIfPresent(UUID.self, forKey: .agentTaskId)
        isAllDay = try container.decodeIfPresent(Bool.self, forKey: .isAllDay) ?? false
        isLocked = try container.decodeIfPresent(Bool.self, forKey: .isLocked) ?? false
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

public struct PairingOperationToken: Equatable {
    fileprivate let generation: UInt
    public let pairing: Pairing
    public let proposalID: UUID?

    fileprivate init(generation: UInt, pairing: Pairing, proposalID: UUID?) {
        self.generation = generation
        self.pairing = pairing
        self.proposalID = proposalID
    }
}

/// Invalidates delayed async completions when a newer operation or pairing
/// supersedes the state they were created for.
public struct PairingOperationGate {
    private var generation: UInt = 0

    public init() {}

    public mutating func begin(
        pairing: Pairing,
        proposalID: UUID? = nil
    ) -> PairingOperationToken {
        generation &+= 1
        return PairingOperationToken(
            generation: generation,
            pairing: pairing,
            proposalID: proposalID
        )
    }

    public mutating func invalidate() {
        generation &+= 1
    }

    public func isCurrent(
        _ token: PairingOperationToken,
        pairing: Pairing?,
        proposalID: UUID? = nil
    ) -> Bool {
        guard token.generation == generation, token.pairing == pairing else {
            return false
        }
        guard let proposalID else { return true }
        return token.proposalID == proposalID
    }
}

/// Prevents a polling response captured before or during proposal resolution
/// from restoring a proposal that another request already resolved.
public struct ResolutionAwareRefreshGate {
    private var refreshGeneration: UInt = 0
    private var resolutionGeneration: UInt = 0
    private var activeResolutions: Set<UInt> = []

    public init() {}

    public mutating func beginRefresh() -> UInt {
        refreshGeneration &+= 1
        return refreshGeneration
    }

    public mutating func beginResolution() -> UInt {
        resolutionGeneration &+= 1
        activeResolutions.insert(resolutionGeneration)
        refreshGeneration &+= 1
        return resolutionGeneration
    }

    @discardableResult
    public mutating func finishResolution(_ token: UInt) -> Bool {
        guard activeResolutions.remove(token) != nil else { return false }
        refreshGeneration &+= 1
        return true
    }

    public mutating func invalidate() {
        resolutionGeneration &+= 1
        activeResolutions.removeAll()
        refreshGeneration &+= 1
    }

    public func canApplyRefresh(_ token: UInt) -> Bool {
        activeResolutions.isEmpty && token == refreshGeneration
    }
}

public enum WatchDecisionLayoutPolicy {
    public static let minimumButtonHeight: Double = 42
    public static let keepsActionsOutsideScrollContent = true

    public static func canResolve(
        isDecisionContextReady: Bool,
        hasCurrentWellnessContext: Bool
    ) -> Bool {
        isDecisionContextReady && hasCurrentWellnessContext
    }
}

public enum WellnessDecisionSafety {
    public static func canResolve(
        hasHealthSnapshot: Bool,
        isBriefingStale: Bool,
        sceneAllowsActions: Bool
    ) -> Bool {
        hasHealthSnapshot && !isBriefingStale && sceneAllowsActions
    }
}

public struct TimelineLaneAssignment: Equatable {
    public let lane: Int
    public let laneCount: Int

    public init(lane: Int, laneCount: Int) {
        self.lane = lane
        self.laneCount = laneCount
    }
}

public enum WellnessTimelinePolicy {
    public static func dayInterval(containing date: Date, timeZone: TimeZone) -> DateInterval? {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
        let start = calendar.startOfDay(for: date)
        guard let end = calendar.date(byAdding: .day, value: 1, to: start) else {
            return nil
        }
        return DateInterval(start: start, end: end)
    }

    public static func sevenDayInterval(
        containing date: Date,
        timeZone: TimeZone
    ) -> DateInterval? {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
        let start = calendar.startOfDay(for: date)
        guard let end = calendar.date(byAdding: .day, value: 7, to: start) else {
            return nil
        }
        return DateInterval(start: start, end: end)
    }

    public static func hourBounds(
        for intervals: [DateInterval],
        timeZone: TimeZone
    ) -> Range<Int> {
        guard !intervals.isEmpty else { return 8..<18 }
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
        let firstHour = intervals.map {
            calendar.component(.hour, from: $0.start)
        }.min() ?? 8
        let lastHour = intervals.map { interval in
            if calendar.startOfDay(for: interval.end) > calendar.startOfDay(for: interval.start) {
                return 24
            }
            let components = calendar.dateComponents([.hour, .minute], from: interval.end)
            return min((components.hour ?? 17) + ((components.minute ?? 0) > 0 ? 1 : 0), 24)
        }.max() ?? 18
        let lower = max(firstHour - 1, 0)
        let upper = min(max(lastHour + 1, lower + 3), 24)
        return lower..<upper
    }

    public static func laneAssignments(
        for intervals: [DateInterval]
    ) -> [TimelineLaneAssignment] {
        guard !intervals.isEmpty else { return [] }
        var assignments = Array(
            repeating: TimelineLaneAssignment(lane: 0, laneCount: 1),
            count: intervals.count
        )
        var clusterIndices: [Int] = []
        var laneEnds: [Date] = []
        var clusterEnd: Date?

        func finishCluster() {
            let laneCount = max(
                clusterIndices.map { assignments[$0].lane }.max().map { $0 + 1 } ?? 1,
                1
            )
            for index in clusterIndices {
                assignments[index] = TimelineLaneAssignment(
                    lane: assignments[index].lane,
                    laneCount: laneCount
                )
            }
        }

        for (index, interval) in intervals.enumerated() {
            if let currentClusterEnd = clusterEnd, interval.start >= currentClusterEnd {
                finishCluster()
                clusterIndices.removeAll(keepingCapacity: true)
                laneEnds.removeAll(keepingCapacity: true)
                clusterEnd = nil
            }

            let lane = laneEnds.firstIndex(where: { $0 <= interval.start }) ?? laneEnds.count
            if lane == laneEnds.count {
                laneEnds.append(interval.end)
            } else {
                laneEnds[lane] = interval.end
            }
            assignments[index] = TimelineLaneAssignment(lane: lane, laneCount: 1)
            clusterIndices.append(index)
            clusterEnd = max(clusterEnd ?? interval.end, interval.end)
        }
        finishCluster()
        return assignments
    }

    public static func shouldUseReadableList(maxLaneCount: Int) -> Bool {
        maxLaneCount >= 4
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
