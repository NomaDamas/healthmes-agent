import SwiftUI

struct MacPlanView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let onSelect: (MacDetailContext) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                MacPageHeader(
                    eyebrow: "Plan",
                    title: "Goals and time, in one place.",
                    subtitle: "HealthMes may propose a change, but your calendar moves only after you confirm."
                )

                if !glanceStore.pendingProposals.isEmpty {
                    pendingChanges
                }

                HStack(alignment: .top, spacing: 20) {
                    VStack(alignment: .leading, spacing: 22) {
                        goalsSection
                        tasksSection
                    }
                    .frame(maxWidth: .infinity, alignment: .top)

                    scheduleSection
                        .frame(maxWidth: .infinity, alignment: .top)
                }
            }
            .padding(32)
        }
    }

    private var pendingChanges: some View {
        VStack(alignment: .leading, spacing: 12) {
            MacSectionHeader("Waiting for you", count: glanceStore.pendingProposals.count)
            ForEach(glanceStore.pendingProposals) { proposal in
                MacPlanProposalRow(
                    proposal: proposal,
                    alert: glanceStore.alerts.first(where: { $0.proposalId == proposal.id }),
                    pairing: dashboardStore.pairing,
                    onResolve: { action in
                        let outcome = await glanceStore.resolve(proposal, action: action)
                        await dashboardStore.refresh()
                        return outcome
                    },
                    onDetail: {
                        onSelect(
                            .proposal(
                                proposal,
                                alert: glanceStore.alerts.first(where: {
                                    $0.proposalId == proposal.id
                                })
                            )
                        )
                    }
                )
            }
        }
        .padding(20)
        .background(
            MacHealthMesStyle.amber.opacity(0.09),
            in: RoundedRectangle(cornerRadius: 20, style: .continuous)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(MacHealthMesStyle.amber.opacity(0.18))
        }
    }

    private var goalsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("This week's goals", count: dashboardStore.goals.count)
            if dashboardStore.goals.isEmpty {
                compactEmpty("No active goals yet.")
            } else {
                ForEach(dashboardStore.goals.prefix(6)) { goal in
                    Button {
                        onSelect(.goal(goal))
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: goal.status == "done" ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(
                                    goal.status == "done" ? MacHealthMesStyle.moss : .secondary
                                )
                            VStack(alignment: .leading, spacing: 3) {
                                Text(verbatim: goal.title)
                                    .font(.body.weight(.medium))
                                    .lineLimit(2)
                                Text(verbatim: goal.weekStart)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            if goal.priority > 0 {
                                Text("P\(goal.priority)")
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(13)
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 13))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var tasksSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("Open tasks", count: openTasks.count)
            if openTasks.isEmpty {
                compactEmpty("Speak a task when something should enter the plan.")
            } else {
                ForEach(openTasks.prefix(10)) { task in
                    Button {
                        onSelect(.task(task))
                    } label: {
                        HStack(spacing: 12) {
                            demandMark(task.energyDemand)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(verbatim: task.title)
                                    .font(.body.weight(.medium))
                                    .lineLimit(2)
                                Text(verbatim: taskMeta(task))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(verbatim: task.status.replacingOccurrences(of: "_", with: " "))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(13)
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 13))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var scheduleSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("Calendar", count: dashboardStore.events.count)
            if dashboardStore.events.isEmpty {
                MacEmptyState(
                    systemImage: "calendar.badge.exclamationmark",
                    title: "No mirrored events",
                    message: "Connect a calendar in Settings, then refresh."
                )
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
            } else {
                ForEach(groupedEvents, id: \.day) { group in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(verbatim: group.day)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .textCase(.uppercase)
                        ForEach(group.events) { event in
                            Button {
                                onSelect(.event(event))
                            } label: {
                                HStack(alignment: .top, spacing: 12) {
                                    Text(verbatim: event.startAt.healthMesShortTime)
                                        .font(.callout.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                        .frame(width: 54, alignment: .leading)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(verbatim: event.summary ?? String(localized: "Untitled event"))
                                            .font(.body.weight(.medium))
                                            .lineLimit(2)
                                        if event.isAgentCreated {
                                            Label("HealthMes", systemImage: "sparkles")
                                                .font(.caption2)
                                                .foregroundStyle(MacHealthMesStyle.moss)
                                        }
                                    }
                                    Spacer()
                                }
                                .padding(12)
                                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 13))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.bottom, 8)
                }
            }
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
        .overlay {
            RoundedRectangle(cornerRadius: 20)
                .stroke(MacHealthMesStyle.line)
        }
    }

    private var openTasks: [TaskItem] {
        dashboardStore.tasks.filter(\.isOpen)
    }

    private var groupedEvents: [(day: String, events: [CalendarEventItem])] {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEEE, MMM d"
        return Dictionary(grouping: dashboardStore.events) { event in
            formatter.string(from: event.startAt)
        }
        .map { (day: $0.key, events: $0.value.sorted { $0.startAt < $1.startAt }) }
        .sorted { lhs, rhs in
            (lhs.events.first?.startAt ?? .distantFuture)
                < (rhs.events.first?.startAt ?? .distantFuture)
        }
    }

    private func compactEmpty(_ text: LocalizedStringKey) -> some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 13))
    }

    private func demandMark(_ demand: String) -> some View {
        Circle()
            .fill(
                demand == "high"
                    ? MacHealthMesStyle.amber
                    : demand == "low" ? MacHealthMesStyle.moss.opacity(0.55) : .secondary
            )
            .frame(width: 9, height: 9)
            .accessibilityLabel(Text("\(demand) energy"))
    }

    private func taskMeta(_ task: TaskItem) -> String {
        var parts: [String] = [task.energyDemand.capitalized + " energy"]
        if let minutes = task.estimatedMinutes {
            parts.append("\(minutes) min")
        }
        if let deadline = task.deadline {
            parts.append("Due \(deadline.healthMesShortDateTime)")
        }
        return parts.joined(separator: " · ")
    }
}

