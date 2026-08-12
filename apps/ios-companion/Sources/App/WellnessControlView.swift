import SwiftUI

private enum CommandWriteKind: Equatable {
    case task
    case goal
}

private struct CommandPreview: Identifiable {
    let id = UUID()
    let kind: CommandWriteKind
    let title: String
}

struct WellnessControlView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var briefing = BriefingHomeModel()
    @StateObject private var plan = PlanModel()
    @StateObject private var decisions = DecisionsModel()
    @StateObject private var command = VoiceCommandModel()
    @State private var lens: WellnessLens = .now
    @State private var preview: CommandPreview?
    @State private var commandMessage: String?
    @State private var generatedScene: WellnessScene?
    @State private var generatedSceneOperation: PairingOperationToken?
    @State private var isGeneratingScene = false
    @State private var sceneOperationGate = PairingOperationGate()
    @State private var resolutionOperationGate = PairingOperationGate()
    @State private var resolvingSceneProposalID: UUID?
    @State private var lastFocusRequest = 0
    @State private var lastHomeRequest = 0
    @FocusState private var commandFocused: Bool

    private let moss = Color(red: 0.08, green: 0.38, blue: 0.28)

    var body: some View {
        VStack(spacing: 0) {
            statusRail

            ScrollView {
                LazyVStack(spacing: 14) {
                    if let proposalBanner = briefing.proposalBanner {
                        proposalResultBanner(proposalBanner)
                    }
                    sceneContent
                    if let preview {
                        commandPreview(preview)
                    }
                    if let commandMessage {
                        commandResult(commandMessage)
                    }
                }
                .padding(16)
                .padding(.bottom, 8)
            }
            .refreshable { await refreshAll() }

            quickActionDock
        }
        .background(
            LinearGradient(
                colors: [
                    Color(red: 0.94, green: 0.96, blue: 0.92),
                    Color(uiColor: .systemGroupedBackground),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        )
        .environment(\.timeZone, displayTimeZone)
        .navigationTitle(Text("HealthMes"))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                exploreMenu
            }
        }
        .task { await refreshAll() }
        .onReceive(router.$commandFocusRequest) { request in
            guard request > lastFocusRequest else { return }
            lastFocusRequest = request
            if let prefill = router.consumePendingCommand() {
                command.transcript = prefill
            }
            commandFocused = true
        }
        .onReceive(router.$homeRequest) { request in
            guard request > lastHomeRequest else { return }
            lastHomeRequest = request
            preview = nil
            selectDetail(.now)
        }
        .onChange(of: command.transcript) { _, transcript in
            if command.isListening || !transcript.isEmpty {
                commandFocused = true
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .healthmesPairingChanged)
        ) { _ in
            invalidateGeneratedScene()
            resolutionOperationGate.invalidate()
            resolvingSceneProposalID = nil
            briefing.resetForPairingChange()
            plan.resetForPairingChange()
            decisions.resetForPairingChange()
            Task { await refreshAll() }
        }
        .onDisappear { command.reset() }
    }

    private var statusRail: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                statusPill(
                    title: energyStatus,
                    systemImage: "waveform.path.ecg",
                    color: briefing.isStale ? .orange : moss
                )
                statusPill(
                    title: calendarStatus,
                    systemImage: "calendar.badge.clock",
                    color: plan.events.isEmpty ? .secondary : moss
                )
                Spacer(minLength: 0)
                Text(Date(), format: .dateTime.month(.abbreviated).day())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 6) {
                ForEach(WellnessLens.allCases) { target in
                    Button {
                        selectDetail(target)
                    } label: {
                        Label(target.title, systemImage: detailIcon(for: target))
                            .font(.caption.weight(.semibold))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 7)
                    .foregroundStyle(lens == target ? Color.white : Color.primary)
                    .background(
                        lens == target ? moss : Color.primary.opacity(0.06),
                        in: Capsule()
                    )
                    .accessibilityAddTraits(lens == target ? .isSelected : [])
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.thinMaterial)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("healthmes-wellness-control")
    }

    private var exploreMenu: some View {
        Menu {
            if let pairing = PairingStore.shared.load() {
                Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["dashboard"])) {
                    Label("자세히 보기", systemImage: "safari")
                }
            }
            Button {
                router.modal = .settings
            } label: {
                Label("설정", systemImage: "gearshape")
            }
        } label: {
            Image(systemName: "ellipsis.circle")
                .font(.title3)
        }
        .accessibilityLabel(Text("더 보기"))
    }

    @ViewBuilder
    private var sceneContent: some View {
        if isGeneratingScene {
            ProductCard(kicker: "Wellness insight", systemImage: "sparkles") {
                ProgressView("건강·일정·목표를 함께 보고 있습니다…")
            }
        } else if let generatedScene {
            Group {
                WellnessSceneRenderer(
                    scene: generatedScene,
                    maximumVisualizations: 1,
                    busyProposalIDs: briefing.busyProposalIDs.union(
                        resolvingSceneProposalID.map { [$0] } ?? []
                    ),
                    showsActions: false,
                    onAction: handleSceneAction
                )
                primaryDecisionCard
                todayTimeBlocks
            }
        } else {
            nowScene
        }
    }

    private var nowScene: some View {
        Group {
            ProductCard(kicker: "오늘", systemImage: "bolt.heart.fill") {
                if let payload = briefing.snapshot?.payload {
                    Text(
                        verbatim: briefing.isStale
                            ? "판단 보류: 최신 건강 상태를 확인할 때까지 일정 변경을 제안하지 않습니다."
                            : bodyPlanImpact(payload)
                    )
                        .font(.system(.title3, design: .rounded).weight(.semibold))
                        .lineLimit(2)
                    if briefing.isStale, let snapshot = briefing.snapshot {
                        Text(
                            verbatim:
                                "마지막 동기화 \(WellnessDateFormat.abbreviatedDateTime(snapshot.fetchedAt, timeZone: displayTimeZone))"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    capacityBar(payload)
                } else {
                    insufficientData(briefing.glanceError ?? "Health data has not arrived yet.")
                }
            }

            primaryDecisionCard
            todayTimeBlocks
        }
    }

    private var coordinateScene: some View {
        Group {
            primaryDecisionCard
            ProductCard(kicker: "보호할 목표와 할 일", systemImage: "shield.lefthalf.filled") {
                if plan.goals.isEmpty && plan.tasks.isEmpty {
                    insufficientData("일정 조정에서 보호할 주간 목표나 할 일이 없습니다.")
                } else {
                    if let goal = plan.goals.first {
                        Label {
                            Text(verbatim: goal.title)
                        } icon: {
                            Image(systemName: "scope")
                        }
                        .font(.headline)
                        Text("일정을 바꾸더라도 이 주간 목표를 우선 보호합니다.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }

                    ForEach(plan.tasks.prefix(3)) { task in
                        if plan.goals.first != nil || task.id != plan.tasks.first?.id {
                            Divider()
                        }
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: task.energyDemand == "high" ? "bolt.fill" : "checkmark.circle")
                                .foregroundStyle(task.energyDemand == "high" ? Color.orange : moss)
                                .frame(width: 20)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(verbatim: task.title)
                                    .font(.body.weight(.medium))
                                Text(verbatim: taskDetail(task))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            scheduleTimeline
        }
    }

    private var changeScene: some View {
        Group {
            ProductCard(kicker: "Outcome loop", systemImage: "arrow.triangle.2.circlepath") {
                HStack {
                    outcomeMetric(
                        value: "\(decisions.records.count)",
                        label: "decisions"
                    )
                    Divider()
                    outcomeMetric(
                        value: "\(plan.proposals.filter { $0.status == .pushed }.count)",
                        label: "applied"
                    )
                    Divider()
                    outcomeMetric(
                        value: "\(plan.proposals.filter { $0.status == .accepted }.count)",
                        label: "sync pending"
                    )
                }
                Text("A decision becomes learning only after HealthMes can compare the later health and work outcome.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            ProductCard(kicker: "Recent evidence", systemImage: "chart.line.uptrend.xyaxis") {
                if decisions.records.isEmpty {
                    insufficientData("No prior decisions are available for an outcome comparison.")
                } else {
                    ForEach(decisions.records.prefix(4)) { record in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(verbatim: record.summary)
                                .font(.body.weight(.medium))
                            Text(record.createdAt, style: .relative)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        if record.id != decisions.records.prefix(4).last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private var scheduleTimeline: some View {
        ProductCard(kicker: "일정 영향", systemImage: "calendar") {
            if plan.events.isEmpty {
                insufficientData("No synced calendar events are available. Open Settings to inspect the connection.")
            } else {
                ForEach(plan.events.prefix(4)) { event in
                    HStack(alignment: .top, spacing: 10) {
                        Text(event.startAt, format: .dateTime.hour().minute())
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 54, alignment: .leading)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: event.summary ?? "Untitled event")
                                .font(.body.weight(.medium))
                            Text(
                                verbatim: event.isAgentCreated
                                    ? "HealthMes-managed"
                                    : calendarName(event.calendarSource)
                            )
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private var primaryDecisionCard: some View {
        ProductCard(kicker: "지금 필요한 결정", systemImage: "wand.and.stars") {
            if let decision = activeDecision {
                Text(verbatim: decision.prompt)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let reason = decision.reason {
                    Text(verbatim: reason)
                        .foregroundStyle(.secondary)
                }
                Text(
                    verbatim: ProposalFormat.windowLine(
                        decision.proposal,
                        timeZone: displayTimeZone
                    )
                )
                    .font(.footnote.weight(.medium))
                HStack(spacing: 10) {
                    Button {
                        performSceneDecision(.declineProposal, decision: decision)
                    } label: {
                        Label("유지", systemImage: "xmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)

                    Button {
                        performSceneDecision(.acceptProposal, decision: decision)
                    } label: {
                        Label("변경 승인", systemImage: "checkmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(moss)
                }
                .disabled(briefing.busyProposalIDs.contains(decision.id))

                if let url = decision.exactWebURL {
                    Button {
                        router.openDecision(url)
                    } label: {
                        Label("이 제안의 이유와 영향", systemImage: "arrow.up.right.square")
                    }
                    .font(.footnote.weight(.semibold))
                }
            } else {
                Label("지금 바꿀 행동이 없습니다", systemImage: "checkmark.seal")
                    .font(.headline)
                Text("HealthMes는 근거가 있는 한 가지 변경을 찾을 때만 제안합니다.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var activeDecision: PendingDecision? {
        let pending = briefing.pendingDecisions
        guard
            let generatedScene,
            WellnessDecisionSafety.canResolve(
                hasHealthSnapshot: briefing.snapshot != nil,
                isBriefingStale: briefing.isStale,
                sceneAllowsActions: generatedScene.allowsProposalActions
            ),
            let sceneOperation = generatedSceneOperation,
            sceneOperationGate.isCurrent(
                sceneOperation,
                pairing: PairingStore.shared.load()
            )
        else { return nil }
        let proposalID =
            generatedScene.exactMutationPreview?.proposalID
            ?? generatedSceneOperation?.proposalID
        guard
            let proposalID,
            generatedScene.allowsProposalActions(for: proposalID),
            sceneOperation.proposalID == proposalID
        else {
            return nil
        }
        return pending.first { $0.id == proposalID }
    }

    private func performSceneDecision(
        _ kind: WellnessActionKind,
        decision: PendingDecision
    ) {
        guard
            let action = generatedScene?.actions.first(where: {
                $0.kind == kind && $0.proposalID == decision.id
            })
        else {
            commandMessage = "이 제안은 더 이상 승인할 수 없습니다. 새로고침해 주세요."
            return
        }
        handleSceneAction(action)
    }

    private func proposalResultBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: message.hasPrefix("Approved") ? "checkmark.circle.fill" : "info.circle.fill")
                .foregroundStyle(message.hasPrefix("Approved") ? moss : Color.orange)
            Text(verbatim: message)
                .font(.footnote.weight(.semibold))
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                briefing.proposalBanner = nil
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Text("Dismiss status"))
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.primary.opacity(0.08))
        }
        .accessibilityElement(children: .contain)
    }

    private var quickActionDock: some View {
        VStack(spacing: 8) {
            if command.isListening {
                HStack(spacing: 8) {
                    Circle().fill(.red).frame(width: 7, height: 7)
                    Text("듣고 있습니다. 보내기 전에 내용을 확인하세요.")
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
                        .frame(width: 40, height: 40)
                        .foregroundStyle(command.isListening ? .white : moss)
                        .background(
                            command.isListening ? Color.red : Color.primary.opacity(0.07),
                            in: Circle()
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel(
                    Text(command.isListening ? "듣기 중지" : "음성으로 질문")
                )

                TextField(
                    "오늘 일정에서 무엇을 바꿔야 해?",
                    text: $command.transcript,
                    axis: .vertical
                )
                .lineLimit(1...3)
                .focused($commandFocused)
                .accessibilityIdentifier("healthmes-command-input")
                .textFieldStyle(.plain)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(
                    Color.primary.opacity(0.06),
                    in: RoundedRectangle(cornerRadius: 16)
                )
                .submitLabel(.send)
                .onSubmit(submitCommand)

                Button(action: submitCommand) {
                    Image(systemName: "arrow.up")
                        .font(.body.bold())
                        .foregroundStyle(.white)
                        .frame(width: 40, height: 40)
                        .background(moss, in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(
                    command.transcript
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                        .isEmpty
                )
                .accessibilityLabel(Text("질문 보내기"))
            }

            HStack {
                Button {
                    router.modal = .capture
                } label: {
                    Label("식사 사진", systemImage: "camera.fill")
                }
                .buttonStyle(.borderless)

                Spacer()

                Text("대화 기록 대신 판단 화면으로 바뀝니다.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private func capacityBar(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("가용 에너지")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                    .font(.caption.bold().monospacedDigit())
            }
            if let rawScore = payload.energy.score {
                GeometryReader { proxy in
                    let score = CGFloat(min(max(rawScore, 0), 100)) / 100
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.08))
                        Capsule()
                            .fill(moss)
                            .frame(width: proxy.size.width * score)
                    }
                }
                .frame(height: 12)
                .accessibilityLabel(Text("가용 에너지"))
                .accessibilityValue(Text(verbatim: GlanceFormat.scoreText(rawScore)))
            } else {
                Label("에너지 데이터 없음", systemImage: "chart.bar.xaxis")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var todayTimeBlocks: some View {
        ProductCard(kicker: "오늘 일정", systemImage: "calendar.day.timeline.left") {
            if plan.events.isEmpty && activeDecision == nil {
                insufficientData("Apple 또는 Google Calendar에서 동기화된 오늘 일정이 없습니다.")
            } else {
                ProportionalDayTimeline(
                    events: plan.events,
                    decision: activeDecision,
                    timeZone: displayTimeZone
                )
            }
        }
    }

    private func commandPreview(_ preview: CommandPreview) -> some View {
        ProductCard(kicker: "Confirm", systemImage: "checkmark.shield") {
            Text(preview.kind == .task ? "Create this task?" : "Create this weekly goal?")
                .font(.headline)
            Text(verbatim: preview.title)
                .font(.title3.weight(.semibold))
            Text("Nothing is written until you confirm.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            HStack {
                Button("Cancel") {
                    self.preview = nil
                }
                .buttonStyle(.bordered)
                Spacer()
                Button("Confirm") {
                    Task { await confirm(preview) }
                }
                .buttonStyle(.borderedProminent)
                .tint(moss)
            }
        }
    }

    private func commandResult(_ message: String) -> some View {
        ProductCard(kicker: "Command result", systemImage: "sparkles") {
            Text(verbatim: message)
                .font(.body.weight(.medium))
            if message.contains("지원") || message.contains("Choose") {
                HStack {
                    detailChip(.now)
                    detailChip(.coordinate)
                    detailChip(.change)
                }
            }
        }
    }

    private func detailChip(_ target: WellnessLens) -> some View {
        Button(detailTitle(for: target)) {
            selectDetail(target)
        }
        .buttonStyle(.bordered)
    }

    private func submitCommand() {
        command.stopListening()
        guard let intent = WellnessCommandParser.parse(command.transcript) else { return }
        preview = nil
        commandMessage = nil
        switch intent {
        case .show(let target):
            selectDetail(target)
            let query = command.transcript
            command.transcript = ""
            Task { await loadScene(query: query) }
        case .createTask(let title):
            preview = CommandPreview(kind: .task, title: title)
        case .createGoal(let title):
            preview = CommandPreview(kind: .goal, title: title)
        case .clarify(let query):
            command.transcript = ""
            Task { await loadScene(query: query) }
        }
    }

    private func confirm(_ preview: CommandPreview) async {
        command.destination = preview.kind == .task ? .task : .weeklyGoal
        command.transcript = preview.title
        await command.save()
        commandMessage = command.message
        self.preview = nil
        selectDetail(.coordinate, clearMessage: false)
        await refreshAll()
    }

    private func selectDetail(_ target: WellnessLens, clearMessage: Bool = true) {
        if generatedScene?.lens != target {
            invalidateGeneratedScene()
        }
        withAnimation(.easeOut(duration: 0.18)) {
            lens = target
            if clearMessage {
                commandMessage = nil
            }
        }
    }

    private func detailTitle(for target: WellnessLens) -> String {
        switch target {
        case .now: return "현재 영향"
        case .coordinate: return "일정과 목표"
        case .change: return "결정 결과"
        }
    }

    private func detailIcon(for target: WellnessLens) -> String {
        switch target {
        case .now: return "bolt.heart"
        case .coordinate: return "calendar"
        case .change: return "chart.line.uptrend.xyaxis"
        }
    }

    private func refreshAll() async {
        invalidateGeneratedScene()
        guard let pairingSnapshot = PairingStore.shared.load() else { return }
        let refreshOperation = sceneOperationGate.begin(pairing: pairingSnapshot)
        async let decisionsRefresh: Void = decisions.refresh()
        await briefing.refresh()
        async let planRefresh: Void = plan.refresh(timeZone: displayTimeZone)
        _ = await (planRefresh, decisionsRefresh)
        guard sceneOperationGate.isCurrent(
            refreshOperation,
            pairing: PairingStore.shared.load()
        ) else { return }
        if let proposal = ProactiveProposalSelection.firstEligible(
            in: briefing.pendingProposals
        ) {
            await loadScene(
                query: "\(proposal.id) 일정 제안을 현재 상태 기준으로 검토해줘",
                source: .proactive,
                proposalID: proposal.id,
                decisionRecordID: proposal.decisionRecordId
            )
        } else {
            await loadScene(query: sceneQuery(for: lens))
        }
    }

    private func loadScene(
        query: String,
        source: WellnessSceneRequest.Source = .user,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) async {
        generatedScene = nil
        generatedSceneOperation = nil
        isGeneratingScene = true
        commandMessage = nil
        guard let pairingSnapshot = PairingStore.shared.load() else {
            isGeneratingScene = false
            commandMessage = "HealthMes 연결을 먼저 설정해 주세요."
            return
        }
        let sceneOperation = sceneOperationGate.begin(
            pairing: pairingSnapshot,
            proposalID: proposalID
        )
        defer {
            if sceneOperationGate.isCurrent(
                sceneOperation,
                pairing: PairingStore.shared.load()
            ) {
                isGeneratingScene = false
            }
        }
        do {
            let scene = try await HealthMesAPI().createWellnessScene(
                query: query,
                source: source,
                proposalID: proposalID,
                decisionRecordID: decisionRecordID,
                pairing: pairingSnapshot
            )
            guard
                sceneOperationGate.isCurrent(
                    sceneOperation,
                    pairing: PairingStore.shared.load()
                )
            else { return }
            lens = scene.lens
            generatedScene = scene
            generatedSceneOperation = sceneOperation
        } catch {
            guard
                sceneOperationGate.isCurrent(
                    sceneOperation,
                    pairing: PairingStore.shared.load()
                )
            else { return }
            generatedScene = nil
            generatedSceneOperation = nil
            commandMessage = "Wellness insight를 불러오지 못했습니다. 연결 상태를 확인한 뒤 다시 시도하세요."
        }
    }

    private func handleSceneAction(_ action: WellnessSceneAction) {
        switch action.kind {
        case .acceptProposal, .declineProposal:
            guard
                WellnessDecisionSafety.canResolve(
                    hasHealthSnapshot: briefing.snapshot != nil,
                    isBriefingStale: briefing.isStale,
                    sceneAllowsActions: generatedScene?.allowsProposalActions == true
                ),
                let sceneOperation = generatedSceneOperation,
                sceneOperationGate.isCurrent(
                    sceneOperation,
                    pairing: PairingStore.shared.load()
                ),
                generatedScene?.allowsProposalActions == true,
                let proposalID = action.proposalID,
                sceneOperation.proposalID == proposalID,
                resolvingSceneProposalID == nil,
                let proposal = briefing.pendingProposals.first(where: {
                    $0.id == proposalID && $0.isActionable
                })
            else {
                commandMessage = "이 제안은 더 이상 승인할 수 없습니다. 새로고침해 주세요."
                return
            }
            resolvingSceneProposalID = proposalID
            let proposalAction: ProposalAction =
                action.kind == .acceptProposal ? .accept : .decline
            let resolutionOperation = resolutionOperationGate.begin(
                pairing: sceneOperation.pairing,
                proposalID: proposalID
            )
            Task {
                await briefing.resolve(
                    proposal,
                    action: proposalAction,
                    pairing: sceneOperation.pairing
                )
                guard resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: proposalID
                ) else { return }
                resolvingSceneProposalID = nil
                guard
                    generatedSceneOperation == sceneOperation,
                    sceneOperationGate.isCurrent(
                        sceneOperation,
                        pairing: PairingStore.shared.load(),
                        proposalID: sceneOperation.proposalID
                    )
                else { return }
                await refreshAll()
            }
        case .openWebDetail:
            if let url = action.url {
                router.openDecision(url)
            }
        case .refresh:
            Task {
                generatedScene = nil
                await refreshAll()
            }
        case .switchLens:
            if let value = action.value, let target = WellnessLens(rawValue: value) {
                selectDetail(target)
            }
        case .modifyProposal, .createTask, .createGoal:
            commandMessage = "이 동작은 확인 가능한 기존 흐름에서만 실행됩니다."
        }
    }

    private func invalidateGeneratedScene() {
        sceneOperationGate.invalidate()
        generatedScene = nil
        generatedSceneOperation = nil
        isGeneratingScene = false
    }

    private func sceneQuery(for lens: WellnessLens) -> String {
        switch lens {
        case .now:
            return "현재 몸 상태가 오늘 수행 능력에 주는 영향을 보여줘"
        case .coordinate:
            return "이번 주 일정과 목표를 현재 가용 에너지 기준으로 보여줘"
        case .change:
            return "최근 상태와 결정 결과에서 확인 가능한 변화만 보여줘"
        }
    }

    private func statusPill(title: String, systemImage: String, color: Color) -> some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(color.opacity(0.1), in: Capsule())
    }

    private func insufficientData(_ message: String) -> some View {
        Label {
            Text(verbatim: message)
        } icon: {
            Image(systemName: "questionmark.diamond")
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
    }

    private func outcomeMetric(value: String, label: String) -> some View {
        VStack(spacing: 3) {
            Text(verbatim: value)
                .font(.system(.title2, design: .rounded).bold())
            Text(verbatim: label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private var energyStatus: String {
        guard let payload = briefing.snapshot?.payload else {
            return briefing.glanceError == nil ? "Health loading" : "Health unavailable"
        }
        let score = GlanceFormat.scoreText(payload.energy.score)
        return briefing.isStale ? "Energy \(score) · cached" : "Energy \(score)"
    }

    private var displayTimeZone: TimeZone {
        let identifier =
            generatedScene?.timezone
            ?? briefing.snapshot?.payload.timezone
            ?? TimeZone.autoupdatingCurrent.identifier
        return TimeZone(identifier: identifier) ?? .autoupdatingCurrent
    }

    private var calendarStatus: String {
        if plan.message?.contains("Calendar:") == true {
            return "Calendar needs attention"
        }
        if plan.events.isEmpty {
            return "연동 일정 없음"
        }
        return "캘린더 연동됨"
    }

    private func bodyPlanImpact(_ payload: GlancePayload) -> String {
        if let summary = payload.alerts.top?.summary {
            return summary
        }
        guard let score = payload.energy.score else {
            return "상태 데이터가 부족해 일정 영향을 판단하지 않습니다."
        }
        if score < 45 {
            return "회복을 먼저 보호하고 높은 에너지 일정은 승인 전에 다시 확인하세요."
        }
        if score < 70 {
            return "에너지를 집중 블록에 남기도록 저강도 일정을 주변에 배치하세요."
        }
        return "현재 capacity가 높은 구간을 핵심 목표에 우선 사용하세요."
    }

    private func taskDetail(_ task: TaskItem) -> String {
        var parts = ["에너지 \(task.energyDemand)"]
        if let minutes = task.estimatedMinutes {
            parts.append("약 \(minutes)분")
        }
        return parts.joined(separator: " · ")
    }

    private func confidenceLabel(_ confidence: GlanceConfidence) -> String {
        switch confidence {
        case .high: return "높음"
        case .medium: return "보통"
        case .low: return "낮음"
        }
    }

    private func calendarName(_ source: String) -> String {
        switch source.lowercased() {
        case "google":
            return "Google Calendar"
        case "caldav", "icloud":
            return "Apple Calendar"
        default:
            return source
        }
    }
}

private struct ProportionalDayTimeline: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private struct Entry: Identifiable {
        let id: String
        let title: String
        let startsAt: Date
        let endsAt: Date
        let provider: String
        let isProposal: Bool
        let isLocked: Bool
        let isAllDay: Bool
    }

    private struct PositionedEntry: Identifiable {
        let entry: Entry
        let lane: Int
        let laneCount: Int

        var id: String { entry.id }
    }

    let events: [CalendarEventItem]
    let decision: PendingDecision?
    let timeZone: TimeZone

    private let hourHeight: CGFloat = 54
    private let labelWidth: CGFloat = 46

    var body: some View {
        let entries = visibleEntries
        let allDayEntries = entries.filter(\.isAllDay)
        let timedEntries = entries.filter { !$0.isAllDay }
        let bounds = hourBounds(timedEntries)
        let positioned = positionedEntries(timedEntries)
        let maxLaneCount = positioned.map(\.laneCount).max() ?? 1
        let height = CGFloat(bounds.upperBound - bounds.lowerBound) * hourHeight

        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(verbatim: dayTitle(entries))
                    .font(.subheadline.weight(.semibold))
                Spacer()
                providerLegend(entries)
            }

            if entries.isEmpty {
                Label(
                    "오늘과 겹치는 일정 또는 변경 제안이 없습니다.",
                    systemImage: "calendar.badge.checkmark"
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            } else {
                if !allDayEntries.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("종일")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                        ForEach(allDayEntries) { entry in
                            timelineBlock(entry, availableHeight: 44)
                                .frame(minHeight: 44)
                        }
                    }
                }

                if !timedEntries.isEmpty,
                    dynamicTypeSize.isAccessibilitySize
                        || WellnessTimelinePolicy.shouldUseReadableList(
                            maxLaneCount: maxLaneCount
                        )
                {
                    accessibleTimeline(timedEntries)
                } else if !timedEntries.isEmpty {
                    GeometryReader { proxy in
                        let contentWidth = max(proxy.size.width - labelWidth - 8, 1)

                        ZStack(alignment: .topLeading) {
                            ForEach(bounds.lowerBound...bounds.upperBound, id: \.self) { hour in
                                let y = CGFloat(hour - bounds.lowerBound) * hourHeight
                                Text(verbatim: String(format: "%02d", hour))
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                                    .frame(width: labelWidth, alignment: .leading)
                                    .offset(y: y - 6)
                                    .accessibilityHidden(true)
                                Rectangle()
                                    .fill(Color.primary.opacity(0.07))
                                    .frame(width: contentWidth, height: 1)
                                    .offset(x: labelWidth, y: y)
                                    .accessibilityHidden(true)
                            }

                            ForEach(positioned) { positionedEntry in
                                let entry = positionedEntry.entry
                                let start = minuteOffset(
                                    entry.startsAt,
                                    fromHour: bounds.lowerBound
                                )
                                let duration = max(
                                    entry.endsAt.timeIntervalSince(entry.startsAt) / 60,
                                    15
                                )
                                let y = CGFloat(start / 60) * hourHeight
                                let blockHeight = max(
                                    CGFloat(duration / 60) * hourHeight,
                                    36
                                )
                                let laneWidth =
                                    contentWidth / CGFloat(positionedEntry.laneCount)
                                let x = labelWidth + CGFloat(positionedEntry.lane) * laneWidth

                                timelineBlock(entry, availableHeight: blockHeight)
                                    .frame(
                                        width: max(laneWidth - 6, 1),
                                        height: blockHeight,
                                        alignment: .topLeading
                                    )
                                    .offset(x: x + 3, y: y)
                            }
                        }
                    }
                    .frame(height: max(height, hourHeight * 3))
                    .accessibilityElement(children: .contain)
                }
            }
        }
    }

    private var visibleEntries: [Entry] {
        guard
            let day = WellnessTimelinePolicy.dayInterval(
                containing: Date(),
                timeZone: timeZone
            )
        else {
            return []
        }
        let dayStart = day.start
        let dayEnd = day.end
        var entries = events.filter {
            $0.endAt > dayStart && $0.startAt < dayEnd
        }.map {
            Entry(
                id: $0.id.uuidString,
                title: $0.summary ?? String(localized: "제목 없는 일정"),
                startsAt: max($0.startAt, dayStart),
                endsAt: min($0.endAt, dayEnd),
                provider: $0.calendarSource,
                isProposal: false,
                isLocked: $0.isLocked || !$0.isAgentCreated,
                isAllDay: $0.isAllDay
            )
        }
        if let decision,
            decision.proposal.proposedEnd > dayStart,
            decision.proposal.proposedStart < dayEnd
        {
            entries.append(
                Entry(
                    id: "proposal-\(decision.id.uuidString)",
                    title: decision.secondaryContextTitle ?? "HealthMes 일정 변경 제안",
                    startsAt: max(decision.proposal.proposedStart, dayStart),
                    endsAt: min(decision.proposal.proposedEnd, dayEnd),
                    provider: "healthmes",
                    isProposal: true,
                    isLocked: false,
                    isAllDay: false
                )
            )
        }
        return entries.sorted {
            $0.startsAt == $1.startsAt ? $0.endsAt < $1.endsAt : $0.startsAt < $1.startsAt
        }
    }

    private func hourBounds(_ entries: [Entry]) -> Range<Int> {
        WellnessTimelinePolicy.hourBounds(
            for: entries.map { DateInterval(start: $0.startsAt, end: $0.endsAt) },
            timeZone: timeZone
        )
    }

    private func positionedEntries(_ entries: [Entry]) -> [PositionedEntry] {
        let assignments = WellnessTimelinePolicy.laneAssignments(
            for: entries.map {
                DateInterval(start: $0.startsAt, end: $0.endsAt)
            }
        )
        return zip(entries, assignments).map { entry, assignment in
            PositionedEntry(
                entry: entry,
                lane: assignment.lane,
                laneCount: assignment.laneCount
            )
        }
    }

    private func minuteOffset(_ date: Date, fromHour: Int) -> Double {
        let components = timelineCalendar.dateComponents(
            [.hour, .minute],
            from: date
        )
        return Double(((components.hour ?? fromHour) - fromHour) * 60 + (components.minute ?? 0))
    }

    private func dayTitle(_ entries: [Entry]) -> String {
        guard let date = entries.first?.startsAt else {
            return String(localized: "오늘")
        }
        let formatter = DateFormatter()
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate("MMMEd")
        return formatter.string(from: date)
    }

    private func timelineBlock(_ entry: Entry, availableHeight: CGFloat) -> some View {
        let color = providerColor(entry.provider)
        let duration = entry.endsAt.timeIntervalSince(entry.startsAt) / 60
        return VStack(alignment: .leading, spacing: 2) {
            Text(verbatim: entry.title)
                .font(.caption.weight(.semibold))
                .lineLimit(availableHeight < 54 ? 1 : 2)
            if entry.isAllDay {
                Text("종일")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if duration >= 45 {
                Text(verbatim: timeRange(entry))
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
            }
            if availableHeight >= 72 {
                Spacer(minLength: 0)
                HStack(spacing: 3) {
                    if entry.isProposal {
                        Image(systemName: "wand.and.stars")
                    } else if entry.isLocked {
                        Image(systemName: "lock.fill")
                    }
                    Text(verbatim: providerName(entry.provider))
                        .lineLimit(1)
                }
                .font(.caption2.weight(.medium))
                .foregroundStyle(color)
            }
        }
        .padding(7)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(color.opacity(entry.isProposal ? 0.08 : 0.13))
        .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(
                    color.opacity(0.8),
                    style: StrokeStyle(
                        lineWidth: entry.isProposal ? 1.5 : 1,
                        dash: entry.isProposal ? [5, 4] : []
                    )
                )
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(verbatim: accessibilitySummary(entry)))
    }

    private func accessibleTimeline(_ entries: [Entry]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(entries) { entry in
                HStack(alignment: .top, spacing: 10) {
                    Text(verbatim: timeRange(entry))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                    Text(verbatim: entry.title)
                        .font(.body.weight(.semibold))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(10)
                .background(
                    providerColor(entry.provider).opacity(entry.isProposal ? 0.08 : 0.13),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(Text(verbatim: accessibilitySummary(entry)))
            }
        }
    }

    private var timelineCalendar: Calendar {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = timeZone
        return calendar
    }

    private func timeRange(_ entry: Entry) -> String {
        let formatter = DateFormatter()
        formatter.timeZone = timeZone
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return "\(formatter.string(from: entry.startsAt))–\(formatter.string(from: entry.endsAt))"
    }

    private func accessibilitySummary(_ entry: Entry) -> String {
        let source = providerName(entry.provider)
        let state = entry.isProposal
            ? String(localized: "승인 전 변경 제안")
            : entry.isLocked
                ? String(localized: "고정 일정")
                : String(localized: "조정 가능한 일정")
        return "\(entry.title), \(timeRange(entry)), \(source), \(state)"
    }

    @ViewBuilder
    private func providerLegend(_ entries: [Entry]) -> some View {
        HStack(spacing: 8) {
            ForEach(Array(Set(entries.map(\.provider))).sorted(), id: \.self) { provider in
                HStack(spacing: 3) {
                    Circle()
                        .fill(providerColor(provider))
                        .frame(width: 6, height: 6)
                    Text(verbatim: providerName(provider))
                }
            }
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    private func providerName(_ provider: String) -> String {
        switch provider.lowercased() {
        case "google": return "Google"
        case "caldav", "icloud": return "Apple"
        case "healthmes": return "제안"
        default: return provider
        }
    }

    private func providerColor(_ provider: String) -> Color {
        switch provider.lowercased() {
        case "google": return Color(red: 0.26, green: 0.52, blue: 0.96)
        case "caldav", "icloud": return Color(red: 0.08, green: 0.58, blue: 0.49)
        case "healthmes": return Color(red: 0.84, green: 0.45, blue: 0.12)
        default: return .secondary
        }
    }
}

struct HealthMesOnboardingView: View {
    @State private var showSelfHost = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Image(systemName: "bolt.heart.fill")
                        .font(.system(size: 44))
                        .foregroundStyle(Color(red: 0.08, green: 0.38, blue: 0.28))
                    Text("HealthMes 시작하기")
                        .font(.system(.largeTitle, design: .rounded).bold())
                    Text("한 번의 연결로 건강 상태, 캘린더 조율, 알림, Apple Watch를 같은 리모컨에서 사용합니다.")
                        .foregroundStyle(.secondary)

                    setupRow("계정과 안전한 인스턴스", detail: "관리형 HTTPS 연결은 아직 배포 준비가 필요합니다.", image: "person.crop.circle")
                    setupRow("건강 데이터", detail: "HealthKit 권한과 데이터 신선도를 확인합니다.", image: "heart.text.square")
                    setupRow("Google Calendar", detail: "Google 계정으로 HealthMes 서버에 연결합니다.", image: "g.circle")
                    setupRow("Apple Calendar", detail: "iPhone 권한과 iCloud 서버 동기화를 함께 확인합니다.", image: "calendar")
                    setupRow("알림과 Apple Watch", detail: "손목과 잠금화면에서 제안을 승인합니다.", image: "applewatch")

                    Button("관리형 HealthMes로 계속") {}
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(true)
                    Text("이 오픈소스 빌드에는 계정 provisioning과 App Store 배포가 아직 포함되지 않았습니다.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    DisclosureGroup("Advanced · self-host 연결", isExpanded: $showSelfHost) {
                        PairingView()
                            .padding(.top, 10)
                    }
                }
                .padding(22)
            }
            .navigationTitle(Text("HealthMes"))
        }
    }

    private func setupRow(_ title: String, detail: String, image: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: image)
                .font(.title3)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: title).font(.headline)
                Text(verbatim: detail)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
