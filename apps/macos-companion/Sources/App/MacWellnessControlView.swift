import SwiftUI

private enum MacCommandWriteKind {
    case task
    case goal

    var actionKind: WellnessActionKind {
        switch self {
        case .task: return .createTask
        case .goal: return .createGoal
        }
    }

    var confirmationTitle: String {
        switch self {
        case .task: return "Create this task?"
        case .goal: return "Create this weekly goal?"
        }
    }
}

private struct MacCommandPreview: Identifiable {
    let id = UUID()
    let kind: MacCommandWriteKind
    let title: String
}

private enum MacControlMessageTone {
    case neutral
    case success
    case caution
}

private struct MacControlMessage: Identifiable {
    let id = UUID()
    let text: String
    let tone: MacControlMessageTone
}

/// The macOS product surface is deliberately one shell:
/// status rail, perspective controls, a bounded scene renderer and a
/// persistent voice/text dock. It projects existing API state and never
/// invents a mutation that the current HealthMes API cannot perform.
struct MacWellnessControlView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let onSelect: (MacDetailContext) -> Void
    let onRefresh: (Bool) async -> Void
    let onSettings: () -> Void

    @EnvironmentObject private var router: MacAppRouter
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var speech = MacSpeechController()
    @State private var commandText = ""
    @State private var preview: MacCommandPreview?
    @State private var message: MacControlMessage?
    @State private var decisionOutcome: (proposalID: UUID, outcome: ProposalOutcome)?
    @State private var resolvingProposalID: UUID?
    @State private var lastHandledSpeakRequest = 0
    @State private var preserveMessageOnNextLensChange = false
    @FocusState private var commandFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            statusRail

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if router.lens != .now {
                        detailContextBar
                    }

                    if let preview {
                        commandPreview(preview)
                    }
                    if let message {
                        commandResult(message)
                    }

                    sceneModules
                }
                .frame(maxWidth: 1_080)
                .padding(.horizontal, 28)
                .padding(.top, 22)
                .padding(.bottom, 28)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.automatic)

            commandDock
        }
        .background(atmosphere)
        .onReceive(router.$speakRequest) { request in
            guard request > lastHandledSpeakRequest else { return }
            lastHandledSpeakRequest = request
            commandFocused = true
            Task { await speech.start() }
        }
        .onChange(of: speech.transcript) { _, transcript in
            guard !transcript.isEmpty else { return }
            commandText = transcript
            commandFocused = true
        }
        .onChange(of: router.lens) { oldLens, newLens in
            guard oldLens != newLens else { return }
            if preserveMessageOnNextLensChange {
                preserveMessageOnNextLensChange = false
            } else {
                message = nil
            }
        }
        .onDisappear {
            speech.reset()
        }
    }

    private var atmosphere: some View {
        ZStack {
            LinearGradient(
                colors: [
                    MacHealthMesStyle.canvas,
                    Color(red: 0.91, green: 0.94, blue: 0.89),
                    Color(red: 0.96, green: 0.93, blue: 0.86),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )

            Circle()
                .fill(MacHealthMesStyle.moss.opacity(0.09))
                .frame(width: 430, height: 430)
                .blur(radius: 20)
                .offset(x: -360, y: -260)

            Circle()
                .fill(MacHealthMesStyle.amber.opacity(0.08))
                .frame(width: 360, height: 360)
                .blur(radius: 18)
                .offset(x: 390, y: 250)
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }

    private var statusRail: some View {
        HStack(spacing: 10) {
            Label {
                Text("HealthMes")
                    .font(.system(.headline, design: .rounded).weight(.bold))
            } icon: {
                Image(systemName: "bolt.heart.fill")
                    .foregroundStyle(MacHealthMesStyle.moss)
            }

            Divider()
                .frame(height: 20)

            statusPill(
                energyStatus,
                systemImage: "waveform.path.ecg",
                tone: glanceStore.isStale ? MacHealthMesStyle.amber : MacHealthMesStyle.moss
            )
            statusPill(
                calendarStatus,
                systemImage: "calendar.badge.clock",
                tone: calendarTone
            )
            statusPill(
                decisionStatus,
                systemImage: "checkmark.bubble",
                tone: glanceStore.pendingProposals.isEmpty
                    ? MacHealthMesStyle.moss
                    : MacHealthMesStyle.amber
            )

            Spacer(minLength: 8)

            exploreMenu

            MacPrivacyPill(
                isPaired: glanceStore.isPaired,
                isStale: glanceStore.isStale
            )

            Button {
                Task { await onRefresh(true) }
            } label: {
                if glanceStore.isRefreshing || dashboardStore.isRefreshing {
                    ProgressView()
                        .controlSize(.small)
                        .frame(width: 18, height: 18)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .help("Refresh health, calendar and decision state")
            .disabled(glanceStore.isRefreshing || dashboardStore.isRefreshing)
            .accessibilityLabel(Text("Refresh HealthMes"))

        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(.regularMaterial)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(MacHealthMesStyle.line)
                .frame(height: 1)
        }
        .accessibilityElement(children: .contain)
    }

    private var exploreMenu: some View {
        Menu {
            Button("Current impact") {
                selectDetail(.now)
            }
            .accessibilityAddTraits(router.lens == .now ? .isSelected : [])
            Button("Calendar & goals") {
                selectDetail(.coordinate)
            }
            .accessibilityAddTraits(router.lens == .coordinate ? .isSelected : [])
            Button("Decision results") {
                selectDetail(.change)
            }
            .accessibilityAddTraits(router.lens == .change ? .isSelected : [])

            Divider()

            if let pairing = dashboardStore.pairing {
                Link("Open web dashboard", destination: MacWebLinks.dashboard(pairing: pairing))
            }
            Button("Settings", action: onSettings)
        } label: {
            Label("Explore", systemImage: "square.grid.2x2")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Open calendar, goal, outcome and connection details")
        .accessibilityLabel(Text("Explore HealthMes details"))
        .accessibilityValue(Text(detailTitle(router.lens)))
    }

    private var detailContextBar: some View {
        HStack(spacing: 10) {
            Label(detailTitle(router.lens), systemImage: detailIcon(router.lens))
                .font(.callout.weight(.semibold))
                .foregroundStyle(MacHealthMesStyle.graphite)
            Spacer()
            Text(freshnessText)
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            Button("Back to current impact") {
                selectDetail(.now)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(MacHealthMesStyle.moss.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
    }

    private var sceneModules: some View {
        let scene = projectedScene
        return VStack(spacing: 14) {
            ForEach(scene.modules) { module in
                moduleView(module, scene: scene)
                    .id("\(scene.id)-\(module.id)")
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: router.lens)
    }

    @ViewBuilder
    private func moduleView(_ module: WellnessSceneModule, scene: WellnessScene) -> some View {
        switch module.kind {
        case .healthState:
            healthStateModule(module)
        case .decision:
            decisionModule(module, scene: scene)
        case .scheduleTimeline:
            scheduleModule(module)
        case .constraints:
            constraintsModule(module)
        case .outcomeCurve, .goalImpact:
            outcomeModule(module)
        case .calendarSync:
            calendarSyncModule(module)
        case .planImpact, .alternatives, .capacityMap, .clarification, .fallback:
            standardModule(module)
        }
    }

    private func healthStateModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.moss) {
            HStack(alignment: .center, spacing: 24) {
                if let payload = glanceStore.payload {
                    Button {
                        onSelect(.energy(payload))
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                                .font(.system(size: 52, weight: .bold, design: .rounded))
                                .foregroundStyle(MacHealthMesStyle.graphite)
                            Text("cognitive energy")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(
                        Text("Open energy details. \(GlanceFormat.scoreText(payload.energy.score))")
                    )

                    Divider()
                        .frame(height: 58)

                    MacEnergyCurveView(
                        curve: payload.energy.curve24h,
                        currentHour: currentHour(timezone: payload.timezone)
                    )
                    .frame(maxWidth: .infinity, minHeight: 78, maxHeight: 86)
                } else {
                    missingData("Health state has not arrived from the paired instance.")
                }
            }

            itemGrid(module.items.filter { $0.id != "energy" })
        }
    }

    private func decisionModule(
        _ module: WellnessSceneModule,
        scene: WellnessScene
    ) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.amber) {
            itemGrid(module.items)

            if let proposalID = scene.actions.first(where: {
                $0.kind == .acceptProposal || $0.kind == .declineProposal
            })?.proposalID,
                let proposal = glanceStore.pendingProposals.first(where: { $0.id == proposalID })
            {
                if let decisionOutcome, decisionOutcome.proposalID == proposalID {
                    proposalOutcome(decisionOutcome.outcome)
                } else {
                    HStack(spacing: 10) {
                        Button {
                            Task { await resolve(proposal, action: .decline) }
                        } label: {
                            Label("Keep", systemImage: "xmark")
                                .frame(minWidth: 84)
                        }
                        .buttonStyle(.bordered)

                        Button {
                            Task { await resolve(proposal, action: .accept) }
                        } label: {
                            Label("Approve change", systemImage: "checkmark")
                                .frame(minWidth: 84)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(MacHealthMesStyle.moss)

                        if let alert = matchingAlert(for: proposal) {
                            Button("Why?") {
                                onSelect(.proposal(proposal, alert: alert))
                            }
                            .buttonStyle(.link)
                        }

                        Spacer()

                        if let action = scene.actions.first(where: {
                            $0.kind == .openWebDetail
                        }), let url = action.url {
                            Link(destination: url) {
                                Label("Full decision path", systemImage: "arrow.up.right.square")
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                    .controlSize(.large)
                    .disabled(resolvingProposalID != nil)
                }
            } else if glanceStore.pendingProposals.isEmpty {
                Label("No evidence-backed action needs approval.", systemImage: "checkmark.seal")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(MacHealthMesStyle.moss)
            }
        }
    }

    private func scheduleModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.graphite) {
            if module.items.isEmpty {
                missingData("No mirrored calendar events are available.")
            } else {
                ForEach(module.items) { item in
                    moduleItem(item, compact: false)
                    if item.id != module.items.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func constraintsModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.moss) {
            if module.items.isEmpty {
                missingData("No weekly goal is available as a protected constraint.")
            } else {
                HStack(spacing: 8) {
                    ForEach(module.items) { item in
                        Button {
                            if let detail = detailContext(for: item.id) {
                                onSelect(detail)
                            }
                        } label: {
                            Label {
                                Text(verbatim: item.value)
                                    .lineLimit(2)
                            } icon: {
                                Image(systemName: item.label == "Goal" ? "scope" : "bolt")
                            }
                            .font(.callout.weight(.medium))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 9)
                            .background(MacHealthMesStyle.moss.opacity(0.09), in: Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private func outcomeModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.moss) {
            if module.items.isEmpty {
                missingData("No earlier decisions are available for comparison.")
            } else if module.kind == .outcomeCurve {
                HStack(spacing: 0) {
                    ForEach(module.items) { item in
                        Button {
                            if let detail = detailContext(for: item.id) {
                                onSelect(detail)
                            }
                        } label: {
                            VStack(spacing: 4) {
                                Text(verbatim: item.value)
                                    .font(.system(.title, design: .rounded).bold())
                                Text(verbatim: item.label)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.plain)
                        if item.id != module.items.last?.id {
                            Divider()
                                .frame(height: 46)
                        }
                    }
                }
            } else {
                ForEach(module.items) { item in
                    moduleItem(item, compact: true)
                    if item.id != module.items.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func calendarSyncModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: MacHealthMesStyle.amber) {
            itemGrid(module.items)
            if let pairing = dashboardStore.pairing {
                HStack {
                    Link(destination: MacWebLinks.connections(pairing: pairing)) {
                        Label("Inspect calendar setup", systemImage: "arrow.up.right.square")
                    }
                    .buttonStyle(.bordered)
                    Text("Credentials, mirrored data and calendar-applied results are separate states.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func standardModule(_ module: WellnessSceneModule) -> some View {
        MacWellnessModuleCard(module: module, accent: moduleAccent(module.kind)) {
            if module.items.isEmpty {
                if module.kind == .fallback, !glanceStore.isPaired {
                    Button("Open Settings to connect") {
                        onSettings()
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.moss)
                    .controlSize(.large)
                } else {
                    missingData(module.summary)
                }
            } else {
                itemGrid(module.items)
            }
        }
    }

    private func itemGrid(_ items: [WellnessSceneItem]) -> some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 210), spacing: 10)],
            alignment: .leading,
            spacing: 10
        ) {
            ForEach(items) { item in
                moduleItem(item, compact: true)
            }
        }
    }

    private func moduleItem(_ item: WellnessSceneItem, compact: Bool) -> some View {
        Button {
            if let detail = detailContext(for: item.id) {
                onSelect(detail)
            }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(verbatim: item.label)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .textCase(.uppercase)
                    Text(verbatim: item.value)
                        .font(compact ? .body.weight(.medium) : .title3.weight(.semibold))
                        .foregroundStyle(MacHealthMesStyle.graphite)
                        .fixedSize(horizontal: false, vertical: true)
                    if let detail = item.detail {
                        Text(verbatim: detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                if detailContext(for: item.id) != nil {
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .padding(.top, 4)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.42), in: RoundedRectangle(cornerRadius: 13))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(detailContext(for: item.id) == nil)
    }

    private var commandDock: some View {
        VStack(spacing: 7) {
            if speech.isListening {
                HStack(spacing: 8) {
                    Circle()
                        .fill(.red)
                        .frame(width: 7, height: 7)
                    Text("Listening on device. Edit the transcript before running it.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            } else if case .failed(let failure) = speech.phase {
                HStack {
                    Label(failure, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            } else if speech.phase == .denied {
                HStack {
                    Label(
                        "Microphone or speech permission is off.",
                        systemImage: "mic.slash"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    Spacer()
                }
            }

            HStack(alignment: .bottom, spacing: 10) {
                Button {
                    Task { await speech.toggle() }
                } label: {
                    Image(systemName: speech.isListening ? "stop.fill" : "waveform")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(speech.isListening ? .white : MacHealthMesStyle.moss)
                        .frame(width: 42, height: 42)
                        .background(
                            speech.isListening ? Color.red : Color.white.opacity(0.58),
                            in: Circle()
                        )
                }
                .buttonStyle(.plain)
                .keyboardShortcut(" ", modifiers: [.command, .shift])
                .accessibilityLabel(Text(speech.isListening ? "Stop listening" : "Speak command"))

                TextField(
                    "오늘 왜 피곤해? · 할 일: 라이브 QA",
                    text: $commandText,
                    axis: .vertical
                )
                .textFieldStyle(.plain)
                .font(.body)
                .lineLimit(1...3)
                .focused($commandFocused)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Color.white.opacity(0.64), in: RoundedRectangle(cornerRadius: 16))
                .overlay {
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(MacHealthMesStyle.line)
                }
                .onSubmit {
                    submitCommand()
                }

                Button {
                    submitCommand()
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.body.bold())
                        .foregroundStyle(.white)
                        .frame(width: 42, height: 42)
                        .background(MacHealthMesStyle.graphite, in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(commandText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel(Text("Run command"))
            }

            HStack {
                Text("No chat history. Commands become a scene, a preview, or a safe refusal.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
                Text("Task and goal writes always require confirmation.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(.regularMaterial)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(MacHealthMesStyle.line)
                .frame(height: 1)
        }
    }

    private func commandPreview(_ preview: MacCommandPreview) -> some View {
        MacWellnessModuleCard(
            module: WellnessSceneModule(
                id: "command-preview",
                kind: .clarification,
                title: "Confirm before writing",
                summary: preview.kind.confirmationTitle
            ),
            accent: MacHealthMesStyle.amber
        ) {
            Text(verbatim: preview.title)
                .font(.system(.title2, design: .rounded).weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)

            Text(
                "This writes one item through the existing HealthMes API. It does not move a calendar event or infer missing details."
            )
            .font(.callout)
            .foregroundStyle(.secondary)

            HStack {
                Button("Cancel") {
                    self.preview = nil
                }
                .buttonStyle(.bordered)

                Spacer()

                Button {
                    Task { await confirm(preview) }
                } label: {
                    if dashboardStore.isSavingPlanItem {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Label("Confirm", systemImage: "checkmark.shield")
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .disabled(dashboardStore.isSavingPlanItem)
            }
        }
    }

    private func commandResult(_ message: MacControlMessage) -> some View {
        let tone = messageColor(message.tone)
        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: messageIcon(message.tone))
                .foregroundStyle(tone)
            Text(verbatim: message.text)
                .font(.callout.weight(.medium))
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button {
                self.message = nil
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel(Text("Dismiss command result"))
        }
        .padding(14)
        .background(tone.opacity(0.09), in: RoundedRectangle(cornerRadius: 14))
        .overlay {
            RoundedRectangle(cornerRadius: 14)
                .stroke(tone.opacity(0.18))
        }
    }

    private func submitCommand() {
        if speech.isListening {
            speech.stop()
        }

        let input = commandText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty else { return }

        preview = nil
        message = nil

        if handleLocalCommand(input) {
            return
        }

        guard let intent = WellnessCommandParser.parse(input) else { return }
        handle(intent)
    }

    private func handleLocalCommand(_ input: String) -> Bool {
        guard let intent = MacVoiceIntentParser.parse(input) else { return false }
        switch intent {
        case .showToday:
            handle(.show(.now))
        case .showPlan:
            handle(.show(.coordinate))
        case .showDecisions:
            handle(.show(.change))
        case .showSettings:
            onSettings()
            commandText = ""
        case .refresh:
            commandText = ""
            Task {
                await onRefresh(true)
                message = MacControlMessage(
                    text: "Health, calendar and decision state refreshed.",
                    tone: .success
                )
            }
        case .taskDraft(let title):
            handle(.createTask(title))
        case .goalDraft(let title):
            handle(.createGoal(title))
        }
        return true
    }

    private func handle(_ intent: WellnessCommandIntent) {
        switch intent {
        case .show(let lens):
            selectDetail(lens)
            commandText = ""
            message = MacControlMessage(
                text: "\(detailTitle(lens)) is now open on the current control surface.",
                tone: .neutral
            )
        case .createTask(let title):
            presentPreview(kind: .task, title: title)
        case .createGoal(let title):
            presentPreview(kind: .goal, title: title)
        case .clarify:
            message = MacControlMessage(
                text:
                    "HealthMes did not execute that command. Ask about current condition, calendar and goals, or prior decision results. Use an explicit `할 일:` / `주간 목표:` prefix for writes. Calendar moves require an existing proposal.",
                tone: .caution
            )
        }
    }

    private func presentPreview(kind: MacCommandWriteKind, title: String) {
        guard glanceStore.isPaired else {
            message = MacControlMessage(
                text: "Connect a HealthMes instance in Settings before creating plan items.",
                tone: .caution
            )
            return
        }

        let action = WellnessSceneAction(
            id: "preview-\(kind.actionKind.rawValue)",
            kind: kind.actionKind,
            label: kind.confirmationTitle,
            value: title
        )
        let scene = WellnessScene(
            id: "command-preview",
            lens: router.lens,
            title: kind.confirmationTitle,
            summary: title,
            severity: .action,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "preview",
                    kind: .clarification,
                    title: "Confirm before writing",
                    summary: title
                )
            ],
            actions: [action]
        )

        do {
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: dashboardStore.pairing?.baseURL
            )
            preview = MacCommandPreview(kind: kind, title: title)
        } catch {
            message = MacControlMessage(
                text: "The command preview failed validation and nothing was written.",
                tone: .caution
            )
        }
    }

    private func confirm(_ preview: MacCommandPreview) async {
        let succeeded: Bool
        switch preview.kind {
        case .task:
            succeeded = await dashboardStore.createTask(title: preview.title)
        case .goal:
            succeeded = await dashboardStore.createGoal(title: preview.title)
        }

        let result = dashboardStore.planSaveMessage
            ?? (succeeded ? "Saved to HealthMes." : "HealthMes could not save this item.")
        message = MacControlMessage(
            text: result,
            tone: succeeded ? .success : .caution
        )
        self.preview = nil

        if succeeded {
            commandText = ""
            speech.reset()
            selectDetail(.coordinate, clearMessage: false)
        }
    }

    private func selectDetail(_ lens: WellnessLens, clearMessage: Bool = true) {
        if clearMessage {
            message = nil
            preserveMessageOnNextLensChange = false
        } else if router.lens != lens {
            preserveMessageOnNextLensChange = true
        }
        if reduceMotion {
            router.selectLens(lens)
        } else {
            withAnimation(.easeOut(duration: 0.18)) {
                router.selectLens(lens)
            }
        }
    }

    private func detailTitle(_ lens: WellnessLens) -> String {
        switch lens {
        case .now: return "Current health impact"
        case .coordinate: return "Calendar & goals"
        case .change: return "Decision results"
        }
    }

    private func detailIcon(_ lens: WellnessLens) -> String {
        switch lens {
        case .now: return "bolt.heart"
        case .coordinate: return "calendar"
        case .change: return "chart.line.uptrend.xyaxis"
        }
    }

    private func resolve(_ proposal: ProposalItem, action: ProposalAction) async {
        guard resolvingProposalID == nil else { return }
        resolvingProposalID = proposal.id
        defer { resolvingProposalID = nil }

        let outcome = await glanceStore.resolve(proposal, action: action)
        decisionOutcome = (proposal.id, outcome)
        await dashboardStore.refresh()
        message = MacControlMessage(
            text: proposalOutcomeText(outcome),
            tone: proposalOutcomeTone(outcome)
        )
    }

    @ViewBuilder
    private func proposalOutcome(_ outcome: ProposalOutcome) -> some View {
        Label {
            Text(verbatim: proposalOutcomeText(outcome))
        } icon: {
            Image(systemName: proposalOutcomeIcon(outcome))
        }
        .font(.callout.weight(.semibold))
        .foregroundStyle(messageColor(proposalOutcomeTone(outcome)))
    }

    private var projectedScene: WellnessScene {
        MacWellnessSceneProjector.makeScene(
            lens: router.lens,
            isPaired: glanceStore.isPaired,
            payload: glanceStore.payload,
            isStale: glanceStore.isStale,
            errorKey: glanceStore.errorKey,
            lastFetched: glanceStore.lastFetched,
            alerts: glanceStore.alerts,
            proposals: glanceStore.pendingProposals,
            goals: dashboardStore.goals,
            tasks: dashboardStore.tasks,
            events: dashboardStore.events,
            decisions: dashboardStore.decisions,
            report: dashboardStore.weeklyReport,
            dashboardErrors: dashboardStore.errorMessages,
            pairing: dashboardStore.pairing
        )
    }

    private func detailContext(for itemID: String) -> MacDetailContext? {
        if itemID == "energy", let payload = glanceStore.payload {
            return .energy(payload)
        }
        if itemID == "weekly-report", let report = dashboardStore.weeklyReport {
            return .report(report)
        }
        if itemID.hasPrefix("block-"),
            let timestamp = TimeInterval(itemID.dropFirst("block-".count)),
            let payload = glanceStore.payload,
            let block = payload.nextBlocks.first(where: {
                Int($0.start.timeIntervalSince1970) == Int(timestamp)
            })
        {
            return .block(block, timezone: payload.timezone)
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "proposal-", with: "")),
            let proposal = glanceStore.pendingProposals.first(where: { $0.id == id })
        {
            return .proposal(proposal, alert: matchingAlert(for: proposal))
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "alert-", with: "")),
            let alert = glanceStore.alerts.first(where: { $0.id == id })
        {
            return .alert(alert)
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "goal-", with: "")),
            let goal = dashboardStore.goals.first(where: { $0.id == id })
        {
            return .goal(goal)
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "task-", with: "")),
            let task = dashboardStore.tasks.first(where: { $0.id == id })
        {
            return .task(task)
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "event-", with: "")),
            let event = dashboardStore.events.first(where: { $0.id == id })
        {
            return .event(event)
        }
        if let id = UUID(uuidString: itemID.replacingOccurrences(of: "decision-", with: "")),
            let decision = dashboardStore.decisions.first(where: { $0.id == id })
        {
            return .decision(decision)
        }
        return nil
    }

    private func matchingAlert(for proposal: ProposalItem) -> AlertItem? {
        glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
    }

    private var energyStatus: String {
        guard glanceStore.isPaired else { return "Health not connected" }
        guard let payload = glanceStore.payload else {
            return glanceStore.errorKey == nil ? "Health loading" : "Health unavailable"
        }
        let score = GlanceFormat.scoreText(payload.energy.score)
        return glanceStore.isStale ? "Energy \(score) · cached" : "Energy \(score)"
    }

    private var calendarStatus: String {
        guard glanceStore.isPaired else { return "Calendar unavailable" }
        if dashboardStore.errorMessages.contains(where: { $0.hasPrefix("Calendar:") }) {
            return "Calendar needs attention"
        }
        if dashboardStore.events.isEmpty {
            return "No mirrored events"
        }
        return "\(dashboardStore.events.count) mirrored events"
    }

    private var calendarTone: Color {
        if dashboardStore.errorMessages.contains(where: { $0.hasPrefix("Calendar:") }) {
            return MacHealthMesStyle.amber
        }
        return dashboardStore.events.isEmpty ? .secondary : MacHealthMesStyle.moss
    }

    private var decisionStatus: String {
        let count = glanceStore.pendingProposals.count
        return count == 0 ? "No pending action" : "\(count) waiting"
    }

    private var freshnessText: String {
        guard let date = maxDate(glanceStore.lastFetched, dashboardStore.lastUpdated) else {
            return glanceStore.isPaired ? "Waiting for first refresh" : "Not connected"
        }
        return "Updated \(date.formatted(date: .omitted, time: .shortened))"
    }

    private var lensEyebrow: String {
        switch router.lens {
        case .now: return "HEALTH → PLAN"
        case .coordinate: return "DETAIL · CALENDAR & GOALS"
        case .change: return "DETAIL · DECISION RESULTS"
        }
    }

    private func maxDate(_ lhs: Date?, _ rhs: Date?) -> Date? {
        switch (lhs, rhs) {
        case (.some(let lhs), .some(let rhs)): return max(lhs, rhs)
        case (.some(let lhs), .none): return lhs
        case (.none, .some(let rhs)): return rhs
        case (.none, .none): return nil
        }
    }

    private func currentHour(timezone: String) -> Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }

    private func statusPill(
        _ title: String,
        systemImage: String,
        tone: Color
    ) -> some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.semibold))
            .foregroundStyle(tone)
            .lineLimit(1)
            .minimumScaleFactor(0.75)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(tone.opacity(0.09), in: Capsule())
    }

    private func missingData(_ text: String) -> some View {
        Label {
            Text(verbatim: text)
        } icon: {
            Image(systemName: "questionmark.diamond")
        }
        .font(.callout)
        .foregroundStyle(.secondary)
    }

    private func moduleAccent(_ kind: WellnessModuleKind) -> Color {
        switch kind {
        case .decision, .calendarSync, .clarification:
            return MacHealthMesStyle.amber
        case .healthState, .constraints, .capacityMap, .outcomeCurve, .goalImpact:
            return MacHealthMesStyle.moss
        case .planImpact, .scheduleTimeline, .alternatives, .fallback:
            return MacHealthMesStyle.graphite
        }
    }

    private func severityColor(_ severity: WellnessSceneSeverity) -> Color {
        switch severity {
        case .neutral: return .secondary
        case .supportive: return MacHealthMesStyle.moss
        case .caution, .action: return MacHealthMesStyle.amber
        }
    }

    private func messageColor(_ tone: MacControlMessageTone) -> Color {
        switch tone {
        case .neutral: return MacHealthMesStyle.graphite
        case .success: return MacHealthMesStyle.moss
        case .caution: return MacHealthMesStyle.amber
        }
    }

    private func messageIcon(_ tone: MacControlMessageTone) -> String {
        switch tone {
        case .neutral: return "slider.horizontal.3"
        case .success: return "checkmark.circle.fill"
        case .caution: return "exclamationmark.triangle.fill"
        }
    }

    private func proposalOutcomeText(_ outcome: ProposalOutcome) -> String {
        switch outcome {
        case .accepted:
            return "Approval recorded. Calendar sync is still pending."
        case .applied:
            return "Applied to the external calendar."
        case .kept:
            return "Declined. The calendar was kept as-is."
        case .expired:
            return "The proposal expired. The calendar was not changed."
        case .alreadyResolved(let status):
            return "This proposal was already resolved as \(status)."
        case .failed:
            return "HealthMes could not resolve the proposal. Nothing new was claimed."
        }
    }

    private func proposalOutcomeTone(_ outcome: ProposalOutcome) -> MacControlMessageTone {
        switch outcome {
        case .applied, .kept:
            return .success
        case .accepted, .expired, .alreadyResolved, .failed:
            return .caution
        }
    }

    private func proposalOutcomeIcon(_ outcome: ProposalOutcome) -> String {
        switch outcome {
        case .accepted: return "clock.badge.checkmark"
        case .applied: return "checkmark.circle.fill"
        case .kept: return "minus.circle.fill"
        case .expired: return "clock.badge.xmark"
        case .alreadyResolved: return "checkmark.circle"
        case .failed: return "exclamationmark.triangle.fill"
        }
    }
}

private struct MacWellnessModuleCard<Content: View>: View {
    let module: WellnessSceneModule
    let accent: Color
    @ViewBuilder let content: Content

    init(
        module: WellnessSceneModule,
        accent: Color,
        @ViewBuilder content: () -> Content
    ) {
        self.module = module
        self.accent = accent
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: moduleIcon)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(accent)
                    .frame(width: 26)

                VStack(alignment: .leading, spacing: 4) {
                    Text(verbatim: module.title)
                        .font(.headline)
                        .foregroundStyle(MacHealthMesStyle.graphite)
                    Text(verbatim: module.summary)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            content
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 3)
                .fill(accent.opacity(0.72))
                .frame(width: 4)
                .padding(.vertical, 18)
                .offset(x: 1)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .stroke(MacHealthMesStyle.line)
        }
        .shadow(color: .black.opacity(0.035), radius: 18, y: 8)
        .accessibilityElement(children: .contain)
    }

    private var moduleIcon: String {
        switch module.kind {
        case .healthState: return "bolt.heart.fill"
        case .planImpact: return "arrow.triangle.branch"
        case .scheduleTimeline: return "calendar.day.timeline.left"
        case .alternatives: return "arrow.triangle.swap"
        case .constraints: return "shield.lefthalf.filled"
        case .capacityMap: return "chart.xyaxis.line"
        case .outcomeCurve: return "chart.line.uptrend.xyaxis"
        case .goalImpact: return "scope"
        case .clarification: return "checkmark.shield"
        case .decision: return "wand.and.stars"
        case .calendarSync: return "calendar.badge.clock"
        case .fallback: return "questionmark.diamond"
        }
    }
}

/// Deterministic presentation adapter. This is intentionally not a planner:
/// it only projects already-returned HealthMes data into the trusted scene
/// catalog and validates every action before the renderer sees it.
private enum MacWellnessSceneProjector {
    static func makeScene(
        lens: WellnessLens,
        isPaired: Bool,
        payload: GlancePayload?,
        isStale: Bool,
        errorKey: String?,
        lastFetched: Date?,
        alerts: [AlertItem],
        proposals: [ProposalItem],
        goals: [WeeklyGoalItem],
        tasks: [TaskItem],
        events: [CalendarEventItem],
        decisions: [MacDecisionSummary],
        report: WeeklyReport?,
        dashboardErrors: [String],
        pairing: Pairing?
    ) -> WellnessScene {
        guard isPaired else {
            return WellnessScene.fallback(
                lens: lens,
                title: "Connect one private HealthMes instance",
                reason:
                    "The Mac shell is ready, but health, calendar and decision data remain unavailable until Settings completes a connection.",
                freshness: .insufficientData
            )
        }

        let freshness = freshness(
            payload: payload,
            isStale: isStale,
            errorKey: errorKey
        )
        let scene: WellnessScene
        switch lens {
        case .now:
            scene = nowScene(
                payload: payload,
                freshness: freshness,
                lastFetched: lastFetched,
                alerts: alerts,
                proposals: proposals,
                pairing: pairing
            )
        case .coordinate:
            scene = coordinateScene(
                payload: payload,
                freshness: freshness,
                alerts: alerts,
                proposals: proposals,
                goals: goals,
                tasks: tasks,
                events: events,
                dashboardErrors: dashboardErrors,
                pairing: pairing
            )
        case .change:
            scene = changeScene(
                freshness: freshness,
                decisions: decisions,
                report: report,
                dashboardErrors: dashboardErrors,
                pairing: pairing
            )
        }

        do {
            try WellnessSceneValidator.validate(scene, pairedBaseURL: pairing?.baseURL)
            return scene
        } catch {
            return WellnessScene.fallback(
                lens: lens,
                title: "HealthMes hid an unsafe scene",
                reason:
                    "The projected UI did not pass the trusted action contract. No command or calendar action was exposed.",
                freshness: freshness
            )
        }
    }

    private static func nowScene(
        payload: GlancePayload?,
        freshness: WellnessFreshness,
        lastFetched: Date?,
        alerts: [AlertItem],
        proposals: [ProposalItem],
        pairing: Pairing?
    ) -> WellnessScene {
        let pending = proposals.first
        let alert = pending.flatMap { proposal in
            alerts.first(where: { $0.proposalId == proposal.id })
        }
        let title = alert?.decisionCard?.observationShort
            ?? payload?.alerts.top?.summary
            ?? "How your body should change today's plan"
        let summary = bodyPlanImpact(payload)

        var modules: [WellnessSceneModule] = [
            healthModule(payload: payload, lastFetched: lastFetched, summary: summary),
            nextImpactModule(payload: payload),
        ]
        modules.append(
            decisionModule(
                proposal: pending,
                alert: alert,
                pairing: pairing
            )
        )

        return WellnessScene(
            id: "mac-now",
            lens: .now,
            title: title,
            summary: summary,
            severity: pending == nil ? .supportive : .action,
            freshness: freshness,
            modules: modules,
            actions: decisionActions(
                proposal: pending,
                alert: alert,
                pairing: pairing
            )
        )
    }

    private static func coordinateScene(
        payload: GlancePayload?,
        freshness: WellnessFreshness,
        alerts: [AlertItem],
        proposals: [ProposalItem],
        goals: [WeeklyGoalItem],
        tasks: [TaskItem],
        events: [CalendarEventItem],
        dashboardErrors: [String],
        pairing: Pairing?
    ) -> WellnessScene {
        let pending = proposals.first
        let alert = pending.flatMap { proposal in
            alerts.first(where: { $0.proposalId == proposal.id })
        }
        let title = alert.flatMap(ProposalActionPresentation.exactPrompt)
            ?? "Coordinate capacity, goals and calendar constraints"
        let summary =
            "HealthMes shows one existing intervention, what it protects and the mirrored schedule around it."

        let constraintItems = Array(goals.prefix(3)).map {
            WellnessSceneItem(
                id: "goal-\($0.id.uuidString)",
                label: "Goal",
                value: $0.title,
                detail: "Priority \($0.priority)"
            )
        } + Array(tasks.filter(\.isOpen).prefix(2)).map {
            WellnessSceneItem(
                id: "task-\($0.id.uuidString)",
                label: "Open task",
                value: $0.title,
                detail: "\($0.energyDemand.capitalized) energy"
            )
        }

        let eventItems = Array(events.prefix(8)).map {
            WellnessSceneItem(
                id: "event-\($0.id.uuidString)",
                label: $0.startAt.healthMesShortTime,
                value: $0.summary ?? "Untitled event",
                detail:
                    "\($0.endAt.healthMesShortTime) · \($0.isAgentCreated ? "HealthMes-managed" : $0.calendarSource)"
            )
        }

        let modules = [
            decisionModule(proposal: pending, alert: alert, pairing: pairing),
            WellnessSceneModule(
                id: "constraints",
                kind: .constraints,
                title: "Protected constraints",
                summary:
                    "These goals and open tasks are context, not permission to rewrite the calendar.",
                items: constraintItems
            ),
            WellnessSceneModule(
                id: "schedule",
                kind: .scheduleTimeline,
                title: "Mirrored schedule",
                summary:
                    "This is what HealthMes can currently see. An approved proposal is not applied until its status becomes pushed.",
                items: eventItems
            ),
            calendarModule(
                events: events,
                dashboardErrors: dashboardErrors,
                report: nil
            ),
        ]

        return WellnessScene(
            id: "mac-coordinate",
            lens: .coordinate,
            title: title,
            summary: summary,
            severity: pending == nil ? .neutral : .action,
            freshness: freshness,
            modules: modules,
            actions: decisionActions(
                proposal: pending,
                alert: alert,
                pairing: pairing
            )
        )
    }

    private static func changeScene(
        freshness: WellnessFreshness,
        decisions: [MacDecisionSummary],
        report: WeeklyReport?,
        dashboardErrors: [String],
        pairing: Pairing?
    ) -> WellnessScene {
        let breakdown = report?.schedule.displayBreakdown
        let metrics: [WellnessSceneItem]
        if let report, let breakdown {
            metrics = [
                WellnessSceneItem(
                    id: "weekly-decisions",
                    label: "Decisions",
                    value: "\(report.decisions.count)"
                ),
                WellnessSceneItem(
                    id: "weekly-sync-pending",
                    label: "Sync pending",
                    value: "\(breakdown.syncPending)"
                ),
                WellnessSceneItem(
                    id: "weekly-applied",
                    label: "Calendar applied",
                    value: "\(breakdown.applied)"
                ),
                WellnessSceneItem(
                    id: "weekly-acceptance",
                    label: "Plan acceptance",
                    value: report.schedule.acceptancePct.map { "\($0)%" } ?? "—"
                ),
            ]
        } else {
            metrics = []
        }

        let history = Array(decisions.prefix(8)).map {
            WellnessSceneItem(
                id: "decision-\($0.id.uuidString)",
                label: $0.createdAt.healthMesShortDateTime,
                value: $0.summary,
                detail: $0.kind.rawValue.replacingOccurrences(of: "_", with: " ")
            )
        }

        var actions: [WellnessSceneAction] = []
        if let pairing {
            actions.append(
                WellnessSceneAction(
                    id: "open-dashboard",
                    kind: .openWebDetail,
                    label: "Open web dashboard",
                    url: MacWebLinks.dashboard(pairing: pairing)
                )
            )
        }

        return WellnessScene(
            id: "mac-change",
            lens: .change,
            title: "Did earlier decisions actually help?",
            summary:
                "HealthMes separates approval, calendar application and later outcomes so activity is not mistaken for improvement.",
            severity: .supportive,
            freshness: freshness,
            modules: [
                WellnessSceneModule(
                    id: "outcome-loop",
                    kind: .outcomeCurve,
                    title: "Decision → execution",
                    summary:
                        "Accepted means approved but waiting. Pushed means the external calendar was updated.",
                    items: metrics
                ),
                WellnessSceneModule(
                    id: "decision-history",
                    kind: .goalImpact,
                    title: "Recent decision evidence",
                    summary:
                        "Open a record to inspect why it happened. Long-term health and productivity attribution remains insufficient until those outcomes are recorded.",
                    items: history
                ),
                calendarModule(
                    events: [],
                    dashboardErrors: dashboardErrors,
                    report: report
                ),
            ],
            actions: actions
        )
    }

    private static func healthModule(
        payload: GlancePayload?,
        lastFetched: Date?,
        summary: String
    ) -> WellnessSceneModule {
        guard let payload else {
            return WellnessSceneModule(
                id: "health-state",
                kind: .healthState,
                title: "Current state",
                summary: "Health data is insufficient, so no schedule conclusion is generated."
            )
        }
        return WellnessSceneModule(
            id: "health-state",
            kind: .healthState,
            title: "Current capacity",
            summary: summary,
            items: [
                WellnessSceneItem(
                    id: "energy",
                    label: "Energy",
                    value: GlanceFormat.scoreText(payload.energy.score)
                ),
                WellnessSceneItem(
                    id: "confidence",
                    label: "Confidence",
                    value: confidenceText(payload.energy.confidence)
                ),
                WellnessSceneItem(
                    id: "freshness",
                    label: "Observed",
                    value: lastFetched?.healthMesShortDateTime ?? "Unknown"
                ),
            ]
        )
    }

    private static func nextImpactModule(payload: GlancePayload?) -> WellnessSceneModule {
        guard let payload, let block = payload.nextBlocks.first else {
            return WellnessSceneModule(
                id: "next-impact",
                kind: .planImpact,
                title: "Next schedule impact",
                summary: "No upcoming block is available to compare with current capacity."
            )
        }
        return WellnessSceneModule(
            id: "next-impact",
            kind: .planImpact,
            title: "Next protected block",
            summary:
                "The block is shown as context. HealthMes changes it only through an explicit proposal and confirmation.",
            items: [
                WellnessSceneItem(
                    id: "block-\(Int(block.start.timeIntervalSince1970))",
                    label: "\(block.start.healthMesShortTime)–\(block.end.healthMesShortTime)",
                    value: block.title ?? "Untitled block",
                    detail:
                        "\(block.energyDemand?.rawValue.capitalized ?? "Unknown") energy · \(block.source.rawValue)"
                )
            ]
        )
    }

    private static func decisionModule(
        proposal: ProposalItem?,
        alert: AlertItem?,
        pairing: Pairing?
    ) -> WellnessSceneModule {
        guard let proposal,
            let prompt = ProposalActionPresentation.exactPrompt(alert: alert)
        else {
            return WellnessSceneModule(
                id: "decision",
                kind: .decision,
                title: "Intervention",
                summary:
                    "No exact, evidence-backed calendar action is waiting. HealthMes remains quiet instead of guessing."
            )
        }

        var items = [
            WellnessSceneItem(
                id: "proposal-\(proposal.id.uuidString)",
                label: "Exact action",
                value: prompt,
                detail:
                    "\(proposal.proposedStart.healthMesShortDateTime)–\(proposal.proposedEnd.healthMesShortTime)"
            )
        ]
        if let observation = alert?.decisionCard?.observationShort ?? alert?.summary {
            items.append(
                WellnessSceneItem(
                    id: "alert-\(alert?.id.uuidString ?? UUID().uuidString)",
                    label: "What changed",
                    value: observation
                )
            )
        }
        if let evidence = alert?.decisionCard?.evidenceShort
            ?? alert.flatMap({ AlertNotificationContent.evidenceLine($0.evidence) })
        {
            items.append(
                WellnessSceneItem(
                    id: "evidence",
                    label: "Why now",
                    value: evidence
                )
            )
        }

        return WellnessSceneModule(
            id: "decision",
            kind: .decision,
            title: "One reversible intervention",
            summary:
                "No changes occur until Yes. Approval may still be waiting for calendar sync.",
            items: items
        )
    }

    private static func decisionActions(
        proposal: ProposalItem?,
        alert: AlertItem?,
        pairing: Pairing?
    ) -> [WellnessSceneAction] {
        guard let proposal,
            proposal.isActionable,
            ProposalActionPresentation.exactPrompt(alert: alert) != nil
        else {
            return []
        }

        var actions = [
            WellnessSceneAction(
                id: "decline-\(proposal.id.uuidString)",
                kind: .declineProposal,
                label: "No",
                proposalID: proposal.id
            ),
            WellnessSceneAction(
                id: "accept-\(proposal.id.uuidString)",
                kind: .acceptProposal,
                label: "Yes",
                proposalID: proposal.id
            ),
        ]
        if let url = alert.flatMap({ MacWebLinks.decision(for: $0, pairing: pairing) })
            ?? MacWebLinks.decision(for: proposal, pairing: pairing)
        {
            actions.append(
                WellnessSceneAction(
                    id: "detail-\(proposal.id.uuidString)",
                    kind: .openWebDetail,
                    label: "Full decision path",
                    proposalID: proposal.id,
                    url: url
                )
            )
        }
        return actions
    }

    private static func calendarModule(
        events: [CalendarEventItem],
        dashboardErrors: [String],
        report: WeeklyReport?
    ) -> WellnessSceneModule {
        let calendarError = dashboardErrors.first(where: { $0.hasPrefix("Calendar:") })
        var items: [WellnessSceneItem] = []
        if !events.isEmpty {
            let googleCount = events.filter {
                $0.calendarSource.lowercased() == "google"
            }.count
            let appleCount = events.filter {
                ["caldav", "icloud"].contains($0.calendarSource.lowercased())
            }.count
            items.append(
                WellnessSceneItem(
                    id: "calendar-visible",
                    label: "Mirrored",
                    value: "\(events.count) visible events",
                    detail: "This proves API data was loaded, not that every provider is healthy."
                )
            )
            items.append(
                WellnessSceneItem(
                    id: "calendar-google",
                    label: "Google Calendar",
                    value: "\(googleCount) mirrored events"
                )
            )
            items.append(
                WellnessSceneItem(
                    id: "calendar-apple",
                    label: "Apple Calendar",
                    value: "\(appleCount) mirrored events"
                )
            )
        }
        if let breakdown = report?.schedule.displayBreakdown {
            items.append(
                WellnessSceneItem(
                    id: "calendar-sync-pending",
                    label: "Approved",
                    value: "\(breakdown.syncPending) sync pending"
                )
            )
            items.append(
                WellnessSceneItem(
                    id: "calendar-applied",
                    label: "Applied",
                    value: "\(breakdown.applied) pushed"
                )
            )
        }
        if let calendarError {
            items.append(
                WellnessSceneItem(
                    id: "calendar-error",
                    label: "Needs attention",
                    value: calendarError
                )
            )
        }

        return WellnessSceneModule(
            id: "calendar-sync",
            kind: .calendarSync,
            title: "Calendar delivery truth",
            summary:
                "Configured, mirrored, approved and applied are distinct states. Only pushed proves an external calendar write.",
            items: items
        )
    }

    private static func freshness(
        payload: GlancePayload?,
        isStale: Bool,
        errorKey: String?
    ) -> WellnessFreshness {
        if isStale { return .stale }
        if payload == nil, errorKey != nil { return .offline }
        if payload == nil { return .insufficientData }
        return .current
    }

    private static func bodyPlanImpact(_ payload: GlancePayload?) -> String {
        guard let payload else {
            return "Health data is insufficient, so HealthMes does not infer a schedule change."
        }
        if let summary = payload.alerts.top?.summary {
            return summary
        }
        guard let score = payload.energy.score else {
            return "Capacity is unknown. Keep the current plan until more health context arrives."
        }
        switch score {
        case ..<45:
            return "Recovery capacity is limited. Review high-demand blocks before accepting a change."
        case 45..<70:
            return "Capacity is moderate. Protect the strongest available focus window."
        default:
            return "Capacity is currently high enough to prioritize demanding goal work."
        }
    }
}
