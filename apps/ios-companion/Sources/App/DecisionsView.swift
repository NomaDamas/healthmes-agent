import SwiftUI

struct DecisionHistoryItem: Identifiable {
    let proposal: ProposalItem
    let alert: AlertItem?

    var id: UUID { proposal.id }

    var title: String {
        alert?.decisionCard?.title
            ?? alert?.summary
            ?? String(localized: "Schedule decision")
    }

    var exactWebURL: URL? {
        alert?.exactDecisionURL
    }
}

@MainActor
final class DecisionsModel: ObservableObject {
    @Published var alerts: [AlertItem] = []
    @Published var proposals: [ProposalItem] = []
    @Published var records: [ProductDecisionSummary] = []
    @Published var message: String?
    @Published var busyProposalIDs: Set<UUID> = []

    private let api = HealthMesAPI()
    private var refreshGate = LatestRefreshGate()
    private var resolutionTokens: [UUID: UUID] = [:]

    var pending: [PendingDecision] {
        PendingDecision.correlate(alerts: alerts, proposals: proposals)
    }

    var history: [DecisionHistoryItem] {
        let alertsByDecision = Dictionary(
            alerts.compactMap { alert -> (UUID, AlertItem)? in
                guard let id = alert.exactDecisionRecordID else { return nil }
                return (id, alert)
            },
            uniquingKeysWith: { first, _ in first }
        )
        return proposals
            .filter { $0.status != .proposed }
            .sorted { ($0.decidedAt ?? $0.proposedStart) > ($1.decidedAt ?? $1.proposedStart) }
            .map {
                DecisionHistoryItem(
                    proposal: $0,
                    alert: $0.decisionRecordId.flatMap { alertsByDecision[$0] }
                )
            }
    }

    func refresh() async {
        let refreshID = refreshGate.begin()
        guard let pairingSnapshot = PairingStore.shared.load() else {
            message = String(localized: "Not paired — open Settings.")
            return
        }

        async let alertsResult = productRefreshResult {
            try await api.listAlerts(pairing: pairingSnapshot, hours: 168)
        }
        async let proposalsResult = productRefreshResult {
            try await api.listProposals(pairing: pairingSnapshot)
        }
        async let recordsResult = productRefreshResult {
            try await api.listDecisionRecords(pairing: pairingSnapshot)
        }
        let results = await (alertsResult, proposalsResult, recordsResult)
        guard
            refreshGate.isCurrent(refreshID),
            PairingStore.shared.load() == pairingSnapshot
        else { return }

        var errors: [String] = []
        switch results.0 {
        case .success(let page):
            alerts = page.data
        case .failure(let error):
            errors.append("Alerts: \(BriefingHomeModel.describe(error))")
        }
        switch results.1 {
        case .success(let page):
            proposals = page.data
        case .failure(let error):
            errors.append("Proposals: \(BriefingHomeModel.describe(error))")
        }
        switch results.2 {
        case .success(let page):
            records = page.data
        case .failure(let error):
            errors.append("History: \(BriefingHomeModel.describe(error))")
        }
        message = errors.isEmpty ? nil : errors.joined(separator: "\n")
    }

