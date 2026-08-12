import Foundation

public enum WorkspaceCanvasKind: String, Codable, CaseIterable, Identifiable {
    case dashboard
    case calendar
    case visualization
    case decisions
    case conversation
    case mixed

    public var id: String { rawValue }
}

public enum WorkspaceSystemChannel: String, Codable, CaseIterable, Identifiable {
    case overview
    case calendar
    case insights
    case decisions
    case agent

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .overview: return "overview"
        case .calendar: return "calendar"
        case .insights: return "insights"
        case .decisions: return "decisions"
        case .agent: return "agent"
        }
    }

    public var canvas: WorkspaceCanvasKind {
        switch self {
        case .overview: return .dashboard
        case .calendar: return .calendar
        case .insights: return .visualization
        case .decisions: return .decisions
        case .agent: return .conversation
        }
    }

    public var symbolName: String {
        switch self {
        case .overview: return "rectangle.grid.2x2"
        case .calendar: return "calendar"
        case .insights: return "chart.xyaxis.line"
        case .decisions: return "checkmark.bubble"
        case .agent: return "waveform"
        }
    }
}

public enum WorkspaceCardKind: String, Codable, CaseIterable, Identifiable {
    case wellnessStatus
    case capacity
    case calendarTimeline
    case energyCurve
    case baseline
    case factors
    case goalProgress
    case pendingDecision
    case decisionResult
    case nutrition
    case summary
    case command

    public var id: String { rawValue }
}

public enum WorkspaceCardSize: String, Codable, CaseIterable {
    case compact
    case regular
    case expanded
}

public struct WorkspaceCard: Codable, Equatable, Identifiable {
    public let id: UUID
    public var kind: WorkspaceCardKind
    public var size: WorkspaceCardSize
    public var isVisible: Bool

    public init(
        id: UUID = UUID(),
        kind: WorkspaceCardKind,
        size: WorkspaceCardSize = .regular,
        isVisible: Bool = true
    ) {
        self.id = id
        self.kind = kind
        self.size = size
        self.isVisible = isVisible
    }
}

public struct WorkspaceChannel: Codable, Equatable, Identifiable {
    public let id: UUID
    public var systemKind: WorkspaceSystemChannel?
    public var title: String
    public var symbolName: String
    public var colorHex: String
    public var canvas: WorkspaceCanvasKind
    public var cards: [WorkspaceCard]
    public var isHidden: Bool
    public var isFavorite: Bool

    public var isSystem: Bool { systemKind != nil }

    public init(
        id: UUID = UUID(),
        systemKind: WorkspaceSystemChannel? = nil,
        title: String,
        symbolName: String = "number",
        colorHex: String = "#2F6B55",
        canvas: WorkspaceCanvasKind,
        cards: [WorkspaceCard] = [],
        isHidden: Bool = false,
        isFavorite: Bool = false
    ) {
        self.id = id
        self.systemKind = systemKind
        self.title = title
        self.symbolName = symbolName
        self.colorHex = colorHex
        self.canvas = canvas
        self.cards = cards
        self.isHidden = isHidden
        self.isFavorite = isFavorite
    }
}

public struct WorkspaceCategory: Codable, Equatable, Identifiable {
    public let id: UUID
    public var title: String
    public var symbolName: String?
    public var colorHex: String?
    public var isSystem: Bool
    public var isCollapsed: Bool
    public var channels: [WorkspaceChannel]

    public init(
        id: UUID = UUID(),
        title: String,
        symbolName: String? = nil,
        colorHex: String? = nil,
        isSystem: Bool = false,
        isCollapsed: Bool = false,
        channels: [WorkspaceChannel] = []
    ) {
        self.id = id
        self.title = title
        self.symbolName = symbolName
        self.colorHex = colorHex
        self.isSystem = isSystem
        self.isCollapsed = isCollapsed
        self.channels = channels
    }
}

public enum WorkspaceThreadAnchorKind: String, Codable {
    case post
    case card
    case calendarEvent
    case visualization
    case decision
    case nutrition
}

public struct WorkspaceThreadAnchor: Codable, Equatable {
    public var kind: WorkspaceThreadAnchorKind
    public var localID: String
    public var title: String
    public var proposalID: UUID?
    public var decisionRecordID: UUID?

    public init(
        kind: WorkspaceThreadAnchorKind,
        localID: String,
        title: String,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) {
        self.kind = kind
        self.localID = localID
        self.title = title
        self.proposalID = proposalID
        self.decisionRecordID = decisionRecordID
    }
}

