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

    var lastUpdatedText: String {
        guard let snapshot else { return "—" }
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        formatter.dateStyle = .none
        return formatter.string(from: snapshot.fetchedAt)
    }

    func refresh() async {
        guard PairingStore.shared.load() != nil else {
            glanceError = String(localized: "Not paired — open Settings.")
            return
        }
        async let glanceTask: Void = refreshGlance()
        async let alertsTask: Void = refreshAlerts()
        async let proposalsTask: Void = refreshProposals()
        _ = await (glanceTask, alertsTask, proposalsTask)
    }

    private func refreshGlance() async {
        do {
            snapshot = try await glanceClient.fetch()
            isStale = false
            glanceError = nil
        } catch {
            if let cachedPayload = glanceClient.cache.decodedPayload(),
                let cached = glanceClient.cache.load()
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
    }

    private func refreshAlerts() async {
        do {
            let page = try await api.listAlerts(hours: 24)
            alerts = page.data
            alertsError = nil
        } catch {
            alertsError = Self.describe(error)
        }
    }

    private func refreshProposals() async {
        do {
            let page = try await api.listProposals(status: .proposed)
            pendingProposals = page.data
        } catch {
            // The proposals section simply hides on failure (alerts carry
            // the connectivity message already).
            pendingProposals = []
        }
    }

    /// Accept/decline through the real endpoint. A 409 means someone (or
    /// another surface — Telegram) already resolved it: refresh and say so.
    func resolve(_ proposal: ProposalItem, action: ProposalAction) async {
        busyProposalIDs.insert(proposal.id)
        defer { busyProposalIDs.remove(proposal.id) }
        do {
            let resolved = try await api.resolveProposal(id: proposal.id, action: action)
            pendingProposals.removeAll { $0.id == proposal.id }
            proposalBanner =
                resolved.status == .accepted
                ? String(localized: "Proposal applied.")
                : String(localized: "Kept as is — proposal declined.")
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            pendingProposals.removeAll { $0.id == proposal.id }
            let status = error.alreadyResolvedStatus ?? "resolved"
            proposalBanner = String(localized: "Already resolved (\(status)).")
        } catch {
            proposalBanner = Self.describe(error)
        }
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
