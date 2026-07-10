import SwiftUI
import WatchConnectivity
import WidgetKit

@main
struct HealthMesWatchApp: App {
    init() {
        // Receive the pairing pushed by the iPhone app (application context).
        WatchPairingReceiver.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            WatchHomeView()
        }
    }
}

/// Stores the pairing pushed from the phone into this watch's own App Group
/// defaults + keychain, then reloads the complications. The watch never
/// talks to anything but the paired healthmes instance.
final class WatchPairingReceiver: NSObject, WCSessionDelegate {
    static let shared = WatchPairingReceiver()

    func activate() {
        guard WCSession.isSupported() else { return }
        let session = WCSession.default
        session.delegate = self
        session.activate()
    }

    func session(
        _ session: WCSession,
        activationDidCompleteWith activationState: WCSessionActivationState,
        error: Error?
    ) {
        // A context may have arrived while this app was not running.
        applyContext(session.receivedApplicationContext)
    }

    func session(_ session: WCSession, didReceiveApplicationContext context: [String: Any]) {
        applyContext(context)
    }

    private func applyContext(_ context: [String: Any]) {
        guard let baseURL = context[PairingSyncKeys.baseURL] as? String else { return }
        let token = context[PairingSyncKeys.token] as? String ?? ""
        if baseURL.isEmpty {
            PairingStore.shared.clear()
            GlanceSnapshotCache.shared.clear()
        } else {
            _ = try? PairingStore.shared.save(baseURLString: baseURL, token: token)
        }
        WidgetCenter.shared.reloadAllTimelines()
    }
}