    func resolve(
        _ decision: PendingDecision,
        action: ProposalAction,
        pairing: Pairing? = nil
    ) async {
        guard let pairingSnapshot = pairing ?? PairingStore.shared.load() else {
            message = String(localized: "Not paired — open Settings.")
            return
        }
        let resolutionToken = UUID()
        resolutionTokens[decision.id] = resolutionToken
        busyProposalIDs.insert(decision.id)
        defer {
            if resolutionTokens[decision.id] == resolutionToken {
                resolutionTokens[decision.id] = nil
                busyProposalIDs.remove(decision.id)
            }
        }
        do {
            _ = try await api.resolveProposal(
                decision.proposal,
                action: action,
                pairing: pairingSnapshot
            )
            guard resolutionIsCurrent(
                decision.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            await refresh()
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            guard resolutionIsCurrent(
                decision.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            message = String(
                format: String(localized: "Already resolved (%@)."),
                error.alreadyResolvedStatus ?? "resolved"
            )
            await refresh()
        } catch let error as HealthMesAPIError where error.isProposalExpired {
            guard resolutionIsCurrent(
                decision.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            message = String(localized: "Expired · calendar unchanged")
            await refresh()
        } catch {
            guard resolutionIsCurrent(
                decision.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            message = BriefingHomeModel.describe(error)
        }
    }

    func resetForPairingChange() {
        _ = refreshGate.begin()
        resolutionTokens.removeAll()
        alerts = []
        proposals = []
        records = []
        message = nil
        busyProposalIDs = []
    }

    private func resolutionIsCurrent(
        _ proposalID: UUID,
        token: UUID,
        pairing: Pairing
    ) -> Bool {
        resolutionTokens[proposalID] == token
            && PairingStore.shared.load() == pairing
    }
}

struct DecisionsView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model = DecisionsModel()

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 14) {
                pendingCard
                historyCard
                if let message = model.message {
                    Label {
                        Text(verbatim: message)
                    } icon: {
                        Image(systemName: "info.circle")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 4)
                }
            }
            .padding(16)
        }
        .background(HealthMesVisualStyle.canvas.ignoresSafeArea())
        .navigationTitle(Text("Decisions"))
        .refreshable { await model.refresh() }
        .task {
            if model.alerts.isEmpty && model.proposals.isEmpty && model.records.isEmpty {
                await model.refresh()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            model.resetForPairingChange()
            Task { await model.refresh() }
        }
    }

    private var pendingCard: some View {
        ProductCard(
            kicker: "Pending",
            systemImage: "questionmark.circle",
            accent: HealthMesVisualStyle.proposal
        ) {
            if model.pending.isEmpty {
                Text("No pending decisions")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(model.pending) { decision in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(verbatim: decision.prompt)
                            .font(.body.weight(.semibold))
                        if let reason = decision.reason {
                            Text(verbatim: reason)
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                        HStack(spacing: 10) {
                            Button {
                                Task { await model.resolve(decision, action: .decline) }
                            } label: {
                                Text("No").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            Button {
                                Task { await model.resolve(decision, action: .accept) }
                            } label: {
                                Text("Yes").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(HealthMesVisualStyle.decision)
                        }
                        .disabled(model.busyProposalIDs.contains(decision.id))
                        Button {
                            router.openAgentVoice(
                                prefill: "I want a different option for \(decision.primaryActionText). "
                            )
                        } label: {
                            Label("Speak", systemImage: "microphone.fill")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .tint(HealthMesVisualStyle.brand)
                        .disabled(model.busyProposalIDs.contains(decision.id))
                        .accessibilityHint(
                            Text("Speak a different instruction, review it, then confirm")
                        )
                        if let url = decision.exactWebURL {
                            Button {
                                router.openDecision(url)
                            } label: {
                                Label("View on web", systemImage: "safari")
                            }
                            .font(.footnote.weight(.semibold))
                        }
                    }
                    if decision.id != model.pending.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private var historyCard: some View {
        ProductCard(
            kicker: "History",
            systemImage: "clock.arrow.circlepath",
            accent: HealthMesVisualStyle.data
        ) {
            if model.records.isEmpty && model.history.isEmpty {
                Text("No decisions recorded")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(model.records.prefix(20)) { record in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: decisionIcon(record.kind))
                            .foregroundStyle(HealthMesVisualStyle.data)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: record.summary)
                                .font(.body.weight(.medium))
                            Text(verbatim: decisionKindLabel(record.kind))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(record.createdAt, style: .relative)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                        Spacer()
                        if let pairing = PairingStore.shared.load() {
                            Button {
                                router.openDecision(
                                    ViewerURL.make(
                                        pairing: pairing,
                                        pathComponents: [
                                            "decisions",
                                            record.id.uuidString.lowercased(),
                                        ]
                                    )
                                )
                            } label: {
                                Image(systemName: "safari")
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(Text("View on web"))
                        }
                    }
                    Divider()
                }
                ForEach(model.history.prefix(20)) { item in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: ProposalStatusPresentation.systemImage(for: item.proposal.status))
                            .foregroundStyle(statusColor(item.proposal.status))
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: item.title)
                                .font(.body.weight(.medium))
                            Text(verbatim: ProposalStatusPresentation.label(for: item.proposal.status))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if let decidedAt = item.proposal.decidedAt {
                                Text(decidedAt, style: .relative)
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        Spacer()
                        if let url = item.exactWebURL {
                            Button {
                                router.openDecision(url)
                            } label: {
                                Image(systemName: "safari")
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(Text("View on web"))
                        }
                    }
                    if item.id != model.history.prefix(20).last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func decisionIcon(_ kind: ProductDecisionKind) -> String {
        switch kind {
        case .scheduleChange: return "calendar.badge.clock"
        case .alert: return "bell.badge"
        case .insight: return "lightbulb"
        case .capture: return "waveform"
        }
    }

    private func decisionKindLabel(_ kind: ProductDecisionKind) -> String {
        switch kind {
        case .scheduleChange: return String(localized: "Schedule change")
        case .alert: return String(localized: "Alert")
        case .insight: return String(localized: "Insight")
        case .capture: return String(localized: "Capture")
        }
    }

    private func statusColor(_ status: ProposalStatus) -> Color {
        switch status {
        case .accepted: return .orange
        case .pushed: return .green
        case .declined, .invalidated: return .secondary
        case .proposed: return .blue
        }
    }
}
