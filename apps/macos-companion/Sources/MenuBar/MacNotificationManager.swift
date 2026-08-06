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

    public init(api: HealthMesAPI = HealthMesAPI(), seenStore: SeenAlertsStore = .shared) {
        self.api = api
        self.seenStore = seenStore
        super.init()
    }

    /// UNUserNotificationCenter aborts in processes without a bundle
    /// identifier (bare test runners); every entry point guards through here.
    private var center: UNUserNotificationCenter? {
        guard Bundle.main.bundleIdentifier != nil else { return nil }
        return .current()
    }

    public var isEnabled: Bool {
        UserDefaults.standard.bool(forKey: Self.enabledDefaultsKey)
    }

    /// Called once at app launch: wire the delegate + categories so action
    /// taps reach us even when the popover never opened.
    public func bootstrap() {
        guard let center else { return }
        center.delegate = self
        registerCategories(center)
    }

    /// Settings toggle handler. Enabling requests authorization and primes
    /// the seen-store with the current history so an existing backlog never
    /// replays as a notification storm.
    public func setEnabled(
        _ enabled: Bool,
        currentAlerts: [AlertItem],
        hasLoadedAlerts: Bool
    ) async {
        guard enabled else {
            UserDefaults.standard.set(false, forKey: Self.enabledDefaultsKey)
            return
        }
        if hasLoadedAlerts {
            seenStore.primeWithoutNotifying(currentAlerts)
        } else {
            seenStore.deferPrimingUntilNextFeed()
        }
        guard let center else {
            UserDefaults.standard.set(false, forKey: Self.enabledDefaultsKey)
            return
        }
        let granted =
            (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        authorizationDenied = !granted
        UserDefaults.standard.set(granted, forKey: Self.enabledDefaultsKey)
    }

    /// Store hook: post exactly one notification per not-yet-seen alert.
    public func process(alerts: [AlertItem], pendingProposals _: [ProposalItem]) {
        guard isEnabled, let center else { return }
        let unseen = seenStore.unseenOrPrime(from: alerts)
        guard !unseen.isEmpty else { return }

        for alert in unseen {
            let content = AlertNotificationContent.from(alert: alert)
            let unContent = UNMutableNotificationContent()
            unContent.title = content.title
            unContent.subtitle = content.subtitle
            unContent.body = content.body
            unContent.categoryIdentifier = content.categoryID
            unContent.threadIdentifier = content.threadID
            unContent.userInfo = content.userInfo
            unContent.sound = .default
            center.add(
                UNNotificationRequest(
                    identifier: "healthmes-alert-\(alert.id.uuidString.lowercased())",
                    content: unContent,
                    trigger: nil
                )
            )
        }
        seenStore.markSeen(unseen)
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
                let proposal = try await api.getProposal(proposalID)
                if proposal.isActionable {
                    let resolved = try await api.resolveProposal(
                        proposal, action: action, surface: "mac_notification"
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
            postOutcomeNotification(outcome)

        case UNNotificationDefaultActionIdentifier:
            if let decisionURL {
                if
                    let pairing = PairingStore.shared.load(),
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

    private func postOutcomeNotification(_ outcome: ProposalOutcome) {
        guard let center else { return }
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
        center.add(
            UNNotificationRequest(
                identifier: "healthmes-outcome-\(UUID().uuidString)",
                content: content,
                trigger: nil
            )
        )
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
