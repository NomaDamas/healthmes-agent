import SwiftUI

private enum MacWorkspaceInspectorState {
    case thread
    case detail(MacDetailContext)
}

private struct MacWorkspaceEditRequest: Identifiable {
    enum Kind {
        case createCategory
        case createChannel(categoryID: UUID)
        case renameCategory(categoryID: UUID)
        case renameChannel(channelID: UUID)
    }

    let id = UUID()
    let kind: Kind
    let initialTitle: String
}

struct MacWorkspaceView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    @ObservedObject var workspaceStore: MacWorkspaceViewModel
    let onRefresh: (Bool) async -> Void
    let onSettings: () -> Void

    @State private var inspectorState: MacWorkspaceInspectorState?
    @State private var editRequest: MacWorkspaceEditRequest?

    var body: some View {
        NavigationSplitView {
            MacWorkspaceSidebar(
                store: workspaceStore,
                onEdit: { editRequest = $0 }
            )
            .navigationSplitViewColumnWidth(min: 210, ideal: 238, max: 300)
        } detail: {
            MacWorkspaceChannelView(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                workspaceStore: workspaceStore,
                onOpenThread: openThread,
                onOpenDetail: openDetail,
                onRefresh: onRefresh,
                onSettings: onSettings
            )
        }
        .navigationSplitViewStyle(.balanced)
        .inspector(
            isPresented: Binding(
                get: { inspectorState != nil },
                set: {
                    if !$0 {
                        inspectorState = nil
                        workspaceStore.closeThread()
                    }
                }
            )
        ) {
            inspectorContent
                .inspectorColumnWidth(min: 320, ideal: 380, max: 480)
        }
        .sheet(item: $editRequest) { request in
            MacWorkspaceEditor(
                request: request,
                categories: workspaceStore.categories.filter { !$0.isSystem },
                onSave: handleEdit
            )
        }
        .onChange(of: workspaceStore.state.selectedThreadID) { _, selectedThreadID in
            if selectedThreadID != nil {
                inspectorState = .thread
            } else if case .thread = inspectorState {
                inspectorState = nil
            }
        }
        .alert(
            "Local workspace",
            isPresented: Binding(
                get: { workspaceStore.notice != nil },
                set: { if !$0 { workspaceStore.dismissNotice() } }
            )
        ) {
            if !workspaceStore.storageMode.isWritable {
                Button("Reset local data", role: .destructive) {
                    workspaceStore.resetLocalWorkspace()
                }
            }
            Button("OK", role: .cancel) {
                workspaceStore.dismissNotice()
            }
        } message: {
            Text(workspaceStore.notice ?? "")
        }
    }

    @ViewBuilder
    private var inspectorContent: some View {
        switch inspectorState {
        case .thread:
            MacWorkspaceThreadInspector(store: workspaceStore)
        case .detail(let detail):
            MacDetailInspector(
                detail: detail,
                pairing: dashboardStore.pairing,
                onClose: { inspectorState = nil }
            )
        case nil:
            EmptyView()
        }
    }

    private func openThread(
        _ anchor: WorkspaceThreadAnchor,
        initialMessage: String?
    ) {
        guard let channelID = workspaceStore.selectedChannel?.id else { return }
        workspaceStore.openThread(
            channelID: channelID,
            anchor: anchor,
            initialMessage: initialMessage
        )
        inspectorState = .thread
    }

    private func openDetail(_ detail: MacDetailContext) {
        workspaceStore.closeThread()
        inspectorState = .detail(detail)
    }

    private func handleEdit(
        request: MacWorkspaceEditRequest,
        title: String,
        symbolName: String,
        canvas: WorkspaceCanvasKind
    ) {
        switch request.kind {
        case .createCategory:
            workspaceStore.addCategory(
                title: title,
                symbolName: "folder",
                colorHex: "#E34A26"
            )
        case .createChannel(let categoryID):
            workspaceStore.addChannel(
                categoryID: categoryID,
                title: title,
                symbolName: symbolName,
                canvas: canvas
            )
        case .renameCategory(let categoryID):
            workspaceStore.renameCategory(categoryID, title: title)
        case .renameChannel(let channelID):
            workspaceStore.renameChannel(channelID, title: title)
        }
    }
}

private struct MacWorkspaceSidebar: View {
    @ObservedObject var store: MacWorkspaceViewModel
    let onEdit: (MacWorkspaceEditRequest) -> Void

