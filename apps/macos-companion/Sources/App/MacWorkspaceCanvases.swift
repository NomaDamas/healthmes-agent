import SwiftUI

struct MacWorkspaceChannelView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    @ObservedObject var workspaceStore: MacWorkspaceViewModel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void
    let onOpenDetail: (MacDetailContext) -> Void
    let onRefresh: (Bool) async -> Void
    let onSettings: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            channelHeader

            if let channel = workspaceStore.selectedChannel {
                channelCanvas(channel)
            } else {
                MacEmptyState(
                    systemImage: "rectangle.split.3x1",
                    title: "Choose a channel",
                    message: "Your workspace keeps health, calendar and decisions in focused canvases."
                )
            }
        }
        .background(workspaceBackground)
    }

    private var channelHeader: some View {
        HStack(spacing: 12) {
            if let channel = workspaceStore.selectedChannel {
                Image(systemName: channel.symbolName)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(MacHealthMesStyle.moss)
                    .frame(width: 32, height: 32)
                    .background(MacHealthMesStyle.moss.opacity(0.11), in: RoundedRectangle(cornerRadius: 9))
                VStack(alignment: .leading, spacing: 2) {
                    Text(channel.title)
                        .font(.headline)
                    Text(channelSubtitle(channel))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer()

            MacPrivacyPill(
                isPaired: glanceStore.isPaired,
                isStale: glanceStore.isStale
            )

            if let pairing = dashboardStore.pairing {
                Link(destination: MacWebLinks.dashboard(pairing: pairing)) {
                    Image(systemName: "arrow.up.right.square")
                }
                .buttonStyle(.borderless)
                .help("Open detailed web dashboard")
            }

            Button {
                Task { await onRefresh(true) }
            } label: {
                if glanceStore.isRefreshing || dashboardStore.isRefreshing {
                    ProgressView()
                        .controlSize(.small)
                        .frame(width: 16, height: 16)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .disabled(glanceStore.isRefreshing || dashboardStore.isRefreshing)
            .help("Refresh health, calendar and decisions")

            Button(action: onSettings) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.borderless)
            .help("Settings")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 11)
        .background(.regularMaterial)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(MacHealthMesStyle.line)
                .frame(height: 1)
        }
    }

    @ViewBuilder
    private func channelCanvas(_ channel: WorkspaceChannel) -> some View {
        switch channel.systemKind ?? systemEquivalent(for: channel.canvas) {
        case .overview:
            MacWorkspaceOverviewCanvas(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                channel: channel,
                onOpenThread: onOpenThread,
                onOpenDetail: onOpenDetail
            )
        case .calendar:
            MacWorkspaceCalendarCanvas(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                channel: channel,
                onOpenThread: onOpenThread,
                onOpenDetail: onOpenDetail
            )
        case .insights:
            MacWorkspaceInsightsCanvas(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                channel: channel,
                onOpenThread: onOpenThread,
                onOpenDetail: onOpenDetail
            )
        case .decisions:
            MacWorkspaceDecisionsCanvas(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                channel: channel,
                onOpenThread: onOpenThread,
                onOpenDetail: onOpenDetail
            )
        case .agent:
            MacWorkspaceAgentCanvas(
                dashboardStore: dashboardStore,
                workspaceStore: workspaceStore,
                channel: channel,
                onOpenThread: onOpenThread
            )
        }
    }

    private var workspaceBackground: some View {
        LinearGradient(
            colors: [
                MacHealthMesStyle.canvas,
                Color(red: 0.90, green: 0.945, blue: 0.925),
                Color(red: 0.965, green: 0.95, blue: 0.91),
            ],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
        .ignoresSafeArea()
    }

    private func channelSubtitle(_ channel: WorkspaceChannel) -> String {
        switch channel.systemKind ?? systemEquivalent(for: channel.canvas) {
        case .overview: return "Health, schedule and the one decision that matters now"
        case .calendar: return "Apple and Google events mirrored by your HealthMes instance"
        case .insights: return "Capacity, baseline and weekly wellness signals"
        case .decisions: return "Pending proposals, evidence and outcomes"
        case .agent: return "Voice, text and nutrition command canvas"
        }
    }

    private func systemEquivalent(for canvas: WorkspaceCanvasKind) -> WorkspaceSystemChannel {
        switch canvas {
        case .dashboard, .mixed: return .overview
        case .calendar: return .calendar
        case .visualization: return .insights
        case .decisions: return .decisions
        case .conversation: return .agent
        }
    }
}

private struct MacWorkspaceOverviewCanvas: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let channel: WorkspaceChannel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void
    let onOpenDetail: (MacDetailContext) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Today")
                            .font(.system(size: 30, weight: .bold, design: .default))
                        Text(todayConclusion)
                            .font(.title3)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(freshnessText)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }

                LazyVGrid(
                    columns: [
                        GridItem(.flexible(), spacing: 14),
                        GridItem(.flexible(), spacing: 14),
                        GridItem(.flexible(), spacing: 14),
                    ],
                    alignment: .leading,
                    spacing: 14
                ) {
                    capacityCard
                    nextCard
                    decisionCard
                }

                HStack(alignment: .top, spacing: 14) {
                    calendarPreview
                    weeklyPreview
                }
            }
            .padding(24)
            .frame(maxWidth: 1_180)
            .frame(maxWidth: .infinity)
        }
    }

    private var capacityCard: some View {
        MacWorkspaceCard(
            title: "Capacity",
            systemImage: "waveform.path.ecg",
            accent: MacHealthMesStyle.moss,
            onThread: {
                onOpenThread(
                    anchor(
                        kind: .visualization,
                        localID: "today-capacity",
                        title: "Today's capacity"
                    ),
                    todayConclusion
                )
            }
        ) {
            if let payload = glanceStore.payload {
                Button {
                    onOpenDetail(.energy(payload))
                } label: {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .firstTextBaseline, spacing: 7) {
                            Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                                .font(.system(size: 42, weight: .bold, design: .rounded))
                            Text("energy")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                        }
                        MacCapacityBar(score: payload.energy.score)
                        Text(confidenceText(payload.energy.confidence))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                loadingState("Waiting for health context")
            }
        }
    }

    private var nextCard: some View {
        MacWorkspaceCard(
            title: "Next block",
            systemImage: "calendar.badge.clock",
            accent: MacHealthMesStyle.graphite,
            onThread: nextBlock.map { block in
                {
                    onOpenThread(
                        anchor(
                            kind: .calendarEvent,
                            localID: "block-\(Int(block.start.timeIntervalSince1970))",
                            title: block.title ?? "Next calendar block"
                        ),
                        "\(block.start.healthMesShortTime)–\(block.end.healthMesShortTime)"
                    )
                }
            }
        ) {
            if let block = nextBlock, let payload = glanceStore.payload {
                Button {
                    onOpenDetail(.block(block, timezone: payload.timezone))
                } label: {
                    VStack(alignment: .leading, spacing: 9) {
                        Text(block.title ?? "Untitled block")
                            .font(.title3.weight(.semibold))
                            .lineLimit(2)
                        Text("\(block.start.healthMesShortTime)–\(block.end.healthMesShortTime)")
                            .font(.body.monospacedDigit())
                            .foregroundStyle(.secondary)
                        if let demand = block.energyDemand {
                            Label(
                                "\(demand.rawValue.capitalized) energy demand",
                                systemImage: demand == .high ? "bolt.fill" : "bolt"
                            )
                            .font(.caption)
                            .foregroundStyle(
                                demand == .high ? MacHealthMesStyle.amber : MacHealthMesStyle.moss
                            )
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                loadingState("No upcoming block")
            }
        }
    }

    private var decisionCard: some View {
        MacWorkspaceCard(
            title: "Decision",
            systemImage: "checkmark.bubble",
            accent: glanceStore.pendingProposals.isEmpty
                ? MacHealthMesStyle.moss
                : MacHealthMesStyle.amber,
            onThread: pendingProposal.map { proposal in
                {
                    let alert = matchingAlert(proposal)
                    onOpenThread(
                        anchor(
                            kind: .decision,
                            localID: proposal.id.uuidString,
                            title: decisionTitle(proposal),
                            proposalID: proposal.id,
                            decisionRecordID: proposal.decisionRecordId
                        ),
                        alert?.decisionCard?.evidenceShort ?? alert?.summary
                    )
                }
            }
        ) {
            if let proposal = pendingProposal {
                Button {
                    onOpenDetail(.proposal(proposal, alert: matchingAlert(proposal)))
                } label: {
                    VStack(alignment: .leading, spacing: 9) {
                        Text(decisionTitle(proposal))
                            .font(.title3.weight(.semibold))
                            .lineLimit(3)
                        Text(proposal.proposedStart.healthMesShortDateTime)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                        Label("Waiting for your approval", systemImage: "hourglass")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(MacHealthMesStyle.amber)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Nothing needs your decision.")
                        .font(.title3.weight(.semibold))
                    Text("HealthMes stays quiet until a useful action is available.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var calendarPreview: some View {
        MacWorkspacePanel(title: "Calendar", systemImage: "calendar") {
            if dashboardStore.events.isEmpty {
                loadingState("No mirrored events")
            } else {
                ForEach(dashboardStore.events.sorted(by: { $0.startAt < $1.startAt }).prefix(5)) {
                    event in
                    MacWorkspaceEventRow(
                        event: event,
                        onDetail: { onOpenDetail(.event(event)) },
                        onThread: {
                            onOpenThread(
                                anchor(
                                    kind: .calendarEvent,
                                    localID: event.id.uuidString,
                                    title: event.summary ?? "Untitled event"
                                ),
                                "\(event.startAt.healthMesShortDateTime) · \(event.calendarSource.capitalized)"
                            )
                        }
                    )
                }
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var weeklyPreview: some View {
        MacWorkspacePanel(title: "This week", systemImage: "scope") {
            if let report = dashboardStore.weeklyReport {
                Button {
                    onOpenDetail(.report(report))
                } label: {
                    VStack(alignment: .leading, spacing: 14) {
                        metric(
                            "Average energy",
                            report.energy.overallAvg.map(String.init) ?? "—"
                        )
                        metric(
                            "Calendar applied",
                            String(report.schedule.pushed)
                        )
                        metric(
                            "Decision acceptance",
                            report.schedule.acceptancePct.map { "\($0)%" } ?? "—"
                        )
                        if let insight = report.insights.items.first {
                            Divider()
                            Text(insight.statement)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .lineLimit(3)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                loadingState("Weekly report is not available")
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func metric(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .foregroundStyle(.secondary)
            Spacer()
            Text(value)
                .font(.body.weight(.semibold).monospacedDigit())
        }
    }

    private var todayConclusion: String {
        guard let score = glanceStore.payload?.energy.score else {
            return "Connect health data to compare your capacity with today's plan."
        }
        switch score {
        case 70...: return "You have room for demanding work."
        case 45..<70: return "Protect your focus and keep recovery space."
        default: return "Reduce load and protect recovery today."
        }
    }

    private var nextBlock: GlanceBlock? {
        glanceStore.payload?.nextBlocks.first
    }

    private var pendingProposal: ProposalItem? {
        glanceStore.pendingProposals.first
    }

    private var freshnessText: String {
        let date = [glanceStore.lastFetched, dashboardStore.lastUpdated]
            .compactMap { $0 }
            .max()
        return date.map {
            "Updated \($0.formatted(date: .omitted, time: .shortened))"
        } ?? "Waiting for first refresh"
    }

    private func matchingAlert(_ proposal: ProposalItem) -> AlertItem? {
        glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
    }

    private func decisionTitle(_ proposal: ProposalItem) -> String {
        let alert = matchingAlert(proposal)
        return alert?.decisionCard?.title
            ?? alert?.decisionCard?.proposedAction
            ?? alert?.proposal
            ?? "Review schedule change"
    }

    private func anchor(
        kind: WorkspaceThreadAnchorKind,
        localID: String,
        title: String,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) -> WorkspaceThreadAnchor {
        WorkspaceThreadAnchor(
            kind: kind,
            localID: localID,
            title: title,
            proposalID: proposalID,
            decisionRecordID: decisionRecordID
        )
    }

    private func loadingState(_ text: String) -> some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 80, alignment: .leading)
    }
}

private struct MacWorkspaceCalendarCanvas: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let channel: WorkspaceChannel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void
    let onOpenDetail: (MacDetailContext) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Schedule canvas")
                            .font(.system(size: 30, weight: .bold, design: .rounded))
                        Text("Real events first. HealthMes proposals remain visibly pending.")
                            .font(.title3)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    sourceLegend
                }

                if !glanceStore.pendingProposals.isEmpty {
                    pendingStrip
                }

                HStack(alignment: .top, spacing: 16) {
                    MacWorkspacePanel(title: "Timeline", systemImage: "calendar.day.timeline.left") {
                        if groupedEvents.isEmpty {
                            MacEmptyState(
                                systemImage: "calendar.badge.exclamationmark",
                                title: "No mirrored events",
                                message: "Connect Apple or Google Calendar in Settings, then refresh."
                            )
                        } else {
                            ForEach(groupedEvents, id: \.day) { group in
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(group.day)
                                        .font(.caption.weight(.bold))
                                        .textCase(.uppercase)
                                        .foregroundStyle(.secondary)
                                    ForEach(group.events) { event in
                                        MacWorkspaceEventRow(
                                            event: event,
                                            onDetail: { onOpenDetail(.event(event)) },
                                            onThread: {
                                                onOpenThread(
                                                    WorkspaceThreadAnchor(
                                                        kind: .calendarEvent,
                                                        localID: event.id.uuidString,
                                                        title: event.summary ?? "Untitled event"
                                                    ),
                                                    "\(event.startAt.healthMesShortDateTime)–\(event.endAt.healthMesShortTime)"
                                                )
                                            }
                                        )
                                    }
                                }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity)

                    VStack(spacing: 16) {
                        capacityPanel
                        goalPanel
                    }
                    .frame(width: 280)
                }
            }
            .padding(24)
            .frame(maxWidth: 1_180)
            .frame(maxWidth: .infinity)
        }
    }

    private var pendingStrip: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(
                "\(glanceStore.pendingProposals.count) change\(glanceStore.pendingProposals.count == 1 ? "" : "s") waiting",
                systemImage: "sparkles"
            )
            .font(.headline)
            .foregroundStyle(MacHealthMesStyle.amber)
            ForEach(glanceStore.pendingProposals.prefix(3)) { proposal in
                let alert = glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(
                            alert?.decisionCard?.title
                                ?? alert?.proposal
                                ?? "Schedule change"
                        )
                        .font(.callout.weight(.semibold))
                        Text(proposal.proposedStart.healthMesShortDateTime)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Thread") {
                        onOpenThread(
                            WorkspaceThreadAnchor(
                                kind: .decision,
                                localID: proposal.id.uuidString,
                                title: alert?.decisionCard?.title ?? "Schedule change",
                                proposalID: proposal.id,
                                decisionRecordID: proposal.decisionRecordId
                            ),
                            alert?.decisionCard?.evidenceShort ?? alert?.summary
                        )
                    }
                    Button("Review") {
                        onOpenDetail(.proposal(proposal, alert: alert))
                    }
                }
            }
        }
        .padding(16)
        .background(
            MacHealthMesStyle.amber.opacity(0.09),
            in: RoundedRectangle(cornerRadius: 16)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 16)
                .stroke(MacHealthMesStyle.amber.opacity(0.18))
        }
    }

    private var capacityPanel: some View {
        MacWorkspacePanel(title: "Capacity", systemImage: "bolt.heart") {
            if let payload = glanceStore.payload {
                Text(GlanceFormat.scoreText(payload.energy.score))
                    .font(.system(size: 36, weight: .bold, design: .rounded))
                MacCapacityBar(score: payload.energy.score)
                Text("Compare demanding blocks with the hours where your energy is available.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("No health context")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var goalPanel: some View {
        MacWorkspacePanel(title: "Protected goals", systemImage: "scope") {
            if dashboardStore.goals.isEmpty {
                Text("No active weekly goals")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(dashboardStore.goals.prefix(4)) { goal in
                    Button {
                        onOpenDetail(.goal(goal))
                    } label: {
                        HStack {
                            Circle()
                                .fill(MacHealthMesStyle.moss)
                                .frame(width: 7, height: 7)
                            Text(goal.title)
                                .lineLimit(2)
                            Spacer()
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var sourceLegend: some View {
        HStack(spacing: 12) {
            legendDot("Apple", color: Color(red: 0.25, green: 0.49, blue: 0.82))
            legendDot("Google", color: Color(red: 0.80, green: 0.36, blue: 0.23))
            legendDot("HealthMes", color: MacHealthMesStyle.moss)
        }
        .font(.caption)
    }

    private func legendDot(_ title: String, color: Color) -> some View {
        Label {
            Text(title)
        } icon: {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
        }
    }

    private var groupedEvents: [(day: String, events: [CalendarEventItem])] {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEEE, MMM d"
        return Dictionary(grouping: dashboardStore.events) { event in
            formatter.string(from: event.startAt)
        }
        .map { (day: $0.key, events: $0.value.sorted { $0.startAt < $1.startAt }) }
        .sorted {
            ($0.events.first?.startAt ?? .distantFuture)
                < ($1.events.first?.startAt ?? .distantFuture)
        }
    }
}

private struct MacWorkspaceInsightsCanvas: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let channel: WorkspaceChannel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void
    let onOpenDetail: (MacDetailContext) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Wellness insights")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                    Text(insightConclusion)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }

                HStack(alignment: .top, spacing: 14) {
                    energyCurvePanel
                    baselinePanel
                }

                HStack(alignment: .top, spacing: 14) {
                    weeklyFactorsPanel
                    decisionOutcomePanel
                }
            }
            .padding(24)
            .frame(maxWidth: 1_180)
            .frame(maxWidth: .infinity)
        }
    }

    private var energyCurvePanel: some View {
        MacWorkspacePanel(
            title: "Energy curve",
            systemImage: "chart.xyaxis.line",
            onThread: {
                onOpenThread(
                    WorkspaceThreadAnchor(
                        kind: .visualization,
                        localID: "energy-curve",
                        title: "Energy curve"
                    ),
                    insightConclusion
                )
            }
        ) {
            if let payload = glanceStore.payload {
                Button {
                    onOpenDetail(.energy(payload))
                } label: {
                    VStack(alignment: .leading, spacing: 12) {
                        MacEnergyCurveView(
                            curve: payload.energy.curve24h,
                            currentHour: currentHour(timezone: payload.timezone)
                        )
                        .frame(height: 132)
                        HStack {
                            Text("Current")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(GlanceFormat.scoreText(payload.energy.score))
                                .font(.title2.bold().monospacedDigit())
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                Text("No energy curve available")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 150)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var baselinePanel: some View {
        MacWorkspacePanel(
            title: "Weekly baseline",
            systemImage: "chart.bar.fill",
            onThread: {
                onOpenThread(
                    WorkspaceThreadAnchor(
                        kind: .visualization,
                        localID: "weekly-baseline",
                        title: "Weekly baseline"
                    ),
                    baselineSummary
                )
            }
        ) {
            if let report = dashboardStore.weeklyReport {
                VStack(spacing: 11) {
                    ForEach(report.energy.days, id: \.date) { day in
                        HStack(spacing: 10) {
                            Text(shortDay(day.date))
                                .font(.caption.weight(.semibold))
                                .frame(width: 32, alignment: .leading)
                            GeometryReader { proxy in
                                ZStack(alignment: .leading) {
                                    Capsule().fill(Color.primary.opacity(0.07))
                                    Capsule()
                                        .fill(
                                            day.avgScore.map(energyColor) ?? Color.secondary.opacity(0.2)
                                        )
                                        .frame(
                                            width: proxy.size.width
                                                * CGFloat((day.avgScore ?? 0)) / 100
                                        )
                                }
                            }
                            .frame(height: 9)
                            Text(day.avgScore.map(String.init) ?? "—")
                                .font(.caption.monospacedDigit())
                                .frame(width: 26, alignment: .trailing)
                        }
                    }
                }
            } else {
                Text("No weekly baseline available")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 150)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var weeklyFactorsPanel: some View {
        MacWorkspacePanel(title: "What changed", systemImage: "list.bullet.rectangle") {
            if let report = dashboardStore.weeklyReport, !report.insights.items.isEmpty {
                ForEach(report.insights.items.prefix(5)) { insight in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(insight.statement)
                            .font(.body.weight(.medium))
                        Text(insight.confidenceLevel.rawValue.capitalized + " confidence")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if insight.id != report.insights.items.prefix(5).last?.id {
                        Divider()
                    }
                }
            } else {
                Text("No evidence-backed insights yet")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var decisionOutcomePanel: some View {
        MacWorkspacePanel(title: "Decision outcomes", systemImage: "arrow.triangle.2.circlepath") {
            if let report = dashboardStore.weeklyReport {
                VStack(spacing: 13) {
                    outcomeRow("Applied to calendar", report.schedule.pushed, MacHealthMesStyle.moss)
                    outcomeRow("Approved, syncing", report.schedule.accepted, MacHealthMesStyle.amber)
                    outcomeRow("Declined", report.schedule.declined, .secondary)
                    outcomeRow("Waiting", report.schedule.proposed, MacHealthMesStyle.amber)
                    Button("Open report detail") {
                        onOpenDetail(.report(report))
                    }
                    .buttonStyle(.link)
                }
            } else {
                Text("No decision outcomes yet")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func outcomeRow(_ title: String, _ value: Int, _ color: Color) -> some View {
        HStack {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(title)
            Spacer()
            Text(String(value))
                .font(.body.bold().monospacedDigit())
        }
    }

    private var insightConclusion: String {
        guard let score = glanceStore.payload?.energy.score else {
            return "HealthMes will visualize conclusions only after enough data arrives."
        }
        switch score {
        case 70...: return "Your current capacity is above the safer planning threshold."
        case 45..<70: return "Capacity is usable, but the schedule should preserve recovery gaps."
        default: return "Current capacity is low; high-demand blocks deserve review."
        }
    }

    private var baselineSummary: String {
        guard let average = dashboardStore.weeklyReport?.energy.overallAvg else {
            return "A weekly baseline is not available yet."
        }
        return "This week's average energy is \(average)."
    }

    private func currentHour(timezone: String) -> Int {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }

    private func shortDay(_ date: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        guard let value = formatter.date(from: date) else {
            return String(date.suffix(2))
        }
        formatter.dateFormat = "EEE"
        return formatter.string(from: value)
    }

    private func energyColor(_ score: Int) -> Color {
        switch score {
        case 70...: return MacHealthMesStyle.moss
        case 45..<70: return Color(red: 0.55, green: 0.62, blue: 0.30)
        default: return MacHealthMesStyle.amber
        }
    }
}

private struct MacWorkspaceDecisionsCanvas: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let channel: WorkspaceChannel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void
    let onOpenDetail: (MacDetailContext) -> Void

    @State private var resolvingProposalID: UUID?
    @State private var outcomes: [UUID: ProposalOutcome] = [:]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Decision feed")
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                    Text("Every calendar mutation stays pending until you approve it.")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }

                pendingSection
                historySection
            }
            .padding(24)
            .frame(maxWidth: 1_050)
            .frame(maxWidth: .infinity)
        }
    }

    private var pendingSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Waiting for you")
                    .font(.headline)
                Text(String(glanceStore.pendingProposals.count))
                    .font(.caption.bold().monospacedDigit())
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(MacHealthMesStyle.amber.opacity(0.13), in: Capsule())
            }

            if glanceStore.pendingProposals.isEmpty {
                MacEmptyState(
                    systemImage: "checkmark.seal",
                    title: "Nothing needs your approval",
                    message: "HealthMes will ask only when an evidence-backed action can improve the plan."
                )
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
            } else {
                ForEach(glanceStore.pendingProposals) { proposal in
                    proposalCard(proposal)
                }
            }
        }
    }

    private func proposalCard(_ proposal: ProposalItem) -> some View {
        let alert = glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
        let title = alert?.decisionCard?.title
            ?? alert?.decisionCard?.proposedAction
            ?? alert?.proposal
            ?? "Schedule change"

        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "calendar.badge.clock")
                    .font(.title2)
                    .foregroundStyle(MacHealthMesStyle.amber)
                VStack(alignment: .leading, spacing: 5) {
                    Text(title)
                        .font(.title3.weight(.semibold))
                    Text(
                        "\(proposal.proposedStart.healthMesShortDateTime)–\(proposal.proposedEnd.healthMesShortTime)"
                    )
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
                    if let evidence = alert?.decisionCard?.evidenceShort ?? alert?.summary {
                        Text(evidence)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                Spacer()
                Button {
                    onOpenThread(
                        WorkspaceThreadAnchor(
                            kind: .decision,
                            localID: proposal.id.uuidString,
                            title: title,
                            proposalID: proposal.id,
                            decisionRecordID: proposal.decisionRecordId
                        ),
                        alert?.decisionCard?.evidenceShort ?? alert?.summary
                    )
                } label: {
                    Label("Thread", systemImage: "text.bubble")
                }
                .buttonStyle(.borderless)
            }

            Divider()

            HStack {
                Button("Why?") {
                    onOpenDetail(.proposal(proposal, alert: alert))
                }
                .buttonStyle(.link)

                Spacer()

                if let outcome = outcomes[proposal.id] {
                    Label(outcomeText(outcome), systemImage: outcomeIcon(outcome))
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(outcomeColor(outcome))
                } else {
                    Button("No") {
                        Task { await resolve(proposal, action: .decline) }
                    }
                    .buttonStyle(.bordered)
                    Button("Yes") {
                        Task { await resolve(proposal, action: .accept) }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.moss)
                    .disabled(resolvingProposalID != nil)
                }
            }
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .stroke(MacHealthMesStyle.line)
        }
    }

    private var historySection: some View {
        MacWorkspacePanel(title: "History", systemImage: "clock.arrow.circlepath") {
            if dashboardStore.decisions.isEmpty {
                Text("No recorded decisions")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(dashboardStore.decisions) { decision in
                    HStack(alignment: .top, spacing: 12) {
                        Button {
                            onOpenDetail(.decision(decision))
                        } label: {
                            HStack(alignment: .top, spacing: 12) {
                                Image(systemName: decisionSymbol(decision.kind))
                                    .foregroundStyle(MacHealthMesStyle.moss)
                                    .frame(width: 22)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(decision.summary)
                                        .font(.body.weight(.medium))
                                        .lineLimit(2)
                                    Text(decision.createdAt.healthMesShortDateTime)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)

                        Spacer()

                        Button {
                            onOpenThread(
                                WorkspaceThreadAnchor(
                                    kind: .decision,
                                    localID: decision.id.uuidString,
                                    title: decision.summary,
                                    decisionRecordID: decision.id
                                ),
                                "Recorded \(decision.createdAt.healthMesShortDateTime)"
                            )
                        } label: {
                            Image(systemName: "text.bubble")
                        }
                        .buttonStyle(.borderless)
                        .help("Open local thread")
                    }
                    if decision.id != dashboardStore.decisions.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func resolve(_ proposal: ProposalItem, action: ProposalAction) async {
        guard resolvingProposalID == nil else { return }
        resolvingProposalID = proposal.id
        let outcome = await glanceStore.resolve(
            proposal,
            action: action,
            pairing: dashboardStore.pairing
        )
        outcomes[proposal.id] = outcome
        await dashboardStore.refresh()
        resolvingProposalID = nil
    }

    private func decisionSymbol(_ kind: MacDecisionKind) -> String {
        switch kind {
        case .scheduleChange: return "calendar.badge.clock"
        case .alert: return "bell.badge"
        case .insight: return "lightbulb"
        case .capture: return "tray.and.arrow.down"
        }
    }

    private func outcomeText(_ outcome: ProposalOutcome) -> String {
        switch outcome {
        case .accepted: return "Approved"
        case .applied: return "Applied to calendar"
        case .kept: return "Kept"
        case .expired: return "No longer available"
        case .alreadyResolved(let status):
            return "Already \(status.replacingOccurrences(of: "_", with: " "))"
        case .failed: return "Could not update"
        }
    }

    private func outcomeIcon(_ outcome: ProposalOutcome) -> String {
        switch outcome {
        case .accepted, .applied: return "checkmark.circle.fill"
        case .kept: return "xmark.circle.fill"
        case .expired, .alreadyResolved, .failed: return "exclamationmark.triangle.fill"
        }
    }

    private func outcomeColor(_ outcome: ProposalOutcome) -> Color {
        switch outcome {
        case .accepted, .applied: return MacHealthMesStyle.moss
        case .kept: return .secondary
        case .expired, .alreadyResolved, .failed: return MacHealthMesStyle.amber
        }
    }
}

private struct MacWorkspaceAgentCanvas: View {
    @ObservedObject var dashboardStore: MacDashboardStore
    @ObservedObject var workspaceStore: MacWorkspaceViewModel
    let channel: WorkspaceChannel
    let onOpenThread: (WorkspaceThreadAnchor, String?) -> Void

    @EnvironmentObject private var router: MacAppRouter

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Label("Command canvas", systemImage: "waveform")
                    .font(.callout.weight(.semibold))
                Text("Voice, text and meal-photo analysis use the existing HealthMes actions.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    onOpenThread(
                        WorkspaceThreadAnchor(
                            kind: .post,
                            localID: "agent-notes",
                            title: "Agent channel notes"
                        ),
                        "This local thread stays on this Mac and is not sent to the agent."
                    )
                } label: {
                    Label("Local thread", systemImage: "text.bubble")
                }
                .buttonStyle(.bordered)
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)

            MacSpeakView(
                dashboardStore: dashboardStore,
                onNavigate: selectChannel(for:),
                onRefresh: {}
            )
        }
    }

    private func selectChannel(for section: MacAppSection) {
        let systemChannel: WorkspaceSystemChannel
        switch section {
        case .today: systemChannel = .overview
        case .plan: systemChannel = .calendar
        case .decisions: systemChannel = .decisions
        case .speak: systemChannel = .agent
        case .settings:
            router.presentSettings()
            return
        }
        workspaceStore.selectChannel(WorkspaceState.systemChannelID(systemChannel))
    }
}

struct MacWorkspaceCard<Content: View>: View {
    let title: String
    let systemImage: String
    let accent: Color
    let onThread: (() -> Void)?
    @ViewBuilder let content: Content

    init(
        title: String,
        systemImage: String,
        accent: Color,
        onThread: (() -> Void)? = nil,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.systemImage = systemImage
        self.accent = accent
        self.onThread = onThread
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(title, systemImage: systemImage)
                    .font(.caption.weight(.bold))
                    .textCase(.uppercase)
                    .tracking(0.8)
                    .foregroundStyle(accent)
                Spacer()
                if let onThread {
                    Button(action: onThread) {
                        Image(systemName: "text.bubble")
                    }
                    .buttonStyle(.borderless)
                    .help("Open local thread")
                }
            }
            content
            Spacer(minLength: 0)
        }
        .padding(17)
        .frame(maxWidth: .infinity, minHeight: 190, alignment: .topLeading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(MacHealthMesStyle.line)
        }
        .shadow(color: MacHealthMesStyle.mossDeep.opacity(0.05), radius: 16, y: 8)
    }
}

struct MacWorkspacePanel<Content: View>: View {
    let title: String
    let systemImage: String
    let onThread: (() -> Void)?
    @ViewBuilder let content: Content

    init(
        title: String,
        systemImage: String,
        onThread: (() -> Void)? = nil,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.systemImage = systemImage
        self.onThread = onThread
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(title, systemImage: systemImage)
                    .font(.headline)
                Spacer()
                if let onThread {
                    Button(action: onThread) {
                        Image(systemName: "text.bubble")
                    }
                    .buttonStyle(.borderless)
                    .help("Open local thread")
                }
            }
            content
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(MacHealthMesStyle.line)
        }
    }
}

struct MacWorkspaceEventRow: View {
    let event: CalendarEventItem
    let onDetail: () -> Void
    let onThread: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 3)
                .fill(sourceColor)
                .frame(width: 4)

            Text(event.startAt.healthMesShortTime)
                .font(.callout.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .leading)

            Button(action: onDetail) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(event.summary ?? "Untitled event")
                        .font(.body.weight(.medium))
                        .lineLimit(2)
                    HStack(spacing: 6) {
                        Text(event.calendarSource.capitalized)
                        if event.isAgentCreated {
                            Text("· HealthMes")
                        }
                        if event.isLocked {
                            Image(systemName: "lock.fill")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            Button(action: onThread) {
                Image(systemName: "text.bubble")
            }
            .buttonStyle(.borderless)
            .help("Open local thread")
        }
        .padding(11)
        .background(Color.white.opacity(0.43), in: RoundedRectangle(cornerRadius: 12))
    }

    private var sourceColor: Color {
        if event.isAgentCreated {
            return MacHealthMesStyle.amber
        }
        switch event.calendarSource.lowercased() {
        case let source where source.contains("google"):
            return MacHealthMesStyle.calendar
        case let source where source.contains("apple"), let source where source.contains("icloud"):
            return MacHealthMesStyle.moss
        default:
            return .secondary
        }
    }
}

struct MacCapacityBar: View {
    let score: Int?

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(Color.primary.opacity(0.08))
                Capsule()
                    .fill(barColor)
                    .frame(
                        width: proxy.size.width
                            * CGFloat(min(max(score ?? 0, 0), 100)) / 100
                    )
            }
        }
        .frame(height: 11)
        .accessibilityLabel(Text("Energy capacity"))
        .accessibilityValue(Text(score.map { "\($0) percent" } ?? "No data"))
    }

    private var barColor: Color {
        switch score {
        case .some(70...): return MacHealthMesStyle.moss
        case .some(45..<70): return Color(red: 0.55, green: 0.62, blue: 0.30)
        case .some: return MacHealthMesStyle.amber
        case .none: return .secondary.opacity(0.25)
        }
    }
}
