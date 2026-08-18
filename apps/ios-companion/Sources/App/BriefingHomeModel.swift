import Foundation
import SwiftUI

/// State for the briefing home. Each leg (glance / alerts / proposals)
/// fails independently — an unreachable instance renders honest per-section
/// errors plus the cached glance snapshot instead of a blank screen.
@MainActor
final class BriefingHomeModel: ObservableObject {
    @Published var snapshot: GlanceSnapshot?
    @Published var isStale = false
    @Published var glanceError: String?
    @Published var alerts: [AlertItem] = []
    @Published var alertsError: String?
    @Published var pendingProposals: [ProposalItem] = []
    @Published var proposalBanner: String?
    @Published var busyProposalIDs: Set<UUID> = []

    private let glanceClient = GlanceClient()
    private let api = HealthMesAPI()
    private var refreshGate = LatestRefreshGate()
    private var resolutionTokens: [UUID: UUID] = [:]

    var lastUpdatedText: String {
        guard let snapshot else { return "—" }
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        formatter.dateStyle = .none
        return formatter.string(from: snapshot.fetchedAt)
    }

    var pendingDecisions: [PendingDecision] {
        PendingDecision.correlate(alerts: alerts, proposals: pendingProposals)
    }

    func refresh() async {
        let refreshID = refreshGate.begin()
        guard let pairingSnapshot = PairingStore.shared.load() else {
            glanceError = String(localized: "Not paired — open Settings.")
            return
        }

        async let glanceResult: Result<GlanceSnapshot, Error> = productRefreshResult {
            try await glanceClient.fetch(pairing: pairingSnapshot)
        }
        async let alertsResult: Result<AlertsPage, Error> = productRefreshResult {
            try await api.listAlerts(pairing: pairingSnapshot, hours: 24)
        }
        async let proposalsResult: Result<ProposalsPage, Error> = productRefreshResult {
            try await api.listProposals(pairing: pairingSnapshot, status: .proposed)
        }
        let results = await (glanceResult, alertsResult, proposalsResult)
        guard
            refreshGate.isCurrent(refreshID),
            PairingStore.shared.load() == pairingSnapshot
        else { return }

        switch results.0 {
        case .success(let freshSnapshot):
            snapshot = freshSnapshot
            isStale = false
            glanceError = nil
        case .failure(let error):
            if let cachedPayload = glanceClient.cache.decodedPayload(for: pairingSnapshot),
                let cached = glanceClient.cache.load(for: pairingSnapshot)
            {
                snapshot = GlanceSnapshot(
                    payload: cachedPayload,
                    fetchedAt: cached.fetchedAt,
                    revalidated: false,
                    nextRefresh: Date()
                )
                isStale = true
                glanceError = nil
            } else {
                glanceError = Self.describe(error)
            }
        }

        switch results.1 {
        case .success(let page):
            alerts = page.data
            alertsError = nil
        case .failure(let error):
            alertsError = Self.describe(error)
        }

        switch results.2 {
        case .success(let page):
            pendingProposals = page.data
        case .failure:
            // The proposals section simply hides on failure (alerts carry
            // the connectivity message already).
            pendingProposals = []
        }
    }

    /// Accept/decline through the real endpoint. A 409 means someone (or
    /// another surface — Telegram) already resolved it: refresh and say so.
    func resolve(
        _ proposal: ProposalItem,
        action: ProposalAction,
        pairing: Pairing? = nil
    ) async {
        guard let pairingSnapshot = pairing ?? PairingStore.shared.load() else {
            proposalBanner = String(localized: "Not paired — open Settings.")
            return
        }
        let resolutionToken = UUID()
        resolutionTokens[proposal.id] = resolutionToken
        busyProposalIDs.insert(proposal.id)
        defer {
            if resolutionTokens[proposal.id] == resolutionToken {
                resolutionTokens[proposal.id] = nil
                busyProposalIDs.remove(proposal.id)
            }
        }
        do {
            let resolved = try await api.resolveProposal(
                proposal,
                action: action,
                pairing: pairingSnapshot
            )
            guard resolutionIsCurrent(
                proposal.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            pendingProposals.removeAll { $0.id == proposal.id }
            proposalBanner = ProposalStatusPresentation.label(for: resolved.status)
            await refresh()
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            guard resolutionIsCurrent(
                proposal.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            pendingProposals.removeAll { $0.id == proposal.id }
            let status = error.alreadyResolvedStatus ?? "resolved"
            proposalBanner = String(
                format: String(localized: "Already resolved (%@)."),
                status
            )
            await refresh()
        } catch {
            guard resolutionIsCurrent(
                proposal.id,
                token: resolutionToken,
                pairing: pairingSnapshot
            ) else { return }
            proposalBanner = Self.describe(error)
        }
    }

    func resetForPairingChange() {
        _ = refreshGate.begin()
        resolutionTokens.removeAll()
        snapshot = nil
        isStale = false
        glanceError = nil
        alerts = []
        alertsError = nil
        pendingProposals = []
        proposalBanner = nil
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

    static func describe(_ error: Error) -> String {
        switch error {
        case GlanceClientError.notPaired, HealthMesAPIError.notPaired:
            return String(localized: "Not paired — open Settings.")
        case GlanceClientError.unauthorized, HealthMesAPIError.unauthorized:
            return String(localized: "Token rejected (401) — re-save the pairing in Settings.")
        case GlanceClientError.transport, HealthMesAPIError.transport:
            return String(localized: "Could not reach your instance. Check Wi-Fi and the URL.")
        case HealthMesAPIError.server(_, _, let message, _):
            return message
        case GlanceClientError.httpStatus(let code), HealthMesAPIError.httpStatus(let code):
            return String(localized: "Server answered HTTP \(code).")
        default:
            return error.localizedDescription
        }
    }
}
