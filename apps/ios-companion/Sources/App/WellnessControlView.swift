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
                    if lens != .now {
                        detailContextBar
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

            commandDock
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
        .onDisappear { command.reset() }
    }

    private var statusRail: some View {
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
            if !briefing.pendingDecisions.isEmpty {
                Text(verbatim: "\(briefing.pendingDecisions.count)")
                    .font(.caption.bold().monospacedDigit())
                    .padding(7)
                    .background(Color.orange.opacity(0.18), in: Circle())
                    .accessibilityLabel(Text("Pending decisions"))
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.thinMaterial)
        .accessibilityElement(children: .contain)
    }

    private var exploreMenu: some View {
        Menu {
            Button {
                selectDetail(.now)
            } label: {
                Label("현재 영향", systemImage: "bolt.heart")
            }
            .accessibilityAddTraits(lens == .now ? .isSelected : [])
            Button {
                selectDetail(.coordinate)
            } label: {
                Label("일정과 목표", systemImage: "calendar")
            }
            .accessibilityAddTraits(lens == .coordinate ? .isSelected : [])
            Button {
                selectDetail(.change)
            } label: {
                Label("결정 결과", systemImage: "chart.line.uptrend.xyaxis")
            }
            .accessibilityAddTraits(lens == .change ? .isSelected : [])

            Divider()

            if let pairing = PairingStore.shared.load() {
                Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["dashboard"])) {
                    Label("웹 대시보드", systemImage: "safari")
                }
            }
            Button {
                router.modal = .settings
            } label: {
                Label("설정", systemImage: "gearshape")
            }
        } label: {
            Label("전체 보기", systemImage: "square.grid.2x2")
                .font(.subheadline.weight(.semibold))
        }
        .accessibilityLabel(Text("전체 보기"))
        .accessibilityValue(Text(detailTitle(for: lens)))
    }

    private var detailContextBar: some View {
        HStack(spacing: 10) {
            Label(detailTitle(for: lens), systemImage: detailIcon(for: lens))
                .font(.subheadline.weight(.semibold))
            Spacer()
            Button("현재로 돌아가기") {
                selectDetail(.now)
            }
            .font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(moss.opacity(0.09), in: RoundedRectangle(cornerRadius: 14))
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var sceneContent: some View {
        switch lens {
        case .now:
            nowScene
        case .coordinate:
            coordinateScene
        case .change:
            changeScene
        }
    }

    private var nowScene: some View {
        Group {
            ProductCard(kicker: "몸 → 오늘 계획", systemImage: "bolt.heart.fill") {
                if let payload = briefing.snapshot?.payload {
                    HStack(alignment: .firstTextBaseline) {
                        Text("인지 에너지")
                            .font(.headline)
                        Spacer()
                        Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                            .font(.system(.title, design: .rounded).bold())
                    }
                    Text(verbatim: bodyPlanImpact(payload))
                        .font(.body.weight(.medium))
                    Text("업데이트 \(briefing.lastUpdatedText) · 신뢰도 \(confidenceLabel(payload.energy.confidence))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    insufficientData(briefing.glanceError ?? "Health data has not arrived yet.")
                }
            }

            nextBlockCard
            primaryDecisionCard
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

    private var nextBlockCard: some View {
        ProductCard(kicker: "다음 보호 일정", systemImage: "calendar.day.timeline.left") {
            if let block = briefing.snapshot?.payload.nextBlocks.first {
                Text(verbatim: block.title ?? String(localized: "Scheduled block"))
                    .font(.title3.weight(.semibold))
                Text(verbatim: "\(block.start.formatted(date: .omitted, time: .shortened))–\(block.end.formatted(date: .omitted, time: .shortened))")
                    .foregroundStyle(.secondary)
                if let demand = block.energyDemand {
                    Text(verbatim: "\(demand.rawValue.capitalized) energy demand")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.primary.opacity(0.07), in: Capsule())
                }
            } else {
                insufficientData("예정된 캘린더 일정이 없습니다.")
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
            if let decision = briefing.pendingDecisions.first {
                Text(verbatim: decision.prompt)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let reason = decision.reason {
                    Text(verbatim: reason)
                        .foregroundStyle(.secondary)
                }
                Text(verbatim: ProposalFormat.windowLine(decision.proposal))
                    .font(.footnote.weight(.medium))
                HStack(spacing: 10) {
                    Button {
                        Task {
                            await briefing.resolve(decision.proposal, action: .decline)
                            await refreshAll()
                        }
                    } label: {
                        Label("유지", systemImage: "xmark").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)

                    Button {
                        Task {
                            await briefing.resolve(decision.proposal, action: .accept)
                            await refreshAll()
                        }
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

    private var commandDock: some View {
        VStack(spacing: 8) {
            if command.isListening {
                HStack(spacing: 8) {
                    Circle().fill(.red).frame(width: 7, height: 7)
                    Text("Listening · edit the transcript before sending")
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
                        .background(command.isListening ? Color.red : Color.primary.opacity(0.07), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(Text(command.isListening ? "Stop listening" : "Speak command"))

                TextField(
                    "오늘 왜 피곤해? · 할 일: 라이브 QA",
                    text: $command.transcript,
                    axis: .vertical
                )
                .lineLimit(1...3)
                .focused($commandFocused)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 16))
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
                .disabled(command.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel(Text("Run command"))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
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
            commandMessage = "\(detailTitle(for: target))를 현재 리모컨에 펼쳤습니다."
            command.transcript = ""
        case .createTask(let title):
            preview = CommandPreview(kind: .task, title: title)
        case .createGoal(let title):
            preview = CommandPreview(kind: .goal, title: title)
        case .clarify:
            commandMessage = "HealthMes는 임의로 일정을 바꾸지 않습니다. 현재 몸 상태, 일정과 목표, 이전 결정 결과를 물어보거나 `할 일:` 또는 `주간 목표:`로 명확히 입력하세요."
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
        async let briefingRefresh: Void = briefing.refresh()
        async let planRefresh: Void = plan.refresh()
        async let decisionsRefresh: Void = decisions.refresh()
        _ = await (briefingRefresh, planRefresh, decisionsRefresh)
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
