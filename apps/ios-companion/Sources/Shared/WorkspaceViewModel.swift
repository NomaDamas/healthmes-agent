import Combine
import Foundation

@MainActor
public final class WorkspaceViewModel: ObservableObject {
    @Published public private(set) var state: WorkspaceState

    private let store: WorkspaceLocalStore

    public init(store: WorkspaceLocalStore = .shared) {
        self.store = store
        state = store.load()
    }

    public var selectedChannel: WorkspaceChannel? {
        guard let selectedID = state.selectedChannelID else { return nil }
        return state.categories
            .flatMap(\.channels)
            .first { $0.id == selectedID }
    }

    public var selectedThread: WorkspaceThread? {
        guard let selectedID = state.selectedThreadID else { return nil }
        return state.threads.first { $0.id == selectedID }
    }

    public func selectChannel(_ id: UUID) {
        state.selectedChannelID = id
        state.selectedThreadID = nil
        persist()
    }

    public func selectThread(_ id: UUID?) {
        state.selectedThreadID = id
        persist()
    }

    @discardableResult
    public func createCategory(title: String) -> UUID {
        let category = WorkspaceCategory(
            title: title,
            symbolName: "folder",
            colorHex: "#2F6B55"
        )
        state.categories.append(category)
        persist()
        return category.id
    }

    public func renameCategory(_ id: UUID, title: String) {
        guard let index = state.categories.firstIndex(where: { $0.id == id }),
            !state.categories[index].isSystem
        else { return }
        state.categories[index].title = title
        persist()
    }

    public func deleteCategory(_ id: UUID) {
        guard let category = state.categories.first(where: { $0.id == id }),
            !category.isSystem
        else { return }
        let removedChannelIDs = Set(category.channels.map(\.id))
        state.categories.removeAll { $0.id == id }
        state.threads.removeAll { removedChannelIDs.contains($0.channelID) }
        if removedChannelIDs.contains(state.selectedChannelID ?? UUID()) {
            state.selectedChannelID = WorkspaceState.systemChannelID(.overview)
        }
        persist()
    }

    public func setCategoryCollapsed(_ id: UUID, collapsed: Bool) {
        guard let index = state.categories.firstIndex(where: { $0.id == id }) else {
            return
        }
        state.categories[index].isCollapsed = collapsed
        persist()
    }

    @discardableResult
    public func createChannel(
        in categoryID: UUID,
        title: String,
        canvas: WorkspaceCanvasKind
    ) -> UUID? {
        guard let index = state.categories.firstIndex(where: { $0.id == categoryID }),
            !state.categories[index].isSystem
        else { return nil }
        let channel = WorkspaceChannel(
            title: title,
            canvas: canvas,
            cards: defaultCards(for: canvas)
        )
        state.categories[index].channels.append(channel)
        state.selectedChannelID = channel.id
        persist()
        return channel.id
    }

    public func renameChannel(_ id: UUID, title: String) {
        mutateChannel(id) { channel in
            guard !channel.isSystem else { return }
            channel.title = title
        }
    }

    public func deleteChannel(_ id: UUID) {
        guard selectedChannel(with: id)?.isSystem == false else { return }
        for index in state.categories.indices {
            state.categories[index].channels.removeAll { $0.id == id }
        }
        state.threads.removeAll { $0.channelID == id }
        if state.selectedChannelID == id {
            state.selectedChannelID = WorkspaceState.systemChannelID(.overview)
        }
        persist()
    }

    public func setChannelFavorite(_ id: UUID, favorite: Bool) {
        mutateChannel(id) { $0.isFavorite = favorite }
    }

    public func setChannelHidden(_ id: UUID, hidden: Bool) {
        mutateChannel(id) { $0.isHidden = hidden }
        if hidden, state.selectedChannelID == id {
            state.selectedChannelID = state.categories
                .flatMap(\.channels)
                .first(where: { !$0.isHidden })?
                .id
            persist()
        }
    }

    public func restoreSystemChannels() {
        for categoryIndex in state.categories.indices {
            for channelIndex in state.categories[categoryIndex].channels.indices
            where state.categories[categoryIndex].channels[channelIndex].isSystem {
                state.categories[categoryIndex].channels[channelIndex].isHidden = false
            }
        }
        persist()
    }

