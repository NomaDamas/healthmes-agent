import AppKit
import SwiftUI

/// The menu bar popover: the whole glance briefing in one 360 pt column.
///
/// PLACEHOLDER VISUALS (docs/design/WATCH-NOTIFICATIONS.ko.md): layout,
/// colors, badge vocabulary and copy are engineering placeholders over the
/// stable contracts. Real plumbing: ETag-cheap refresh, §8.5-ordered alert
/// rows (observation/evidence/proposal, never reordered or invented),
/// working accept/decline against the live endpoints, decision links that
/// open the default browser.
struct BriefingPopoverView: View {
    @ObservedObject var store: GlanceStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if !store.isPaired {
                        notPairedSection
                    } else if let payload = store.payload {
                        headerSection(payload)
                        if let errorKey = store.errorKey {
                            errorLine(errorKey)
                        }
                        curveSection(payload)
                        blocksSection(payload)
                        if !store.pendingProposals.isEmpty {
                            proposalsSection
                        }
                        alertsSection(payload)
                        decisionSection(payload)
                    } else {
                        emptySection
                    }
                }
                .padding(14)
            }
            .frame(maxHeight: 480)
            Divider()
            footer
        }
        .frame(width: 360)
        .task {
            // Opening the popover revalidates (a 304 when nothing changed).
            await store.refresh(force: false)
        }
    }

    // MARK: - Sections

    private var notPairedSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("popover.notPaired.title", systemImage: "link.badge.plus")
                .font(.headline)
            Text("popover.notPaired.body")
                .font(.callout)
                .foregroundStyle(.secondary)
            SettingsLink {
                Text("popover.openSettings")
            }
            Text("settings.localFirst")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
    }

    private var emptySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let errorKey = store.errorKey {
                errorLine(errorKey)
            } else {
                Text("popover.loading")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func headerSection(_ payload: GlancePayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                .font(.system(size: 40, weight: .bold, design: .rounded))
                .accessibilityLabel(
                    Text("menubar.a11y.energy \(GlanceFormat.scoreText(payload.energy.score)) \(confidenceText(payload.energy.confidence))")
                )
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: confidenceText(payload.energy.confidence))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                updatedLine
            }
            Spacer()
            Button {
                Task { await store.refresh(force: true) }
            } label: {
                if store.isRefreshing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .accessibilityLabel(Text("popover.refresh"))
            .disabled(store.isRefreshing)
        }
    }

    @ViewBuilder
    private var updatedLine: some View {
        HStack(spacing: 4) {
            if let minutes = store.minutesSinceFetch {
                Text("popover.updated \(minutes)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            if store.isStale {
                Text("popover.stale")
                    .font(.caption2)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.orange.opacity(0.2), in: Capsule())
                    .foregroundStyle(.orange)
            }
        }
    }

    private func errorLine(_ key: String) -> some View {
        Label {
            Text(LocalizedStringKey(key))
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill")
        }
        .font(.caption)
        .foregroundStyle(.orange)
    }

    private func curveSection(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            sectionHeader("section.curve")
            MacEnergyCurveView(
                curve: payload.energy.curve24h,
                currentHour: currentHour(in: payload.timezone)
            )
            .frame(height: 56)
            HStack {
                Text(verbatim: "0h").font(.caption2).foregroundStyle(.tertiary)
                Spacer()
                Text(verbatim: "12h").font(.caption2).foregroundStyle(.tertiary)
                Spacer()
                Text(verbatim: "23h").font(.caption2).foregroundStyle(.tertiary)
            }
            .accessibilityHidden(true)
        }
    }

    private func blocksSection(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("section.blocks")
            if payload.nextBlocks.isEmpty {
                Text("blocks.empty")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(payload.nextBlocks.prefix(3).enumerated()), id: \.offset) { _, block in
                    HStack(spacing: 6) {
                        // Server-timezone rendering — the plan never lies
                        // about wall-clock time (GlanceFormat contract).
                        Text(verbatim: GlanceFormat.blockLine(block, timezone: payload.timezone))
                            .font(.callout)
                            .lineLimit(1)
                        if block.source == .proposal {
                            Text("block.source.proposal")
                                .font(.caption2)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(.blue.opacity(0.15), in: Capsule())
                                .foregroundStyle(.blue)
                        }
                        Spacer()
                    }
                }
            }
        }
    }

    private var proposalsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("section.proposals")
            ForEach(store.pendingProposals) { proposal in
                ProposalRowView(store: store, proposal: proposal, timezone: store.payload?.timezone)
            }
        }
    }

    private func alertsSection(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                sectionHeader("section.alerts")
                if payload.alerts.unresolvedCount > 0 {
                    Text("alerts.count \(payload.alerts.unresolvedCount)")
                        .font(.caption2)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(.red.opacity(0.15), in: Capsule())
                        .foregroundStyle(.red)
                }
                Spacer()
            }
            if store.alerts.isEmpty {
                Text("alerts.none")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(store.alerts.prefix(5)) { alert in
                    AlertRowView(alert: alert)
                }
            }
        }
    }

    @ViewBuilder
    private func decisionSection(_ payload: GlancePayload) -> some View {
        if let decision = payload.latestDecision, let url = URL(string: decision.url) {
            VStack(alignment: .leading, spacing: 4) {
                sectionHeader("section.decision")
                // Opens the default browser (tokenized read-only viewer link
                // minted by the paired instance itself).
                Link("decision.open", destination: url)
                    .font(.callout)
            }
        }
    }

    private var footer: some View {
        HStack {
            SettingsLink {
                Label("popover.openSettings", systemImage: "gearshape")
                    .labelStyle(.titleAndIcon)
            }
            .font(.caption)
            Spacer()
            Button {
                NSApp.terminate(nil)
            } label: {
                Text("footer.quit")
            }
            .font(.caption)
            .accessibilityLabel(Text("footer.quit"))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private func sectionHeader(_ key: LocalizedStringKey) -> some View {
        Text(key)
            .font(.caption)
            .fontWeight(.semibold)
            .foregroundStyle(.secondary)
            .textCase(.uppercase)
    }

    private func currentHour(in timezone: String) -> Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }
}

