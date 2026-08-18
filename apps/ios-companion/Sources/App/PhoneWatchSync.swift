import Foundation
import WatchConnectivity

/// Pushes the pairing (base URL + token) to the watch app via the
/// WatchConnectivity application context — Apple's encrypted phone<->watch
/// channel; nothing leaves the user's devices. Best effort by design: the
/// context is delivered whenever the watch app next runs.
final class PhoneWatchSync: NSObject, WCSessionDelegate {
    static let shared = PhoneWatchSync()
    private var pendingContext: [String: Any]?
    private var pendingUserInfo: [[String: Any]] = []
    private let pendingUserInfoLock = NSLock()

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
            deliverPendingUserInfo()
        }
    }

    func sessionWatchStateDidChange(_ session: WCSession) {
        queuePersistedPairing()
        deliverPendingContext()
    }

    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard
            let command = userInfo[SpeakCommandSyncKeys.command] as? String,
            !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return }

        let requestID =
            userInfo[SpeakCommandSyncKeys.requestID] as? String
            ?? UUID().uuidString.lowercased()
        let proposalID =
            (userInfo[SpeakCommandSyncKeys.proposalID] as? String)
            .flatMap(UUID.init(uuidString:))

        Task {
            await relayToHealthMes(
                command: command,
                requestID: requestID,
                proposalID: proposalID
            )
        }
    }

    func sessionDidBecomeInactive(_ session: WCSession) {}

    func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    private func relayToHealthMes(
        command: String,
        requestID: String,
        proposalID: UUID?
    ) async {
        do {
            let scene = try await HealthMesAPI().createWellnessScene(
                query: command,
                source: .user,
                proposalID: proposalID
            )
            let detail = AlertNotificationContent.compactLine(
                scene.summary,
                limit: 120
            )
            sendSpeakResult(
                requestID: requestID,
                status: "completed",
                title: scene.title,
                detail: detail
            )
            await NotificationManager.shared.postOutcome(
                title: String(localized: "HealthMes processed your instruction"),
                body: detail
            )
        } catch {
            let detail = String(
                localized:
                    "Check the connection to your paired HealthMes instance and try again."
            )
            sendSpeakResult(
                requestID: requestID,
                status: "failed",
                title: String(localized: "HealthMes could not process the instruction"),
                detail: detail
            )
            await NotificationManager.shared.postOutcome(
                title: String(localized: "HealthMes command failed"),
                body: detail
            )
        }
    }

    private func sendSpeakResult(
        requestID: String,
        status: String,
        title: String,
        detail: String
    ) {
        guard WCSession.isSupported() else { return }
        enqueueUserInfo([
            SpeakCommandSyncKeys.requestID: requestID,
            SpeakCommandSyncKeys.resultStatus: status,
            SpeakCommandSyncKeys.resultTitle: title,
            SpeakCommandSyncKeys.resultDetail: detail,
        ])
        deliverPendingUserInfo()
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