private struct MacPlanProposalRow: View {
    let proposal: ProposalItem
    let alert: AlertItem?
    let pairing: Pairing?
    let onResolve: (ProposalAction) async -> ProposalOutcome
    let onDetail: () -> Void

    @State private var isWorking = false
    @State private var outcome: ProposalOutcome?

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                if let actionPrompt {
                    Text(verbatim: actionPrompt)
                        .font(.headline)
                        .lineLimit(2)
                } else {
                    Text("Exact action details unavailable")
                        .font(.headline)
                        .foregroundStyle(.secondary)
                }
                Text(
                    verbatim:
                        "\(proposal.proposedStart.healthMesShortDateTime)–\(proposal.proposedEnd.healthMesShortTime)"
                )
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                if let outcome {
                    Text(verbatim: outcomeText(outcome))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(outcomeColor(outcome))
                }
            }
            Spacer()
            if outcome == nil, actionPrompt != nil {
                Button("No") { Task { await resolve(.decline) } }
                    .buttonStyle(.bordered)
                Button("Yes") { Task { await resolve(.accept) } }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.moss)
            }
            Button("Why?", action: onDetail)
                .buttonStyle(.link)
            MacWebDetailLink(url: webURL)
                .controlSize(.small)
        }
        .disabled(isWorking)
    }

    private var actionPrompt: String? {
        ProposalActionPresentation.exactPrompt(alert: alert)
    }

    private var webURL: URL? {
        if let alert, let url = MacWebLinks.decision(for: alert, pairing: pairing) {
            return url
        }
        return MacWebLinks.decision(for: proposal, pairing: pairing)
    }

    private func resolve(_ action: ProposalAction) async {
        isWorking = true
        defer { isWorking = false }
        outcome = await onResolve(action)
    }

    private func outcomeText(_ outcome: ProposalOutcome) -> String {
        switch outcome {
        case .accepted: return String(localized: "Approved · calendar sync pending")
        case .applied: return String(localized: "Applied to calendar")
        case .kept: return String(localized: "Kept as is")
        case .expired: return String(localized: "Expired · calendar unchanged")
        case .alreadyResolved(let status): return String(localized: "Already \(status)")
        case .failed: return String(localized: "Could not apply")
        }
    }

    private func outcomeColor(_ outcome: ProposalOutcome) -> Color {
        switch outcome {
        case .accepted: return MacHealthMesStyle.amber
        case .applied: return MacHealthMesStyle.moss
        case .kept: return .secondary
        case .expired: return MacHealthMesStyle.amber
        case .alreadyResolved: return MacHealthMesStyle.amber
        case .failed: return .red
        }
    }
}
