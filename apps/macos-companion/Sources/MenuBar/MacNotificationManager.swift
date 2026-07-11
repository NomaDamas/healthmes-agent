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
/// ✅ Apply / ❌ Keep actions are attached ONLY when exactly one schedule
/// proposal is pending (no alert→proposal FK exists, so that is the only
/// case where "Apply" is unambiguous — same rule as the iOS/Android apps)
/// and they call the real accept/decline endpoints from the action handler.
/// ✏️ Adjust and plain clicks open the decision viewer in the browser.
@MainActor
public final class MacNotificationManager: NSObject, ObservableObject {
    public static let shared = MacNotificationManager()

    public static let enabledDefaultsKey = "healthmes.mac.notificationsEnabled"

    enum ActionID {
        static let apply = "HEALTHMES_APPLY"
        static let adjust = "HEALTHMES_ADJUST"
        static let keep = "HEALTHMES_KEEP"
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
    public func setEnabled(_ enabled: Bool, currentAlerts: [AlertItem]) async {
        UserDefaults.standard.set(enabled, forKey: Self.enabledDefaultsKey)
        guard enabled, let center else { return }
        let granted =
            (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        authorizationDenied = !granted
        if granted {
            seenStore.primeWithoutNotifying(currentAlerts)
        }
    }

    /// Store hook: post exactly one notification per not-yet-seen alert.
    public func process(alerts: [AlertItem], pendingProposals: [ProposalItem]) {
        guard isEnabled, let center else { return }
        let unseen = seenStore.unseen(from: alerts)
        guard !unseen.isEmpty else { return }
        let pendingProposalID = pendingProposals.count == 1 ? pendingProposals[0].id : nil

        for alert in unseen {
            let content = AlertNotificationContent.from(
                alert: alert, pendingProposalID: pendingProposalID
            )
            let unContent = UNMutableNotificationContent()
            unContent.title = content.title
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
        let apply = UNNotificationAction(
            identifier: ActionID.apply,
            title: String(localized: "proposal.apply"),
            options: []
        )
        let adjust = UNNotificationAction(
            identifier: ActionID.adjust,
            title: String(localized: "proposal.adjust"),
            options: []
        )
        let keep = UNNotificationAction(
            identifier: ActionID.keep,
            title: String(localized: "proposal.keep"),
            options: []
        )
        let actionable = UNNotificationCategory(
            identifier: AlertNotificationContent.actionableCategoryID,
            actions: [apply, adjust, keep],
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
        case ActionID.apply, ActionID.keep:
            guard
                let proposalID = userInfo[AlertNotificationContent.userInfoProposalID]
                    .flatMap(UUID.init(uuidString:))
            else { return }
            let action: ProposalAction = actionIdentifier == ActionID.apply ? .accept : .decline
            let outcome: ProposalOutcome
            do {
                _ = try await api.resolveProposal(id: proposalID, action: action)
                outcome = ProposalOutcome.from(action: action, error: nil)
            } catch let error as HealthMesAPIError {
                outcome = ProposalOutcome.from(action: action, error: error)
            } catch {
                outcome = .failed
            }
            postOutcomeNotification(outcome)

        case ActionID.adjust, UNNotificationDefaultActionIdentifier:
            // Desktop mapping of ✏️ Adjust / tap-through: the decision viewer
            // in the browser ("why this?" — §8.5 line 5). Placeholder until a
            // native adjust surface exists (docs/design/WATCH-NOTIFICATIONS.ko.md).
            if let decisionURL {
                NSWorkspace.shared.open(decisionURL)
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
        case .applied:
            content.title = String(localized: "proposal.applied")
        case .kept:
            content.title = String(localized: "proposal.declined")
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
