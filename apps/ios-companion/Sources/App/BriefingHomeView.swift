import SwiftUI
import WidgetKit

/// In-app briefing home (issue #10): energy score + 24 h curve, next blocks,
/// pending schedule proposals with REAL accept/decline actions, the
/// unresolved-alerts list (`GET /v1/alerts`) rendered in §8.5 grammar lines,
/// and the latest decision link. Pull-to-refresh re-polls everything; the
/// glance leg stays ETag-cheap (304 when unchanged).
struct BriefingHomeView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model = BriefingHomeModel()

    var body: some View {
        List {
            energySection
            blocksSection
            proposalsSection
            alertsSection
            decisionSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle(Text("Home"))
        .refreshable {
            await model.refresh()
            await RefreshCoordinator.shared.sync(isForeground: true)
        }
        .task {
            if model.snapshot == nil {
                await model.refresh()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            Task { await model.refresh() }
        }
    }

    // MARK: Energy

    @ViewBuilder
    private var energySection: some View {
        Section {
            if let payload = model.snapshot?.payload {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                            .font(.system(.largeTitle, design: .rounded).weight(.bold))
                            .accessibilityLabel(Text("Cognitive energy score"))
                            .accessibilityValue(
                                Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                            )
                        VStack(alignment: .leading) {
                            Text("Cognitive energy")
                                .font(.subheadline)
                            ConfidenceBadge(rawLevel: payload.energy.confidence.rawValue)
                        }
                        Spacer()
                        if model.isStale {
                            Label("Cached", systemImage: "clock.arrow.circlepath")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    EnergyCurveView(curve: payload.energy.curve24h, timezone: payload.timezone)
                    Text("Updated \(model.lastUpdatedText)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            } else if let error = model.glanceError {
                offlineRow(message: error)
            } else {
                loadingRow
            }
        } header: {
            Text("Today")
        }
    }

    // MARK: Next blocks

    @ViewBuilder
    private var blocksSection: some View {
        if let payload = model.snapshot?.payload {
            Section {
                if payload.nextBlocks.isEmpty {
                    Text("No upcoming blocks")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(Array(payload.nextBlocks.enumerated()), id: \.offset) { _, block in
                        HStack {
                            Text(verbatim: GlanceFormat.blockLine(block, timezone: payload.timezone))
                                .font(.body)
                            Spacer()
                            if block.source == .proposal {
                                Text("proposal")
                                    .font(.caption2)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Color.accentColor.opacity(0.15), in: Capsule())
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            } header: {
                Text("Next blocks")
            }
        }
    }

    // MARK: Proposals (real §8.5 buttons)

    @ViewBuilder
    private var proposalsSection: some View {
        if !model.pendingProposals.isEmpty || model.proposalBanner != nil {
            Section {
                if let banner = model.proposalBanner {
                    Text(verbatim: banner)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                ForEach(model.pendingProposals) { proposal in
                    ProposalRowView(
                        proposal: proposal,
                        busy: model.busyProposalIDs.contains(proposal.id),
                        onApply: { Task { await model.resolve(proposal, action: .accept) } },
                        onKeep: { Task { await model.resolve(proposal, action: .decline) } },
                        onAdjust: { router.openProposalDetail(proposal.id) }
                    )
                }
            } header: {
                Text("Pending proposals")
            } footer: {
                Text("Apply moves the plan; Keep as is declines. Adjust opens the details.")
            }
        }
    }

    // MARK: Alerts

    private var alertsSection: some View {
        Section {
            if model.alerts.isEmpty {
                if let error = model.alertsError {
                    offlineRow(message: error)
                } else {
                    Text("No recent alerts")
                        .foregroundStyle(.secondary)
                }
            } else {
                ForEach(model.alerts) { alert in
                    AlertRowView(alert: alert) { url in
                        router.openDecision(url)
                    }
                }
            }
        } header: {
            HStack {
                Text("Alerts · last 24h")
                Spacer()
                if !model.alerts.isEmpty {
                    Text(verbatim: "\(model.alerts.count)")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: Latest decision

    @ViewBuilder
    private var decisionSection: some View {
        if let decision = model.snapshot?.payload.latestDecision,
            let url = URL(string: decision.url)
        {
            Section {
                Button {
                    router.openDecision(url)
                } label: {
                    Label("Latest decision — why this?", systemImage: "questionmark.circle")
                }
                .accessibilityHint(Text("Opens the decision viewer"))
            } header: {
                Text("Decisions")
            }
        }
    }

    // MARK: Shared rows

    private var loadingRow: some View {
        HStack {
            ProgressView()
            Text("Loading briefing…")
                .foregroundStyle(.secondary)
        }
    }

    private func offlineRow(message: String) -> some View {
        Label {
            Text(verbatim: message)
        } icon: {
            Image(systemName: "wifi.exclamationmark")
        }
        .foregroundStyle(.secondary)
        .font(.footnote)
    }
}

/// Confidence chip; wording placeholder (expert-owned, Q5).
struct ConfidenceBadge: View {
    let rawLevel: String

    var body: some View {
        Text(verbatim: rawLevel)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(background, in: Capsule())
            .accessibilityLabel(Text("Confidence"))
            .accessibilityValue(Text(verbatim: rawLevel))
    }

    private var background: Color {
        switch rawLevel {
        case "high": return Color.green.opacity(0.2)
        case "medium", "med": return Color.yellow.opacity(0.25)
        case "low": return Color.orange.opacity(0.25)
        default: return Color.secondary.opacity(0.15)
        }
    }
}

/// One alert in §8.5 grammar order: observation (summary), evidence line,
/// proposal line, "why this?" link. Lines the payload does not carry are
/// dropped, never invented.
struct AlertRowView: View {
    let alert: AlertItem
    let onOpenDecision: (URL) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(verbatim: alert.summary)
                    .font(.headline)
                Spacer()
                Text(alert.firedAt, style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let evidence = AlertNotificationContent.evidenceLine(alert.evidence) {
                Text(verbatim: evidence)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let proposal = alert.proposal, !proposal.isEmpty {
                Text(verbatim: proposal)
                    .font(.footnote)
            }
            if let raw = alert.decisionUrl, let url = URL(string: raw) {
                Button {
                    onOpenDecision(url)
                } label: {
                    Text("Why this?")
                        .font(.footnote.weight(.medium))
                }
                .buttonStyle(.borderless)
                .accessibilityHint(Text("Opens the decision viewer"))
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .contain)
    }
}