    var body: some View {
        VStack(spacing: 0) {
            workspaceHeader

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if !store.favoriteChannels.isEmpty {
                        favoriteSection
                    }

                    ForEach(store.categories) { category in
                        categorySection(category)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 14)
            }

            sidebarFooter
        }
        .background(
            LinearGradient(
                colors: [
                    MacHealthMesStyle.sidebarTop,
                    MacHealthMesStyle.sidebarBottom,
                ],
                startPoint: .top,
                endPoint: .bottom
            )
        )
        .foregroundStyle(.white)
    }

    private var workspaceHeader: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color.white.opacity(0.13))
                    .frame(width: 36, height: 36)
                Image(systemName: "sun.max.fill")
                    .foregroundStyle(MacHealthMesStyle.brand)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text("HealthMes")
                    .font(.headline)
                Text("Private workspace")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.62))
            }
            Spacer()
            Menu {
                Button {
                    onEdit(
                        MacWorkspaceEditRequest(
                            kind: .createCategory,
                            initialTitle: ""
                        )
                    )
                } label: {
                    Label("New category", systemImage: "folder.badge.plus")
                }

                if let category = store.categories.first(where: { !$0.isSystem }) {
                    Button {
                        onEdit(
                            MacWorkspaceEditRequest(
                                kind: .createChannel(categoryID: category.id),
                                initialTitle: ""
                            )
                        )
                    } label: {
                        Label("New channel", systemImage: "number.circle")
                    }
                }
            } label: {
                Image(systemName: "plus")
                    .frame(width: 28, height: 28)
                    .background(Color.white.opacity(0.1), in: Circle())
            }
            .menuStyle(.borderlessButton)
            .accessibilityLabel(Text("Add category or channel"))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Color.white.opacity(0.1))
                .frame(height: 1)
        }
    }

    private var favoriteSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            sidebarLabel("Favorites", systemImage: "star.fill")
            ForEach(store.favoriteChannels) { channel in
                channelRow(channel)
            }
        }
    }

    private func categorySection(_ category: WorkspaceCategory) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Button {
                    store.toggleCategory(category.id)
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: category.isCollapsed ? "chevron.right" : "chevron.down")
                            .font(.caption2.weight(.bold))
                        Text(category.isSystem ? "Core" : category.title)
                            .font(.caption.weight(.bold))
                            .textCase(.uppercase)
                            .tracking(0.7)
                            .lineLimit(1)
                    }
                    .foregroundStyle(.white.opacity(0.62))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(Text(category.isSystem ? "Core" : category.title))
                .accessibilityValue(
                    Text(category.isCollapsed ? "Collapsed" : "Expanded")
                )

                Spacer()

                if !category.isSystem {
                    Menu {
                        Button {
                            onEdit(
                                MacWorkspaceEditRequest(
                                    kind: .createChannel(categoryID: category.id),
                                    initialTitle: ""
                                )
                            )
                        } label: {
                            Label("New channel", systemImage: "plus")
                        }
                        Button {
                            onEdit(
                                MacWorkspaceEditRequest(
                                    kind: .renameCategory(categoryID: category.id),
                                    initialTitle: category.title
                                )
                            )
                        } label: {
                            Label("Rename category", systemImage: "pencil")
                        }
                        Button {
                            store.moveCategory(category.id, offset: -1)
                        } label: {
                            Label("Move up", systemImage: "arrow.up")
                        }
                        Button {
                            store.moveCategory(category.id, offset: 1)
                        } label: {
                            Label("Move down", systemImage: "arrow.down")
                        }
                        Divider()
                        Button(role: .destructive) {
                            store.deleteCategory(category.id)
                        } label: {
                            Label("Delete category", systemImage: "trash")
                        }
                    } label: {
                        Image(systemName: "ellipsis")
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.6))
                    }
                    .menuStyle(.borderlessButton)
                    .accessibilityLabel(Text("Edit \(category.title)"))
                }
            }
            .padding(.horizontal, 8)

            if !category.isCollapsed {
                ForEach(category.channels.filter { !$0.isHidden }) { channel in
                    channelRow(channel)
                }

                if category.channels.isEmpty, !category.isSystem {
                    Button {
                        onEdit(
                            MacWorkspaceEditRequest(
                                kind: .createChannel(categoryID: category.id),
                                initialTitle: ""
                            )
                        )
                    } label: {
                        Label("Add first channel", systemImage: "plus")
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.62))
                            .padding(.horizontal, 9)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func channelRow(_ channel: WorkspaceChannel) -> some View {
        let selected = store.selectedChannel?.id == channel.id
        return Button {
            store.selectChannel(channel.id)
        } label: {
            HStack(spacing: 9) {
                Image(systemName: channel.symbolName)
                    .font(.callout)
                    .frame(width: 16)
                    .foregroundStyle(
                        selected
                            ? Color(red: 0.12, green: 0.21, blue: 0.15)
                            : .white.opacity(0.72)
                    )
                Text(channel.title)
                    .font(.callout.weight(selected ? .semibold : .regular))
                    .lineLimit(1)
                Spacer(minLength: 4)
                if channel.isFavorite {
                    Image(systemName: "star.fill")
                        .font(.caption2)
                        .foregroundStyle(
                            selected
                                ? Color.primary.opacity(0.6)
                                : Color.white.opacity(0.45)
                        )
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .foregroundStyle(selected ? MacHealthMesStyle.graphite : .white.opacity(0.88))
            .background(
                selected
                    ? Color(red: 0.74, green: 0.91, blue: 0.84)
                    : Color.clear,
                in: RoundedRectangle(cornerRadius: 8)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityValue(Text(channel.isFavorite ? "Favorite" : "Channel"))
        .contextMenu {
            Button {
                store.toggleFavorite(channel.id)
            } label: {
                Label(
                    channel.isFavorite ? "Remove from favorites" : "Add to favorites",
                    systemImage: channel.isFavorite ? "star.slash" : "star"
                )
            }
            if !channel.isSystem {
                Button {
                    onEdit(
                        MacWorkspaceEditRequest(
                            kind: .renameChannel(channelID: channel.id),
                            initialTitle: channel.title
                        )
                    )
                } label: {
                    Label("Rename channel", systemImage: "pencil")
                }
                Button {
                    store.moveChannel(channel.id, offset: -1)
                } label: {
                    Label("Move up", systemImage: "arrow.up")
                }
                Button {
                    store.moveChannel(channel.id, offset: 1)
                } label: {
                    Label("Move down", systemImage: "arrow.down")
                }
                if store.categories.filter({ !$0.isSystem }).count > 1 {
                    Menu("Move to category") {
                        ForEach(
                            store.categories.filter { category in
                                !category.isSystem
                                    && !category.channels.contains(where: {
                                        $0.id == channel.id
                                    })
                            }
                        ) { category in
                            Button(category.title) {
                                store.moveChannel(channel.id, to: category.id)
                            }
                        }
                    }
                }
                Divider()
                Button(role: .destructive) {
                    store.deleteChannel(channel.id)
                } label: {
                    Label("Delete channel", systemImage: "trash")
                }
            }
        }
    }

    private func sidebarLabel(_ title: String, systemImage: String) -> some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.bold))
            .textCase(.uppercase)
            .tracking(0.7)
            .foregroundStyle(.white.opacity(0.62))
            .padding(.horizontal, 8)
    }

    private var sidebarFooter: some View {
        HStack(spacing: 8) {
            Image(systemName: "internaldrive")
            Text("Channels and threads stay on this Mac")
                .lineLimit(2)
            Spacer(minLength: 0)
        }
        .font(.caption2)
        .foregroundStyle(.white.opacity(0.56))
        .padding(14)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(Color.white.opacity(0.1))
                .frame(height: 1)
        }
    }
}

