import Foundation
import UserNotifications
import WatchConnectivity

final class WatchNotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = WatchNotificationManager()

    func configure() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let no = UNNotificationAction(
            identifier: AlertNotificationActionID.no,
            title: String(localized: "No"),
            options: [.authenticationRequired],
            icon: UNNotificationActionIcon(systemImageName: "xmark")
        )
        let yes = UNNotificationAction(
            identifier: AlertNotificationActionID.yes,
            title: String(localized: "Yes"),
            options: [.authenticationRequired],
            icon: UNNotificationActionIcon(systemImageName: "checkmark")
        )
        let speak = UNTextInputNotificationAction(
            identifier: AlertNotificationActionID.speak,
            title: String(localized: "Speak"),
            options: [.authenticationRequired],
            icon: UNNotificationActionIcon(systemImageName: "microphone.fill"),
            textInputButtonTitle: String(localized: "Apply"),
            textInputPlaceholder: String(localized: "Speak, review, then apply")
        )
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: AlertNotificationContent.actionableCategoryID,
                actions: [no, yes, speak],
                intentIdentifiers: []
            )
        ])
    }

    func clearAccountSurfaces() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let result = await NotificationDeletionBarrier().clear(
            using: notificationSurface(center)
        )
        if case .success = result {
            return true
        }
        return false
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
            content.title = String(localized: "Move Deep Work?")
            content.subtitle = String(
                localized:
                    "Recovery is below your baseline after short sleep and a high-stress morning"
            )
            content.body = AlertNotificationContent.targetLine(after: proposedStart)
            content.categoryIdentifier = AlertNotificationContent.actionableCategoryID
            content.sound = .default
            var userInfo: [String: String] = [
                AlertNotificationContent.userInfoProposalID:
                    "00000000-0000-0000-0000-000000000091",
                AlertNotificationContent.userInfoDecisionTitle:
                    String(localized: "Deep Work"),
                AlertNotificationContent.userInfoDecisionObservation:
                    content.subtitle,
                AlertNotificationContent.userInfoDecisionEvidence:
                    String(localized: "HRV is 18% below your baseline"),
                AlertNotificationContent.userInfoDecisionAction:
                    String(localized: "Move the 2:00 PM focus block to tomorrow at 9:30 AM?"),
                AlertNotificationContent.userInfoDecisionCompactPrompt:
                    String(localized: "Move Deep Work?"),
                AlertNotificationContent.userInfoDecisionBefore:
                    formatter.string(from: todayFocus),
                AlertNotificationContent.userInfoDecisionAfter:
                    formatter.string(from: proposedStart),
                AlertNotificationContent.userInfoDecisionEndsAt:
                    formatter.string(from: proposedEnd),
            ]
            if
                let identity = PairingContextCoordinator
                    .persistedSourceIdentity()
            {
                userInfo[
                    AlertNotificationContent
                        .userInfoPairingFingerprint
                ] = identity.fingerprint
                userInfo[
                    AlertNotificationContent
                        .userInfoPairingGeneration
                ] = String(identity.generation)
            }
            content.userInfo = userInfo

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
        let userInfo = response.notification.request.content.userInfo
        guard
            let sourceGeneration = PairingScope.generation(
                from: userInfo[
                    AlertNotificationContent.userInfoPairingGeneration
                ]
            ),
            let pairing = PairingContextCoordinator
                .matchingSourcePairing(
                fingerprint: userInfo[
                    AlertNotificationContent.userInfoPairingFingerprint
                ] as? String,
                generation: sourceGeneration
            ),
            let proposalText =
                userInfo[
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
        case AlertNotificationActionID.speak,
            AlertNotificationActionID.legacyAlternative:
            let text = (response as? UNTextInputNotificationResponse)?.userText ?? ""
            let requestID = UUID().uuidString.lowercased()
            let command = SpeakCommand.compose(
                userText: text,
                proposalID: proposalID,
                title: userInfo[AlertNotificationContent.userInfoDecisionTitle] as? String,
                proposedAction: userInfo[
                    AlertNotificationContent.userInfoDecisionAction
                ] as? String
            )
            let queued = WatchPairingReceiver.shared.sendSpokenCommand(
                command,
                requestID: requestID,
                proposalID: proposalID,
                pairing: pairing
            )
            Task {
                let title =
                    queued
                    ? String(localized: "Queued for iPhone")
                    : String(localized: "iPhone connection unavailable")
                let detail =
                    queued
                    ? String(
                        localized:
                            "HealthMes will process the instruction when the iPhone receives it."
                    )
                    : String(
                        localized:
                            "Open HealthMes on the paired iPhone and try again."
                    )
                await postSpokenCommandOutcome(
                    title: title,
                    detail: detail,
                    pairing: pairing,
                    sourceGeneration: sourceGeneration
                )
                completionHandler()
            }
            return
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
                guard
                    let relayLease = PairingRelayGate.shared.begin(
                        pairing: pairing
                    )
                else { return }
                defer {
                    PairingRelayGate.shared.end(relayLease)
                }
                let api = HealthMesAPI()
                let proposal = try await api.getProposal(
                    proposalID,
                    pairing: pairing
                )
                guard proposal.isActionable else {
                    await postOutcome(
                        resultForResolvedProposal(proposal),
                        pairing: pairing,
                        sourceGeneration: sourceGeneration
                    )
                    center.removeDeliveredNotifications(withIdentifiers: [
                        response.notification.request.identifier
                    ])
                    return
                }
                let resolved = try await api.resolveProposal(
                    proposal,
                    action: action,
                    surface: "apple_watch_notification",
                    pairing: pairing
                )
                await postOutcome(
                    resultForStatus(
                        resolved.status.rawValue,
                        alreadyResolved: false
                    ),
                    pairing: pairing,
                    sourceGeneration: sourceGeneration
                )
                center.removeDeliveredNotifications(withIdentifiers: [
                    response.notification.request.identifier
                ])
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                guard PairingStore.shared.load() == pairing else { return }
                await postOutcome(
                    resultForStatus(error.alreadyResolvedStatus),
                    pairing: pairing,
                    sourceGeneration: sourceGeneration
                )
            } catch let error as HealthMesAPIError where error.isProposalExpired {
                await postOutcome(
                    .expired,
                    pairing: pairing,
                    sourceGeneration: sourceGeneration
                )
            } catch {
                await postOutcome(
                    .offline,
                    pairing: pairing,
                    sourceGeneration: sourceGeneration
                )
            }
        }
    }

    private func resultForResolvedProposal(_ proposal: ProposalItem) -> WatchDecisionResult {
        if proposal.status == .proposed {
            return .expired
        }
        return resultForStatus(proposal.status.rawValue)
    }

    private func resultForStatus(
        _ rawStatus: String?,
        alreadyResolved: Bool = true
    ) -> WatchDecisionResult {
        guard let rawStatus, let status = ProposalStatus(rawValue: rawStatus) else {
            return .expired
        }
        switch status {
        case .accepted:
            return alreadyResolved ? .alreadyApproved : .approved
        case .pushed:
            return .applied
        case .declined:
            return alreadyResolved ? .alreadyDeclined : .declined
        case .proposed, .invalidated:
            return .expired
        }
    }

    private func postOutcome(
        _ result: WatchDecisionResult,
        pairing: Pairing,
        sourceGeneration: UInt64
    ) async {
        guard
            PairingContextCoordinator.matchingSourcePairing(
                fingerprint: pairing.cacheFingerprint,
                generation: sourceGeneration
            ) == pairing,
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing
            )
        else {
            return
        }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        let content = UNMutableNotificationContent()
        content.title = result.title
        content.body = result.detail
        content.threadIdentifier = "healthmes-watch-outcome"
        let request = UNNotificationRequest(
            identifier: "healthmes-watch-outcome-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        try? await UNUserNotificationCenter.current().add(request)
    }

    func postSpokenCommandOutcome(
        title: String,
        detail: String,
        pairing: Pairing,
        sourceGeneration: UInt64
    ) async {
        guard
            PairingContextCoordinator.matchingSourcePairing(
                fingerprint: pairing.cacheFingerprint,
                generation: sourceGeneration
            ) == pairing,
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing
            )
        else {
            return
        }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = detail
        content.threadIdentifier = "healthmes-watch-spoken-command"
        let request = UNNotificationRequest(
            identifier: "healthmes-watch-spoken-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        try? await UNUserNotificationCenter.current().add(request)
    }

    private func notificationSurface(
        _ center: UNUserNotificationCenter
    ) -> NotificationSurface {
        NotificationSurface(
            removeAll: {
                center.removeAllPendingNotificationRequests()
                center.removeAllDeliveredNotifications()
            },
            snapshot: {
                async let pending =
                    center.pendingNotificationRequests()
                async let delivered =
                    center.deliveredNotifications()
                return await NotificationSurfaceSnapshot(
                    pendingCount: pending.count,
                    deliveredCount: delivered.count
                )
            }
        )
    }
}
