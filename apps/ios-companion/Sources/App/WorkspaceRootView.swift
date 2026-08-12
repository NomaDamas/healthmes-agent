import SwiftUI

private enum WorkspaceCreationSheet: Identifiable {
    case category
    case channel(UUID)
    case renameCategory(UUID, String)
    case renameChannel(UUID, String)

    var id: String {
        switch self {
        case .category: return "category"
        case .channel(let categoryID): return "channel-\(categoryID)"
        case .renameCategory(let id, _): return "rename-category-\(id)"
        case .renameChannel(let id, _): return "rename-channel-\(id)"
        }
    }
}

struct WorkspaceRootView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var workspace = WorkspaceViewModel()
    @State private var creationSheet: WorkspaceCreationSheet?
    @State private var columnVisibility: NavigationSplitViewVisibility = .detailOnly

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            WorkspaceSidebar(
                workspace: workspace,
                onCreateCategory: { creationSheet = .category },
                onCreateChannel: { creationSheet = .channel($0) },
                onRenameCategory: { creationSheet = .renameCategory($0.id, $0.title) },
                onRenameChannel: { creationSheet = .renameChannel($0.id, $0.title) }
            )
            .navigationSplitViewColumnWidth(min: 230, ideal: 280, max: 340)
        } detail: {
            WorkspaceChannelView(workspace: workspace)
                .environmentObject(router)
        }
        .navigationSplitViewStyle(.balanced)
        .sheet(item: $creationSheet) { target in
            switch target {
            case .category:
                WorkspaceCategoryEditor(workspace: workspace)
            case .channel(let categoryID):
                WorkspaceChannelEditor(
                    workspace: workspace,
                    categoryID: categoryID
                )
            case .renameCategory(let id, let title):
                WorkspaceRenameEditor(
                    title: "카테고리 이름 변경",
                    initialTitle: title,
                    onSave: { workspace.renameCategory(id, title: $0) }
                )
            case .renameChannel(let id, let title):
                WorkspaceRenameEditor(
                    title: "채널 이름 변경",
                    initialTitle: title,
                    onSave: { workspace.renameChannel(id, title: $0) }
                )
            }
        }
        .sheet(
            isPresented: Binding(
                get: { workspace.state.selectedThreadID != nil },
                set: { if !$0 { workspace.selectThread(nil) } }
            )
        ) {
            WorkspaceThreadSheet(workspace: workspace)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .healthmesPairingChanged)
        ) { _ in
            workspace.reloadForPairingChange()
        }
    }
}

private struct WorkspaceSidebar: View {
    @ObservedObject var workspace: WorkspaceViewModel
    let onCreateCategory: () -> Void
    let onCreateChannel: (UUID) -> Void
    let onRenameCategory: (WorkspaceCategory) -> Void
    let onRenameChannel: (WorkspaceChannel) -> Void