/// One pending proposal with the real §8.5 buttons: ✅ Apply → accept,
/// ❌ Keep as is → decline; ✏️ Adjust discloses the details (the desktop
/// adjust surface is the decision viewer / chat until a native editor
/// exists — placeholder noted).
struct ProposalRowView: View {
    @ObservedObject var store: GlanceStore
    let proposal: ProposalItem
    let timezone: String?

    @State private var outcome: ProposalOutcome?
    @State private var isWorking = false
    @State private var showDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(verbatim: timeRange)
                .font(.callout)
                .fontWeight(.medium)
            if let outcome {
                outcomeLine(outcome)
            } else {
                HStack(spacing: 8) {
                    Button {
                        Task { await act(.accept) }
                    } label: {
                        Text("proposal.apply")
                    }
                    .disabled(isWorking)
                    Button {
                        Task { await act(.decline) }
                    } label: {
                        Text("proposal.keep")
                    }
                    .disabled(isWorking)
                    Button {
                        showDetails.toggle()
                    } label: {
                        Text("proposal.adjust")
                    }
                    .buttonStyle(.borderless)
                    .font(.caption)
                }
                .controlSize(.small)
            }
            if showDetails {
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: "\(proposal.proposedStart.formatted()) → \(proposal.proposedEnd.formatted())")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text("proposal.adjust.hint")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
    }

    private var timeRange: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        formatter.timeZone = timezone.flatMap(TimeZone.init(identifier:)) ?? .current
        return "\(formatter.string(from: proposal.proposedStart))–\(formatter.string(from: proposal.proposedEnd))"
    }

    private func act(_ action: ProposalAction) async {
        isWorking = true
        defer { isWorking = false }
        outcome = await store.resolve(proposal, action: action)
    }

    @ViewBuilder
    private func outcomeLine(_ outcome: ProposalOutcome) -> some View {
        switch outcome {
        case .applied:
            Text("proposal.applied").font(.caption).foregroundStyle(.green)
        case .kept:
            Text("proposal.declined").font(.caption).foregroundStyle(.secondary)
        case .alreadyResolved(let status):
            Text("proposal.alreadyResolved \(status)").font(.caption).foregroundStyle(.orange)
        case .failed:
            Text("proposal.actionFailed").font(.caption).foregroundStyle(.red)
        }
    }
}

/// One alert in strict §8.5 line order: observation → evidence → proposal →
/// "why this?" link. Missing lines are dropped, never invented or reordered.
struct AlertRowView: View {
    let alert: AlertItem

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(verbatim: alert.summary)
                .font(.callout)
                .fontWeight(.medium)
                .lineLimit(2)
            if let evidence = AlertNotificationContent.evidenceLine(alert.evidence) {
                Text(verbatim: evidence)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if let proposal = alert.proposal, !proposal.isEmpty {
                Text(verbatim: proposal)
                    .font(.caption)
                    .lineLimit(2)
            }
            HStack {
                Text(verbatim: alert.firedAt.formatted(.relative(presentation: .named)))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
                if let urlString = alert.decisionUrl, let url = URL(string: urlString) {
                    Link("alert.why", destination: url)
                        .font(.caption)
                }
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}
