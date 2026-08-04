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
/// - Action buttons are wired to REAL endpoints: Yes →
///   `POST /v1/schedule/proposals/{id}/accept`, No → `…/decline`.
///   The buttons appear only
///   when the refresh loop attached a pending proposal id; otherwise the
///   notification carries just the tap-through ("why this?" viewer).
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()

    enum ActionID {
        static let yes = "HEALTHMES_YES"
        static let no = "HEALTHMES_NO"
    }

    func configure() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        #if targetEnvironment(simulator)
            let protectedActionOptions: UNNotificationActionOptions = []
        #else
            let protectedActionOptions: UNNotificationActionOptions = [.authenticationRequired]
        #endif

        // The same native category is mirrored to Apple Watch, where the
        // owner can decide without opening either app.
        let yes = UNNotificationAction(
            identifier: ActionID.yes,
            title: String(localized: "Yes"),
            options: protectedActionOptions,
            icon: UNNotificationActionIcon(systemImageName: "checkmark.circle.fill")
        )
        let no = UNNotificationAction(
            identifier: ActionID.no,
            title: String(localized: "No"),
            options: protectedActionOptions,
            icon: UNNotificationActionIcon(systemImageName: "xmark.circle")
        )
        let actionable = UNNotificationCategory(
            identifier: AlertNotificationContent.actionableCategoryID,
            // Apple Watch Double Tap can invoke the first non-destructive
            // action. Default to the non-mutating choice so an accidental
            // gesture can never approve a calendar change.
            actions: [no, yes],
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
        case ActionID.yes:
            resolve(proposalID, action: .accept, completionHandler: completionHandler)
        case ActionID.no:
            resolve(proposalID, action: .decline, completionHandler: completionHandler)
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
                let api = HealthMesAPI()
                let pending = try await api.getProposal(proposalID)
                guard pending.isActionable else {
                    let stillProposed = pending.status == .proposed
                    await postOutcome(
                        title: stillProposed
                            ? String(localized: "Proposal expired")
                            : String(localized: "Already resolved"),
                        body: stillProposed
                            ? String(localized: "The decision window closed without a change.")
                            : String(localized: "This proposal is no longer pending.")
                    )
                    return
                }
                let proposal = try await api.resolveProposal(
                    pending,
                    action: action,
                    // A mirrored Watch action is delivered through the iPhone
                    // delegate, which cannot reliably distinguish the device.
                    surface: "apple_notification"
                )
                let title =
                    proposal.status == .accepted
                    ? String(localized: "Calendar change approved")
                    : String(localized: "Calendar change declined")
                await postOutcome(
                    title: title,
                    body: proposal.status == .accepted
                        ? String(localized: "HealthMes recorded Yes. Calendar sync will apply it.")
                        : String(localized: "HealthMes recorded No. The calendar stays unchanged.")
                )
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                await postOutcome(
                    title: String(localized: "Already resolved"),
                    body: String(
                        localized: "This proposal was already decided (\(error.alreadyResolvedStatus ?? "resolved"))."
                    )
                )
            } catch let error as HealthMesAPIError where error.isProposalExpired {
                await postOutcome(
                    title: String(localized: "Proposal expired"),
                    body: String(localized: "The decision window closed without a change.")
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
