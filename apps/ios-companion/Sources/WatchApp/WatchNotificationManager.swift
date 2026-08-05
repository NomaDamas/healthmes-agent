import Foundation
import UserNotifications

final class WatchNotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = WatchNotificationManager()

    func configure() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let no = UNNotificationAction(
            identifier: AlertNotificationActionID.no,
            title: String(localized: "No"),
            options: [],
            icon: UNNotificationActionIcon(systemImageName: "xmark")
        )
        let yes = UNNotificationAction(
            identifier: AlertNotificationActionID.yes,
            title: String(localized: "Yes"),
            options: [.authenticationRequired],
            icon: UNNotificationActionIcon(systemImageName: "checkmark")
        )
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: AlertNotificationContent.actionableCategoryID,
                actions: [no, yes],
                intentIdentifiers: []
            )
        ])
    }

    #if DEBUG
        func postDecisionDemo() async {
            let center = UNUserNotificationCenter.current()
            let granted =
                (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
            guard granted else { return }

            center.removeAllPendingNotificationRequests()
            center.removeAllDeliveredNotifications()

            let formatter = ISO8601DateFormatter()
            let now = Date()
            let calendar = Calendar.autoupdatingCurrent
            let todayFocus =
                calendar.date(bySettingHour: 14, minute: 0, second: 0, of: now) ?? now
            let tomorrow = calendar.date(byAdding: .day, value: 1, to: now) ?? now
            let proposedStart =
                calendar.date(bySettingHour: 9, minute: 30, second: 0, of: tomorrow)
                ?? tomorrow
            let proposedEnd =
                calendar.date(byAdding: .minute, value: 90, to: proposedStart)
                ?? proposedStart.addingTimeInterval(90 * 60)

            let content = UNMutableNotificationContent()
            content.title = String(localized: "Move 2 PM focus?")
            content.body = AlertNotificationContent.targetLine(after: proposedStart)
            content.categoryIdentifier = AlertNotificationContent.actionableCategoryID
            content.sound = .default
            content.userInfo = [
                AlertNotificationContent.userInfoProposalID:
                    "00000000-0000-0000-0000-000000000091",
                AlertNotificationContent.userInfoDecisionTitle:
                    String(localized: "Protect recovery"),
                AlertNotificationContent.userInfoDecisionObservation:
                    String(localized: "Sleep debt · recovery low"),
                AlertNotificationContent.userInfoDecisionEvidence:
                    String(localized: "HRV is 18% below your baseline"),
                AlertNotificationContent.userInfoDecisionAction:
                    String(localized: "Move the 2:00 PM focus block to tomorrow at 9:30 AM?"),
                AlertNotificationContent.userInfoDecisionBefore:
                    formatter.string(from: todayFocus),
                AlertNotificationContent.userInfoDecisionAfter:
                    formatter.string(from: proposedStart),
                AlertNotificationContent.userInfoDecisionEndsAt:
                    formatter.string(from: proposedEnd),
            ]

            let request = UNNotificationRequest(
                identifier: "healthmes-watch-decision-demo",
                content: content,
                trigger: UNTimeIntervalNotificationTrigger(timeInterval: 4, repeats: false)
            )
            try? await center.add(request)
        }
    #endif

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        guard
            let proposalText =
                response.notification.request.content.userInfo[
                    AlertNotificationContent.userInfoProposalID
                ] as? String,
            let proposalID = UUID(uuidString: proposalText)
        else {
            completionHandler()
            return
        }

        let action: ProposalAction
        switch response.actionIdentifier {
        case AlertNotificationActionID.yes:
            action = .accept
        case AlertNotificationActionID.no:
            action = .decline
        case UNNotificationDefaultActionIdentifier:
            Task { @MainActor in
                WatchDecisionInbox.shared.present(
                    content: response.notification.request.content
                )
                completionHandler()
            }
            return
        default:
            completionHandler()
            return
        }

        Task {
            defer { completionHandler() }
            do {
                let api = HealthMesAPI()
                let proposal = try await api.getProposal(proposalID)
                guard proposal.isActionable else { return }
                _ = try await api.resolveProposal(
                    proposal,
                    action: action,
                    surface: "apple_watch_notification"
                )
            } catch {
                // The server remains the source of truth; reopening the
                // proposal on iPhone exposes any expired or network error.
            }
        }
    }
}
