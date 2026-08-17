import SwiftUI

private enum WorkspaceCreationSheet: Identifiable {
    case category
    case channel(UUID)
    case cards(UUID)
    case renameCategory(UUID, String)
    case renameChannel(UUID, String)

    var id: String {
        switch self {
        case .category: return "category"
        case .channel(let categoryID): return "channel-\(categoryID)"
        case .cards(let channelID): return "cards-\(channelID)"
        case .renameCategory(let id, _): return "rename-category-\(id)"
        case .renameChannel(let id, _): return "rename-channel-\(id)"
        }
    }
}

struct WorkspaceRootView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var workspace = WorkspaceViewModel()
    @State private var creationSheet: WorkspaceCreationSheet?
    @State private var isSidebarPresented = false
    @State private var sidebarDragOffset: CGFloat = 0
    @State private var lastAgentChannelRequest = 0

    var body: some View {
        GeometryReader { proxy in
            let drawerWidth = min(max(proxy.size.width * 0.82, 286), 360)

            NavigationStack {
                WorkspaceChannelView(
                    workspace: workspace,
                    onOpenSidebar: openSidebar,
                    onEditCards: { creationSheet = .cards($0) }
                )
                .environmentObject(router)
            }
            .overlay {
                if isSidebarPresented {
                    Color.black.opacity(0.28 * drawerProgress(drawerWidth))
                        .ignoresSafeArea()
                        .contentShape(Rectangle())
                        .onTapGesture(perform: closeSidebar)
                        .accessibilityLabel("사이드바 닫기")
                }
            }
            .overlay(alignment: .leading) {
                if isSidebarPresented {
                    WorkspaceDrawer(
                        workspace: workspace,
                        width: drawerWidth,
                        onClose: closeSidebar,
                        onSelectChannel: { channelID in
                            workspace.selectChannel(channelID)
                            closeSidebar()
                        },
                        onCreateCategory: { creationSheet = .category },
                        onCreateChannel: { creationSheet = .channel($0) },
                        onRenameCategory: {
                            creationSheet = .renameCategory($0.id, $0.title)
                        },
                        onRenameChannel: {
                            creationSheet = .renameChannel($0.id, $0.title)
                        }
                    )
                    .offset(x: min(sidebarDragOffset, 0))
                    .transition(.move(edge: .leading))
                    .gesture(
                        DragGesture(minimumDistance: 10)
                            .onChanged { value in
                                sidebarDragOffset = min(max(value.translation.width, -drawerWidth), 0)
                            }
                            .onEnded { value in
                                if value.translation.width < -drawerWidth * 0.22
                                    || value.predictedEndTranslation.width < -drawerWidth * 0.45
                                {
                                    closeSidebar()
                                } else {
                                    withAnimation(.snappy(duration: 0.24)) {
                                        sidebarDragOffset = 0
                                    }
                                }
                            }
                    )
                    .zIndex(2)
                } else {
                    Color.clear
                        .frame(width: 24)
                        .frame(maxHeight: .infinity)
                        .contentShape(Rectangle())
                        .gesture(
                            DragGesture(minimumDistance: 12)
                                .onEnded { value in
                                    guard value.translation.width > 48,
                                        abs(value.translation.height) < 60
                                    else { return }
                                    openSidebar()
                                }
                        )
                        .accessibilityHidden(true)
                }
            }
            .animation(.snappy(duration: 0.26), value: isSidebarPresented)
        }
        .sheet(item: $creationSheet) { target in
            switch target {
            case .category:
                WorkspaceCategoryEditor(workspace: workspace)
            case .channel(let categoryID):
                WorkspaceChannelEditor(
                    workspace: workspace,
                    categoryID: categoryID
                )
            case .cards(let channelID):
                WorkspaceCardEditor(
                    workspace: workspace,
                    channelID: channelID
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
        .onReceive(router.$agentChannelRequest) { request in
            guard request > lastAgentChannelRequest else { return }
            lastAgentChannelRequest = request
            workspace.routeToChannel(WorkspaceState.systemChannelID(.agent))
            closeSidebar()
            Task { @MainActor in
                await Task.yield()
                router.focusCommandDock()
            }
        }
        .alert(
            "로컬 워크스페이스",
            isPresented: Binding(
                get: { workspace.notice != nil },
                set: { if !$0 { workspace.dismissNotice() } }
            )
        ) {
            if !workspace.canEdit {
                Button("로컬 데이터 초기화", role: .destructive) {
                    workspace.reset()
                }
            }
            Button("확인", role: .cancel) {
                workspace.dismissNotice()
            }
        } message: {
            Text(workspace.notice ?? "")
        }
    }

    private func openSidebar() {
        sidebarDragOffset = 0
        withAnimation(.snappy(duration: 0.26)) {
            isSidebarPresented = true
        }
    }

    private func closeSidebar() {
        withAnimation(.snappy(duration: 0.24)) {
            isSidebarPresented = false
            sidebarDragOffset = 0
        }
    }

    private func drawerProgress(_ width: CGFloat) -> Double {
        Double(min(max(1 + sidebarDragOffset / width, 0), 1))
    }
}

private struct WorkspaceDrawer: View {
    @ObservedObject var workspace: WorkspaceViewModel
    let width: CGFloat
    let onClose: () -> Void
    let onSelectChannel: (UUID) -> Void
    let onCreateCategory: () -> Void
    let onCreateChannel: (UUID) -> Void
    let onRenameCategory: (WorkspaceCategory) -> Void
    let onRenameChannel: (WorkspaceChannel) -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "waveform.path.ecg.rectangle.fill")
                    .font(.title2)
                    .foregroundStyle(HealthMesVisualStyle.brand)
                VStack(alignment: .leading, spacing: 1) {
                    Text("HealthMes")
                        .font(.headline)
                    Text("Local wellness workspace")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.62))
                }
                Spacer()
                Button(action: onCreateCategory) {
                    Image(systemName: "folder.badge.plus")
                }
                .accessibilityLabel("카테고리 추가")
                Button(action: onClose) {
                    Image(systemName: "xmark")
                }
                .accessibilityLabel("사이드바 닫기")
            }
            .padding(.horizontal, 16)
            .frame(height: 58)
            .background(Color.white.opacity(0.045))

            Divider()

            WorkspaceSidebar(
                workspace: workspace,
                onSelectChannel: onSelectChannel,
                onCreateCategory: onCreateCategory,
                onCreateChannel: onCreateChannel,
                onRenameCategory: onRenameCategory,
                onRenameChannel: onRenameChannel
            )
        }
        .frame(width: width)
        .frame(maxHeight: .infinity)
        .background(HealthMesVisualStyle.drawer)
        .foregroundStyle(.white)
        .clipShape(
            UnevenRoundedRectangle(
                bottomTrailingRadius: 24,
                topTrailingRadius: 24
            )
        )
        .shadow(color: .black.opacity(0.22), radius: 24, x: 8)
        .accessibilityIdentifier("healthmes-workspace-drawer")
        .accessibilityAddTraits(.isModal)
    }
}