private struct MacWorkspaceEditor: View {
    let request: MacWorkspaceEditRequest
    let categories: [WorkspaceCategory]
    let onSave: (
        MacWorkspaceEditRequest,
        String,
        String,
        WorkspaceCanvasKind
    ) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var canvas: WorkspaceCanvasKind

    init(
        request: MacWorkspaceEditRequest,
        categories: [WorkspaceCategory],
        onSave: @escaping (
            MacWorkspaceEditRequest,
            String,
            String,
            WorkspaceCanvasKind
        ) -> Void
    ) {
        self.request = request
        self.categories = categories
        self.onSave = onSave
        _title = State(initialValue: request.initialTitle)
        _canvas = State(initialValue: .mixed)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text(editorTitle)
                    .font(.title2.weight(.semibold))
                Text(editorSubtitle)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            TextField("Name", text: $title)
                .textFieldStyle(.roundedBorder)

            if isChannelRequest {
                Picker("Canvas", selection: $canvas) {
                    ForEach(WorkspaceCanvasKind.allCases) { kind in
                        Label(canvasTitle(kind), systemImage: canvasSymbol(kind))
                            .tag(kind)
                    }
                }
                .pickerStyle(.menu)
            }

            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                Button("Save") {
                    onSave(request, title, canvasSymbol(canvas), canvas)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 420)
    }

    private var isChannelRequest: Bool {
        switch request.kind {
        case .createChannel, .renameChannel:
            return true
        case .createCategory, .renameCategory:
            return false
        }
    }

    private var editorTitle: String {
        switch request.kind {
        case .createCategory: return "New category"
        case .createChannel: return "New channel"
        case .renameCategory: return "Rename category"
        case .renameChannel: return "Rename channel"
        }
    }

    private var editorSubtitle: String {
        switch request.kind {
        case .createCategory:
            return "Group channels around a project, routine or health question."
        case .createChannel:
            return "Choose the canvas that best matches how you want to work."
        case .renameCategory:
            return "This changes only the local sidebar."
        case .renameChannel:
            return "The channel content and local threads are preserved."
        }
    }

    private func canvasTitle(_ kind: WorkspaceCanvasKind) -> String {
        switch kind {
        case .dashboard: return "Dashboard"
        case .calendar: return "Calendar"
        case .visualization: return "Insights"
        case .decisions: return "Decisions"
        case .conversation: return "Agent"
        case .mixed: return "Mixed canvas"
        }
    }

    private func canvasSymbol(_ kind: WorkspaceCanvasKind) -> String {
        switch kind {
        case .dashboard: return "rectangle.grid.2x2"
        case .calendar: return "calendar"
        case .visualization: return "chart.xyaxis.line"
        case .decisions: return "checkmark.bubble"
        case .conversation: return "waveform"
        case .mixed: return "square.grid.3x3"
        }
    }
}
