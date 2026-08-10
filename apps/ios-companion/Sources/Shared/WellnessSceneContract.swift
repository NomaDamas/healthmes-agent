import Foundation

/// Stable perspectives over one HealthMes control canvas. A lens changes
/// emphasis, never navigation or command state.
public enum WellnessLens: String, Codable, CaseIterable, Identifiable {
    case now
    case coordinate
    case change

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .now: return "지금"
        case .coordinate: return "조율"
        case .change: return "변화"
        }
    }
}

public enum WellnessSceneSeverity: String, Codable {
    case neutral
    case supportive
    case caution
    case action
}

public enum WellnessFreshness: String, Codable {
    case current
    case stale
    case insufficientData = "insufficient_data"
    case offline
}

/// The trusted catalog that native and web renderers are allowed to display.
/// A scene is data, not executable SwiftUI, HTML, CSS, or a mutation endpoint.
public enum WellnessModuleKind: String, Codable, CaseIterable {
    case healthState = "health_state"
    case planImpact = "plan_impact"
    case scheduleTimeline = "schedule_timeline"
    case alternatives
    case constraints
    case capacityMap = "capacity_map"
    case outcomeCurve = "outcome_curve"
    case goalImpact = "goal_impact"
    case clarification
    case decision
    case calendarSync = "calendar_sync"
    case capacityBar = "capacity_bar"
    case energyCurve = "energy_curve"
    case calendarCanvas = "calendar_canvas"
    case scheduleComparison = "schedule_comparison"
    case timeSeries = "time_series"
    case baselineBand = "baseline_band"
    case comparisonBar = "comparison_bar"
    case factorContribution = "factor_contribution"
    case eventAlignedTrend = "event_aligned_trend"
    case goalTrajectory = "goal_trajectory"
    case decisionOutcome = "decision_outcome"
    case nutritionEvidence = "nutrition_evidence"
    case proposalPreview = "proposal_preview"
    case fallback
}

public enum WellnessVisualizationKind: String, Codable, CaseIterable {
    case capacityBar = "capacity_bar"
    case energyCurve = "energy_curve"
    case calendarCanvas = "calendar_canvas"
    case scheduleComparison = "schedule_comparison"
    case timeSeries = "time_series"
    case baselineBand = "baseline_band"
    case comparisonBar = "comparison_bar"
    case factorContribution = "factor_contribution"
    case eventAlignedTrend = "event_aligned_trend"
    case goalTrajectory = "goal_trajectory"
    case decisionOutcome = "decision_outcome"
}

public struct WellnessConfidence: Codable, Equatable {
    public enum Level: String, Codable {
        case high
        case medium
        case low
        case insufficientData = "insufficient_data"
    }

    public let level: Level
    public let coverage: String
    public let limitations: [String]

    public init(level: Level, coverage: String, limitations: [String] = []) {
        self.level = level
        self.coverage = coverage
        self.limitations = limitations
    }
}

public struct WellnessPoint: Codable, Equatable {
    public let label: String
    public let value: Double?
    public let secondaryValue: Double?
    public let annotation: String?

    public init(
        label: String,
        value: Double?,
        secondaryValue: Double? = nil,
        annotation: String? = nil
    ) {
        self.label = label
        self.value = value
        self.secondaryValue = secondaryValue
        self.annotation = annotation
    }

    enum CodingKeys: String, CodingKey {
        case label
        case value
        case secondaryValue = "secondary_value"
        case annotation
    }
}

public struct WellnessSeries: Codable, Equatable, Identifiable {
    public let id: String
    public let label: String
    public let points: [WellnessPoint]

    public init(id: String, label: String, points: [WellnessPoint]) {
        self.id = id
        self.label = label
        self.points = points
    }
}

public struct WellnessCalendarEvent: Codable, Equatable, Identifiable {
    public enum Status: String, Codable {
        case current
        case proposed
    }

