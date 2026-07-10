import Foundation
import WatchConnectivity

/// Pushes the pairing (base URL + token) to the watch app via the
/// WatchConnectivity application context — Apple's encrypted phone<->watch
/// channel; nothing leaves the user's devices. Best effort by design: the
/// context is delivered whenever the watch app next runs.
final class PhoneWatchSync: NSObject, WCSessionDelegate {
    static let shared = PhoneWatchSync()

    func activate() {
        guard WCSession.isSupported() else { return }
        let session = WCSession.default
        session.delegate = self
        session.activate()
    }

    func pushPairing(baseURL: String, token: String) {
        push([PairingSyncKeys.baseURL: baseURL, PairingSyncKeys.token: token])
    }

    func pushUnpair() {
        push([PairingSyncKeys.baseURL: "", PairingSyncKeys.token: ""])
    }

    private func push(_ context: [String: Any]) {
        guard WCSession.isSupported() else { return }
        // Throws when no watch is paired / session not activated yet —
        // harmless here; the pairing stays on the phone and can be re-saved.
        try? WCSession.default.updateApplicationContext(context)
    }

    // MARK: WCSessionDelegate (iOS)

    func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {}

    func sessionDidBecomeInactive(_ session: WCSession) {}

    func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }
}
