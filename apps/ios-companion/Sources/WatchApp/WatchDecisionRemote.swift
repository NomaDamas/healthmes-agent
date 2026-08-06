import SwiftUI

enum WatchDecisionResult: Equatable {
    case approved
    case applied
    case declined
    case alreadyApproved
    case alreadyDeclined
    case expired
    case offline

    var title: String {
        switch self {
        case .approved:
            return String(localized: "Yes recorded")
        case .applied:
            return ProposalStatusPresentation.label(for: .pushed)
        case .declined:
            return String(localized: "No recorded")
        case .alreadyApproved:
            return String(localized: "Already approved")
        case .alreadyDeclined:
            return String(localized: "Already declined")
        case .expired:
            return String(localized: "Decision expired")
        case .offline:
            return String(localized: "Not sent")
        }
    }

    var detail: String {
        switch self {
        case .approved:
            return ProposalStatusPresentation.detail(for: .accepted)
        case .applied:
            return ProposalStatusPresentation.detail(for: .pushed)
        case .declined:
            return String(localized: "Your calendar stays unchanged.")
        case .alreadyApproved:
            return String(localized: "Another device already approved it.")
        case .alreadyDeclined:
            return String(localized: "Another device already declined it.")
        case .expired:
            return String(localized: "The decision window closed without a change.")
        case .offline:
            return String(localized: "Reconnect, then try again.")
        }
    }

    var systemImage: String {
        switch self {
        case .approved, .applied, .alreadyApproved:
            return "checkmark.circle.fill"
        case .declined, .alreadyDeclined:
            return "xmark.circle.fill"
        case .expired:
            return "clock.badge.xmark"
        case .offline:
            return "wifi.slash"
        }
    }

    var tint: Color {
        switch self {
        case .approved, .applied, .alreadyApproved:
            return .green
        case .declined, .alreadyDeclined:
            return .secondary
        case .expired, .offline:
            return .orange
        }
    }
}

@MainActor
final class WatchDecisionRemoteModel: ObservableObject {
    @Published var decision: PendingDecision?
    @Published var isLoading = false
    @Published var applyingAction: ProposalAction?
    @Published var result: WatchDecisionResult?
    @Published var glanceLine: String?

    private let api = HealthMesAPI()
    private let glanceClient = GlanceClient()

    func refresh() async {
        guard PairingStore.shared.load() != nil else {
            result = .offline
            glanceLine = String(localized: "Pair with the iPhone app.")
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            async let alertsCall = api.listAlerts(hours: 24)
            async let proposalsCall = api.listProposals(status: .proposed)
            let (alerts, proposals) = try await (alertsCall, proposalsCall)
            decision = PendingDecision.correlate(
                alerts: alerts.data,
                proposals: proposals.data
            ).first
            result = nil
        } catch {
            decision = nil
            result = .offline
        }

        do {
            let glance = try await glanceClient.fetch()
            glanceLine = GlanceFormat.nextBlockLine(glance.payload)
                ?? GlanceFormat.energyLine(glance.payload)
        } catch {
            if let cached = GlanceSnapshotCache.shared.decodedPayload() {
                glanceLine = GlanceFormat.nextBlockLine(cached)
                    ?? GlanceFormat.energyLine(cached)
            }
        }
    }

    func resolve(_ action: ProposalAction) async {
        guard let decision else { return }
        applyingAction = action
        result = nil
        defer { applyingAction = nil }
        do {
            let current = try await api.getProposal(decision.id)
            guard current.isActionable else {
                result = Self.result(for: current)
                self.decision = nil
                return
            }
            let resolved = try await api.resolveProposal(
                current,
                action: action,
                surface: "apple_watch_app"
            )
            result = Self.result(for: resolved.status)
            self.decision = nil
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            result = Self.result(for: error.alreadyResolvedStatus)
            self.decision = nil
        } catch let error as HealthMesAPIError where error.isProposalExpired {
            result = .expired
            self.decision = nil
        } catch {
            result = .offline
        }
    }

    private static func result(for proposal: ProposalItem) -> WatchDecisionResult {
        if proposal.status == .proposed {
            return .expired
        }
        return result(for: proposal.status.rawValue)
    }

