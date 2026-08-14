import SwiftUI
import WatchConnectivity
import WidgetKit

@main
struct HealthMesWatchApp: App {
    init() {
        // Receive the pairing pushed by the iPhone app (application context).
        WatchPairingReceiver.shared.activate()
        WatchNotificationManager.shared.configure()

        #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-healthmes-watch-notification-demo") {
                Task {
                    await WatchNotificationManager.shared.postDecisionDemo()
                }
            }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            WatchHomeView()
        }
        WKNotificationScene(
            controller: WatchDecisionNotificationController.self,
            category: AlertNotificationContent.actionableCategoryID
        )
    }
}

/// Stores the pairing pushed from the phone into this watch's own App Group
/// defaults + keychain, then reloads the complications. The watch never
/// talks to anything but the paired healthmes instance.
final class WatchPairingReceiver: NSObject, WCSessionDelegate {
    static let shared = WatchPairingReceiver()
    private var pendingUserInfo: [[String: Any]] = []
    private let pendingUserInfoLock = NSLock()

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
        if activationState == .activated, error == nil {
            deliverPendingUserInfo()
        }
    }

    func session(_ session: WCSession, didReceiveApplicationContext context: [String: Any]) {
        applyContext(context)
    }

    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard
            let title = userInfo[SpeakCommandSyncKeys.resultTitle] as? String,
            let detail = userInfo[SpeakCommandSyncKeys.resultDetail] as? String,
            userInfo[SpeakCommandSyncKeys.resultStatus] as? String != nil
        else { return }

        Task {
            await WatchNotificationManager.shared.postSpokenCommandOutcome(
                title: title,
                detail: detail
            )
        }
    }

    func sendSpokenCommand(
        _ command: String,
        requestID: String,
        proposalID: UUID
    ) -> Bool {
        guard WCSession.isSupported() else { return false }
        enqueueUserInfo([
            SpeakCommandSyncKeys.command: command,
            SpeakCommandSyncKeys.requestID: requestID,
            SpeakCommandSyncKeys.proposalID: proposalID.uuidString.lowercased(),
        ])
        deliverPendingUserInfo()
        return true
    }

    #if os(iOS)
        func sessionDidBecomeInactive(_ session: WCSession) {}

        func sessionDidDeactivate(_ session: WCSession) {
            session.activate()
        }
    #endif

    private func applyContext(_ context: [String: Any]) {
        guard let baseURL = context[PairingSyncKeys.baseURL] as? String else { return }
        let token = context[PairingSyncKeys.token] as? String ?? ""
        guard PairingContextApplication.apply(
            baseURLString: baseURL,
            token: token
        ) else { return }
        if baseURL.isEmpty {
            GlanceSnapshotCache.shared.clear()
        }
        Task { @MainActor in
            NotificationCenter.default.post(name: .healthmesPairingChanged, object: nil)
            WidgetCenter.shared.reloadAllTimelines()
        }
    }

    private func deliverPendingUserInfo() {
        guard
            WCSession.isSupported(),
            WCSession.default.activationState == .activated
        else {
            WCSession.default.activate()
            return
        }
        for userInfo in takePendingUserInfo() {
            WCSession.default.transferUserInfo(userInfo)
        }
    }

    private func enqueueUserInfo(_ userInfo: [String: Any]) {
        pendingUserInfoLock.lock()
        defer { pendingUserInfoLock.unlock() }
        pendingUserInfo.append(userInfo)
    }

    private func takePendingUserInfo() -> [[String: Any]] {
        pendingUserInfoLock.lock()
        defer { pendingUserInfoLock.unlock() }
        let queued = pendingUserInfo
        pendingUserInfo.removeAll()
        return queued
    }
}
