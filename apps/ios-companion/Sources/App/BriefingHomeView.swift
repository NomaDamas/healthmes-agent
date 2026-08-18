import SwiftUI
import WidgetKit

/// Issue #108 Today hierarchy: one current state, one next block, one
/// decision, then progressive detail. The same real APIs and proposal
/// actions remain underneath the simplified surface.
struct BriefingHomeView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model = BriefingHomeModel()
    @State private var whyExpanded = false

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 14) {
                nowCard
                nextCard
                decisionCard

                if let banner = model.proposalBanner {
                    Label {
                        Text(verbatim: banner)
                    } icon: {
                        Image(systemName: "checkmark.circle")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 4)
                }
            }
            .padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle(Text("Today"))
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

    @ViewBuilder
    private var nowCard: some View {
        ProductCard(kicker: "Now", systemImage: "waveform.path.ecg") {
            if let payload = model.snapshot?.payload {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Cognitive energy")
                            .font(.title3.weight(.semibold))
                        Spacer()
                        Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                            .font(.system(.title, design: .rounded).weight(.bold))
                            .accessibilityLabel(Text("Cognitive energy score"))
                        if model.isStale {
                            Image(systemName: "clock.arrow.circlepath")
                                .foregroundStyle(.secondary)
                                .accessibilityLabel(Text("Cached"))
                        }
                    }
                    HStack {
                        ConfidenceBadge(rawLevel: payload.energy.confidence.rawValue)
                        Text("Updated \(model.lastUpdatedText)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text("Open Plan for the day; details stay out of the way until you ask.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } else if let error = model.glanceError {
                offlineRow(message: error)
            } else {
                loadingRow
            }
        }
    }

    @ViewBuilder
    private var nextCard: some View {
        ProductCard(kicker: "Next", systemImage: "calendar") {
            if let payload = model.snapshot?.payload, let block = payload.nextBlocks.first {
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(verbatim: block.title ?? String(localized: "Scheduled block"))
                            .font(.title3.weight(.semibold))
                            .lineLimit(2)
                        Text(verbatim: blockTime(block, timezone: payload.timezone))
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if block.source == .proposal {
                        Image(systemName: "sparkles")
                            .foregroundStyle(Color.accentColor)
                            .accessibilityLabel(Text("HealthMes proposal"))
                    }
                }
                .accessibilityElement(children: .combine)
            } else if model.snapshot != nil {
                Text("No upcoming blocks")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                loadingRow
            }
        }
    }

    @ViewBuilder
    private var decisionCard: some View {
        ProductCard(kicker: "Decision", systemImage: "checkmark.circle") {
            if let decision = model.pendingDecisions.first {
                VStack(alignment: .leading, spacing: 12) {
                    Text(verbatim: decision.prompt)
                        .font(.title3.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)

                    if let reason = decision.reason {
                        Text(verbatim: reason)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }

                    Text(verbatim: ProposalFormat.windowLine(decision.proposal))
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.secondary)

                    HStack(spacing: 10) {
                        Button {
                            Task { await model.resolve(decision.proposal, action: .decline) }
                        } label: {
                            Label("No", systemImage: "xmark")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)

                        Button {
                            Task { await model.resolve(decision.proposal, action: .accept) }
                        } label: {
                            Label("Yes", systemImage: "checkmark")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(HealthMesVisualStyle.capacity)
                    }
                    .disabled(model.busyProposalIDs.contains(decision.id))

                    if model.busyProposalIDs.contains(decision.id) {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Applying…")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    }

                    HStack(spacing: 16) {
                        Button {
                            withAnimation(.easeInOut(duration: 0.2)) {
                                whyExpanded.toggle()
                            }
                        } label: {
                            Label("Why this?", systemImage: "questionmark.circle")
                        }
                        .buttonStyle(.plain)

                        if let webURL = decision.exactWebURL {
                            Button {
                                router.openDecision(webURL)
                            } label: {
                                Label("View on web", systemImage: "safari")
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .font(.footnote.weight(.semibold))

                    if whyExpanded {
                        VStack(alignment: .leading, spacing: 6) {
                            if let evidence = decision.card?.evidenceShort {
                                Text(verbatim: evidence)
                            }
                            if let action = decision.card?.proposedAction {
                                Text(verbatim: action)
                            }
                            if decision.card?.evidenceShort == nil
                                && decision.card?.proposedAction == nil
                            {
                                Text("Open the exact web decision for the full reasoning.")
                            }
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                }
            } else if model.pendingProposals.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Nothing needs a decision")
                        .font(.title3.weight(.semibold))
                    Text("HealthMes will ask only when an action can change your plan.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Schedule proposal")
                        .font(.title3.weight(.semibold))
                    Text(verbatim: ProposalFormat.windowLine(model.pendingProposals[0]))
                    Button {
                        router.openProposalDetail(model.pendingProposals[0].id)
                    } label: {
                        Text("Open details")
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    private func blockTime(_ block: GlanceBlock, timezone: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = .autoupdatingCurrent
        formatter.timeZone = TimeZone(identifier: timezone) ?? .autoupdatingCurrent
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return "\(formatter.string(from: block.start))–\(formatter.string(from: block.end))"
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

struct ProductCard<Content: View>: View {
    let kicker: LocalizedStringKey
    let systemImage: String
    @ViewBuilder let content: Content

    init(
        kicker: LocalizedStringKey,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) {
        self.kicker = kicker
        self.systemImage = systemImage
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(kicker, systemImage: systemImage)
                .font(.caption.weight(.bold))
                .foregroundStyle(HealthMesVisualStyle.capacityDeep)
                .textCase(.uppercase)
                .tracking(0.7)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(17)
        .healthMesSurface()
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
            if let url = alert.exactDecisionURL {
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