    public let id: String
    public let title: String
    public let startsAt: Date
    public let endsAt: Date
    public let provider: String
    public let calendarID: String
    public let calendarName: String
    public let calendarColor: String
    public let isHealthMesManaged: Bool
    public let energyDemand: String?
    public let isAllDay: Bool
    public let isRecurring: Bool
    public let isLocked: Bool
    public let hasAttendees: Bool
    public let organizerSelf: Bool
    public let providerStatus: String?
    public let status: Status

    public init(
        id: String,
        title: String,
        startsAt: Date,
        endsAt: Date,
        provider: String,
        calendarID: String = "default",
        calendarName: String = "Calendar",
        calendarColor: String = "#6B7280",
        isHealthMesManaged: Bool,
        energyDemand: String? = nil,
        isAllDay: Bool = false,
        isRecurring: Bool = false,
        isLocked: Bool = false,
        hasAttendees: Bool = false,
        organizerSelf: Bool = false,
        providerStatus: String? = nil,
        status: Status = .current
    ) {
        self.id = id
        self.title = title
        self.startsAt = startsAt
        self.endsAt = endsAt
        self.provider = provider
        self.calendarID = calendarID
        self.calendarName = calendarName
        self.calendarColor = calendarColor
        self.isHealthMesManaged = isHealthMesManaged
        self.energyDemand = energyDemand
        self.isAllDay = isAllDay
        self.isRecurring = isRecurring
        self.isLocked = isLocked
        self.hasAttendees = hasAttendees
        self.organizerSelf = organizerSelf
        self.providerStatus = providerStatus
        self.status = status
    }

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case startsAt = "starts_at"
        case endsAt = "ends_at"
        case provider
        case calendarID = "calendar_id"
        case calendarName = "calendar_name"
        case calendarColor = "calendar_color"
        case isHealthMesManaged = "is_healthmes_managed"
        case energyDemand = "energy_demand"
        case isAllDay = "is_all_day"
        case isRecurring = "is_recurring"
        case isLocked = "is_locked"
        case hasAttendees = "has_attendees"
        case organizerSelf = "organizer_self"
        case providerStatus = "provider_status"
        case status
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        title = try container.decode(String.self, forKey: .title)
        startsAt = try container.decode(Date.self, forKey: .startsAt)
        endsAt = try container.decode(Date.self, forKey: .endsAt)
        provider = try container.decode(String.self, forKey: .provider)
        calendarID = try container.decodeIfPresent(String.self, forKey: .calendarID) ?? "default"
        calendarName =
            try container.decodeIfPresent(String.self, forKey: .calendarName) ?? "Calendar"
        calendarColor =
            try container.decodeIfPresent(String.self, forKey: .calendarColor) ?? "#6B7280"
        isHealthMesManaged = try container.decode(Bool.self, forKey: .isHealthMesManaged)
        energyDemand = try container.decodeIfPresent(String.self, forKey: .energyDemand)
        isAllDay = try container.decodeIfPresent(Bool.self, forKey: .isAllDay) ?? false
        isRecurring = try container.decodeIfPresent(Bool.self, forKey: .isRecurring) ?? false
        isLocked = try container.decodeIfPresent(Bool.self, forKey: .isLocked) ?? false
        hasAttendees =
            try container.decodeIfPresent(Bool.self, forKey: .hasAttendees) ?? false
        organizerSelf =
            try container.decodeIfPresent(Bool.self, forKey: .organizerSelf) ?? false
        providerStatus = try container.decodeIfPresent(
            String.self,
            forKey: .providerStatus
        )
        status = try container.decodeIfPresent(Status.self, forKey: .status) ?? .current
    }
}

public struct WellnessVisualization: Codable, Equatable {
    public let kind: WellnessVisualizationKind
    public let unit: String?
    public let minimum: Double?
    public let maximum: Double?
    public let series: [WellnessSeries]
    public let events: [WellnessCalendarEvent]

    public init(
        kind: WellnessVisualizationKind,
        unit: String? = nil,
        minimum: Double? = nil,
        maximum: Double? = nil,
        series: [WellnessSeries] = [],
        events: [WellnessCalendarEvent] = []
    ) {
        self.kind = kind
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum
        self.series = series
        self.events = events
    }
}