public enum WorkspaceMessageAuthor: String, Codable {
    case user
    case healthmes
    case system
}

public struct WorkspaceThreadMessage: Codable, Equatable, Identifiable {
    public let id: UUID
    public var author: WorkspaceMessageAuthor
    public var body: String
    public var createdAt: Date
    public var isLocalOnly: Bool

    public init(
        id: UUID = UUID(),
        author: WorkspaceMessageAuthor,
        body: String,
        createdAt: Date = Date(),
        isLocalOnly: Bool = true
    ) {
        self.id = id
        self.author = author
        self.body = body
        self.createdAt = createdAt
        self.isLocalOnly = isLocalOnly
    }
}

public struct WorkspaceThread: Codable, Equatable, Identifiable {
    public let id: UUID
    public var channelID: UUID
    public var anchor: WorkspaceThreadAnchor
    public var messages: [WorkspaceThreadMessage]
    public var draft: String
    public var updatedAt: Date

    public init(
        id: UUID = UUID(),
        channelID: UUID,
        anchor: WorkspaceThreadAnchor,
        messages: [WorkspaceThreadMessage] = [],
        draft: String = "",
        updatedAt: Date = Date()
    ) {
        self.id = id
        self.channelID = channelID
        self.anchor = anchor
        self.messages = messages
        self.draft = draft
        self.updatedAt = updatedAt
    }
}

public struct WorkspaceState: Codable, Equatable {
    public static let currentSchemaVersion = 1
    public static let maximumUserCategories = 40
    public static let maximumChannelsPerCategory = 80
    public static let maximumMessagesPerThread = 500

    public var schemaVersion: Int
    public var categories: [WorkspaceCategory]
    public var threads: [WorkspaceThread]
    public var selectedChannelID: UUID?
    public var selectedThreadID: UUID?

    public init(
        schemaVersion: Int = Self.currentSchemaVersion,
        categories: [WorkspaceCategory],
        threads: [WorkspaceThread] = [],
        selectedChannelID: UUID? = nil,
        selectedThreadID: UUID? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.categories = categories
        self.threads = threads
        self.selectedChannelID = selectedChannelID
        self.selectedThreadID = selectedThreadID
    }

    public static func defaults() -> WorkspaceState {
        let channels = WorkspaceSystemChannel.allCases.map { kind in
            WorkspaceChannel(
                id: stableSystemChannelID(kind),
                systemKind: kind,
                title: kind.title,
                symbolName: kind.symbolName,
                canvas: kind.canvas,
                cards: defaultCards(for: kind)
            )
        }
        return WorkspaceState(
            categories: [
                WorkspaceCategory(
                    id: stableSystemCategoryID,
                    title: "기본 기능",
                    symbolName: "sparkles",
                    isSystem: true,
                    channels: channels
                )
            ],
            selectedChannelID: channels.first?.id
        )
    }