private struct WorkspaceSidebar: View {
    @ObservedObject var workspace: WorkspaceViewModel
    let onSelectChannel: (UUID) -> Void
    let onCreateCategory: () -> Void
    let onCreateChannel: (UUID) -> Void
    let onRenameCategory: (WorkspaceCategory) -> Void
    let onRenameChannel: (WorkspaceChannel) -> Void

    var body: some View {
        List {
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
        .scrollContentBackground(.hidden)
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

    private func channelRow(_ channel: WorkspaceChannel) -> some View {
        Button {
            onSelectChannel(channel.id)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: channel.symbolName)
                    .frame(width: 20)
                Text(verbatim: channel.title)
                    .lineLimit(1)
                Spacer()
                if channel.isFavorite {
                    Image(systemName: "star.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
            .buttonStyle(.plain)
            .listRowBackground(
                workspace.state.selectedChannelID == channel.id
                    ? Color.accentColor.opacity(0.12)
                    : Color.clear
            )
            .accessibilityAddTraits(
                workspace.state.selectedChannelID == channel.id ? .isSelected : []
            )
            .accessibilityValue(
                channel.isFavorite ? "즐겨찾기, 선택 가능" : "선택 가능"
            )
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
            .accessibilityLabel(
                "\(category.title), \(category.isCollapsed ? "접힘" : "펼쳐짐")"
            )
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
    let onOpenSidebar: () -> Void
    let onEditCards: (UUID) -> Void
    @State private var postChannel: WorkspaceChannel?
    @State private var newPostTitle = ""

    var body: some View {
        Group {
            if let channel = workspace.selectedChannel {
                channelCanvas(channel)
                    .navigationTitle(Text(verbatim: channel.title))
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .topBarLeading) {
                            Button(action: onOpenSidebar) {
                                Image(systemName: "line.3.horizontal")
                            }
                            .accessibilityLabel("채널 사이드바 열기")
                            .accessibilityIdentifier("healthmes-open-workspace-drawer")
                        }
                        ToolbarItemGroup(placement: .topBarTrailing) {
                            channelPostsMenu(channel)
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
            WorkspaceOverviewCanvas(
                onOpenDecisionThread: {
                    openCardThread(
                        channel,
                        kind: .pendingDecision,
                        title: "지금 필요한 결정"
                    )
                }
            )
        case .calendar:
            PlanView()
        case .insights:
            WorkspaceInsightCanvas(onOpenThread: {
                openCardThread(channel, kind: .energyCurve, title: "Wellness insight")
            })
        case .decisions:
            DecisionsView()
        case .agent:
            WorkspaceAgentCanvas(
                workspace: workspace,
                channel: channel,
                onOpenThread: {
                    openChannelThread(channel)
                }
            )
        case nil:
            customChannelCanvas(channel)
        }
    }

    @ViewBuilder
    private func customChannelCanvas(_ channel: WorkspaceChannel) -> some View {
        switch channel.canvas {
        case .dashboard:
            WorkspaceCustomDashboardCanvas(
                channel: channel,
                onEditCards: { onEditCards(channel.id) },
                onOpenThread: {
                    openChannelThread(channel)
                }
            )
        case .mixed:
            WorkspaceMixedCanvas(
                channel: channel,
                onEditCards: { onEditCards(channel.id) },
                onOpenThread: {
                    openChannelThread(channel)
                }
            )
        case .calendar:
            PlanView()
        case .visualization:
            WorkspaceInsightCanvas(onOpenThread: {
                openCardThread(channel, kind: .energyCurve, title: "Wellness insight")
            })
        case .decisions:
            DecisionsView()
        case .conversation:
            WorkspaceAgentCanvas(
                workspace: workspace,
                channel: channel,
                onOpenThread: {
                    openChannelThread(channel)
                }
            )
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
        postChannel = channel
        newPostTitle = ""
    }

    private func channelPostsMenu(_ channel: WorkspaceChannel) -> some View {
        Menu {
            Button {
                openChannelThread(channel)
            } label: {
                Label("새 게시글", systemImage: "square.and.pencil")
            }
            let threads = workspace.threads(in: channel.id)
            if !threads.isEmpty {
                Divider()
                ForEach(threads.prefix(8)) { thread in
                    Button {
                        workspace.selectThread(thread.id)
                    } label: {
                        Label(thread.anchor.title, systemImage: "text.bubble")
                    }
                }
            }
        } label: {
            Image(systemName: "bubble.left.and.bubble.right")
        }
        .accessibilityLabel("채널 게시글과 스레드")
        .alert(
            "새 게시글",
            isPresented: Binding(
                get: { postChannel?.id == channel.id },
                set: { if !$0 { postChannel = nil } }
            )
        ) {
            TextField("게시글 제목", text: $newPostTitle)
            Button("취소", role: .cancel) {
                postChannel = nil
            }
            Button("만들기") {
                _ = workspace.createPostThread(
                    channelID: channel.id,
                    title: newPostTitle
                )
                postChannel = nil
            }
            .disabled(
                newPostTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        } message: {
            Text("게시글마다 독립된 답글 스레드가 이 기기에 저장됩니다.")
        }
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

private struct WorkspaceOverviewCanvas: View {
    @StateObject private var briefing = BriefingHomeModel()
    @StateObject private var plan = PlanModel()
    let onOpenDecisionThread: () -> Void

    private let moss = HealthMesVisualStyle.capacity

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                overviewHeader
                capacityCard
                todayBlocksCard
                decisionCard
            }
            .padding(.horizontal, 16)
            .padding(.top, 18)
            .padding(.bottom, 28)
        }
        .background(workspaceBackground)
        .refreshable { await refresh() }
        .task {
            await refresh()
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            briefing.resetForPairingChange()
            plan.resetForPairingChange()
            Task { await refresh() }
        }
        .accessibilityIdentifier("healthmes-overview-canvas")
    }

    private var overviewHeader: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(Date(), format: .dateTime.weekday(.wide).month(.wide).day())
                .font(.caption.weight(.bold))
                .foregroundStyle(HealthMesVisualStyle.capacityDeep)
                .textCase(.uppercase)
                .tracking(0.8)
            Text(conclusion)
                .font(.title2.weight(.bold))
                .tracking(-0.35)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var capacityCard: some View {
        ProductCard(kicker: "Capacity", systemImage: "waveform.path.ecg") {
            if let payload = briefing.snapshot?.payload {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                            .font(.system(size: 42, weight: .bold, design: .default))
                            .foregroundStyle(HealthMesVisualStyle.capacityDeep)
                        Text("available")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        ConfidenceBadge(rawLevel: payload.energy.confidence.rawValue)
                    }
                    WorkspaceCapacityBar(score: payload.energy.score)
                    EnergyCurveView(
                        curve: payload.energy.curve24h,
                        timezone: payload.timezone
                    )
                }
                .overlay(alignment: .topLeading) {
                    if !briefing.isStale {
                        Color.clear
                            .frame(width: 1, height: 1)
                            .accessibilityElement()
                            .accessibilityIdentifier("healthmes-live-pairing-ready")
                    }
                }
            } else {
                unavailable(briefing.glanceError ?? "건강 데이터가 아직 없습니다.")
            }
        }
    }

    private var todayBlocksCard: some View {
        ProductCard(kicker: "Today", systemImage: "calendar.day.timeline.left") {
            calendarFreshness
            let blocks = todayBlocks
            if blocks.isEmpty {
                unavailable(plan.message ?? "오늘 동기화된 Apple·Google 일정이 없습니다.")
            } else {
                ForEach(blocks) { event in
                    HStack(alignment: .top, spacing: 12) {
                        RoundedRectangle(cornerRadius: 3)
                            .fill(eventColor(event))
                            .frame(width: 4, height: 46)
                        Text(event.startAt, format: .dateTime.hour().minute())
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 52, alignment: .leading)
                            .environment(\.timeZone, displayTimeZone)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(verbatim: event.summary ?? "제목 없는 일정")
                                .font(.body.weight(.semibold))
                                .lineLimit(2)
                            Text(verbatim: eventSource(event))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(.vertical, 2)
                    if event.id != blocks.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var calendarFreshness: some View {
        if let error = plan.calendarError {
            Label(
                plan.events.isEmpty
                    ? "캘린더 동기화 실패"
                    : "오프라인 일정 · 마지막 성공 데이터를 표시 중",
                systemImage: "exclamationmark.arrow.triangle.2.circlepath"
            )
            .font(.caption.weight(.semibold))
            .foregroundStyle(.orange)
            Text(verbatim: error)
                .font(.caption2)
                .foregroundStyle(.secondary)
        } else if let syncedAt = plan.calendarLastSyncedAt {
            Label(
                "Calendar 동기화 \(syncedAt.formatted(.relative(presentation: .named)))",
                systemImage: "checkmark.icloud"
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        } else if plan.isLoading {
            ProgressView("Apple·Google Calendar 확인 중")
                .font(.caption)
        }
    }

    private var decisionCard: some View {
        ProductCard(kicker: "One decision", systemImage: "checkmark.bubble") {
            if let decision = briefing.pendingDecisions.first {
                Text(verbatim: decision.prompt)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let reason = decision.reason {
                    Label(reason, systemImage: "waveform.path.ecg")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Text(
                    verbatim: ProposalFormat.windowLine(
                        decision.proposal,
                        timeZone: displayTimeZone
                    )
                )
                .font(.footnote.weight(.semibold).monospacedDigit())
                HStack(spacing: 10) {
                    Button {
                        Task { await briefing.resolve(decision.proposal, action: .decline) }
                    } label: {
                        Text("No").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    Button {
                        Task { await briefing.resolve(decision.proposal, action: .accept) }
                    } label: {
                        Text("Yes").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(moss)
                }
                .disabled(
                    !canResolveDecision
                        || briefing.busyProposalIDs.contains(decision.id)
                )
                if !canResolveDecision {
                    Label(
                        "최신 건강 상태를 확인한 뒤 결정할 수 있습니다.",
                        systemImage: "lock.shield"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                Button(action: onOpenDecisionThread) {
                    Label("이 결정에 메모", systemImage: "text.bubble")
                }
                .font(.footnote.weight(.semibold))
            } else {
                Label("지금 승인할 변경이 없습니다.", systemImage: "checkmark.seal.fill")
                    .font(.headline)
                    .foregroundStyle(moss)
                Text("HealthMes는 근거가 있는 한 가지 변경만 먼저 보냅니다.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            if let banner = briefing.proposalBanner {
                Label(banner, systemImage: decisionResultPresentation.icon)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(decisionResultPresentation.color)
                    .accessibilityIdentifier("healthmes-overview-decision-result")
            }
        }
    }

    private var decisionResultPresentation: (icon: String, color: Color) {
        guard let banner = briefing.proposalBanner?.lowercased() else {
            return ("info.circle", .secondary)
        }
        if banner.contains("approved") || banner.contains("applied")
            || banner.contains("승인") || banner.contains("반영")
        {
            return ("checkmark.circle.fill", moss)
        }
        if banner.contains("declined") || banner.contains("expired")
            || banner.contains("거절") || banner.contains("만료")
        {
            return ("info.circle.fill", .orange)
        }
        return ("exclamationmark.circle.fill", .red)
    }

    private var canResolveDecision: Bool {
        briefing.snapshot != nil && !briefing.isStale
    }

    private var conclusion: String {
        if briefing.isStale {
            return "최신 건강 데이터를 기다리는 중입니다."
        }
        if let top = briefing.snapshot?.payload.alerts.top?.summary {
            return top
        }
        guard let score = briefing.snapshot?.payload.energy.score else {
            return "몸 상태와 오늘 일정을 연결할 데이터가 더 필요합니다."
        }
        switch score {
        case ..<45:
            return "오늘은 회복을 보호하고 고집중 일정은 다시 확인하세요."
        case 45..<70:
            return "핵심 일정 하나에 에너지를 남겨두는 편이 안전합니다."
        default:
            return "가용 에너지가 높은 시간에 가장 중요한 목표를 배치하세요."
        }
    }

    private var displayTimeZone: TimeZone {
        guard let identifier = briefing.snapshot?.payload.timezone else {
            return .autoupdatingCurrent
        }
        return TimeZone(identifier: identifier) ?? .autoupdatingCurrent
    }

    private var todayBlocks: [CalendarEventItem] {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = displayTimeZone
        return plan.events
            .filter { calendar.isDate($0.startAt, inSameDayAs: Date()) }
            .sorted { $0.startAt < $1.startAt }
            .prefix(4)
            .map { $0 }
    }

    private func refresh() async {
        async let briefingRefresh: Void = briefing.refresh()
        async let planRefresh: Void = plan.refresh(timeZone: displayTimeZone)
        _ = await (briefingRefresh, planRefresh)
    }

    private func unavailable(_ message: String) -> some View {
        Label(message, systemImage: "questionmark.diamond")
            .font(.footnote)
            .foregroundStyle(.secondary)
    }

    private func eventSource(_ event: CalendarEventItem) -> String {
        if event.isAgentCreated { return "HealthMes-managed" }
        let source = event.calendarSource.lowercased()
        if source.contains("google") { return "Google Calendar" }
        if source.contains("apple") || source.contains("icloud") || source.contains("caldav") {
            return "Apple Calendar"
        }
        return event.calendarSource
    }

    private func eventColor(_ event: CalendarEventItem) -> Color {
        if event.isAgentCreated { return HealthMesVisualStyle.proposal }
        return event.calendarSource.lowercased().contains("google")
            ? HealthMesVisualStyle.calendar
            : HealthMesVisualStyle.capacity
    }

    private var workspaceBackground: some View {
        HealthMesVisualStyle.canvas.ignoresSafeArea()
    }
}

private struct WorkspaceCapacityBar: View {
    let score: Int?

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.08))
                Capsule()
                    .fill(barColor)
                    .frame(
                        width: proxy.size.width
                            * CGFloat(min(max(score ?? 0, 0), 100)) / 100
                    )
            }
        }
        .frame(height: 12)
        .accessibilityLabel("가용 에너지")
        .accessibilityValue(score.map { "\($0) percent" } ?? "데이터 없음")
    }

    private var barColor: Color {
        HealthMesVisualStyle.capacityColor(score)
    }
}

private struct WorkspaceInsightCanvas: View {
    @State private var scene: WellnessScene?
    @State private var message: String?
    @State private var isLoading = false
    @State private var operationGate = PairingOperationGate()
    let onOpenThread: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 14) {
                if isLoading {
                    ProductCard(kicker: "Wellness insight", systemImage: "sparkles") {
                        ProgressView("개인 기준선과 최근 추이를 조합하는 중…")
                    }
                } else if let scene {
                    WellnessSceneRenderer(
                        scene: scene,
                        maximumVisualizations: 3,
                        busyProposalIDs: [],
                        showsActions: false,
                        onAction: handleAction
                    )
                } else {
                    ProductCard(kicker: "Wellness insight", systemImage: "chart.xyaxis.line") {
                        Text(verbatim: message ?? "검증된 데이터가 들어오면 추이와 영향 요인을 보여줍니다.")
                            .font(.body.weight(.medium))
                        Text("표본이 부족하면 차트를 만들지 않습니다.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                Button(action: onOpenThread) {
                    Label("이 인사이트에 메모", systemImage: "text.bubble")
                }
                .buttonStyle(.bordered)
            }
            .padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .refreshable { await load() }
        .task {
            if scene == nil && !isLoading {
                await load()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            operationGate.invalidate()
            scene = nil
            message = nil
            isLoading = false
            Task { await load() }
        }
        .accessibilityIdentifier("healthmes-insights-canvas")
    }

    private func load() async {
        guard let pairing = PairingStore.shared.load() else {
            scene = nil
            message = "HealthMes 연결을 먼저 설정해 주세요."
            return
        }
        let operation = operationGate.begin(pairing: pairing)
        isLoading = true
        defer {
            if operationGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load()
            ) {
                isLoading = false
            }
        }
        do {
            let generated = try await HealthMesAPI().createWellnessScene(
                query: "최근 건강·활동·식사·일정 데이터에서 검증 가능한 wellness 추이와 영향 요인을 보여줘",
                pairing: pairing
            )
            guard operationGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load()
            ) else { return }
            scene = generated
            message = nil
        } catch {
            guard operationGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load()
            ) else { return }
            scene = nil
            message = BriefingHomeModel.describe(error)
        }
    }

    private func handleAction(_ action: WellnessSceneAction) {
        guard action.kind == .refresh else { return }
        Task { await load() }
    }
}

private struct WorkspaceAgentCanvas: View {
    @EnvironmentObject private var router: AppRouter
    @ObservedObject var workspace: WorkspaceViewModel
    let channel: WorkspaceChannel
    let onOpenThread: () -> Void

    @StateObject private var command = VoiceCommandModel()
    @StateObject private var briefing = BriefingHomeModel()
    @StateObject private var plan = PlanModel()
    @State private var scene: WellnessScene?
    @State private var submittedCommand: String?
    @State private var statusMessage: String?
    @State private var isRunning = false
    @State private var busyProposalID: UUID?
    @State private var sceneOperation: PairingOperationToken?
    @State private var sceneGate = PairingOperationGate()
    @State private var resolutionGate = PairingOperationGate()
    @State private var writePreview: AgentWritePreview?
    @State private var lastFocusRequest = 0
    @State private var lastVoiceStartRequest = 0
    @FocusState private var commandFocused: Bool

    private let moss = HealthMesVisualStyle.capacity

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    agentIdentity
                    executionPipeline
                    if let writePreview {
                        writeConfirmation(writePreview)
                    } else if let scene {
                        WellnessSceneRenderer(
                            scene: scene,
                            maximumVisualizations: 2,
                            busyProposalIDs: busyProposalID.map { [$0] } ?? [],
                            showsActions: true,
                            onAction: handleAction
                        )
                    } else {
                        readyState
                    }
                    if let statusMessage {
                        Label(statusMessage, systemImage: "info.circle.fill")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 4)
                    }
                }
                .padding(16)
                .padding(.bottom, 8)
            }
            .background(workspaceBackground)

            agentComposer
        }
        .task {
            if briefing.snapshot == nil {
                await refreshEvidence()
            }
        }
        .onReceive(router.$commandFocusRequest) { request in
            guard request > lastFocusRequest else { return }
            lastFocusRequest = request
            if let prefill = router.consumePendingCommand() {
                command.transcript = prefill
            }
            commandFocused = true
        }
        .onReceive(router.$voiceStartRequest) { request in
            guard request > lastVoiceStartRequest else { return }
            lastVoiceStartRequest = request
            let launch = router.consumePendingVoiceStart(request: request)
            guard launch.shouldStart else { return }
            commandFocused = false
            Task {
                await command.startListening(prefill: launch.prefill)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            sceneGate.invalidate()
            resolutionGate.invalidate()
            scene = nil
            sceneOperation = nil
            submittedCommand = nil
            statusMessage = nil
            busyProposalID = nil
            writePreview = nil
            briefing.resetForPairingChange()
            plan.resetForPairingChange()
            Task { await refreshEvidence() }
        }
        .onDisappear { command.reset() }
        .overlay(alignment: .topLeading) {
            Color.clear
                .frame(width: 1, height: 1)
                .accessibilityElement()
                .accessibilityIdentifier("healthmes-agent-canvas")
        }
    }

    private var agentIdentity: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(moss.gradient)
                    .frame(width: 52, height: 52)
                Image(systemName: "waveform.and.magnifyingglass")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.white)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("HealthMes Agent")
                    .font(.system(.title2, design: .rounded).weight(.bold))
                Text("몸 상태·목표·Apple·Google Calendar를 함께 보고 실행 가능한 최소 변경안을 만듭니다.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Button(action: onOpenThread) {
                Image(systemName: "text.bubble")
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("로컬 에이전트 메모 열기")
        }
    }

    private var executionPipeline: some View {
        ProductCard(kicker: "Agent run", systemImage: "point.3.connected.trianglepath.dotted") {
            if let submittedCommand {
                Text(verbatim: "“\(submittedCommand)”")
                    .font(.body.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 10) {
                agentStage(
                    title: "요청 해석",
                    detail: submittedCommand == nil ? "명령 대기" : inferredIntent,
                    state: submittedCommand == nil ? .waiting : .complete
                )
                agentStage(
                    title: "근거 확인",
                    detail: evidenceSummary,
                    state: isRunning ? .active : (scene == nil ? .waiting : .complete)
                )
                agentStage(
                    title: "Wellness UI 생성",
                    detail: scene == nil ? "아직 생성되지 않음" : sceneResultSummary,
                    state: isRunning ? .active : (scene == nil ? .waiting : .complete)
                )
                agentStage(
                    title: "실행",
                    detail: executionSummary,
                    state: executionStageState
                )
            }
        }
    }

    private var readyState: some View {
        ProductCard(kicker: "Ready", systemImage: "sparkles") {
            Text("말하거나 입력하면 대화 목록이 아니라 상황에 맞는 UI가 생성됩니다.")
                .font(.body.weight(.semibold))
            HStack(spacing: 8) {
                suggestion("오늘 일정을 몸 상태에 맞춰 조정해줘")
                suggestion("왜 오후에 지치는지 보여줘")
            }
            Button {
                router.modal = .capture
            } label: {
                Label("식사 사진으로 분석", systemImage: "camera.fill")
            }
            .buttonStyle(.bordered)
            if let decision = briefing.pendingDecisions.first {
                Button {
                    Task { await review(decision) }
                } label: {
                    Label("대기 제안 검토", systemImage: "checkmark.bubble")
                }
                .buttonStyle(.borderedProminent)
                .tint(moss)
                .accessibilityIdentifier("healthmes-review-pending-proposal")
                .disabled(isRunning)
            }
        }
    }

    private func writeConfirmation(_ preview: AgentWritePreview) -> some View {
        ProductCard(kicker: "Confirm", systemImage: "checkmark.shield") {
            Text(preview.kind == .task ? "이 작업을 만들까요?" : "이 주간 목표를 만들까요?")
                .font(.headline)
            Text(verbatim: preview.title)
                .font(.title3.weight(.semibold))
            Text("확인 전에는 아무것도 저장하지 않습니다.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            HStack {
                Button("취소") {
                    writePreview = nil
                }
                .buttonStyle(.bordered)
                Spacer()
                Button("생성") {
                    guard !command.isSaving else { return }
                    writePreview = nil
                    Task { await confirm(preview) }
                }
                .buttonStyle(.borderedProminent)
                .tint(moss)
                .disabled(command.isSaving)
            }
        }
    }

    private var agentComposer: some View {
        VStack(spacing: 8) {
            if command.isListening {
                HStack(spacing: 7) {
                    Circle().fill(.red).frame(width: 7, height: 7)
                    Text("듣는 중 · 전송 전에 내용을 확인합니다.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
                Button {
                    Task { await command.toggleListening() }
                } label: {
                    Image(systemName: command.isListening ? "stop.fill" : "waveform")
                        .frame(width: 42, height: 42)
                        .foregroundStyle(command.isListening ? .white : moss)
                        .background(
                            command.isListening ? Color.red : moss.opacity(0.12),
                            in: Circle()
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel(command.isListening ? "음성 입력 중지" : "음성으로 지시")

                TextField(
                    "HealthMes에게 무엇을 맡길까요?",
                    text: $command.transcript,
                    axis: .vertical
                )
                .lineLimit(1...4)
                .focused($commandFocused)
                .textFieldStyle(.plain)
                .padding(.horizontal, 13)
                .padding(.vertical, 11)
                .background(
                    Color.primary.opacity(0.055),
                    in: RoundedRectangle(cornerRadius: 17, style: .continuous)
                )
                .overlay {
                    RoundedRectangle(cornerRadius: 17, style: .continuous)
                        .stroke(HealthMesVisualStyle.line)
                }
                .submitLabel(.send)
                .onSubmit(submit)
                .accessibilityIdentifier("healthmes-command-input")

                Button(action: submit) {
                    Image(systemName: "arrow.up")
                        .font(.body.bold())
                        .foregroundStyle(.white)
                        .frame(width: 42, height: 42)
                        .background(moss, in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(
                    command.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || isRunning
                )
                .accessibilityLabel("Agent 실행")

                Button {
                    router.modal = .capture
                } label: {
                    Image(systemName: "camera.fill")
                        .frame(width: 42, height: 42)
                }
                .buttonStyle(.bordered)
                .clipShape(Circle())
                .accessibilityLabel("식사 사진")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) { Divider() }
    }

    private func suggestion(_ text: String) -> some View {
        Button {
            command.transcript = text
            commandFocused = true
        } label: {
            Text(verbatim: text)
                .font(.caption.weight(.semibold))
                .lineLimit(2)
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .buttonStyle(.bordered)
    }

    private enum StageState {
        case waiting
        case active
        case complete
        case attention
    }

    private func agentStage(
        title: String,
        detail: String,
        state: StageState
    ) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Group {
                if state == .active {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: stageIcon(state))
                        .foregroundStyle(stageColor(state))
                }
            }
            .frame(width: 20, height: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: title)
                    .font(.subheadline.weight(.semibold))
                Text(verbatim: detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
    }

    private func stageIcon(_ state: StageState) -> String {
        switch state {
        case .waiting: return "circle"
        case .active: return "circle.dotted"
        case .complete: return "checkmark.circle.fill"
        case .attention: return "exclamationmark.circle.fill"
        }
    }

    private func stageColor(_ state: StageState) -> Color {
        switch state {
        case .waiting: return .secondary
        case .active, .complete: return moss
        case .attention: return .orange
        }
    }

    private var inferredIntent: String {
        guard let submittedCommand,
            let intent = WellnessCommandParser.parse(submittedCommand)
        else { return "요청을 Wellness Scene으로 구조화" }
        switch intent {
        case .show(.now): return "현재 몸 상태와 수행 능력 분석"
        case .show(.coordinate): return "일정·목표 조율안 생성"
        case .show(.change): return "이전 결정과 결과 비교"
        case .createTask: return "새 작업 생성 요청"
        case .createGoal: return "주간 목표 생성 요청"
        case .clarify: return "질문에 맞는 wellness 인사이트 생성"
        }
    }

    private var evidenceSummary: String {
        var sources: [String] = []
        sources.append(briefing.snapshot == nil ? "건강 상태 없음" : "건강 상태 확인")
        sources.append(
            plan.events.isEmpty
                ? "동기화 일정 없음"
                : "일정 \(plan.events.count)개"
        )
        sources.append(
            plan.goals.isEmpty
                ? "활성 목표 없음"
                : "목표 \(plan.goals.count)개"
        )
        sources.append(
            briefing.pendingDecisions.isEmpty
                ? "대기 결정 없음"
                : "대기 결정 \(briefing.pendingDecisions.count)개"
        )
        return sources.joined(separator: " · ")
    }

    private var sceneResultSummary: String {
        guard let scene else { return "아직 생성되지 않음" }
        return "\(scene.title) · 신뢰도 \(scene.confidence.level.rawValue)"
    }

    private var executionSummary: String {
        guard let scene else { return "승인할 변경 없음" }
        if scene.actions.contains(where: {
            $0.kind == .acceptProposal || $0.kind == .declineProposal
        }) {
            return busyProposalID == nil ? "사용자 승인 대기" : "Calendar 반영 요청 중"
        }
        return "읽기 전용 인사이트 · Calendar 변경 없음"
    }

    private var executionStageState: StageState {
        guard scene != nil else { return .waiting }
        if busyProposalID != nil { return .active }
        if scene?.actions.contains(where: {
            $0.kind == .acceptProposal || $0.kind == .declineProposal
        }) == true {
            return .attention
        }
        return .complete
    }

    private func submit() {
        command.stopListening()
        let query = command.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, !isRunning else { return }
        resolutionGate.invalidate()
        busyProposalID = nil
        submittedCommand = query
        command.transcript = ""
        scene = nil
        statusMessage = nil
        writePreview = nil
        switch WellnessCommandParser.parse(query) {
        case .createTask(let title):
            writePreview = AgentWritePreview(kind: .task, title: title)
        case .createGoal(let title):
            writePreview = AgentWritePreview(kind: .goal, title: title)
        case .show, .clarify:
            Task { await run(query) }
        case nil:
            break
        }
    }

    private func run(_ query: String) async {
        guard !isRunning else { return }
        guard let pairing = PairingStore.shared.load() else {
            statusMessage = "HealthMes 연결을 먼저 설정해 주세요."
            return
        }
        isRunning = true
        defer { isRunning = false }
        await refreshEvidence()
        guard PairingStore.shared.load() == pairing else { return }

        let operation = sceneGate.begin(pairing: pairing)
        do {
            let generated = try await HealthMesAPI().createWellnessScene(
                query: query,
                pairing: pairing
            )
            guard sceneGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load()
            ) else { return }
            scene = generated
            sceneOperation = operation
        } catch {
            guard sceneGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load()
            ) else { return }
            scene = nil
            sceneOperation = nil
            statusMessage = BriefingHomeModel.describe(error)
        }
    }

    private func review(_ decision: PendingDecision) async {
        guard !isRunning else { return }
        guard let pairing = PairingStore.shared.load() else {
            statusMessage = "HealthMes 연결을 먼저 설정해 주세요."
            return
        }
        submittedCommand = decision.prompt
        writePreview = nil
        scene = nil
        statusMessage = nil
        isRunning = true
        defer { isRunning = false }
        await refreshEvidence()
        guard
            PairingStore.shared.load() == pairing,
            briefing.pendingDecisions.contains(decision)
        else {
            statusMessage = "제안 상태가 바뀌어 다시 불러왔습니다."
            return
        }
        let operation = sceneGate.begin(
            pairing: pairing,
            proposalID: decision.proposal.id
        )
        do {
            let generated = try await HealthMesAPI().createWellnessScene(
                query: "\(decision.proposal.id) 일정 제안을 현재 상태 기준으로 검토해줘",
                source: .proactive,
                proposalID: decision.proposal.id,
                decisionRecordID: decision.proposal.decisionRecordId,
                pairing: pairing
            )
            guard sceneGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load(),
                proposalID: decision.proposal.id
            ) else { return }
            scene = generated
            sceneOperation = operation
        } catch {
            guard sceneGate.isCurrent(
                operation,
                pairing: PairingStore.shared.load(),
                proposalID: decision.proposal.id
            ) else { return }
            statusMessage = BriefingHomeModel.describe(error)
        }
    }

    private func confirm(_ preview: AgentWritePreview) async {
        guard !command.isSaving else { return }
        command.destination = preview.kind == .task ? .task : .weeklyGoal
        command.transcript = preview.title
        await command.save()
        statusMessage = command.message
        await refreshEvidence()
    }

    private func refreshEvidence() async {
        let timeZone =
            briefing.snapshot.flatMap {
                TimeZone(identifier: $0.payload.timezone)
            }
            ?? .autoupdatingCurrent
        async let briefingRefresh: Void = briefing.refresh()
        async let planRefresh: Void = plan.refresh(timeZone: timeZone)
        _ = await (briefingRefresh, planRefresh)
    }

    private func handleAction(_ action: WellnessSceneAction) {
        switch action.kind {
        case .acceptProposal, .declineProposal:
            guard
                let scene,
                scene.allowsProposalActions,
                !briefing.isStale,
                briefing.snapshot != nil,
                let proposalID = action.proposalID,
                scene.allowsProposalActions(for: proposalID),
                let operation = sceneOperation,
                sceneGate.isCurrent(
                    operation,
                    pairing: PairingStore.shared.load()
                ),
                let proposal = briefing.pendingProposals.first(where: {
                    $0.id == proposalID && $0.isActionable
                }),
                let decision = briefing.pendingDecisions.first(where: {
                    $0.id == proposalID && $0.hasExactDecisionCorrelation
                }),
                decision.proposal == proposal
            else {
                statusMessage = "이 제안은 현재 상태와 정확히 연결되지 않아 실행하지 않았습니다."
                return
            }
            busyProposalID = proposalID
            let resolutionOperation = resolutionGate.begin(
                pairing: operation.pairing,
                proposalID: proposalID
            )
            let resolvedSceneOperation = operation
            Task {
                await briefing.resolve(
                    proposal,
                    action: action.kind == .acceptProposal ? .accept : .decline,
                    pairing: operation.pairing
                )
                guard resolutionGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: proposalID
                ) else { return }
                busyProposalID = nil
                statusMessage = briefing.proposalBanner
                guard sceneOperation == resolvedSceneOperation else { return }
                self.scene = nil
                sceneOperation = nil
            }
        case .openWebDetail:
            if let url = action.url {
                router.openDecision(url)
            }
        case .refresh:
            if let submittedCommand {
                Task { await run(submittedCommand) }
            }
        case .createTask, .createGoal, .modifyProposal, .switchLens:
            statusMessage = "이 동작은 기존 확인 절차가 있는 화면에서만 실행됩니다."
        }
    }

    private var workspaceBackground: some View {
        HealthMesVisualStyle.canvas.ignoresSafeArea()
    }
}

private struct WorkspaceCustomDashboardCanvas: View {
    @StateObject private var briefing = BriefingHomeModel()
    @StateObject private var plan = PlanModel()
    let channel: WorkspaceChannel
    let onEditCards: () -> Void
    let onOpenThread: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                customHeader(
                    subtitle: "이 채널에 고정한 wellness 모듈만 모아봅니다."
                )
                ForEach(channel.cards.filter(\.isVisible)) { card in
                    WorkspaceConfiguredCard(
                        card: card,
                        briefing: briefing,
                        plan: plan
                    )
                }
                if channel.cards.filter(\.isVisible).isEmpty {
                    ContentUnavailableView(
                        "표시할 카드가 없습니다",
                        systemImage: "rectangle.badge.plus",
                        description: Text("채널 설정에서 필요한 wellness 카드만 추가하세요.")
                    )
                }
            }
            .padding(16)
        }
        .background(HealthMesVisualStyle.canvas)
        .task { await refresh() }
        .refreshable { await refresh() }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            briefing.resetForPairingChange()
            plan.resetForPairingChange()
            Task { await refresh() }
        }
        .accessibilityIdentifier("healthmes-custom-dashboard-canvas")
    }

    private func refresh() async {
        async let briefingRefresh: Void = briefing.refresh()
        async let planRefresh: Void = plan.refresh()
        _ = await (briefingRefresh, planRefresh)
    }

    private func customHeader(subtitle: String) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: channel.title)
                    .font(.system(.title2, design: .rounded).weight(.bold))
                Text(verbatim: subtitle)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button(action: onOpenThread) {
                Image(systemName: "text.bubble")
            }
            .buttonStyle(.bordered)
            Button(action: onEditCards) {
                Image(systemName: "slider.horizontal.3")
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("대시보드 카드 편집")
        }
    }
}

private struct WorkspaceMixedCanvas: View {
    @StateObject private var briefing = BriefingHomeModel()
    @StateObject private var plan = PlanModel()
    let channel: WorkspaceChannel
    let onEditCards: () -> Void
    let onOpenThread: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(verbatim: channel.title)
                            .font(.system(.title2, design: .rounded).weight(.bold))
                        Text("카드, 캘린더, 결정 메모를 한 로컬 보드에 조합합니다.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(action: onOpenThread) {
                        Image(systemName: "text.bubble")
                    }
                    .buttonStyle(.bordered)
                    Button(action: onEditCards) {
                        Image(systemName: "slider.horizontal.3")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("혼합 캔버스 카드 편집")
                }
                ForEach(channel.cards.filter(\.isVisible)) { card in
                    WorkspaceConfiguredCard(
                        card: card,
                        briefing: briefing,
                        plan: plan
                    )
                }
            }
            .padding(16)
        }
        .background(HealthMesVisualStyle.canvas)
        .task { await refresh() }
        .refreshable { await refresh() }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            briefing.resetForPairingChange()
            plan.resetForPairingChange()
            Task { await refresh() }
        }
        .accessibilityIdentifier("healthmes-mixed-canvas")
    }

    private func refresh() async {
        async let briefingRefresh: Void = briefing.refresh()
        async let planRefresh: Void = plan.refresh()
        _ = await (briefingRefresh, planRefresh)
    }
}