public struct WellnessSceneItem: Codable, Equatable, Identifiable {
    public let id: String
    public let label: String
    public let value: String
    public let detail: String?

    public init(id: String, label: String, value: String, detail: String? = nil) {
        self.id = id
        self.label = label
        self.value = value
        self.detail = detail
    }
}

public struct WellnessSceneModule: Codable, Equatable, Identifiable {
    public let id: String
    public let kind: WellnessModuleKind
    public let title: String
    public let summary: String
    public let items: [WellnessSceneItem]
    public let visualization: WellnessVisualization?
    public let accessibilitySummary: String

    public init(
        id: String,
        kind: WellnessModuleKind,
        title: String,
        summary: String,
        items: [WellnessSceneItem] = [],
        visualization: WellnessVisualization? = nil,
        accessibilitySummary: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.summary = summary
        self.items = items
        self.visualization = visualization
        self.accessibilitySummary = accessibilitySummary ?? "\(title). \(summary)"
    }

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case summary
        case items
        case visualization
        case accessibilitySummary = "accessibility_summary"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        kind = try container.decode(WellnessModuleKind.self, forKey: .kind)
        title = try container.decode(String.self, forKey: .title)
        summary = try container.decode(String.self, forKey: .summary)
        items = try container.decodeIfPresent([WellnessSceneItem].self, forKey: .items) ?? []
        visualization = try container.decodeIfPresent(
            WellnessVisualization.self,
            forKey: .visualization
        )
        accessibilitySummary =
            try container.decodeIfPresent(String.self, forKey: .accessibilitySummary)
            ?? "\(title). \(summary)"
    }
}

public enum WellnessActionKind: String, Codable {
    case acceptProposal = "accept_proposal"
    case declineProposal = "decline_proposal"
    case modifyProposal = "modify_proposal"
    case createTask = "create_task"
    case createGoal = "create_goal"
    case openWebDetail = "open_web_detail"
    case refresh
    case switchLens = "switch_lens"
}

public struct WellnessSceneAction: Codable, Equatable, Identifiable {
    public let id: String
    public let kind: WellnessActionKind
    public let label: String
    public let proposalID: UUID?
    public let value: String?
    public let url: URL?

    public init(
        id: String,
        kind: WellnessActionKind,
        label: String,
        proposalID: UUID? = nil,
        value: String? = nil,
        url: URL? = nil
    ) {
        self.id = id
        self.kind = kind
        self.label = label
        self.proposalID = proposalID
        self.value = value
        self.url = url
    }

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case label
        case proposalID = "proposal_id"
        case value
        case url
    }
}

public struct WellnessScene: Codable, Equatable, Identifiable {
    public let schemaVersion: String
    public let id: String
    public let intent: String
    public let lens: WellnessLens
    public let title: String
    public let summary: String
    public let severity: WellnessSceneSeverity
    public let freshness: WellnessFreshness
    public let confidence: WellnessConfidence
    public let modules: [WellnessSceneModule]
    public let actions: [WellnessSceneAction]
    public let generatedAt: Date
    public let timezone: String

    public init(
        schemaVersion: String = "1",
        id: String,
        intent: String = "wellness_overview",
        lens: WellnessLens,
        title: String,
        summary: String,
        severity: WellnessSceneSeverity,
        freshness: WellnessFreshness,
        confidence: WellnessConfidence = WellnessConfidence(
            level: .insufficientData,
            coverage: "로컬 projection"
        ),
        modules: [WellnessSceneModule],
        actions: [WellnessSceneAction] = [],
        generatedAt: Date = Date(),
        timezone: String = TimeZone.current.identifier
    ) {
        self.schemaVersion = schemaVersion
        self.id = id
        self.intent = intent
        self.lens = lens
        self.title = title
        self.summary = summary
        self.severity = severity
        self.freshness = freshness
        self.confidence = confidence
        self.modules = modules
        self.actions = actions
        self.generatedAt = generatedAt
        self.timezone = timezone
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case id
        case intent
        case lens
        case title
        case summary
        case severity
        case freshness
        case confidence
        case modules
        case actions
        case generatedAt = "generated_at"
        case timezone
    }

