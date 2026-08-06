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
    case fallback
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
    public let accessibilitySummary: String

    public init(
        id: String,
        kind: WellnessModuleKind,
        title: String,
        summary: String,
        items: [WellnessSceneItem] = [],
        accessibilitySummary: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.summary = summary
        self.items = items
        self.accessibilitySummary = accessibilitySummary ?? "\(title). \(summary)"
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
}

public struct WellnessScene: Codable, Equatable, Identifiable {
    public let id: String
    public let lens: WellnessLens
    public let title: String
    public let summary: String
    public let severity: WellnessSceneSeverity
    public let freshness: WellnessFreshness
    public let modules: [WellnessSceneModule]
    public let actions: [WellnessSceneAction]
    public let generatedAt: Date

    public init(
        id: String,
        lens: WellnessLens,
        title: String,
        summary: String,
        severity: WellnessSceneSeverity,
        freshness: WellnessFreshness,
        modules: [WellnessSceneModule],
        actions: [WellnessSceneAction] = [],
        generatedAt: Date = Date()
    ) {
        self.id = id
        self.lens = lens
        self.title = title
        self.summary = summary
        self.severity = severity
        self.freshness = freshness
        self.modules = modules
        self.actions = actions
        self.generatedAt = generatedAt
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

public enum WellnessSceneValidationError: Error, Equatable {
    case emptyScene
    case duplicateItemID
    case mutationWithoutProposal
    case createWithoutValue
    case unsafeWebURL
    case invalidLens
}

public enum WellnessSceneValidator {
    public static func validate(
        _ scene: WellnessScene,
        pairedBaseURL: URL?
    ) throws {
        guard !scene.modules.isEmpty else {
            throw WellnessSceneValidationError.emptyScene
        }
        let itemIDs = scene.modules.flatMap(\.items).map(\.id)
        guard Set(itemIDs).count == itemIDs.count else {
            throw WellnessSceneValidationError.duplicateItemID
        }
        for action in scene.actions {
            switch action.kind {
            case .acceptProposal, .declineProposal, .modifyProposal:
                guard action.proposalID != nil else {
                    throw WellnessSceneValidationError.mutationWithoutProposal
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
            || normalized.contains("옮") || normalized.contains("adjust")
        {
            return .show(.coordinate)
        }
        if normalized.contains("지금") || normalized.contains("상태")
            || normalized.contains("today") || normalized.contains("energy")
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
