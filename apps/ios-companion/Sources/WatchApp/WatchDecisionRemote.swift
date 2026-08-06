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

enum WatchWellnessAvailability: Equatable {
    case current
    case stale
    case unpaired
    case unauthorized
    case offline
    case contractError

    var label: String {
        switch self {
        case .current: return String(localized: "Current")
        case .stale: return String(localized: "Cached · may be old")
        case .unpaired: return String(localized: "Pair with iPhone")
        case .unauthorized: return String(localized: "Connection needs attention")
        case .offline: return String(localized: "Offline")
        case .contractError: return String(localized: "App update needed")
        }
    }

    var systemImage: String {
        switch self {
        case .current: return "checkmark.circle"
        case .stale: return "clock.arrow.circlepath"
        case .unpaired: return "iphone.gen2"
        case .unauthorized: return "key.slash"
        case .offline: return "wifi.slash"
        case .contractError: return "exclamationmark.arrow.triangle.2.circlepath"
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
    @Published var energyScore: Int?
    @Published var wellnessImpact: String?
    @Published var availability: WatchWellnessAvailability = .offline

    private let api = HealthMesAPI()
    private let glanceClient = GlanceClient()

    func refresh() async {
        guard PairingStore.shared.load() != nil else {
            result = .offline
            availability = .unpaired
            glanceLine = nil
            energyScore = nil
            wellnessImpact = String(localized: "Connect HealthMes on iPhone first.")
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
            availability = Self.availability(for: error)
        }

        do {
            let glance = try await glanceClient.fetch()
            energyScore = glance.payload.energy.score
            wellnessImpact = Self.impact(for: glance.payload.energy.score)
            glanceLine = GlanceFormat.nextBlockLine(glance.payload)
            availability = .current
        } catch {
            if let cached = GlanceSnapshotCache.shared.decodedPayload() {
                energyScore = cached.energy.score
                wellnessImpact = Self.impact(for: cached.energy.score)
                glanceLine = GlanceFormat.nextBlockLine(cached)
                availability = .stale
            } else {
                energyScore = nil
                wellnessImpact = String(localized: "Not enough health data to adjust the plan.")
                availability = Self.availability(for: error)
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

    private static func availability(for error: Error) -> WatchWellnessAvailability {
        switch error {
        case GlanceClientError.notPaired, HealthMesAPIError.notPaired:
            return .unpaired
        case GlanceClientError.unauthorized, HealthMesAPIError.unauthorized:
            return .unauthorized
        case GlanceClientError.decoding, HealthMesAPIError.decoding:
            return .contractError
        default:
            return .offline
        }
    }

    private static func impact(for score: Int?) -> String {
        guard let score else {
            return String(localized: "Not enough data to change today's plan.")
        }
        if score < 45 {
            return String(localized: "Protect recovery before high-energy work.")
        }
        if score < 70 {
            return String(localized: "Save capacity for one important block.")
        }
        return String(localized: "Use this capacity on the highest-priority goal.")
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
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text("CAPACITY")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let energyScore = model.energyScore {
                        Text(verbatim: "\(energyScore)")
                            .font(.system(.title2, design: .rounded).bold())
                            .accessibilityLabel(Text("Cognitive energy"))
                            .accessibilityValue(Text(verbatim: "\(energyScore)"))
                    }
                }

                Text(verbatim: model.wellnessImpact ?? String(localized: "Checking body-to-plan impact…"))
                    .font(.headline)
                    .lineLimit(3)
                    .minimumScaleFactor(0.78)
                    .fixedSize(horizontal: false, vertical: true)

                if let glanceLine = model.glanceLine {
                    Label {
                        Text(verbatim: glanceLine)
                            .lineLimit(2)
                    } icon: {
                        Image(systemName: "calendar")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                Label(
                    model.availability.label,
                    systemImage: model.availability.systemImage
                )
                .font(.caption2)
                .foregroundStyle(model.availability == .current ? .green : .orange)

                Button("Refresh") {
                    Task { await model.refresh() }
                }
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity)
            }
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
