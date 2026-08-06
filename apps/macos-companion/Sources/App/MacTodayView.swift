import SwiftUI

struct MacTodayView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let onSelect: (MacDetailContext) -> Void
    let onSpeak: () -> Void

    @State private var decisionOutcome: (proposalID: UUID, outcome: ProposalOutcome)?
    @State private var isResolving = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HStack(alignment: .bottom) {
                    MacPageHeader(
                        eyebrow: "Today",
                        title: "Make today fit your capacity.",
                        subtitle: "One state, one next block, one decision. Details stay out of the way until you ask."
                    )
                    Spacer()
                    Button(action: onSpeak) {
                        Label("Speak", systemImage: "waveform")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.graphite)
                    .controlSize(.large)
                    .keyboardShortcut(" ", modifiers: [.command, .shift])
                }

                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 245), spacing: 16)],
                    spacing: 16
                ) {
                    nowCard
                    nextCard
                    decisionCard
                }

                if let report = dashboardStore.weeklyReport {
                    weeklyLine(report)
                }

                if !dashboardStore.errorMessages.isEmpty {
                    DisclosureGroup("Some details could not refresh") {
                        ForEach(dashboardStore.errorMessages, id: \.self) { message in
                            Text(verbatim: message)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
            .padding(32)
        }
    }

    private var nowCard: some View {
        MacSurfaceCard("Now", systemImage: "circle.dotted.circle") {
            if let payload = glanceStore.payload {
                Button {
                    onSelect(.energy(payload))
                } label: {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(verbatim: stateHeadline(payload))
                            .font(.title2.weight(.semibold))
                            .foregroundStyle(MacHealthMesStyle.graphite)
                            .multilineTextAlignment(.leading)
                        HStack(spacing: 8) {
                            if let score = payload.energy.score {
                                Text(verbatim: "\(score)")
                                    .font(.system(.title3, design: .rounded).weight(.bold))
                            }
                            Text(verbatim: confidenceText(payload.energy.confidence))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Text("Open the energy detail")
                            .font(.caption)
                            .foregroundStyle(MacHealthMesStyle.moss)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                compactLoading
            }
        }
    }

    private var nextCard: some View {
        MacSurfaceCard("Next", systemImage: "arrow.right.circle") {
            if let payload = glanceStore.payload, let block = payload.nextBlocks.first {
                Button {
                    onSelect(.block(block, timezone: payload.timezone))
                } label: {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(verbatim: block.title ?? String(localized: "Untitled block"))
                            .font(.title2.weight(.semibold))
                            .lineLimit(2)
                            .foregroundStyle(MacHealthMesStyle.graphite)
                        Text(verbatim: blockTime(block, timezone: payload.timezone))
                            .font(.body.monospacedDigit())
                            .foregroundStyle(.secondary)
                        if block.source == .proposal {
                            Label("HealthMes proposal", systemImage: "sparkles")
                                .font(.caption)
                                .foregroundStyle(MacHealthMesStyle.moss)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                Text("Your schedule is clear.")
                    .font(.title3.weight(.medium))
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var decisionCard: some View {
        MacSurfaceCard("Decision", systemImage: "checkmark.bubble") {
            if let proposal = glanceStore.pendingProposals.first {
                let alert = matchingAlert(for: proposal)
                VStack(alignment: .leading, spacing: 12) {
                    if let actionPrompt = decisionQuestion(alert: alert) {
                        Text(verbatim: actionPrompt)
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(MacHealthMesStyle.graphite)
                            .lineLimit(3)
                    } else {
                        Text("Exact action details unavailable")
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(.secondary)
                        Text(
                            verbatim:
                                "\(proposal.proposedStart.healthMesShortDateTime)–\(proposal.proposedEnd.healthMesShortTime)"
                        )
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                    }

                    if let evidence = alert?.decisionCard?.evidenceShort {
                        Text(verbatim: evidence)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }

                    if let decisionOutcome, decisionOutcome.proposalID == proposal.id {
                        outcomeLabel(decisionOutcome.outcome)
                    } else if decisionQuestion(alert: alert) != nil {
                        HStack(spacing: 10) {
                            Button("No") {
                                Task { await resolve(proposal, action: .decline) }
                            }
                            .buttonStyle(.bordered)
                            Button("Yes") {
                                Task { await resolve(proposal, action: .accept) }
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(MacHealthMesStyle.moss)
                        }
                        .controlSize(.large)
                        .disabled(isResolving)
                    }

                    HStack(spacing: 12) {
                        Button("Why?") {
                            onSelect(.proposal(proposal, alert: alert))
                        }
                        .buttonStyle(.link)
                        MacWebDetailLink(url: decisionURL(proposal: proposal, alert: alert))
                    }
                    .controlSize(.small)
                }
            } else if let alert = glanceStore.alerts.first {
                VStack(alignment: .leading, spacing: 10) {
                    Text(verbatim: alert.summary)
                        .font(.title3.weight(.semibold))
                    Text("No action is waiting. Open the reason only if you need it.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Why?") {
                        onSelect(.alert(alert))
                    }
                    .buttonStyle(.link)
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Nothing needs your decision.")
                        .font(.title3.weight(.medium))
                    Text("HealthMes will ask only when an action can improve the plan.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func weeklyLine(_ report: WeeklyReport) -> some View {
        Button {
            onSelect(.report(report))
        } label: {
            HStack(spacing: 16) {
                Image(systemName: "calendar.badge.checkmark")
                    .font(.title2)
                    .foregroundStyle(MacHealthMesStyle.moss)
                VStack(alignment: .leading, spacing: 3) {
                    Text("This week")
                        .font(.headline)
                    Text(verbatim: weeklySummary(report))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .foregroundStyle(.tertiary)
            }
            .padding(18)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var compactLoading: some View {
        HStack {
            ProgressView()
            Text("Loading your day…")
                .foregroundStyle(.secondary)
        }
    }

    private func stateHeadline(_ payload: GlancePayload) -> String {
        guard let score = payload.energy.score else {
            return String(localized: "Waiting for enough health context.")
        }
        switch score {
        case 70...:
            return String(localized: "You have room for demanding work.")
        case 45..<70:
            return String(localized: "Protect your focus this afternoon.")
        default:
            return String(localized: "Recovery is the priority right now.")
        }
    }

    private func blockTime(_ block: GlanceBlock, timezone: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = .autoupdatingCurrent
        formatter.timeZone = TimeZone(identifier: timezone) ?? .current
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return "\(formatter.string(from: block.start))–\(formatter.string(from: block.end))"
    }

    private func matchingAlert(for proposal: ProposalItem) -> AlertItem? {
        glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
    }

    private func decisionQuestion(alert: AlertItem?) -> String? {
        ProposalActionPresentation.exactPrompt(alert: alert)
    }

    private func decisionURL(proposal: ProposalItem, alert: AlertItem?) -> URL? {
        if let alert,
            let url = MacWebLinks.decision(for: alert, pairing: dashboardStore.pairing)
        {
            return url
        }
        return MacWebLinks.decision(for: proposal, pairing: dashboardStore.pairing)
    }

    private func resolve(_ proposal: ProposalItem, action: ProposalAction) async {
        isResolving = true
        defer { isResolving = false }
        decisionOutcome = (
            proposal.id,
            await glanceStore.resolve(proposal, action: action)
        )
        await dashboardStore.refresh()
    }

    @ViewBuilder
    private func outcomeLabel(_ outcome: ProposalOutcome) -> some View {
        switch outcome {
        case .accepted:
            Label("Approved · calendar sync pending", systemImage: "clock.badge.checkmark")
                .foregroundStyle(MacHealthMesStyle.amber)
        case .applied:
            Label("Applied to calendar", systemImage: "checkmark.circle.fill")
                .foregroundStyle(MacHealthMesStyle.moss)
        case .kept:
            Label("Kept as is", systemImage: "minus.circle.fill")
                .foregroundStyle(.secondary)
        case .expired:
            Label("Expired · calendar unchanged", systemImage: "clock.badge.xmark")
                .foregroundStyle(MacHealthMesStyle.amber)
        case .alreadyResolved(let status):
            Label("Already resolved: \(status)", systemImage: "checkmark.circle")
                .foregroundStyle(MacHealthMesStyle.amber)
        case .failed:
            Label("Could not apply. Try again.", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
        }
    }

    private func weeklySummary(_ report: WeeklyReport) -> String {
        let decisions = report.decisions.count
        let acceptance = report.schedule.acceptancePct.map { "\($0)%" } ?? "—"
        return String(localized: "\(decisions) decisions · \(acceptance) plan acceptance")
    }
}
