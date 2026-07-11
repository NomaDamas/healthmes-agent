import Foundation
import UserNotifications

/// UNUserNotificationCenter wiring for the §8.5 alert grammar (parity with
/// the Android companion's AlertNotifier):
///
/// - Local notifications only, derived from polling `GET /v1/alerts` —
///   there is deliberately NO push relay (APNs stays out of scope,
///   local-first; Telegram remains the guaranteed-delivery channel).
/// - Content comes from the shared `AlertNotificationContent` builder
///   (observation title, evidence+proposal body).
/// - Action buttons are wired to REAL endpoints: ✅ Apply →
///   `POST /v1/schedule/proposals/{id}/accept`, ❌ Keep as is → `…/decline`,
///   ✏️ Adjust → opens the proposal detail in-app. The buttons appear only
///   when the refresh loop attached a pending proposal id; otherwise the
///   notification carries just the tap-through ("why this?" viewer).
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()

    enum ActionID {
        static let apply = "HEALTHMES_APPLY"
        static let adjust = "HEALTHMES_ADJUST"
        static let keep = "HEALTHMES_KEEP"
    }

    func configure() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        // §8.5 button row. Titles are localized (ko/en); the emoji prefixes
        // mirror the Telegram inline keyboard so the vocabulary stays one
        // system (wording itself is the domain expert's to refine —
        // docs/design/WATCH-NOTIFICATIONS.ko.md).
        let apply = UNNotificationAction(
            identifier: ActionID.apply,
            title: String(localized: "✅ Apply"),
            options: [.authenticationRequired]
        )
        let adjust = UNNotificationAction(
            identifier: ActionID.adjust,
            title: String(localized: "✏️ Adjust"),
            options: [.foreground]
        )
        let keep = UNNotificationAction(
            identifier: ActionID.keep,
            title: String(localized: "❌ Keep as is"),
            options: [.authenticationRequired]
        )
        let actionable = UNNotificationCategory(
            identifier: AlertNotificationContent.actionableCategoryID,
            actions: [apply, adjust, keep],
            intentIdentifiers: []
        )
        let info = UNNotificationCategory(
            identifier: AlertNotificationContent.infoCategoryID,
            actions: [],
            intentIdentifiers: []
        )
        center.setNotificationCategories([actionable, info])
    }

    /// Ask once, right after pairing succeeds (Settings can re-trigger).
    func requestAuthorization() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let granted =
            (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        return granted
    }

    func authorizationStatus() async -> UNAuthorizationStatus {
        await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }

    // MARK: - Posting

    /// Post one local notification for an alert-history item.
    func post(content: AlertNotificationContent) async {
        let notification = UNMutableNotificationContent()
        notification.title = content.title
        if !content.body.isEmpty {
            notification.body = content.body
        }
        notification.categoryIdentifier = content.categoryID
        notification.threadIdentifier = content.threadID
        notification.userInfo = content.userInfo
        notification.sound = .default
        // Alert id as request id → posting the same alert twice collapses.
        let request = UNNotificationRequest(
            identifier: content.userInfo[AlertNotificationContent.userInfoAlertID]
                ?? UUID().uuidString,
            content: notification,
            trigger: nil
        )
        try? await UNUserNotificationCenter.current().add(request)
    }

    /// Outcome toast for actions taken from the lock screen (there is no
    /// visible UI to confirm in).
    func postOutcome(title: String, body: String) async {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.threadIdentifier = "healthmes-outcome"
        let request = UNNotificationRequest(
            identifier: "healthmes-outcome-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        try? await UNUserNotificationCenter.current().add(request)
    }

    func setBadge(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(count)
    }

    // MARK: - UNUserNotificationCenterDelegate

    /// Foreground presentation: show the banner (the §8.5 loop is exactly
    /// about proactive interruption; the list on the home tab mirrors it).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        let decisionURL = (userInfo[AlertNotificationContent.userInfoDecisionURL] as? String)
            .flatMap(URL.init(string:))
        let proposalID = (userInfo[AlertNotificationContent.userInfoProposalID] as? String)
            .flatMap(UUID.init(uuidString:))

        switch response.actionIdentifier {
        case ActionID.apply:
            resolve(proposalID, action: .accept, completionHandler: completionHandler)
        case ActionID.keep:
            resolve(proposalID, action: .decline, completionHandler: completionHandler)
        case ActionID.adjust:
            // .foreground option: the app is coming up — route to the sheet.
            Task { @MainActor in
                if let proposalID {
                    AppRouter.shared.openProposalDetail(proposalID)
                } else {
                    AppRouter.shared.tab = .home
                }
                completionHandler()
            }
        case UNNotificationDefaultActionIdentifier:
            // Tap-through = the §8.5 "why this?" link when the alert has a
            // decision record; home otherwise.
            Task { @MainActor in
                if let decisionURL {
                    AppRouter.shared.openDecision(decisionURL)
                } else {
                    AppRouter.shared.tab = .home
                }
                completionHandler()
            }
        default:
            completionHandler()
        }
    }

    /// Background action → real endpoint call → outcome notification.
    private func resolve(
        _ proposalID: UUID?,
        action: ProposalAction,
        completionHandler: @escaping () -> Void
    ) {
        Task {
            defer { completionHandler() }
            guard let proposalID else {
                await postOutcome(
                    title: String(localized: "Nothing to apply"),
                    body: String(localized: "This alert has no pending proposal attached.")
                )
                return
            }
            do {
                let proposal = try await HealthMesAPI().resolveProposal(
                    id: proposalID, action: action
                )
                let title =
                    proposal.status == .accepted
                    ? String(localized: "Proposal applied")
                    : String(localized: "Kept as is")
                await postOutcome(
                    title: title,
                    body: String(localized: "Your HealthMes instance recorded the decision.")
                )
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                await postOutcome(
                    title: String(localized: "Already resolved"),
                    body: String(
                        localized: "This proposal was already decided (\(error.alreadyResolvedStatus ?? "resolved"))."
                    )
                )
            } catch {
                await postOutcome(
                    title: String(localized: "Could not reach your instance"),
                    body: String(localized: "Open the app and retry from the Home tab.")
                )
            }
        }
    }
}