    public var allowsProposalActions: Bool {
        freshness == .current && confidence.level != .insufficientData
    }

    public func allowsProposalActions(for proposalID: UUID) -> Bool {
        guard allowsProposalActions, exactProposalPreview(for: proposalID) != nil else {
            return false
        }
        let proposalActions = actions.filter { action in
            switch action.kind {
            case .acceptProposal, .declineProposal, .modifyProposal:
                return action.proposalID == proposalID
            default:
                return false
            }
        }
        guard proposalActions.allSatisfy({ $0.proposalID == proposalID }) else {
            return false
        }
        return proposalActions.contains { $0.kind == .acceptProposal }
            && proposalActions.contains { $0.kind == .declineProposal }
    }

    public func exactProposalPreview(for proposalID: UUID) -> WellnessProposalPreview? {
        let previews = modules.filter { $0.kind == .proposalPreview }
        guard previews.count == 1, let module = previews.first else { return nil }
        guard
            let preview = WellnessProposalPreview(module: module),
            preview.proposalID == proposalID
        else { return nil }
        return preview
    }

    public var exactMutationPreview: WellnessProposalPreview? {
        let proposalIDs = actions.compactMap { action -> UUID? in
            switch action.kind {
            case .acceptProposal, .declineProposal, .modifyProposal:
                return action.proposalID
            default:
                return nil
            }
        }
        guard Set(proposalIDs).count == 1, let proposalID = proposalIDs.first else {
            return nil
        }
        return exactProposalPreview(for: proposalID)
    }

    public static func fallback(
        lens: WellnessLens,
        title: String,
        reason: String,
        freshness: WellnessFreshness
    ) -> WellnessScene {
        WellnessScene(
            id: "fallback-\(lens.rawValue)",
            lens: lens,
            title: title,
            summary: reason,
            severity: .neutral,
            freshness: freshness,
            modules: [
                WellnessSceneModule(
                    id: "fallback",
                    kind: .fallback,
                    title: "판단을 만들 수 없습니다",
                    summary: reason
                )
            ],
            actions: [
                WellnessSceneAction(id: "refresh", kind: .refresh, label: "새로고침")
            ]
        )
    }
}

public struct WellnessProposalPreview: Equatable {
    public let proposalID: UUID
    public let task: String
    public let window: String
    public let reason: String?

    public init(proposalID: UUID, task: String, window: String, reason: String?) {
        self.proposalID = proposalID
        self.task = task
        self.window = window
        self.reason = reason
    }

    public init?(module: WellnessSceneModule) {
        guard module.kind == .proposalPreview else { return nil }
        let values = Dictionary(
            module.items.map { ($0.id, $0.value.trimmingCharacters(in: .whitespacesAndNewlines)) },
            uniquingKeysWith: { first, _ in first }
        )
        guard
            let rawID = values["proposal-id"],
            let proposalID = UUID(uuidString: rawID),
            let task = values["proposal-task"],
            !task.isEmpty,
            let window = values["proposal-window"],
            !window.isEmpty
        else { return nil }
        self.init(
            proposalID: proposalID,
            task: task,
            window: window,
            reason: values["proposal-reason"].flatMap { $0.isEmpty ? nil : $0 }
        )
    }

    public var dateInterval: DateInterval? {
        let bounds = window.split(separator: "/", maxSplits: 1).map(String.init)
        guard bounds.count == 2 else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let start = formatter.date(from: bounds[0]) ?? ISO8601DateFormatter().date(from: bounds[0])
        let end = formatter.date(from: bounds[1]) ?? ISO8601DateFormatter().date(from: bounds[1])
        guard let start, let end, end >= start else { return nil }
        return DateInterval(start: start, end: end)
    }

