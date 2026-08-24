import AppKit
import Foundation
import UserNotifications

/// Optional native notifications for the menu bar app, rendering the
/// docs/PLAN.md §8.5 grammar via the shared `AlertNotificationContent`
/// builder (observation title, evidence+proposal body, per-rule thread).
///
/// Delivery honesty: these derive from the app's own 5-minute polling —
/// there is no push relay by design (local-first), so Telegram remains the
/// guaranteed-delivery channel. The Settings toggle says exactly that.
///
/// No / Yes actions are attached only when the alert response carries
/// its exact pending proposal id, and they call the real accept/decline
/// endpoints from the action handler.
/// Plain clicks open the decision viewer in the browser.
@MainActor
public final class MacNotificationManager: NSObject, ObservableObject {
    public static let shared = MacNotificationManager()

    public static let enabledDefaultsKey = "healthmes.mac.notificationsEnabled"

    enum ActionID {
        static let yes = "HEALTHMES_YES"
        static let no = "HEALTHMES_NO"
    }

    @Published public private(set) var authorizationDenied = false

    private let api: HealthMesAPI
    private let seenStore: SeenAlertsStore
    private let pairingStore: PairingStore
    private let defaults: UserDefaults
    private let notificationOperations: MacNotificationOperations?
    private var notificationGeneration: UInt64 = 0
    private var isProcessing = false
    private var processWaiters: [CheckedContinuation<Void, Never>] = []

    public init(
        api: HealthMesAPI = HealthMesAPI(),
        seenStore: SeenAlertsStore = .shared,
        pairingStore: PairingStore = .shared,
        defaults: UserDefaults = .standard,
        notificationOperations: MacNotificationOperations? = nil
    ) {
        self.api = api
        self.seenStore = seenStore
        self.pairingStore = pairingStore
        self.defaults = defaults
        self.notificationOperations =
            notificationOperations ?? Self.liveNotificationOperations()
        super.init()
    }

    /// UNUserNotificationCenter aborts in processes without a bundle
    /// identifier (bare test runners); every entry point guards through here.
    private var center: UNUserNotificationCenter? {
        guard Bundle.main.bundleIdentifier != nil else { return nil }
        return .current()
    }

    public var isEnabled: Bool {
        defaults.bool(forKey: Self.enabledDefaultsKey)
    }

    /// Called once at app launch: wire the delegate + categories so action
    /// taps reach us even when the popover never opened.
    public func bootstrap() {
        guard let center else { return }
        center.delegate = self
        registerCategories(center)
    }

    public func clearAccountSurfaces() async -> Bool {
        guard let notificationOperations else { return true }
        let result = await NotificationDeletionBarrier().clear(
            using: notificationOperations.deletionSurface
        )
        if case .success = result {
            return true
        }
        return false
    }

    /// Settings toggle handler. Enabling requests authorization and primes
    /// the seen-store with the current history so an existing backlog never
    /// replays as a notification storm.
    public func setEnabled(
        _ enabled: Bool,
        currentAlerts: [AlertItem],
        hasLoadedAlerts: Bool
    ) async {
        notificationGeneration &+= 1
        let generation = notificationGeneration
        guard enabled else {
            defaults.set(false, forKey: Self.enabledDefaultsKey)
            return
        }
        if hasLoadedAlerts {
            seenStore.primeWithoutNotifying(currentAlerts)
        } else {
            seenStore.deferPrimingUntilNextFeed()
        }
        guard let center else {
            defaults.set(false, forKey: Self.enabledDefaultsKey)
            return
        }
        let granted =
            (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        guard generation == notificationGeneration else { return }
        authorizationDenied = !granted
        defaults.set(granted, forKey: Self.enabledDefaultsKey)
    }

    /// Store hook: post exactly one notification per not-yet-seen alert.
    public func process(
        alerts: [AlertItem],
        pendingProposals _: [ProposalItem],
        pairing: Pairing
    ) async {
        await acquireProcessingSlot()
        defer { releaseProcessingSlot() }
        let generation = notificationGeneration
        guard
            isEnabled,
            let notificationOperations,
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing,
                store: pairingStore
            )
        else { return }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        let unseen = seenStore.unseenOrPrime(from: alerts)
        guard !unseen.isEmpty else { return }
        guard
            let identity = pairingStore.cacheIdentity(for: pairing)
        else {
            return
        }

        var scheduled: [AlertItem] = []
        var shouldRetry = false
        for alert in unseen {
            guard
                generation == notificationGeneration,
                isEnabled,
                currentPairing(identity: identity) == pairing
            else { break }
            let content = AlertNotificationContent.from(
                alert: alert,
                pairingFingerprint: identity.fingerprint,
                pairingGeneration: identity.generation
            )
            let unContent = UNMutableNotificationContent()
            unContent.title = content.title
            unContent.subtitle = content.subtitle
            unContent.body = content.body
            unContent.categoryIdentifier = content.categoryID
            unContent.threadIdentifier = content.threadID
            unContent.userInfo = content.userInfo
            unContent.sound = .default
            let identifier =
                "healthmes-alert-\(alert.id.uuidString.lowercased())"
            do {
                let request = UNNotificationRequest(
                    identifier: identifier,
                    content: unContent,
                    trigger: nil
                )
                try await pairingStore.withPairingLease(
                    for: pairing
                ) {
                    try await notificationOperations.add(request)
                }
                guard
                    generation == notificationGeneration,
                    isEnabled
                else {
                    removeNotification(
                        identifier,
                        using: notificationOperations
                    )
                    break
                }
            } catch {
                if let pairingError = error as? PairingError,
                    pairingError == .storageLockFailed
                    || pairingError == .transitionInProgress
                {
                    shouldRetry = true
                }
                continue
            }
            guard currentPairing(identity: identity) == pairing else {
                removeNotification(identifier, using: notificationOperations)
                continue
            }
            scheduled.append(alert)
        }
        seenStore.markSeen(scheduled)
        guard shouldRetry,
            generation == notificationGeneration,
            isEnabled
        else { return }
        scheduleRetry(
            alerts: alerts,
            pairing: pairing,
            generation: generation
        )
    }