    var body: some View {
        List(selection: selectedChannelBinding) {
            if !favoriteChannels.isEmpty {
                Section("즐겨찾기") {
                    ForEach(favoriteChannels) { channel in
                        channelRow(channel)
                    }
                }
            }

            ForEach(workspace.state.categories) { category in
                Section {
                    if !category.isCollapsed {
                        ForEach(category.channels.filter { !$0.isHidden }) { channel in
                            channelRow(channel)
                        }
                        if !category.isSystem {
                            Button {
                                onCreateChannel(category.id)
                            } label: {
                                Label("채널 추가", systemImage: "plus")
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } header: {
                    categoryHeader(category)
                }
            }

            if hiddenSystemChannelCount > 0 {
                Section {
                    Button {
                        workspace.restoreSystemChannels()
                    } label: {
                        Label(
                            "숨긴 기본 채널 복원 (\(hiddenSystemChannelCount))",
                            systemImage: "eye"
                        )
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("HealthMes")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: onCreateCategory) {
                    Image(systemName: "folder.badge.plus")
                }
                .accessibilityLabel("카테고리 추가")
            }
        }
    }

    private var favoriteChannels: [WorkspaceChannel] {
        workspace.state.categories
            .flatMap(\.channels)
            .filter { $0.isFavorite && !$0.isHidden }
    }

    private var hiddenSystemChannelCount: Int {
        workspace.state.categories
            .flatMap(\.channels)
            .filter { $0.isSystem && $0.isHidden }
            .count
    }

    private var selectedChannelBinding: Binding<UUID?> {
        Binding(
            get: { workspace.state.selectedChannelID },
            set: { if let id = $0 { workspace.selectChannel(id) } }
        )
    }

    private func channelRow(_ channel: WorkspaceChannel) -> some View {
        Label(channel.title, systemImage: channel.symbolName)
            .tag(channel.id)
            .contextMenu {
                Button {
                    workspace.setChannelFavorite(
                        channel.id,
                        favorite: !channel.isFavorite
                    )
                } label: {
                    Label(
                        channel.isFavorite ? "즐겨찾기 해제" : "즐겨찾기",
                        systemImage: channel.isFavorite ? "star.slash" : "star"
                    )
                }
                if channel.isSystem {
                    Button {
                        workspace.setChannelHidden(channel.id, hidden: true)
                    } label: {
                        Label("사이드바에서 숨기기", systemImage: "eye.slash")
                    }
                } else {
                    Button {
                        onRenameChannel(channel)
                    } label: {
                        Label("채널 이름 변경", systemImage: "pencil")
                    }
                    Button {
                        workspace.moveChannel(channel.id, offset: -1)
                    } label: {
                        Label("위로 이동", systemImage: "arrow.up")
                    }
                    Button {
                        workspace.moveChannel(channel.id, offset: 1)
                    } label: {
                        Label("아래로 이동", systemImage: "arrow.down")
                    }
                    if workspace.state.categories.filter({ !$0.isSystem }).count > 1 {
                        Menu {
                            ForEach(
                                workspace.state.categories.filter { category in
                                    !category.isSystem
                                        && !category.channels.contains(where: {
                                            $0.id == channel.id
                                        })
                                }
                            ) { category in
                                Button(category.title) {
                                    workspace.moveChannel(
                                        channel.id,
                                        to: category.id
                                    )
                                }
                            }
                        } label: {
                            Label("다른 카테고리로 이동", systemImage: "folder")
                        }
                    }
                    Button(role: .destructive) {
                        workspace.deleteChannel(channel.id)
                    } label: {
                        Label("채널 삭제", systemImage: "trash")
                    }
                }
            }
    }

    private func categoryHeader(_ category: WorkspaceCategory) -> some View {
        HStack(spacing: 5) {
            Button {
                workspace.setCategoryCollapsed(
                    category.id,
                    collapsed: !category.isCollapsed
                )
            } label: {
                Image(
                    systemName: category.isCollapsed
                        ? "chevron.right"
                        : "chevron.down"
                )
                .font(.caption2.bold())
            }
            .buttonStyle(.plain)
            Text(verbatim: category.title)
            Spacer()
            if !category.isSystem {
                Menu {
                    Button {
                        onCreateChannel(category.id)
                    } label: {
                        Label("채널 추가", systemImage: "plus")
                    }
                    Button {
                        onRenameCategory(category)
                    } label: {
                        Label("카테고리 이름 변경", systemImage: "pencil")
                    }
                    Button {
                        workspace.moveCategory(category.id, offset: -1)
                    } label: {
                        Label("위로 이동", systemImage: "arrow.up")
                    }
                    Button {
                        workspace.moveCategory(category.id, offset: 1)
                    } label: {
                        Label("아래로 이동", systemImage: "arrow.down")
                    }
                    Divider()
                    Button(role: .destructive) {
                        workspace.deleteCategory(category.id)
                    } label: {
                        Label("카테고리 삭제", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis")
                }
                .buttonStyle(.plain)
            }
        }
    }
}

private struct WorkspaceChannelView: View {
    @EnvironmentObject private var router: AppRouter
    @ObservedObject var workspace: WorkspaceViewModel

    var body: some View {
        Group {
            if let channel = workspace.selectedChannel {
                channelCanvas(channel)
                    .navigationTitle(Text(verbatim: channel.title))
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItemGroup(placement: .topBarTrailing) {
                            Button {
                                openChannelThread(channel)
                            } label: {
                                Image(systemName: "bubble.left.and.bubble.right")
                            }
                            .accessibilityLabel("이 채널의 스레드 열기")
                            channelMenu(channel)
                        }
                    }
            } else {
                ContentUnavailableView(
                    "채널을 선택하세요",
                    systemImage: "sidebar.left"
                )
            }
        }
    }

    @ViewBuilder
    private func channelCanvas(_ channel: WorkspaceChannel) -> some View {
        switch channel.systemKind {
        case .overview:
            WellnessControlView()
        case .calendar:
            PlanView()
        case .insights:
            WorkspaceInsightCanvas(onOpenThread: {
                openCardThread(channel, kind: .energyCurve, title: "Wellness insight")
            })
        case .decisions:
            DecisionsView()
        case .agent:
            WellnessControlView()
        case nil:
            customChannelCanvas(channel)
        }
    }

    @ViewBuilder
    private func customChannelCanvas(_ channel: WorkspaceChannel) -> some View {
        switch channel.canvas {
        case .dashboard, .mixed:
            WellnessControlView()
        case .calendar:
            PlanView()
        case .visualization:
            WorkspaceInsightCanvas(onOpenThread: {
                openCardThread(channel, kind: .energyCurve, title: "Wellness insight")
            })
        case .decisions:
            DecisionsView()
        case .conversation:
            WellnessControlView()
        }
    }

    private func channelMenu(_ channel: WorkspaceChannel) -> some View {
        Menu {
            Button {
                workspace.setChannelFavorite(
                    channel.id,
                    favorite: !channel.isFavorite
                )
            } label: {
                Label(
                    channel.isFavorite ? "즐겨찾기 해제" : "즐겨찾기",
                    systemImage: channel.isFavorite ? "star.slash" : "star"
                )
            }
            if let pairing = PairingStore.shared.load() {
                Link(
                    destination: ViewerURL.make(
                        pairing: pairing,
                        pathComponents: ["dashboard"]
                    )
                ) {
                    Label("웹에서 자세히", systemImage: "safari")
                }
            }
            Button {
                router.modal = .settings
            } label: {
                Label("설정", systemImage: "gearshape")
            }
        } label: {
            Image(systemName: "ellipsis.circle")
        }
    }

    private func openChannelThread(_ channel: WorkspaceChannel) {
        workspace.openThread(
            channelID: channel.id,
            anchor: WorkspaceThreadAnchor(
                kind: .post,
                localID: "channel-\(channel.id.uuidString)",
                title: "#\(channel.title)"
            )
        )
    }

    private func openCardThread(
        _ channel: WorkspaceChannel,
        kind: WorkspaceCardKind,
        title: String
    ) {
        workspace.openThread(
            channelID: channel.id,
            anchor: WorkspaceThreadAnchor(
                kind: .card,
                localID: "\(channel.id.uuidString)-\(kind.rawValue)",
                title: title
            )
        )
    }

    private func cardTitle(_ kind: WorkspaceCardKind) -> String {
        switch kind {
        case .wellnessStatus: return "현재 상태"
        case .capacity: return "가용 에너지"
        case .calendarTimeline: return "캘린더"
        case .energyCurve: return "에너지 추이"
        case .baseline: return "개인 기준선"
        case .factors: return "영향 요인"
        case .goalProgress: return "목표 진행"
        case .pendingDecision: return "대기 중인 결정"
        case .decisionResult: return "결정 결과"
        case .nutrition: return "식사 기록"
        case .summary: return "요약"
        case .command: return "HealthMes Agent"
        }
    }
}

private struct WorkspaceInsightCanvas: View {
    let onOpenThread: () -> Void

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                ProductCard(kicker: "Wellness insight", systemImage: "chart.xyaxis.line") {
                    Text("질문에 맞는 시각화는 HealthMes의 분석 결과가 있을 때만 생성됩니다.")
                        .font(.title3.weight(.semibold))
                    Text("에너지 추이, 개인 기준선, 영향 요인과 실제 캘린더를 함께 비교합니다.")
                        .foregroundStyle(.secondary)
                    Button(action: onOpenThread) {
                        Label("이 인사이트로 대화", systemImage: "bubble.left")
                    }
                    .buttonStyle(.bordered)
                }
                WellnessControlView()
                    .frame(minHeight: 640)
            }
            .padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }
}

private struct WorkspaceThreadSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var workspace: WorkspaceViewModel

    var body: some View {
        NavigationStack {
            Group {
                if let thread = workspace.selectedThread {
                    VStack(spacing: 0) {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 12) {
                                threadAnchor(thread)
                                ForEach(thread.messages) { message in
                                    messageRow(message)
                                }
                            }
                            .padding(16)
                        }
                        Divider()
                        threadComposer(thread)
                    }
                } else {
                    ContentUnavailableView("스레드 없음", systemImage: "bubble.left")
                }
            }
            .navigationTitle("스레드")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("완료") { dismiss() }
                }
            }
        }
    }

    private func threadAnchor(_ thread: WorkspaceThread) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(thread.anchor.title, systemImage: "link")
                .font(.headline)
            Text("이 대화는 현재 기기에만 저장됩니다.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private func messageRow(_ message: WorkspaceThreadMessage) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(message.author == .user ? "나" : "HealthMes")
                .font(.caption.bold())
                .foregroundStyle(.secondary)
            Text(verbatim: message.body)
            Text(message.createdAt, style: .time)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            message.author == .user
                ? Color.accentColor.opacity(0.09)
                : Color.primary.opacity(0.05),
            in: RoundedRectangle(cornerRadius: 14)
        )
    }

    private func threadComposer(_ thread: WorkspaceThread) -> some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField(
                "이 항목에 답글",
                text: Binding(
                    get: { workspace.selectedThread?.draft ?? "" },
                    set: { workspace.updateDraft(threadID: thread.id, draft: $0) }
                ),
                axis: .vertical
            )
            .lineLimit(1...4)
            .textFieldStyle(.plain)
            Button {
                workspace.sendMessage(threadID: thread.id)
            } label: {
                Image(systemName: "arrow.up")
                    .font(.body.bold())
                    .foregroundStyle(.white)
                    .frame(width: 36, height: 36)
                    .background(Color.accentColor, in: Circle())
            }
            .buttonStyle(.plain)
            .disabled(thread.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(12)
        .background(.bar)
    }
}