    private static func result(for rawStatus: String?) -> WatchDecisionResult {
        guard let rawStatus, let status = ProposalStatus(rawValue: rawStatus) else {
            return .expired
        }
        return result(for: status, alreadyResolved: true)
    }

    private static func result(
        for status: ProposalStatus,
        alreadyResolved: Bool = false
    ) -> WatchDecisionResult {
        switch status {
        case .accepted:
            return alreadyResolved ? .alreadyApproved : .approved
        case .pushed:
            return .applied
        case .declined:
            return alreadyResolved ? .alreadyDeclined : .declined
        case .proposed, .invalidated:
            return .expired
        }
    }
}

struct WatchDecisionRemoteView: View {
    @ObservedObject var model: WatchDecisionRemoteModel
    @State private var detail: WatchDecisionDetail?

    var body: some View {
        Group {
            if let decision = model.decision {
                pending(decision)
            } else if let result = model.result {
                resultView(result)
            } else if model.isLoading {
                ProgressView()
            } else {
                glance
            }
        }
        .sheet(item: $detail) { detail in
            WatchDecisionDetailView(detail: detail)
        }
    }

    private func pending(_ decision: PendingDecision) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(decision.reason ?? String(localized: "Health-based plan"))
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Text(verbatim: decision.prompt)
                .font(.headline)
                .lineLimit(2)
                .minimumScaleFactor(0.82)
                .fixedSize(horizontal: false, vertical: true)

            Text(verbatim: ProposalFormat.compactWindowLine(decision.proposal))
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.78)

            HStack(spacing: 7) {
                decisionButton(
                    title: "No",
                    image: "xmark",
                    action: .decline,
                    prominent: false
                )
                decisionButton(
                    title: "Yes",
                    image: "checkmark",
                    action: .accept,
                    prominent: true
                )
            }

            Button {
                detail = WatchDecisionDetail(decision: decision)
            } label: {
                Text("Why?")
                    .font(.caption2)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
        }
    }

    private func decisionButton(
        title: LocalizedStringKey,
        image: String,
        action: ProposalAction,
        prominent: Bool
    ) -> some View {
        Group {
            if prominent {
                decisionButtonContent(title: title, image: image, action: action)
                    .buttonStyle(.borderedProminent)
                    .tint(.green)
            } else {
                decisionButtonContent(title: title, image: image, action: action)
                    .buttonStyle(.bordered)
                    .tint(.secondary)
            }
        }
    }

    private func decisionButtonContent(
        title: LocalizedStringKey,
        image: String,
        action: ProposalAction
    ) -> some View {
        Button {
            Task { await model.resolve(action) }
        } label: {
            Group {
                if model.applyingAction == action {
                    ProgressView()
                } else {
                    Label(title, systemImage: image)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 38)
        }
        .disabled(model.applyingAction != nil)
    }

    private func resultView(_ result: WatchDecisionResult) -> some View {
        VStack(spacing: 8) {
            Image(systemName: result.systemImage)
                .font(.title2)
                .foregroundStyle(result.tint)
            Text(verbatim: result.title)
                .font(.headline)
                .multilineTextAlignment(.center)
            Text(verbatim: result.detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(2)
            Button("Refresh") {
                Task { await model.refresh() }
            }
            .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity)
    }

    private var glance: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("HealthMes", systemImage: "waveform.path.ecg")
                .font(.headline)
            Text("No decision waiting")
                .font(.subheadline.weight(.semibold))
            if let glanceLine = model.glanceLine {
                Text(verbatim: glanceLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Button("Refresh") {
                Task { await model.refresh() }
            }
            .buttonStyle(.bordered)
        }
    }
}

private extension WatchDecisionDetail {
    init(decision: PendingDecision) {
        self.init(
            prompt: decision.prompt,
            target: ProposalFormat.windowLine(decision.proposal),
            observation: decision.card?.observationShort ?? decision.alert?.summary,
            evidence: decision.card?.evidenceShort,
            action: decision.card?.proposedAction,
            before: decision.card?.before,
            after: decision.card?.after ?? decision.proposal.proposedStart,
            endsAt: decision.card?.endsAt ?? decision.proposal.proposedEnd,
            expiresAt: decision.card?.expiresAt
        )
    }
}