    private func removeNotification(
        _ identifier: String,
        using operations: MacNotificationOperations
    ) {
        operations.removePending([identifier])
        operations.removeDelivered([identifier])
    }

    private func acquireProcessingSlot() async {
        if !isProcessing {
            isProcessing = true
            return
        }
        await withCheckedContinuation { continuation in
            processWaiters.append(continuation)
        }
    }

    private func releaseProcessingSlot() {
        if processWaiters.isEmpty {
            isProcessing = false
        } else {
            processWaiters.removeFirst().resume()
        }
    }

    private func scheduleRetry(
        alerts: [AlertItem],
        pairing: Pairing,
        generation: UInt64
    ) {
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard
                let self,
                self.notificationGeneration == generation,
                self.isEnabled
            else { return }
            await self.process(
                alerts: alerts,
                pendingProposals: [],
                pairing: pairing
            )
        }
    }

    private func currentPairing(
        identity: PairingCacheIdentity
    ) -> Pairing? {
        PairingScope.matchingPairing(
            fingerprint: identity.fingerprint,
            generation: identity.generation,
            store: pairingStore
        )
    }

    private static func liveNotificationOperations()
        -> MacNotificationOperations?
    {
        guard Bundle.main.bundleIdentifier != nil else { return nil }
        let center = UNUserNotificationCenter.current()
        return MacNotificationOperations(
            add: { request in
                try await center.add(request)
            },
            removePending: { identifiers in
                center.removePendingNotificationRequests(
                    withIdentifiers: identifiers
                )
            },
            removeDelivered: { identifiers in
                center.removeDeliveredNotifications(
                    withIdentifiers: identifiers
                )
            },
            deletionSurface: NotificationSurface(
                removeAll: {
                    center.removeAllPendingNotificationRequests()
                    center.removeAllDeliveredNotifications()
                },
                snapshot: {
                    async let pending = center.pendingNotificationRequests()
                    async let delivered = center.deliveredNotifications()
                    return await NotificationSurfaceSnapshot(
                        pendingCount: pending.count,
                        deliveredCount: delivered.count
                    )
                }
            )
        )
    }

    private func registerCategories(_ center: UNUserNotificationCenter) {
        let no = UNNotificationAction(
            identifier: ActionID.no,
            title: String(localized: "No"),
            options: [.authenticationRequired]
        )
        let yes = UNNotificationAction(
            identifier: ActionID.yes,
            title: String(localized: "Yes"),
            options: [.authenticationRequired]
        )
        let actionable = UNNotificationCategory(
            identifier: AlertNotificationContent.actionableCategoryID,
            actions: [no, yes],
            intentIdentifiers: [],
            options: []
        )
        let info = UNNotificationCategory(
            identifier: AlertNotificationContent.infoCategoryID,
            actions: [],
            intentIdentifiers: [],
            options: []
        )
        center.setNotificationCategories([actionable, info])
    }

    private func handle(actionIdentifier: String, userInfo: [String: String]) async {
        guard
            let pairing = PairingScope.matchingPairing(
                fingerprint: userInfo[
                    AlertNotificationContent.userInfoPairingFingerprint
                ],
                generation: PairingScope.generation(
                    from: userInfo[
                        AlertNotificationContent.userInfoPairingGeneration
                    ]
                ),
                store: pairingStore
            )
        else { return }
        let decisionURL = userInfo[AlertNotificationContent.userInfoDecisionURL]
            .flatMap(URL.init(string:))

        switch actionIdentifier {
        case ActionID.yes, ActionID.no:
            guard
                let proposalID = userInfo[AlertNotificationContent.userInfoProposalID]
                    .flatMap(UUID.init(uuidString:))
            else { return }
            let action: ProposalAction = actionIdentifier == ActionID.yes ? .accept : .decline
            let outcome: ProposalOutcome
            do {
                guard
                    let relayLease = PairingRelayGate.shared.begin(
                        pairing: pairing,
                        store: pairingStore
                    )
                else { return }
                defer {
                    PairingRelayGate.shared.end(relayLease)
                }
                let proposal = try await api.getProposal(
                    proposalID,
                    pairing: pairing
                )
                if proposal.isActionable {
                    let resolved = try await api.resolveProposal(
                        proposal,
                        action: action,
                        surface: "mac_notification",
                        pairing: pairing
                    )
                    outcome = ProposalOutcome.from(
                        action: action,
                        resolvedStatus: resolved.status,
                        error: nil
                    )
                } else {
                    outcome = .alreadyResolved(status: proposal.status.rawValue)
                }
            } catch let error as HealthMesAPIError {
                outcome = ProposalOutcome.from(action: action, error: error)
            } catch {
                outcome = .failed
            }
            guard pairingStore.load() == pairing else { return }
            await postOutcomeNotification(
                outcome,
                pairing: pairing
            )

        case UNNotificationDefaultActionIdentifier:
            if let decisionURL {
                if
                    ViewerURL.hasSameOrigin(decisionURL, as: pairing.baseURL)
                {
                    NSWorkspace.shared.open(
                        ViewerURL.authenticate(decisionURL, pairing: pairing)
                    )
                }
            } else {
                NSApp.activate(ignoringOtherApps: true)
            }

        default:
            break
        }
    }

    private func postOutcomeNotification(
        _ outcome: ProposalOutcome,
        pairing: Pairing
    ) async {
        let generation = notificationGeneration
        guard
            isEnabled,
            let notificationOperations,
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing,
                store: pairingStore
            )
        else {
            return
        }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        guard let identity = pairingStore.cacheIdentity(for: pairing) else {
            return
        }
        let content = Self.outcomeNotificationContent(
            outcome,
            identity: identity
        )
        let identifier = "healthmes-outcome-\(UUID().uuidString)"
        let request = UNNotificationRequest(
            identifier: identifier,
            content: content,
            trigger: nil
        )
        do {
            try await pairingStore.withPairingLease(for: pairing) {
                try await notificationOperations.add(request)
            }
        } catch {
            return
        }
        guard
            generation == notificationGeneration,
            isEnabled,
            currentPairing(identity: identity) == pairing
        else {
            removeNotification(identifier, using: notificationOperations)
            return
        }
    }

    static func outcomeNotificationContent(
        _ outcome: ProposalOutcome,
        identity: PairingCacheIdentity
    ) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        switch outcome {
        case .accepted:
            content.title = String(localized: "proposal.accepted")
        case .applied:
            content.title = String(localized: "proposal.applied")
        case .kept:
            content.title = String(localized: "proposal.declined")
        case .expired:
            content.title = String(localized: "proposal.expired")
        case .alreadyResolved(let status):
            content.title = String(localized: "proposal.alreadyResolved \(status)")
        case .failed:
            content.title = String(localized: "proposal.actionFailed")
        }
        content.categoryIdentifier = AlertNotificationContent.infoCategoryID
        content.userInfo = [
            AlertNotificationContent.userInfoPairingFingerprint:
                identity.fingerprint,
            AlertNotificationContent.userInfoPairingGeneration:
                String(identity.generation)
        ]
        return content
    }
}

public struct MacNotificationOperations {
    public let add: (UNNotificationRequest) async throws -> Void
    public let removePending: ([String]) -> Void
    public let removeDelivered: ([String]) -> Void
    public let deletionSurface: NotificationSurface

    public init(
        add: @escaping (UNNotificationRequest) async throws -> Void,
        removePending: @escaping ([String]) -> Void,
        removeDelivered: @escaping ([String]) -> Void = { _ in },
        deletionSurface: NotificationSurface
    ) {
        self.add = add
        self.removePending = removePending
        self.removeDelivered = removeDelivered
        self.deletionSurface = deletionSurface
    }
}

extension MacNotificationManager: UNUserNotificationCenterDelegate {
    /// Menu bar apps have no "foreground" in the usual sense — always show.
    public nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    public nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let raw = response.notification.request.content.userInfo
        var userInfo: [String: String] = [:]
        for (key, value) in raw {
            if let key = key as? String, let value = value as? String {
                userInfo[key] = value
            }
        }
        let actionIdentifier = response.actionIdentifier
        Task { @MainActor in
            await self.handle(actionIdentifier: actionIdentifier, userInfo: userInfo)
            completionHandler()
        }
    }
}