    public func localizedWindow(timezone: String) -> String {
        guard let interval = dateInterval else { return window }
        let timeZone = TimeZone(identifier: timezone) ?? .autoupdatingCurrent
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let day = DateFormatter()
        day.locale = .autoupdatingCurrent
        day.timeZone = timeZone
        day.dateStyle = .medium
        day.timeStyle = .none
        let time = DateFormatter()
        time.locale = .autoupdatingCurrent
        time.timeZone = timeZone
        time.dateStyle = .none
        time.timeStyle = .short
        if calendar.isDate(
            interval.start,
            inSameDayAs: interval.end
        ) {
            return "\(day.string(from: interval.start)) · \(time.string(from: interval.start))–\(time.string(from: interval.end))"
        }
        return "\(day.string(from: interval.start)) \(time.string(from: interval.start)) → \(day.string(from: interval.end)) \(time.string(from: interval.end))"
    }
}

public enum WellnessSceneDisplayPolicy {
    public static func primaryInsightModules(
        in scene: WellnessScene,
        maximumInsights: Int
    ) -> [WellnessSceneModule] {
        guard
            scene.freshness == .current,
            scene.confidence.level != .insufficientData
        else { return [] }
        let limit = max(maximumInsights, 0)
        return Array(
            scene.modules
                .filter { module in
                    guard let visualization = module.visualization else { return false }
                    switch visualization.kind {
                    case .calendarCanvas, .scheduleComparison:
                        return false
                    case .capacityBar, .factorContribution, .goalTrajectory:
                        return visualization.series
                            .flatMap(\.points)
                            .contains { $0.value != nil }
                    case .baselineBand, .comparisonBar:
                        return visualization.series.contains { series in
                            series.points.contains {
                                $0.value != nil && $0.secondaryValue != nil
                            }
                        }
                    case .energyCurve, .timeSeries, .eventAlignedTrend,
                        .decisionOutcome:
                        return visualization.series.contains { series in
                            series.points.filter {
                                $0.value != nil || $0.secondaryValue != nil
                            }.count >= 2
                        }
                    }
                }
                .prefix(limit)
        )
    }

    public static func visibleModules(
        in scene: WellnessScene,
        maximumVisualizations: Int
    ) -> [WellnessSceneModule] {
        let limit = max(maximumVisualizations, 0)
        let visualModules = scene.modules.filter { $0.visualization != nil }
        guard visualModules.count > limit else {
            return scene.modules
        }
        let selected = Set(
            visualModules
                .sorted { priority($0) < priority($1) }
                .prefix(limit)
                .map(\.id)
        )
        return scene.modules.filter {
            $0.visualization == nil || selected.contains($0.id)
        }
    }

    private static func priority(_ module: WellnessSceneModule) -> Int {
        switch module.visualization?.kind {
        case .scheduleComparison: return 0
        case .calendarCanvas: return 1
        default: return 2
        }
    }
}

public enum WellnessSceneValidationError: Error, Equatable {
    case emptyScene
    case emptyIdentity
    case duplicateItemID
    case duplicateActionID
    case mutationWithoutProposal
    case unexpectedMutation
    case proposalMismatch
    case multipleMutationProposals
    case createWithoutValue
    case unsafeWebURL
    case invalidLens
    case unsupportedSchema
    case emptyVisualization
    case invalidVisualizationRange
    case duplicateModuleID
    case duplicateVisualizationID
    case moduleVisualizationMismatch
    case invalidCalendarEventRange
    case nonCurrentMutation
    case invalidNumericValue
    case visualizationValueOutOfRange
    case inconsistentXAxis
    case missingExactProposalPreview
}