private struct WorkspaceConfiguredCard: View {
    @EnvironmentObject private var router: AppRouter
    let card: WorkspaceCard
    @ObservedObject var briefing: BriefingHomeModel
    @ObservedObject var plan: PlanModel

    var body: some View {
        ProductCard(kicker: LocalizedStringKey(title), systemImage: symbol) {
            cardContent
        }
        .frame(minHeight: minimumHeight, alignment: .top)
    }

    @ViewBuilder
    private var cardContent: some View {
        switch card.kind {
        case .wellnessStatus, .summary:
            Text(wellnessConclusion)
                .font(.body.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            evidenceLine
        case .capacity:
            HStack(alignment: .firstTextBaseline) {
                Text(verbatim: GlanceFormat.scoreText(briefing.snapshot?.payload.energy.score))
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text("available energy")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            WorkspaceCapacityBar(score: briefing.snapshot?.payload.energy.score)
        case .calendarTimeline:
            if let event = nextEvent {
                Text(event.startAt, format: .dateTime.hour().minute())
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Text(verbatim: event.summary ?? "제목 없는 일정")
                    .font(.body.weight(.semibold))
                Text(verbatim: event.calendarSource)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                unavailable("다가오는 동기화 일정이 없습니다.")
            }
        case .energyCurve:
            if let payload = briefing.snapshot?.payload {
                EnergyCurveView(curve: payload.energy.curve24h, timezone: payload.timezone)
            } else {
                unavailable("에너지 추이를 만들 건강 데이터가 없습니다.")
            }
        case .baseline:
            unavailable(
                "현재 snapshot에는 개인 기준선 비교값이 없습니다. Insights 채널에서 검증된 기준선 Scene이 생성될 때만 표시합니다."
            )
        case .factors:
            unavailable(
                "일정 수나 알림 수를 영향 요인으로 꾸미지 않습니다. Agent가 검증된 factor_contribution을 반환할 때만 표시합니다."
            )
        case .goalProgress:
            if let goal = plan.goals.sorted(by: { $0.priority > $1.priority }).first {
                Text(verbatim: goal.title)
                    .font(.body.weight(.semibold))
                Text("이번 주 우선순위 \(goal.priority)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                unavailable("활성 주간 목표가 없습니다.")
            }
        case .pendingDecision:
            if let decision = briefing.pendingDecisions.first {
                Text(verbatim: decision.prompt)
                    .font(.body.weight(.semibold))
                Text(
                    verbatim: ProposalFormat.windowLine(
                        decision.proposal,
                        timeZone: displayTimeZone
                    )
                )
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    Button {
                        Task {
                            await briefing.resolve(decision.proposal, action: .decline)
                        }
                    } label: {
                        Text("No").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    Button {
                        Task {
                            await briefing.resolve(decision.proposal, action: .accept)
                        }
                    } label: {
                        Text("Yes").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                }
                .disabled(
                    briefing.snapshot == nil
                        || briefing.isStale
                        || briefing.busyProposalIDs.contains(decision.id)
                )
                if briefing.snapshot == nil || briefing.isStale {
                    Label(
                        "최신 건강 상태를 확인한 뒤 결정할 수 있습니다.",
                        systemImage: "lock.shield"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            } else {
                unavailable("대기 중인 결정이 없습니다.")
            }
        case .decisionResult:
            if let banner = briefing.proposalBanner {
                Text(verbatim: banner)
                    .font(.body.weight(.semibold))
            } else {
                unavailable("이 앱 실행 중 완료된 결정 결과가 없습니다.")
            }
        case .nutrition:
            Button {
                router.modal = .capture
            } label: {
                Label("식사 사진 분석 열기", systemImage: "camera.fill")
            }
            .buttonStyle(.borderedProminent)
        case .command:
            Button {
                router.openAgentCommandDock()
            } label: {
                Label("HealthMes Agent 열기", systemImage: "waveform")
            }
            .buttonStyle(.borderedProminent)
        }
    }

    private var evidenceLine: some View {
        HStack(spacing: 12) {
            Label(
                briefing.snapshot == nil ? "건강 없음" : "건강 확인",
                systemImage: "heart.text.square"
            )
            Label("\(plan.events.count) 일정", systemImage: "calendar")
            Label("\(plan.goals.count) 목표", systemImage: "scope")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private var wellnessConclusion: String {
        if briefing.isStale { return "최신 건강 데이터를 기다리는 중입니다." }
        if let summary = briefing.snapshot?.payload.alerts.top?.summary {
            return summary
        }
        guard let score = briefing.snapshot?.payload.energy.score else {
            return "몸 상태와 계획을 연결할 데이터가 더 필요합니다."
        }
        return score < 45
            ? "오늘은 회복을 보호하고 고부하 일정을 다시 확인하세요."
            : "가용 에너지가 높은 시간에 중요한 목표를 배치하세요."
    }

    private var nextEvent: CalendarEventItem? {
        plan.events
            .filter { $0.endAt > Date() }
            .sorted { $0.startAt < $1.startAt }
            .first
    }

    private var displayTimeZone: TimeZone {
        guard let identifier = briefing.snapshot?.payload.timezone else {
            return .autoupdatingCurrent
        }
        return TimeZone(identifier: identifier) ?? .autoupdatingCurrent
    }

    private func unavailable(_ message: String) -> some View {
        Label(message, systemImage: "questionmark.diamond")
            .font(.footnote)
            .foregroundStyle(.secondary)
    }

    private var title: String {
        switch card.kind {
        case .wellnessStatus: return "Wellness status"
        case .capacity: return "Capacity"
        case .calendarTimeline: return "Calendar"
        case .energyCurve: return "Energy curve"
        case .baseline: return "Personal baseline"
        case .factors: return "Influencing factors"
        case .goalProgress: return "Goal trajectory"
        case .pendingDecision: return "Pending decision"
        case .decisionResult: return "Decision outcome"
        case .nutrition: return "Nutrition"
        case .summary: return "Summary"
        case .command: return "Agent command"
        }
    }

    private var symbol: String {
        switch card.kind {
        case .wellnessStatus: return "heart.text.square"
        case .capacity: return "battery.75percent"
        case .calendarTimeline: return "calendar.day.timeline.left"
        case .energyCurve: return "chart.xyaxis.line"
        case .baseline: return "waveform.path"
        case .factors: return "chart.bar.xaxis"
        case .goalProgress: return "scope"
        case .pendingDecision: return "questionmark.bubble"
        case .decisionResult: return "checkmark.circle"
        case .nutrition: return "fork.knife"
        case .summary: return "text.justify.left"
        case .command: return "waveform"
        }
    }

    private var minimumHeight: CGFloat? {
        switch card.size {
        case .compact: return nil
        case .regular: return 140
        case .expanded: return 220
        }
    }
}

private enum AgentWriteKind: Equatable {
    case task
    case goal
}

private struct AgentWritePreview: Equatable {
    let kind: AgentWriteKind
    let title: String
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
        .healthMesSurface(radius: 16)
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
                ? HealthMesVisualStyle.calendar.opacity(0.11)
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
                    .background(HealthMesVisualStyle.brand, in: Circle())
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

private struct WorkspaceCardEditor: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var workspace: WorkspaceViewModel
    let channelID: UUID

    private var channel: WorkspaceChannel? {
        workspace.state.categories
            .flatMap(\.channels)
            .first { $0.id == channelID }
    }

    var body: some View {
        NavigationStack {
            List {
                if let channel {
                    Section("표시 순서") {
                        ForEach(channel.cards) { card in
                            cardRow(card)
                        }
                    }
                    Section {
                        Menu {
                            ForEach(availableKinds) { kind in
                                Button(cardTitle(kind)) {
                                    workspace.addCard(kind, to: channelID)
                                }
                            }
                        } label: {
                            Label("Wellness 카드 추가", systemImage: "plus.rectangle.on.rectangle")
                        }
                        .disabled(availableKinds.isEmpty)
                    } footer: {
                        Text("카드 구성은 이 기기에 저장되며 HealthMes 엔진이나 원본 건강 데이터는 변경하지 않습니다.")
                    }
                } else {
                    ContentUnavailableView("채널 없음", systemImage: "rectangle.slash")
                }
            }
            .navigationTitle("대시보드 편집")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("완료") { dismiss() }
                }
            }
        }
    }

    private func cardRow(_ card: WorkspaceCard) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label(cardTitle(card.kind), systemImage: cardSymbol(card.kind))
                Spacer()
                Menu {
                    Button {
                        workspace.moveCard(card.id, in: channelID, offset: -1)
                    } label: {
                        Label("위로", systemImage: "arrow.up")
                    }
                    Button {
                        workspace.moveCard(card.id, in: channelID, offset: 1)
                    } label: {
                        Label("아래로", systemImage: "arrow.down")
                    }
                    Divider()
                    Button(role: .destructive) {
                        workspace.removeCard(card.id, from: channelID)
                    } label: {
                        Label("삭제", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
            Picker(
                "크기",
                selection: Binding(
                    get: { card.size },
                    set: { workspace.setCardSize(card.id, in: channelID, size: $0) }
                )
            ) {
                Text("작게").tag(WorkspaceCardSize.compact)
                Text("보통").tag(WorkspaceCardSize.regular)
                Text("크게").tag(WorkspaceCardSize.expanded)
            }
            .pickerStyle(.segmented)
        }
        .padding(.vertical, 4)
    }

    private var availableKinds: [WorkspaceCardKind] {
        let used = Set(channel?.cards.map(\.kind) ?? [])
        return WorkspaceCardKind.allCases.filter { !used.contains($0) }
    }

    private func cardTitle(_ kind: WorkspaceCardKind) -> String {
        switch kind {
        case .wellnessStatus: return "현재 Wellness 상태"
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

    private func cardSymbol(_ kind: WorkspaceCardKind) -> String {
        switch kind {
        case .wellnessStatus: return "heart.text.square"
        case .capacity: return "battery.75percent"
        case .calendarTimeline: return "calendar.day.timeline.left"
        case .energyCurve: return "chart.xyaxis.line"
        case .baseline: return "waveform.path"
        case .factors: return "chart.bar.xaxis"
        case .goalProgress: return "scope"
        case .pendingDecision: return "questionmark.bubble"
        case .decisionResult: return "checkmark.circle"
        case .nutrition: return "fork.knife"
        case .summary: return "text.justify.left"
        case .command: return "waveform"
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