    public func normalized() -> WorkspaceState {
        guard schemaVersion <= Self.currentSchemaVersion else {
            return Self.defaults()
        }

        var result = self
        result.schemaVersion = Self.currentSchemaVersion
        let reservedCategoryIDs = Set<UUID>([Self.stableSystemCategoryID])
        let reservedChannelIDs = Set<UUID>(
            WorkspaceSystemChannel.allCases.map(Self.systemChannelID)
        )
        var seenCategoryIDs = reservedCategoryIDs
        var seenChannelIDs = reservedChannelIDs
        var seenThreadIDs = Set<UUID>()
        var remappedChannelIDs = [UUID: UUID]()

        result.categories = categories.compactMap { category in
            guard !category.isSystem else { return nil }
            var normalizedCategory = category
            if reservedCategoryIDs.contains(category.id) {
                normalizedCategory = WorkspaceCategory(
                    title: category.title,
                    symbolName: category.symbolName,
                    colorHex: category.colorHex,
                    isCollapsed: category.isCollapsed,
                    channels: category.channels
                )
                seenCategoryIDs.insert(normalizedCategory.id)
            } else if !seenCategoryIDs.insert(category.id).inserted {
                return nil
            }
            normalizedCategory.title = Self.cleanTitle(
                category.title,
                fallback: "새 카테고리"
            )
            normalizedCategory.channels = category.channels.compactMap { channel in
                var normalizedChannel = channel
                normalizedChannel.systemKind = nil
                if reservedChannelIDs.contains(channel.id) {
                    normalizedChannel = WorkspaceChannel(
                        title: channel.title,
                        symbolName: channel.symbolName,
                        colorHex: channel.colorHex,
                        canvas: channel.canvas,
                        cards: channel.cards,
                        isHidden: channel.isHidden,
                        isFavorite: channel.isFavorite
                    )
                    remappedChannelIDs[channel.id] = normalizedChannel.id
                    seenChannelIDs.insert(normalizedChannel.id)
                } else if !seenChannelIDs.insert(channel.id).inserted {
                    return nil
                }
                normalizedChannel.title = Self.cleanTitle(channel.title, fallback: "새 채널")
                normalizedChannel.cards = Self.normalizedCards(channel.cards)
                return normalizedChannel
            }
            return normalizedCategory
        }

        result.installOrRepairSystemCategory(from: categories)
        let validChannelIDs = Set(
            result.categories.flatMap(\.channels).map(\.id)
        )
        result.threads = threads.compactMap { thread in
            let channelID = remappedChannelIDs[thread.channelID] ?? thread.channelID
            guard
                validChannelIDs.contains(channelID),
                seenThreadIDs.insert(thread.id).inserted
            else { return nil }
            var normalizedThread = thread
            normalizedThread.channelID = channelID
            normalizedThread.messages = thread.messages.filter {
                !$0.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
            normalizedThread.draft = String(thread.draft.prefix(4_000))
            return normalizedThread
        }
        if let selected = result.selectedChannelID,
            let remapped = remappedChannelIDs[selected]
        {
            result.selectedChannelID = remapped
        }
        if !validChannelIDs.contains(result.selectedChannelID ?? UUID())
            || result.channel(with: result.selectedChannelID)?.isHidden == true
        {
            result.selectedChannelID = result.firstVisibleChannelID
        }
        if !result.threads.contains(where: { $0.id == result.selectedThreadID }) {
            result.selectedThreadID = nil
        }
        return result
    }

    public static func systemChannelID(_ kind: WorkspaceSystemChannel) -> UUID {
        stableSystemChannelID(kind)
    }

    private mutating func installOrRepairSystemCategory(from originalCategories: [WorkspaceCategory]) {
        let preservedChannels = originalCategories
            .flatMap(\.channels)
            .reduce(into: [WorkspaceSystemChannel: WorkspaceChannel]()) { result, channel in
                if let kind = channel.systemKind {
                    result[kind] = channel
                }
            }
        let preservedCollapsed = originalCategories
            .first(where: { $0.isSystem })?
            .isCollapsed ?? false
        let systemChannels = WorkspaceSystemChannel.allCases.map { kind in
            let id = Self.systemChannelID(kind)
            let preserved = preservedChannels[kind]
            return WorkspaceChannel(
                id: id,
                systemKind: kind,
                title: kind.title,
                symbolName: kind.symbolName,
                canvas: kind.canvas,
                cards: Self.defaultCards(for: kind),
                isHidden: preserved?.isHidden ?? false,
                isFavorite: preserved?.isFavorite ?? false
            )
        }
        categories.removeAll { $0.isSystem }
        categories.insert(
            WorkspaceCategory(
                id: Self.stableSystemCategoryID,
                title: "기본 기능",
                symbolName: "sparkles",
                isSystem: true,
                isCollapsed: preservedCollapsed,
                channels: systemChannels
            ),
            at: 0
        )
    }

    private func channel(with id: UUID?) -> WorkspaceChannel? {
        guard let id else { return nil }
        return categories.flatMap(\.channels).first { $0.id == id }
    }

    private var firstVisibleChannelID: UUID {
        categories
            .flatMap(\.channels)
            .first(where: { !$0.isHidden })?
            .id ?? Self.systemChannelID(.overview)
    }

    private static func cleanTitle(_ title: String, fallback: String) -> String {
        let clean = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return clean.isEmpty ? fallback : String(clean.prefix(80))
    }

    private static func normalizedCards(_ cards: [WorkspaceCard]) -> [WorkspaceCard] {
        var seen = Set<UUID>()
        return cards.filter { seen.insert($0.id).inserted }.prefix(30).map { $0 }
    }

    private static func defaultCards(for channel: WorkspaceSystemChannel) -> [WorkspaceCard] {
        switch channel {
        case .overview:
            return [
                WorkspaceCard(kind: .wellnessStatus, size: .compact),
                WorkspaceCard(kind: .calendarTimeline, size: .regular),
                WorkspaceCard(kind: .pendingDecision, size: .regular),
            ]
        case .calendar:
            return [
                WorkspaceCard(kind: .calendarTimeline, size: .expanded),
                WorkspaceCard(kind: .capacity, size: .compact),
            ]
        case .insights:
            return [
                WorkspaceCard(kind: .energyCurve, size: .expanded),
                WorkspaceCard(kind: .baseline),
                WorkspaceCard(kind: .factors),
            ]
        case .decisions:
            return [
                WorkspaceCard(kind: .pendingDecision),
                WorkspaceCard(kind: .decisionResult),
            ]
        case .agent:
            return [WorkspaceCard(kind: .command, size: .expanded)]
        }
    }

    private static let stableSystemCategoryID = UUID(
        uuidString: "7A1E9D50-691D-4A5A-96CF-3B4D2D1E1000"
    )!

    private static func stableSystemChannelID(_ kind: WorkspaceSystemChannel) -> UUID {
        let suffix: String
        switch kind {
        case .overview: suffix = "1001"
        case .calendar: suffix = "1002"
        case .insights: suffix = "1003"
        case .decisions: suffix = "1004"
        case .agent: suffix = "1005"
        }
        return UUID(uuidString: "7A1E9D50-691D-4A5A-96CF-3B4D2D1E\(suffix)")!
    }
}

public enum WorkspaceLocalStoreMode: Equatable {
    case writable
    case incompatibleFutureSchema(version: Int)
    case corrupted

