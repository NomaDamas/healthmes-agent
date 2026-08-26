import Combine
import Foundation

@MainActor
final class MacSetupReadinessModel: ObservableObject {
    typealias Loader = (Pairing) async throws -> SetupReadiness

    @Published private(set) var readiness: SetupReadiness?
    @Published private(set) var errorMessage: String?
    @Published private(set) var isLoading = false

    private let pairingStore: PairingStore
    private let loader: Loader
    private var refreshGate = LatestRefreshGate()

    init(
        pairingStore: PairingStore = .shared,
        loader: Loader? = nil
    ) {
        self.pairingStore = pairingStore
        if let loader {
            self.loader = loader
        } else {
            let api = HealthMesAPI(pairingStore: pairingStore)
            self.loader = { pairing in
                try await api.setupReadiness(pairing: pairing)
            }
        }
    }

    func reset() {
        _ = refreshGate.begin()
        readiness = nil
        errorMessage = nil
        isLoading = false
    }

    func load() async {
        let refreshID = refreshGate.begin()
        readiness = nil
        errorMessage = nil
        guard
            let pairing = pairingStore.load(),
            let identity = pairingStore.cacheIdentity(for: pairing)
        else {
            isLoading = false
            return
        }

        isLoading = true
        defer {
            if refreshGate.isCurrent(refreshID) {
                isLoading = false
            }
        }

        do {
            let result = try await loader(pairing)
            guard isCurrent(refreshID, pairing: pairing, identity: identity) else {
                return
            }
            readiness = result
        } catch {
            guard isCurrent(refreshID, pairing: pairing, identity: identity) else {
                return
            }
            errorMessage = "Could not verify setup readiness."
        }
    }

    private func isCurrent(
        _ refreshID: UInt,
        pairing: Pairing,
        identity: PairingCacheIdentity
    ) -> Bool {
        refreshGate.isCurrent(refreshID)
            && pairingStore.load() == pairing
            && pairingStore.cacheIdentity(for: pairing) == identity
    }
}
