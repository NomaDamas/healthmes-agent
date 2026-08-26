import SwiftUI

struct MacDetailInspector: View {
    let detail: MacDetailContext
    let pairing: Pairing?
    let onClose: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack {
                    Text("Details")
                        .font(.headline)
                    Spacer()
                    Button(action: onClose) {
                        Image(systemName: "xmark")
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel(Text("Close details"))
                }

                detailContent
            }
            .padding(20)
        }
        .background(MacHealthMesStyle.canvas.opacity(0.45))
    }

    @ViewBuilder
    private var detailContent: some View {
        switch detail {
        case .energy(let payload):
            energyDetail(payload)
        case .block(let block, let timezone):
            blockDetail(block, timezone: timezone)
        case .proposal(let proposal, let alert):
            proposalDetail(proposal, alert: alert)
        case .alert(let alert):
            alertDetail(alert)
        case .goal(let goal):
            goalDetail(goal)
        case .task(let task):
            taskDetail(task)
        case .event(let event):
            eventDetail(event)
        case .decision(let decision):
            decisionDetail(decision)
        case .report(let report):
            reportDetail(report)
        }
    }

    private func energyDetail(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            inspectorTitle(
                eyebrow: "Now",
                title: "Today's capacity",
                subtitle: payload.energy.score.map { "Energy \($0)" } ?? "Not enough data"
            )
            MacEnergyCurveView(
                curve: payload.energy.curve24h,
                currentHour: currentHour(timezone: payload.timezone)
            )
            .frame(height: 96)
            MacMetadataRow(label: "Confidence", value: confidenceText(payload.energy.confidence))
            MacMetadataRow(label: "Timezone", value: payload.timezone)
            DisclosureGroup("How to read this") {
                Text(
                    "The default screen turns the score into one capacity sentence. The curve and confidence stay here so the number never dominates the decision."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
            }
        }
    }

    private func blockDetail(_ block: GlanceBlock, timezone: String) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            inspectorTitle(
                eyebrow: "Next",
                title: block.title ?? String(localized: "Untitled block"),
                subtitle: "\(block.start.healthMesShortDateTime)–\(block.end.healthMesShortTime)"
            )
            MacMetadataRow(label: "Source", value: block.source.rawValue.capitalized)
            MacMetadataRow(
                label: "Energy demand",
                value: block.energyDemand?.rawValue.capitalized ?? "Not set"
            )
            MacMetadataRow(label: "Calendar timezone", value: timezone)
            if let pairing {
                MacWebDetailLink(url: MacWebLinks.plan(pairing: pairing))
            }
        }
    }

    private func proposalDetail(_ proposal: ProposalItem, alert: AlertItem?) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            inspectorTitle(
                eyebrow: "Decision",
                title: alert?.decisionCard?.title
                    ?? alert?.decisionCard?.proposedAction
                    ?? String(localized: "Schedule change"),
                subtitle:
                    "\(proposal.proposedStart.healthMesShortDateTime)–\(proposal.proposedEnd.healthMesShortTime)"
            )

            if let observation = alert?.decisionCard?.observationShort {
                reasonSection("What changed", text: observation)
            } else if let summary = alert?.summary {
                reasonSection("What changed", text: summary)
            }
            if let evidence = alert?.decisionCard?.evidenceShort
                ?? alert.flatMap({ AlertNotificationContent.evidenceLine($0.evidence) })
            {
                reasonSection("Why now", text: evidence)
            }
            if let action = alert?.decisionCard?.proposedAction ?? alert?.proposal {
                reasonSection("Schedule impact", text: action)
            }

            DisclosureGroup("Decision path") {
                VStack(alignment: .leading, spacing: 8) {
                    decisionStep("Observe", "Health or schedule context changed")
                    decisionStep("Compare", "Personal baseline and nearby commitments")
                    decisionStep("Propose", "One reversible calendar action")
                    decisionStep("Confirm", "Nothing changes until you choose Yes")
                }
                .padding(.top, 8)
            }

            MacMetadataRow(label: "Status", value: proposal.status.rawValue.capitalized)
            MacWebDetailLink(url: proposalWebURL(proposal, alert: alert))
        }
    }

    private func alertDetail(_ alert: AlertItem) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            inspectorTitle(
                eyebrow: "Signal",
                title: alert.decisionCard?.title ?? alert.summary,
                subtitle: alert.firedAt.healthMesShortDateTime
            )
            reasonSection(
                "Observation",
                text: alert.decisionCard?.observationShort ?? alert.summary
            )
            if let evidence = alert.decisionCard?.evidenceShort
                ?? AlertNotificationContent.evidenceLine(alert.evidence)
            {
                reasonSection("Evidence", text: evidence)
            }
            if let proposal = alert.decisionCard?.proposedAction ?? alert.proposal {
                reasonSection("Proposed action", text: proposal)
            }
            MacMetadataRow(label: "Rule", value: alert.ruleId)
            MacWebDetailLink(url: MacWebLinks.decision(for: alert, pairing: pairing))
        }
    }

    private func goalDetail(_ goal: WeeklyGoalItem) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            inspectorTitle(eyebrow: "Goal", title: goal.title, subtitle: goal.weekStart)
            MacMetadataRow(label: "Status", value: goal.status.capitalized)
            MacMetadataRow(label: "Priority", value: String(goal.priority))
            Text(
                "HealthMes uses this goal as context when comparing tasks, available capacity and schedule trade-offs."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            if let pairing {
                MacWebDetailLink(url: MacWebLinks.plan(pairing: pairing))
            }
        }
    }

    private func taskDetail(_ task: TaskItem) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            inspectorTitle(
                eyebrow: "Task",
                title: task.title,
                subtitle: task.deadline?.healthMesShortDateTime ?? String(localized: "No deadline")
            )
            MacMetadataRow(label: "Status", value: task.status.capitalized)
            MacMetadataRow(label: "Energy demand", value: task.energyDemand.capitalized)
            MacMetadataRow(
                label: "Estimate",
                value: task.estimatedMinutes.map { "\($0) min" } ?? "Not set"
            )
            MacMetadataRow(label: "Source", value: task.source.capitalized)
            if let pairing {
                MacWebDetailLink(url: MacWebLinks.plan(pairing: pairing))
            }
        }
    }

    private func eventDetail(_ event: CalendarEventItem) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            inspectorTitle(
                eyebrow: "Calendar",
                title: event.summary ?? String(localized: "Untitled event"),
                subtitle: "\(event.startAt.healthMesShortDateTime)–\(event.endAt.healthMesShortTime)"
            )
            MacMetadataRow(label: "Calendar", value: event.calendarSource.capitalized)
            MacMetadataRow(
                label: "Created by",
                value: event.isAgentCreated ? "HealthMes" : "You or another calendar"
            )
            if let pairing {
                MacWebDetailLink(url: MacWebLinks.plan(pairing: pairing))
            }
        }
    }

    private func decisionDetail(_ decision: MacDecisionSummary) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            inspectorTitle(
                eyebrow: "Decision",
                title: decision.summary,
                subtitle: decision.createdAt.healthMesShortDateTime
            )
            MacMetadataRow(
                label: "Kind",
                value: decision.kind.rawValue.replacingOccurrences(of: "_", with: " ").capitalized
            )
            DisclosureGroup("What the web detail adds") {
                Text(
                    "Health evidence, schedule impact, alternatives, the complete decision path and related outcomes."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
            }
            if let pairing {
                MacWebDetailLink(url: MacWebLinks.decision(id: decision.id, pairing: pairing))
            }
        }
    }

    private func reportDetail(_ report: WeeklyReport) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            inspectorTitle(
                eyebrow: "This week",
                title: "\(report.weekStart) – \(report.weekEnd)",
                subtitle: "\(report.decisions.count) decisions recorded"
            )
            MacMetadataRow(
                label: "Average energy",
                value: report.energy.overallAvg.map(String.init) ?? "Not enough data"
            )
            MacMetadataRow(
                label: "Plan acceptance",
                value: report.schedule.acceptancePct.map { "\($0)%" } ?? "Not enough data"
            )
            MacMetadataRow(
                label: "Approved, sync pending",
                value: String(report.schedule.accepted)
            )
            MacMetadataRow(
                label: "Applied to calendar",
                value: String(report.schedule.pushed)
            )
            MacMetadataRow(label: "Insights", value: String(report.insights.count))
            MacWebDetailLink(
                url: MacWebLinks.weeklyReport(report.reportUrl, pairing: pairing)
            )
        }
    }

    private func inspectorTitle(eyebrow: String, title: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(verbatim: eyebrow.uppercased())
                .font(.caption.weight(.semibold))
                .tracking(1.2)
                .foregroundStyle(MacHealthMesStyle.moss)
            Text(verbatim: title)
                .font(.title2.weight(.semibold))
            Text(verbatim: subtitle)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private func reasonSection(_ title: LocalizedStringKey, text: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(verbatim: text)
                .font(.body)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private func decisionStep(_ title: LocalizedStringKey, _ text: LocalizedStringKey) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Circle()
                .fill(MacHealthMesStyle.moss)
                .frame(width: 6, height: 6)
                .padding(.top, 6)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption.weight(.semibold))
                Text(text)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func proposalWebURL(_ proposal: ProposalItem, alert: AlertItem?) -> URL? {
        if let alert, let url = MacWebLinks.decision(for: alert, pairing: pairing) {
            return url
        }
        return MacWebLinks.decision(for: proposal, pairing: pairing)
    }

    private func currentHour(timezone: String) -> Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }
}