    public var isWritable: Bool {
        self == .writable
    }

    public var userMessage: String? {
        switch self {
        case .writable:
            return nil
        case .incompatibleFutureSchema:
            return "이 기기의 워크스페이스는 더 최신 버전에서 만들어졌습니다. 현재 버전에서는 표시하거나 편집하지 않고 원본을 그대로 보존합니다."
        case .corrupted:
            return "로컬 워크스페이스를 읽을 수 없습니다. 원본을 보호하기 위해 편집을 중지했습니다."
        }
    }
}

public struct WorkspaceLocalSnapshot {
    public let state: WorkspaceState
    public let mode: WorkspaceLocalStoreMode

    public init(state: WorkspaceState, mode: WorkspaceLocalStoreMode) {
        self.state = state
        self.mode = mode
    }
}

public final class WorkspaceLocalStore {
    public static let shared = WorkspaceLocalStore()
    public static let defaultsKey = "healthmes.workspace.state.v1"

    private let defaults: UserDefaults
    private let namespace: () -> String
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(
        defaults: UserDefaults = AppGroup.userDefaults,
        namespace: @escaping () -> String = {
            PairingStore.shared.load()?.cacheFingerprint ?? "unpaired"
        }
    ) {
        self.defaults = defaults
        self.namespace = namespace
        encoder = JSONEncoder()
        decoder = JSONDecoder()
        encoder.dateEncodingStrategy = .iso8601
        decoder.dateDecodingStrategy = .iso8601
    }

    public func loadSnapshot() -> WorkspaceLocalSnapshot {
        guard let data = defaults.data(forKey: scopedDefaultsKey) else {
            return WorkspaceLocalSnapshot(
                state: WorkspaceState.defaults(),
                mode: .writable
            )
        }
        if let version = Self.schemaVersion(in: data),
            version > WorkspaceState.currentSchemaVersion
        {
            return WorkspaceLocalSnapshot(
                state: WorkspaceState.defaults(),
                mode: .incompatibleFutureSchema(version: version)
            )
        }
        guard let decoded = try? decoder.decode(WorkspaceState.self, from: data) else {
            return WorkspaceLocalSnapshot(
                state: WorkspaceState.defaults(),
                mode: .corrupted
            )
        }
        return WorkspaceLocalSnapshot(
            state: decoded.normalized(),
            mode: .writable
        )
    }

    public func load() -> WorkspaceState {
        loadSnapshot().state
    }

    @discardableResult
    public func save(_ state: WorkspaceState) -> WorkspaceLocalSnapshot {
        let current = loadSnapshot()
        guard current.mode.isWritable else {
            return current
        }
        let normalized = state.normalized()
        guard let data = try? encoder.encode(normalized) else {
            return current
        }
        defaults.set(data, forKey: scopedDefaultsKey)
        return WorkspaceLocalSnapshot(state: normalized, mode: .writable)
    }

    public func reset() -> WorkspaceLocalSnapshot {
        defaults.removeObject(forKey: scopedDefaultsKey)
        return WorkspaceLocalSnapshot(
            state: WorkspaceState.defaults(),
            mode: .writable
        )
    }

    private var scopedDefaultsKey: String {
        "\(Self.defaultsKey).\(namespace())"
    }

    private static func schemaVersion(in data: Data) -> Int? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let dictionary = object as? [String: Any],
            let number = dictionary["schemaVersion"] as? NSNumber
        else { return nil }
        return number.intValue
    }
}
