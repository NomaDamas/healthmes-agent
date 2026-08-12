import Foundation
import WatchConnectivity

/// Pushes the pairing (base URL + token) to the watch app via the
/// WatchConnectivity application context — Apple's encrypted phone<->watch
/// channel; nothing leaves the user's devices. Best effort by design: the
/// context is delivered whenever the watch app next runs.
final class PhoneWatchSync: NSObject, WCSessionDelegate {
    static let shared = PhoneWatchSync()
    private var pendingContext: [String: Any]?

    func activate() {
        guard WCSession.isSupported() else { return }
        queuePersistedPairing()
        let session = WCSession.default
        session.delegate = self
        session.activate()
    }

    func pushPairing(baseURL: String, token: String) {
        push([
            PairingSyncKeys.baseURL: baseURL,
            PairingSyncKeys.token: token,
        ])
    }

    func pushUnpair() {
        push([PairingSyncKeys.baseURL: "", PairingSyncKeys.token: ""])
    }

    private func push(_ context: [String: Any]) {
        guard WCSession.isSupported() else { return }
        pendingContext = context
        deliverPendingContext()
    }

    private func queuePersistedPairing() {
        pendingContext = PairingSyncKeys.context(
            for: PairingStore.shared.load()
        )
    }

    private func deliverPendingContext() {
        guard
            WCSession.isSupported(),
            WCSession.default.activationState == .activated,
            let pendingContext
        else { return }
        do {
            try WCSession.default.updateApplicationContext(pendingContext)
            self.pendingContext = nil
        } catch {
            // Retained and retried after the next activation transition.
        }
    }

    // MARK: WCSessionDelegate (iOS)

    func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        if activationState == .activated, error == nil {
            queuePersistedPairing()
            deliverPendingContext()
        }
    }

    func sessionWatchStateDidChange(_ session: WCSession) {
        queuePersistedPairing()
        deliverPendingContext()
    }

    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard
            let command = userInfo[AlternativeCommandSyncKeys.command] as? String,
            !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return }
        Task { @MainActor in
            AppRouter.shared.openAgentCommandDock(prefill: command)
        }
    }

    func sessionDidBecomeInactive(_ session: WCSession) {}

    func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }
}