public enum WellnessSceneValidator {
    public static func validate(
        _ scene: WellnessScene,
        pairedBaseURL: URL?,
        expectedProposalID: UUID? = nil
    ) throws {
        guard scene.schemaVersion == "1" else {
            throw WellnessSceneValidationError.unsupportedSchema
        }
        guard !scene.modules.isEmpty else {
            throw WellnessSceneValidationError.emptyScene
        }
        guard isNonEmpty(scene.id) else {
            throw WellnessSceneValidationError.emptyIdentity
        }
        let moduleIDs = scene.modules.map(\.id)
        guard moduleIDs.allSatisfy(isNonEmpty) else {
            throw WellnessSceneValidationError.emptyIdentity
        }
        guard Set(moduleIDs).count == moduleIDs.count else {
            throw WellnessSceneValidationError.duplicateModuleID
        }
        let itemIDs = scene.modules.flatMap(\.items).map(\.id)
        guard itemIDs.allSatisfy(isNonEmpty) else {
            throw WellnessSceneValidationError.emptyIdentity
        }
        guard Set(itemIDs).count == itemIDs.count else {
            throw WellnessSceneValidationError.duplicateItemID
        }
        let actionIDs = scene.actions.map(\.id)
        guard actionIDs.allSatisfy(isNonEmpty) else {
            throw WellnessSceneValidationError.emptyIdentity
        }
        guard Set(actionIDs).count == actionIDs.count else {
            throw WellnessSceneValidationError.duplicateActionID
        }
        var visualizationIDs = Set<String>()
        var calendarEventIDs = Set<String>()
        for module in scene.modules {
            guard let visualization = module.visualization else { continue }
            guard module.kind.rawValue == visualization.kind.rawValue else {
                throw WellnessSceneValidationError.moduleVisualizationMismatch
            }
            let seriesIDs = visualization.series.map(\.id)
            let eventIDs = visualization.events.map(\.id)
            guard seriesIDs.allSatisfy(isNonEmpty), eventIDs.allSatisfy(isNonEmpty) else {
                throw WellnessSceneValidationError.emptyIdentity
            }
            guard Set(seriesIDs).count == seriesIDs.count,
                Set(eventIDs).count == eventIDs.count
            else {
                throw WellnessSceneValidationError.duplicateVisualizationID
            }
            for id in seriesIDs {
                guard visualizationIDs.insert(id).inserted else {
                    throw WellnessSceneValidationError.duplicateVisualizationID
                }
            }
            for id in eventIDs {
                guard calendarEventIDs.insert(id).inserted else {
                    throw WellnessSceneValidationError.duplicateVisualizationID
                }
            }
            let bounds = [visualization.minimum, visualization.maximum].compactMap { $0 }
            guard bounds.allSatisfy(\.isFinite) else {
                throw WellnessSceneValidationError.invalidNumericValue
            }
            if let minimum = visualization.minimum,
                let maximum = visualization.maximum,
                minimum >= maximum
            {
                throw WellnessSceneValidationError.invalidVisualizationRange
            }
            if visualization.kind == .calendarCanvas
                || visualization.kind == .scheduleComparison
            {
                guard !visualization.events.isEmpty else {
                    throw WellnessSceneValidationError.emptyVisualization
                }
            } else {
                let points = visualization.series.flatMap(\.points)
                guard points.allSatisfy({ isNonEmpty($0.label) }) else {
                    throw WellnessSceneValidationError.emptyIdentity
                }
                let values = points.flatMap { [$0.value, $0.secondaryValue] }.compactMap { $0 }
                guard !values.isEmpty else {
                    throw WellnessSceneValidationError.emptyVisualization
                }
                guard values.allSatisfy(\.isFinite) else {
                    throw WellnessSceneValidationError.invalidNumericValue
                }
                guard values.allSatisfy({
                    if let minimum = visualization.minimum, $0 < minimum {
                        return false
                    }
                    if let maximum = visualization.maximum, $0 > maximum {
                        return false
                    }
                    return true
                }) else {
                    throw WellnessSceneValidationError.visualizationValueOutOfRange
                }
                if requiresSharedXAxis(visualization.kind),
                    let labels = visualization.series.first?.points.map(\.label)
                {
                    guard visualization.series.dropFirst().allSatisfy({
                        $0.points.map(\.label) == labels
                    }) else {
                        throw WellnessSceneValidationError.inconsistentXAxis
                    }
                }
            }
            guard visualization.events.allSatisfy({ $0.startsAt < $0.endsAt }) else {
                throw WellnessSceneValidationError.invalidCalendarEventRange
            }
        }
        let mutationProposalIDs = scene.actions.compactMap { action -> UUID? in
            switch action.kind {
            case .acceptProposal, .declineProposal, .modifyProposal:
                return action.proposalID
            default:
                return nil
            }
        }
        guard Set(mutationProposalIDs).count <= 1 else {
            throw WellnessSceneValidationError.multipleMutationProposals
        }
        for action in scene.actions {
            switch action.kind {
            case .acceptProposal, .declineProposal, .modifyProposal:
                guard let proposalID = action.proposalID else {
                    throw WellnessSceneValidationError.mutationWithoutProposal
                }
                guard let expectedProposalID else {
                    throw WellnessSceneValidationError.unexpectedMutation
                }
                if proposalID != expectedProposalID {
                    throw WellnessSceneValidationError.proposalMismatch
                }
                guard scene.allowsProposalActions else {
                    throw WellnessSceneValidationError.nonCurrentMutation
                }
            case .createTask, .createGoal:
                guard let value = action.value?.trimmingCharacters(in: .whitespacesAndNewlines),
                      !value.isEmpty
                else {
                    throw WellnessSceneValidationError.createWithoutValue
                }
            case .openWebDetail:
                guard
                    let url = action.url,
                    let pairedBaseURL,
                    ViewerURL.hasSameOrigin(url, as: pairedBaseURL)
                else {
                    throw WellnessSceneValidationError.unsafeWebURL
                }
            case .switchLens:
                guard
                    let value = action.value,
                    WellnessLens(rawValue: value) != nil
                else {
                    throw WellnessSceneValidationError.invalidLens
                }
            case .refresh:
                break
            }
        }
        if let mutationProposalID = mutationProposalIDs.first,
            scene.exactProposalPreview(for: mutationProposalID) == nil
        {
            throw WellnessSceneValidationError.missingExactProposalPreview
        }
    }