    public func moveCategory(_ id: UUID, offset: Int) {
        guard let source = state.categories.firstIndex(where: { $0.id == id }),
            !state.categories[source].isSystem
        else { return }
        let destination = min(max(source + offset, 1), state.categories.count - 1)
        guard source != destination else { return }
        let category = state.categories.remove(at: source)
        state.categories.insert(category, at: destination)
        persist()
    }

    public func moveChannel(_ id: UUID, offset: Int) {
        for categoryIndex in state.categories.indices {
            guard let source = state.categories[categoryIndex].channels
                .firstIndex(where: { $0.id == id })
            else { continue }
            let destination = min(
                max(source + offset, 0),
                state.categories[categoryIndex].channels.count - 1
            )
            guard source != destination else { return }
            let channel = state.categories[categoryIndex].channels.remove(at: source)
            state.categories[categoryIndex].channels.insert(channel, at: destination)
            persist()
            return
        }
    }

    public func moveChannel(_ channelID: UUID, to categoryID: UUID) {
        guard let channel = selectedChannel(with: channelID), !channel.isSystem,
            let destination = state.categories.firstIndex(where: { $0.id == categoryID }),
            !state.categories[destination].isSystem
        else { return }
        for index in state.categories.indices {
            state.categories[index].channels.removeAll { $0.id == channelID }
        }
        state.categories[destination].channels.append(channel)
        persist()
    }

    @discardableResult
    public func openThread(
        channelID: UUID,
        anchor: WorkspaceThreadAnchor
    ) -> UUID {
        if let existing = state.threads.first(where: {
            $0.channelID == channelID
                && $0.anchor.kind == anchor.kind
                && $0.anchor.localID == anchor.localID
        }) {
            state.selectedThreadID = existing.id
            persist()
            return existing.id
        }
        let thread = WorkspaceThread(channelID: channelID, anchor: anchor)
        state.threads.append(thread)
        state.selectedThreadID = thread.id
        persist()
        return thread.id
    }

    public func updateDraft(threadID: UUID, draft: String) {
        guard let index = state.threads.firstIndex(where: { $0.id == threadID }) else {
            return
        }
        state.threads[index].draft = String(draft.prefix(4_000))
        state.threads[index].updatedAt = Date()
        persist()
    }

    public func sendMessage(threadID: UUID, author: WorkspaceMessageAuthor = .user) {
        guard let index = state.threads.firstIndex(where: { $0.id == threadID }) else {
            return
        }
        let body = state.threads[index].draft
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return }
        state.threads[index].messages.append(
            WorkspaceThreadMessage(author: author, body: body)
        )
        state.threads[index].messages = Array(
            state.threads[index].messages.suffix(500)
        )
        state.threads[index].draft = ""
        state.threads[index].updatedAt = Date()
        persist()
    }

    public func reset() {
        state = store.reset()
    }

    public func reloadForPairingChange() {
        state = store.load()
    }

    private func selectedChannel(with id: UUID) -> WorkspaceChannel? {
        state.categories.flatMap(\.channels).first { $0.id == id }
    }

    private func mutateChannel(
        _ id: UUID,
        mutation: (inout WorkspaceChannel) -> Void
    ) {
        for categoryIndex in state.categories.indices {
            guard let channelIndex = state.categories[categoryIndex].channels
                .firstIndex(where: { $0.id == id })
            else { continue }
            mutation(&state.categories[categoryIndex].channels[channelIndex])
            persist()
            return
        }
    }

    private func persist() {
        state = store.save(state)
    }

    private func defaultCards(for canvas: WorkspaceCanvasKind) -> [WorkspaceCard] {
        switch canvas {
        case .dashboard, .mixed:
            return [
                WorkspaceCard(kind: .wellnessStatus, size: .compact),
                WorkspaceCard(kind: .calendarTimeline),
                WorkspaceCard(kind: .pendingDecision),
            ]
        case .calendar:
            return [WorkspaceCard(kind: .calendarTimeline, size: .expanded)]
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
