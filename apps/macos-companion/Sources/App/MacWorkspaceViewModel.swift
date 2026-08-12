import Foundation

@MainActor
final class MacWorkspaceViewModel: ObservableObject {
    @Published private(set) var state: WorkspaceState

    private let store: WorkspaceLocalStore

    init(store: WorkspaceLocalStore = .shared) {
        self.store = store
        state = store.load()
    }

    var categories: [WorkspaceCategory] {
        state.categories
    }

    var selectedChannel: WorkspaceChannel? {
        channel(id: state.selectedChannelID)
    }

    var selectedThread: WorkspaceThread? {
        guard let id = state.selectedThreadID else { return nil }
        return state.threads.first(where: { $0.id == id })
    }

    var favoriteChannels: [WorkspaceChannel] {
        state.categories
            .flatMap(\.channels)
            .filter { $0.isFavorite && !$0.isHidden }
    }

    func channel(id: UUID?) -> WorkspaceChannel? {
        guard let id else { return nil }
        return state.categories
            .lazy
            .flatMap(\.channels)
            .first(where: { $0.id == id })
    }

    func selectChannel(_ channelID: UUID) {
        guard channel(id: channelID) != nil else { return }
        mutate { state in
            state.selectedChannelID = channelID
            if let threadID = state.selectedThreadID,
                state.threads.first(where: { $0.id == threadID })?.channelID != channelID
            {
                state.selectedThreadID = nil
            }
        }
    }

    func toggleCategory(_ categoryID: UUID) {
        mutate { state in
            guard let index = state.categories.firstIndex(where: { $0.id == categoryID }) else {
                return
            }
            state.categories[index].isCollapsed.toggle()
        }
    }

    func addCategory(title: String, symbolName: String?, colorHex: String?) {
        let clean = cleanTitle(title, fallback: "New category")
        mutate { state in
            state.categories.append(
                WorkspaceCategory(
                    title: clean,
                    symbolName: symbolName,
                    colorHex: colorHex,
                    channels: []
                )
            )
        }
    }

    func renameCategory(_ categoryID: UUID, title: String) {
        let clean = cleanTitle(title, fallback: "New category")
        mutate { state in
            guard
                let index = state.categories.firstIndex(where: {
                    $0.id == categoryID && !$0.isSystem
                })
            else { return }
            state.categories[index].title = clean
        }
    }

    func deleteCategory(_ categoryID: UUID) {
        mutate { state in
            guard
                let category = state.categories.first(where: {
                    $0.id == categoryID && !$0.isSystem
                })
            else { return }
            let removedChannelIDs = Set(category.channels.map(\.id))
            state.categories.removeAll { $0.id == categoryID }
            state.threads.removeAll { removedChannelIDs.contains($0.channelID) }
            if removedChannelIDs.contains(state.selectedChannelID ?? UUID()) {
                state.selectedChannelID = WorkspaceState.systemChannelID(.overview)
                state.selectedThreadID = nil
            }
        }
    }

    func addChannel(
        categoryID: UUID,
        title: String,
        symbolName: String,
        canvas: WorkspaceCanvasKind
    ) {
        let clean = cleanTitle(title, fallback: "New channel")
        mutate { state in
            guard
                let index = state.categories.firstIndex(where: {
                    $0.id == categoryID && !$0.isSystem
                })
            else { return }
            let channel = WorkspaceChannel(
                title: clean,
                symbolName: symbolName,
                canvas: canvas,
                cards: Self.defaultCards(for: canvas)
            )
            state.categories[index].channels.append(channel)
            state.categories[index].isCollapsed = false
            state.selectedChannelID = channel.id
            state.selectedThreadID = nil
        }
    }

    func renameChannel(_ channelID: UUID, title: String) {
        let clean = cleanTitle(title, fallback: "New channel")
        mutate { state in
            guard let location = Self.channelLocation(channelID, in: state) else { return }
            guard !state.categories[location.category].channels[location.channel].isSystem else {
                return
            }
            state.categories[location.category].channels[location.channel].title = clean
        }
    }

    func deleteChannel(_ channelID: UUID) {
        mutate { state in
            guard let location = Self.channelLocation(channelID, in: state) else { return }
            guard !state.categories[location.category].channels[location.channel].isSystem else {
                return
            }
            state.categories[location.category].channels.remove(at: location.channel)
            state.threads.removeAll { $0.channelID == channelID }
            if state.selectedChannelID == channelID {
                state.selectedChannelID = WorkspaceState.systemChannelID(.overview)
                state.selectedThreadID = nil
            }
        }
    }

    func moveCategory(_ categoryID: UUID, offset: Int) {
        mutate { state in
            guard
                let source = state.categories.firstIndex(where: {
                    $0.id == categoryID && !$0.isSystem
                })
            else { return }
            let destination = min(max(source + offset, 1), state.categories.count - 1)
            guard source != destination else { return }
            let category = state.categories.remove(at: source)
            state.categories.insert(category, at: destination)
        }
    }

