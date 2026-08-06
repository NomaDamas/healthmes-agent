import SwiftUI

struct MacDecisionsView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var dashboardStore: MacDashboardStore
    let onSelect: (MacDetailContext) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                MacPageHeader(
                    eyebrow: "Decisions",
                    title: "What changed, and why.",
                    subtitle: "The short answer comes first. Evidence and the full decision path stay in the inspector or web dashboard."
                )

                if !glanceStore.pendingProposals.isEmpty {
                    pendingSection
                }

                historySection
                signalsSection
            }
            .padding(32)
        }
    }

    private var pendingSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("Waiting for you", count: glanceStore.pendingProposals.count)
            ForEach(glanceStore.pendingProposals) { proposal in
                let alert = glanceStore.alerts.first(where: { $0.proposalId == proposal.id })
                Button {
                    onSelect(.proposal(proposal, alert: alert))
                } label: {
                    HStack(spacing: 14) {
                        Image(systemName: "hourglass")
                            .font(.title2)
                            .foregroundStyle(MacHealthMesStyle.amber)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(
                                verbatim:
                                    alert?.decisionCard?.title
                                    ?? alert?.proposal
                                    ?? String(localized: "Schedule decision")
                            )
                            .font(.headline)
                            .lineLimit(2)
                            Text(verbatim: proposal.proposedStart.healthMesShortDateTime)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text("Review")
                            .font(.callout.weight(.medium))
                            .foregroundStyle(MacHealthMesStyle.moss)
                        Image(systemName: "chevron.right")
                            .foregroundStyle(.tertiary)
                    }
                    .padding(16)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("History", count: dashboardStore.decisions.count)
            if dashboardStore.decisions.isEmpty {
                MacEmptyState(
                    systemImage: "clock.arrow.circlepath",
                    title: "No decisions recorded yet",
                    message: "Approved, declined and explained actions will appear here."
                )
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
            } else {
                ForEach(dashboardStore.decisions) { decision in
                    Button {
                        onSelect(.decision(decision))
                    } label: {
                        HStack(alignment: .top, spacing: 14) {
                            decisionIcon(decision.kind)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(verbatim: decision.summary)
                                    .font(.body.weight(.medium))
                                    .lineLimit(2)
                                Text(verbatim: decision.createdAt.healthMesShortDateTime)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(verbatim: decision.kind.rawValue.replacingOccurrences(of: "_", with: " "))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            Image(systemName: "chevron.right")
                                .foregroundStyle(.tertiary)
                        }
                        .padding(14)
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var signalsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            MacSectionHeader("Recent signals", count: glanceStore.alerts.count)
            if glanceStore.alerts.isEmpty {
                Text("No recent alerts.")
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 10)
            } else {
                ForEach(glanceStore.alerts.prefix(10)) { alert in
                    Button {
                        onSelect(.alert(alert))
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "waveform.path.ecg")
                                .foregroundStyle(MacHealthMesStyle.moss)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(verbatim: alert.summary)
                                    .font(.body.weight(.medium))
                                if let evidence = alert.decisionCard?.evidenceShort {
                                    Text(verbatim: evidence)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                }
                            }
                            Spacer()
                            Text(alert.firedAt, style: .relative)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(13)
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func decisionIcon(_ kind: MacDecisionKind) -> some View {
        let symbol: String
        switch kind {
        case .scheduleChange: symbol = "calendar.badge.clock"
        case .alert: symbol = "bell.badge"
        case .insight: symbol = "lightbulb"
        case .capture: symbol = "tray.and.arrow.down"
        }
        return Image(systemName: symbol)
            .font(.title3)
            .foregroundStyle(MacHealthMesStyle.moss)
            .frame(width: 26)
    }
}