    private static func isNonEmpty(_ value: String) -> Bool {
        !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private static func requiresSharedXAxis(_ kind: WellnessVisualizationKind) -> Bool {
        switch kind {
        case .energyCurve, .timeSeries, .eventAlignedTrend, .decisionOutcome:
            return true
        case .capacityBar, .calendarCanvas, .scheduleComparison, .baselineBand,
             .comparisonBar, .factorContribution, .goalTrajectory:
            return false
        }
    }
}

public enum WellnessCommandIntent: Equatable {
    case show(WellnessLens)
    case createTask(String)
    case createGoal(String)
    case clarify(String)
}

public enum WellnessCommandParser {
    public static func parse(_ input: String) -> WellnessCommandIntent? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let normalized = trimmed.lowercased()

        if let value = value(afterAnyPrefix: ["task:", "할 일:", "할일:"], in: trimmed) {
            return .createTask(value)
        }
        if let value = value(
            afterAnyPrefix: ["weekly goal:", "goal:", "주간 목표:", "목표:"],
            in: trimmed
        ) {
            return .createGoal(value)
        }
        if normalized.contains("변화") || normalized.contains("결과")
            || normalized.contains("패턴") || normalized.contains("outcome")
        {
            return .show(.change)
        }
        if normalized.contains("결정") || normalized.contains("조율")
            || normalized.contains("일정") || normalized.contains("목표")
            || normalized.contains("캘린더") || normalized.contains("옮")
            || normalized.contains("adjust")
        {
            return .show(.coordinate)
        }
        if normalized.contains("지금") || normalized.contains("상태")
            || normalized.contains("현재") || normalized.contains("피곤")
            || normalized.contains("회복") || normalized.contains("today")
            || normalized.contains("energy")
        {
            return .show(.now)
        }
        return .clarify(trimmed)
    }

    private static func value(afterAnyPrefix prefixes: [String], in input: String) -> String? {
        let lowered = input.lowercased()
        for prefix in prefixes {
            guard lowered.hasPrefix(prefix.lowercased()) else { continue }
            let index = input.index(input.startIndex, offsetBy: prefix.count)
            let value = input[index...].trimmingCharacters(in: .whitespacesAndNewlines)
            return value.isEmpty ? nil : value
        }
        return nil
    }
}
