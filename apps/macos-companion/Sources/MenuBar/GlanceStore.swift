import Combine
import Foundation

/// Single source of truth for every menu bar surface: glance payload,
/// alert history, pending proposals, pairing state.
///
/// Polling = the endpoint's own caching contract: the shared `GlanceClient`
/// parses `Cache-Control: max-age=300` into `nextRefresh`, a 60 s ticker
/// refreshes only once that instant passes (i.e. 5-minute cadence), and
/// every poll sends `If-None-Match`, so an unchanged briefing costs a
/// body-less 304. Alerts/proposals ride the same tick (no ETag on those
/// endpoints; the payloads are tiny).
@MainActor
public final class GlanceStore: ObservableObject {
    @Published public private(set) var payload: GlancePayload?
    /// True when `payload` came from the on-disk cache because the instance
    /// was unreachable on the last poll (never rendered silently).
    @Published public private(set) var isStale = false
    @Published public private(set) var lastFetched: Date?
    /// Localization key of the current fetch problem, nil when healthy.
    @Published public private(set) var errorKey: String?
    @Published public private(set) var alerts: [AlertItem] = []
    @Published public private(set) var pendingProposals: [ProposalItem] = []
    @Published public private(set) var isPaired: Bool
    @Published public private(set) var isRefreshing = false

    /// Hook for the notification manager: fires after every alerts refresh
    /// with the full history page + currently pending proposals.
    public var onAlertsRefreshed: (([AlertItem], [ProposalItem]) -> Void)?

    private let client: GlanceClient
    private let api: HealthMesAPI
    private let pairingStore: PairingStore
    private var nextGlanceRefresh = Date.distantPast
    private var timer: Timer?

    public init(
        client: GlanceClient = GlanceClient(),
        api: HealthMesAPI = HealthMesAPI(),
        pairingStore: PairingStore = .shared
    ) {
        self.client = client
        self.api = api
        self.pairingStore = pairingStore
        self.isPaired = pairingStore.load() != nil
    }

    /// Start the 60 s ticker and do the initial fetch.
    public func start() {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.refreshIfDue()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        Task { await self.refresh(force: true) }
    }

    private func refreshIfDue() async {
        guard Date() >= nextGlanceRefresh else { return }
        await refresh(force: false)
    }

    /// One full refresh: glance (conditional GET) + alerts + proposals.
    public func refresh(force: Bool) async {
        isPaired = pairingStore.load() != nil
        guard isPaired else {
            payload = nil
            alerts = []
            pendingProposals = []
            errorKey = nil
            return
        }
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            let snapshot = try await client.fetch()
            payload = snapshot.payload
            isStale = false
            lastFetched = snapshot.fetchedAt
            nextGlanceRefresh = snapshot.nextRefresh
            errorKey = nil
        } catch {
            // Honest stale fallback: last cached payload + explicit marker.
            if let cached = client.cache.decodedPayload() {
                payload = cached
                isStale = true
                lastFetched = client.cache.load()?.fetchedAt
            }
            if case GlanceClientError.unauthorized = error {
                errorKey = "error.unauthorized"
            } else {
                errorKey = "error.unreachable"
            }
            // Retry on the next tick rather than hammering a dead host.
            nextGlanceRefresh = Date().addingTimeInterval(60)
        }

        await refreshAlertsAndProposals()
    }

    private func refreshAlertsAndProposals() async {
        do {
            let page = try await api.listAlerts(hours: 24, limit: 20, offset: 0)
            let proposals = try await api.listProposals(status: .proposed)
            alerts = page.data
            pendingProposals = proposals.data
            onAlertsRefreshed?(alerts, pendingProposals)
        } catch {
            // Keep the last known lists; the glance error banner already
            // covers reachability problems.
        }
    }

    /// Real §8.5 button behaviour (✅ Apply → accept, ❌ Keep → decline).
    public func resolve(_ proposal: ProposalItem, action: ProposalAction) async -> ProposalOutcome {
        do {
            _ = try await api.resolveProposal(id: proposal.id, action: action)
            await refreshAlertsAndProposals()
            return ProposalOutcome.from(action: action, error: nil)
        } catch let error as HealthMesAPIError {
            await refreshAlertsAndProposals()
            return ProposalOutcome.from(action: action, error: error)
        } catch {
            return .failed
        }
    }

    /// Pairing flow used by Settings: save, then prove it with a live fetch.
    public func pair(baseURLString: String, token: String) async throws {
        _ = try pairingStore.save(baseURLString: baseURLString, token: token)
        client.cache.clear()
        isPaired = true
        nextGlanceRefresh = .distantPast
        await refresh(force: true)
        if let errorKey {
            throw PairingTestError(localizationKey: errorKey)
        }
    }

    public func unpair() {
        pairingStore.clear()
        client.cache.clear()
        SeenAlertsStore.shared.clear()
        payload = nil
        alerts = []
        pendingProposals = []
        isPaired = false
        isStale = false
        lastFetched = nil
        errorKey = nil
    }

    /// Minutes since the last successful fetch (footer honesty line).
    public var minutesSinceFetch: Int? {
        lastFetched.map { max(0, Int(Date().timeIntervalSince($0) / 60)) }
    }
}

/// Thrown by `pair(...)` when saving succeeded but the live test fetch
/// failed — Settings renders the localized reason.
public struct PairingTestError: Error {
    public let localizationKey: String
}