    func moveChannel(_ channelID: UUID, offset: Int) {
        mutate { state in
            guard let location = Self.channelLocation(channelID, in: state) else { return }
            let channels = state.categories[location.category].channels
            let destination = min(max(location.channel + offset, 0), channels.count - 1)
            guard location.channel != destination else { return }
            let channel = state.categories[location.category].channels.remove(
                at: location.channel
            )
            state.categories[location.category].channels.insert(
                channel,
                at: destination
            )
        }
    }

    func moveChannel(_ channelID: UUID, to categoryID: UUID) {
        mutate { state in
            guard
                let source = Self.channelLocation(channelID, in: state),
                !state.categories[source.category].channels[source.channel].isSystem,
                let destination = state.categories.firstIndex(where: {
                    $0.id == categoryID && !$0.isSystem
                }),
                source.category != destination
            else { return }
            let channel = state.categories[source.category].channels.remove(
                at: source.channel
            )
            state.categories[destination].channels.append(channel)
            state.categories[destination].isCollapsed = false
        }
    }

    func toggleFavorite(_ channelID: UUID) {
        mutate { state in
            guard let location = Self.channelLocation(channelID, in: state) else { return }
            state.categories[location.category].channels[location.channel].isFavorite.toggle()
        }
    }

    func openThread(
        channelID: UUID,
        anchor: WorkspaceThreadAnchor,
        initialMessage: String? = nil
    ) {
        mutate { state in
            state.selectedChannelID = channelID
            if let existing = state.threads.first(where: {
                $0.channelID == channelID
                    && $0.anchor.kind == anchor.kind
                    && $0.anchor.localID == anchor.localID
            }) {
                state.selectedThreadID = existing.id
                return
            }

            let messages: [WorkspaceThreadMessage]
            if let initialMessage, !initialMessage.isEmpty {
                messages = [
                    WorkspaceThreadMessage(
                        author: .healthmes,
                        body: initialMessage
                    )
                ]
            } else {
                messages = []
            }
            let thread = WorkspaceThread(
                channelID: channelID,
                anchor: anchor,
                messages: messages
            )
            state.threads.append(thread)
            state.selectedThreadID = thread.id
        }
    }

    func closeThread() {
        mutate { $0.selectedThreadID = nil }
    }

    func updateSelectedThreadDraft(_ draft: String) {
        mutate { state in
            guard
                let id = state.selectedThreadID,
                let index = state.threads.firstIndex(where: { $0.id == id })
            else { return }
            state.threads[index].draft = String(draft.prefix(4_000))
            state.threads[index].updatedAt = Date()
        }
    }

    func sendSelectedThreadMessage() {
        mutate { state in
            guard
                let id = state.selectedThreadID,
                let index = state.threads.firstIndex(where: { $0.id == id })
            else { return }
            let body = state.threads[index].draft
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !body.isEmpty else { return }
            state.threads[index].messages.append(
                WorkspaceThreadMessage(author: .user, body: body)
            )
            state.threads[index].draft = ""
            state.threads[index].updatedAt = Date()
        }
    }

    func appendMessage(
        to threadID: UUID,
        author: WorkspaceMessageAuthor,
        body: String
    ) {
        let clean = body.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        mutate { state in
            guard let index = state.threads.firstIndex(where: { $0.id == threadID }) else {
                return
            }
            state.threads[index].messages.append(
                WorkspaceThreadMessage(author: author, body: clean)
            )
            state.threads[index].updatedAt = Date()
        }
    }

    func reloadForPairingChange() {
        state = store.load()
    }

    private func mutate(_ mutation: (inout WorkspaceState) -> Void) {
        var next = state
        mutation(&next)
        state = store.save(next)
    }

    private func cleanTitle(_ title: String, fallback: String) -> String {
        let clean = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return clean.isEmpty ? fallback : String(clean.prefix(80))
    }

    private static func channelLocation(
        _ channelID: UUID,
        in state: WorkspaceState
    ) -> (category: Int, channel: Int)? {
        for categoryIndex in state.categories.indices {
            if let channelIndex = state.categories[categoryIndex].channels.firstIndex(
                where: { $0.id == channelID }
            ) {
                return (categoryIndex, channelIndex)
            }
        }
        return nil
    }

    private static func defaultCards(for canvas: WorkspaceCanvasKind) -> [WorkspaceCard] {
        switch canvas {
        case .dashboard, .mixed:
            return [
                WorkspaceCard(kind: .wellnessStatus, size: .compact),
                WorkspaceCard(kind: .calendarTimeline),
                WorkspaceCard(kind: .pendingDecision),
            ]
        case .calendar:
            return [
                WorkspaceCard(kind: .calendarTimeline, size: .expanded),
                WorkspaceCard(kind: .capacity, size: .compact),
            ]
        case .visualization:
            return [
                WorkspaceCard(kind: .energyCurve, size: .expanded),
                WorkspaceCard(kind: .baseline),
            ]
        case .decisions:
            return [
                WorkspaceCard(kind: .pendingDecision),
                WorkspaceCard(kind: .decisionResult),
            ]
        case .conversation:
            return [WorkspaceCard(kind: .command, size: .expanded)]
        }
    }
}