private struct WorkspaceCategoryEditor: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var workspace: WorkspaceViewModel
    @State private var title = ""

    var body: some View {
        NavigationStack {
            Form {
                TextField("예: 업무, 회복, 운동", text: $title)
            }
            .navigationTitle("카테고리 추가")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("취소") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("추가") {
                        _ = workspace.createCategory(title: title)
                        dismiss()
                    }
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}

private struct WorkspaceChannelEditor: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var workspace: WorkspaceViewModel
    let categoryID: UUID
    @State private var title = ""
    @State private var canvas: WorkspaceCanvasKind = .mixed

    var body: some View {
        NavigationStack {
            Form {
                TextField("채널 이름", text: $title)
                Picker("캔버스", selection: $canvas) {
                    ForEach(WorkspaceCanvasKind.allCases) { kind in
                        Text(canvasTitle(kind)).tag(kind)
                    }
                }
            }
            .navigationTitle("채널 추가")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("취소") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("추가") {
                        _ = workspace.createChannel(
                            in: categoryID,
                            title: title,
                            canvas: canvas
                        )
                        dismiss()
                    }
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }

    private func canvasTitle(_ kind: WorkspaceCanvasKind) -> String {
        switch kind {
        case .dashboard: return "대시보드"
        case .calendar: return "캘린더"
        case .visualization: return "시각화"
        case .decisions: return "결정"
        case .conversation: return "대화"
        case .mixed: return "혼합 캔버스"
        }
    }
}

private struct WorkspaceRenameEditor: View {
    @Environment(\.dismiss) private var dismiss
    let title: String
    let onSave: (String) -> Void
    @State private var value: String

    init(title: String, initialTitle: String, onSave: @escaping (String) -> Void) {
        self.title = title
        self.onSave = onSave
        _value = State(initialValue: initialTitle)
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("이름", text: $value)
            }
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("취소") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("저장") {
                        onSave(value)
                        dismiss()
                    }
                    .disabled(value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}
