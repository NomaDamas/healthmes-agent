import Foundation
import WatchConnectivity

/// Pushes the pairing (base URL + token) to the watch app via the
/// WatchConnectivity application context — Apple's encrypted phone<->watch
/// channel; nothing leaves the user's devices. Best effort by design: the
/// context is delivered whenever the watch app next runs.
final class PhoneWatchSync: NSObject, WCSessionDelegate {
    static let shared = PhoneWatchSync()
    private let pendingContext =
        LockedLatestValue<[String: Any]>()
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
        guard
            let state = try? PairingStore.shared.stableSyncState(),
            let pairing = state.pairing,
            pairing.baseURL.absoluteString == baseURL,
            pairing.token == Pairing(baseURL: pairing.baseURL, token: token).token
        else {
            return
        }
        push(PairingSyncKeys.context(for: state))
    }

    func pushUnpair() {
        guard
            let state = try? PairingStore.shared.stableSyncState(),
            state.pairing == nil
        else {
            return
        }
        push(PairingSyncKeys.context(for: state))
    }

    private func push(_ context: [String: Any]) {
        guard WCSession.isSupported() else { return }
        clearPendingUserInfo()
        replacePendingContext(with: context)
        deliverPendingContext()
    }

    private func queuePersistedPairing() {
        guard
            let context = try? PairingStore.shared.stableSyncState()
        else { return }
        replacePendingContext(with: PairingSyncKeys.context(for: context))
    }

    private func deliverPendingContext() {
        guard
            WCSession.isSupported(),
            WCSession.default.activationState == .activated
        else { return }
        do {
            try pendingContext.deliver { context in
                try WCSession.default.updateApplicationContext(context)
            }
        } catch {
            // Retained and retried after the next activation transition.
        }
    }

    private func replacePendingContext(
        with context: [String: Any]
    ) {
        pendingContext.replace(with: context)
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
            !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            let generation = PairingScope.generation(
                from: userInfo[SpeakCommandSyncKeys.pairingGeneration]
            ),
            let pairing = PairingScope.matchingPairing(
                fingerprint:
                    userInfo[SpeakCommandSyncKeys.pairingFingerprint]
                    as? String,
                generation: generation
            )
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
                proposalID: proposalID,
                pairing: pairing,
                generation: generation
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
        proposalID: UUID?,
        pairing: Pairing,
        generation: UInt64
    ) async {
        guard
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing
            )
        else { return }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        guard PairingStore.shared.load() == pairing else { return }
        do {
            let presentation = try await HealthMesAPI().createWellnessDecision(
                question: WellnessDecisionWatchRelay.question(
                    from: command,
                    proposalID: proposalID
                ),
                idempotencyKey: requestID,
                lens: proposalID == nil ? .now : .coordinate,
                pairing: pairing
            )
            let scene = presentation.scene
            let detail = AlertNotificationContent.compactLine(
                scene.summary,
                limit: 120
            )
            guard PairingStore.shared.load() == pairing else { return }
            sendSpeakResult(
                requestID: requestID,
                status: presentation.output.status.rawValue,
                title: scene.title,
                detail: detail,
                pairingFingerprint: pairing.cacheFingerprint,
                pairingGeneration: generation
            )
            if PairingStore.shared.load() == pairing {
                await NotificationManager.shared.postOutcome(
                    title: String(localized: "HealthMes processed your instruction"),
                    body: detail,
                    pairing: pairing
                )
            }
        } catch {
            guard PairingStore.shared.load() == pairing else { return }
            let detail = String(
                localized:
                    "Check the connection to your paired HealthMes instance and try again."
            )
            sendSpeakResult(
                requestID: requestID,
                status: "failed",
                title: String(localized: "HealthMes could not process the instruction"),
                detail: detail,
                pairingFingerprint: pairing.cacheFingerprint,
                pairingGeneration: generation
            )
            await NotificationManager.shared.postOutcome(
                title: String(localized: "HealthMes command failed"),
                body: detail,
                pairing: pairing
            )
        }
    }

    private func sendSpeakResult(
        requestID: String,
        status: String,
        title: String,
        detail: String,
        pairingFingerprint: String,
        pairingGeneration: UInt64
    ) {
        guard WCSession.isSupported() else { return }
        enqueueUserInfo([
            SpeakCommandSyncKeys.requestID: requestID,
            SpeakCommandSyncKeys.pairingFingerprint: pairingFingerprint,
            SpeakCommandSyncKeys.pairingGeneration: NSNumber(
                value: pairingGeneration
            ),
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
        for userInfo in takePendingUserInfo() where isCurrent(userInfo) {
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

    private func clearPendingUserInfo() {
        pendingUserInfoLock.lock()
        defer { pendingUserInfoLock.unlock() }
        pendingUserInfo.removeAll()
    }

    private func isCurrent(_ userInfo: [String: Any]) -> Bool {
        guard
            let fingerprint = userInfo[
                SpeakCommandSyncKeys.pairingFingerprint
            ] as? String,
            let generation = PairingScope.generation(
                from: userInfo[SpeakCommandSyncKeys.pairingGeneration]
            )
        else {
            return false
        }
        return PairingScope.matchingPairing(
            fingerprint: fingerprint,
            generation: generation
        ) != nil
    }
}
