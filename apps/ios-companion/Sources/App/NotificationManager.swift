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
            identifier: AlertNotificationActionID.yes,
            title: String(localized: "Yes"),
            options: protectedActionOptions,
            icon: UNNotificationActionIcon(systemImageName: "checkmark.circle.fill")
        )
        let no = UNNotificationAction(
            identifier: AlertNotificationActionID.no,
            title: String(localized: "No"),
            options: protectedActionOptions,
            icon: UNNotificationActionIcon(systemImageName: "xmark.circle")
        )
        let speak = UNTextInputNotificationAction(
            identifier: AlertNotificationActionID.speak,
            title: String(localized: "Speak"),
            options: protectedActionOptions,
            icon: UNNotificationActionIcon(systemImageName: "microphone.fill"),
            textInputButtonTitle: String(localized: "Apply"),
            textInputPlaceholder: String(localized: "Speak, review the text, then apply")
        )
        let actionable = UNNotificationCategory(
            identifier: AlertNotificationContent.actionableCategoryID,
            // Apple Watch Double Tap can invoke the first non-destructive
            // action. Default to the non-mutating choice so an accidental
            // gesture can never approve a calendar change. Keep Yes second
            // so both primary decisions remain visible on the 42 mm screen.
            actions: [no, yes, speak],
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
    @discardableResult
    func post(content: AlertNotificationContent) async -> Bool {
        guard
            let pairing = PairingScope.matchingPairing(
                fingerprint: content.userInfo[
                    AlertNotificationContent.userInfoPairingFingerprint
                ],
                generation: PairingScope.generation(
                    from: content.userInfo[
                        AlertNotificationContent.userInfoPairingGeneration
                    ]
                )
            ),
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing
            )
        else {
            return false
        }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        let notification = UNMutableNotificationContent()
        notification.title = content.systemTitle
        if !content.subtitle.isEmpty {
            notification.subtitle = content.subtitle
        }
        if !content.systemBody.isEmpty {
            notification.body = content.systemBody
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
        do {
            let center = UNUserNotificationCenter.current()
            try await PairingStore.shared.withPairingLease(
                for: pairing
            ) {
                try await center.add(request)
            }
            return true
        } catch {
            return false
        }
    }

    #if DEBUG
        /// Deterministic simulator/UI-test entry point for visually proving
        /// the expanded notification card and its two decision actions.
        func postDecisionDemo() async {
            guard await requestAuthorization() else { return }
            guard let pairing = PairingStore.shared.load() else { return }

            let center = UNUserNotificationCenter.current()
            center.removeAllPendingNotificationRequests()
            center.removeAllDeliveredNotifications()

            let formatter = ISO8601DateFormatter()
            let now = Date()
            let calendar = Calendar.autoupdatingCurrent
            let currentStart =
                calendar.date(bySettingHour: 14, minute: 0, second: 0, of: now) ?? now
            let tomorrow = calendar.date(byAdding: .day, value: 1, to: now) ?? now
            let proposedStart =
                calendar.date(
                    bySettingHour: 9,
                    minute: 30,
                    second: 0,
                    of: tomorrow
                ) ?? tomorrow
            let proposedEnd =
                calendar.date(byAdding: .minute, value: 90, to: proposedStart)
                ?? proposedStart.addingTimeInterval(90 * 60)
            let proposalID =
                UUID(uuidString: DecisionActivityAttributes.demoProposalID)!
            let prompt = String(
                localized:
                    "Move the 2:00 PM focus block to tomorrow at 9:30 AM?"
            )
            let reason = String(
                localized:
                    "Recovery is below your baseline after short sleep and a high-stress morning"
            )
            let target = AlertNotificationContent.targetLine(after: proposedStart)
            let expiresAt = now.addingTimeInterval(30 * 60)

            await DecisionLiveActivityController.shared.startDemo(
                proposalID: proposalID,
                title: prompt,
                reason: reason,
                target: target,
                expiresAt: expiresAt
            )

            // Give the UI test enough time to put the app in the background
            // so SpringBoard, rather than the foreground delegate, owns it.
            try? await Task.sleep(for: .seconds(6))

            let alertID = UUID().uuidString.lowercased()
            var userInfo = [
                AlertNotificationContent.userInfoAlertID: alertID,
                AlertNotificationContent.userInfoPairingFingerprint:
                    pairing.cacheFingerprint,
                AlertNotificationContent.userInfoPairingGeneration:
                    String(
                        PairingStore.shared.cacheIdentity(for: pairing)?
                            .generation ?? 0
                    ),
                AlertNotificationContent.userInfoProposalID:
                    proposalID.uuidString.lowercased(),
                AlertNotificationContent.userInfoDecisionObservation:
                    reason,
                AlertNotificationContent.userInfoDecisionTitle:
                    String(localized: "Deep Work"),
                AlertNotificationContent.userInfoDecisionEvidence:
                    String(localized: "HRV is 18% below your baseline"),
                AlertNotificationContent.userInfoDecisionAction:
                    String(
                        localized:
                            "Move the 2:00 PM focus block to tomorrow at 9:30 AM?"
                    ),
                AlertNotificationContent.userInfoDecisionCompactPrompt:
                    String(localized: "Move Deep Work?"),
                AlertNotificationContent.userInfoDecisionBefore:
                    formatter.string(from: currentStart),
                AlertNotificationContent.userInfoDecisionAfter:
                    formatter.string(from: proposedStart),
                AlertNotificationContent.userInfoDecisionEndsAt:
                    formatter.string(from: proposedEnd),
                AlertNotificationContent.userInfoDecisionExpiresAt:
                    formatter.string(from: expiresAt),
            ]
            #if targetEnvironment(simulator)
                if
                    pairing.token == nil,
                    PairingStore.isLoopbackOrigin(pairing.baseURL)
                {
                    userInfo[
                        PairingScope
                            .debugSimulatorLoopbackBaseURLUserInfoKey
                    ] = pairing.baseURL.absoluteString
                }
            #endif
            let content = AlertNotificationContent(
                title: prompt,
                subtitle: reason,
                body: target,
                categoryID: AlertNotificationContent.actionableCategoryID,
                threadID: "healthmes-decision-demo",
                userInfo: userInfo
            )
            _ = await post(content: content)
        }
    #endif

    /// Outcome toast for actions taken from the lock screen (there is no
    /// visible UI to confirm in).
    @discardableResult
    func postOutcome(
        title: String,
        body: String,
        pairing: Pairing
    ) async -> Bool {
        guard
            let relayLease = PairingRelayGate.shared.begin(
                pairing: pairing
            )
        else {
            return false
        }
        defer {
            PairingRelayGate.shared.end(relayLease)
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.threadIdentifier = "healthmes-outcome"
        let request = UNNotificationRequest(
            identifier: "healthmes-outcome-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        do {
            let center = UNUserNotificationCenter.current()
            try await PairingStore.shared.withPairingLease(
                for: pairing
            ) {
                try await center.add(request)
            }
            return true
        } catch {
            return false
        }
    }

    func setBadge(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(count)
    }

    func clearAccountSurfaces() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let result = await NotificationDeletionBarrier().clear(
            using: notificationSurface(center)
        )
        guard case .success = result else {
            return false
        }
        do {
            try await center.setBadgeCount(0)
            return true
        } catch {
            return false
        }
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
        guard
            let pairing = PairingScope.matchingPairing(
                fingerprint: userInfo[
                    AlertNotificationContent.userInfoPairingFingerprint
                ] as? String,
                generation: PairingScope.generation(
                    from: userInfo[
                        AlertNotificationContent
                            .userInfoPairingGeneration
                    ]
                )
            )
        else {
            completionHandler()
            return
        }
        let decisionURL = (userInfo[AlertNotificationContent.userInfoDecisionURL] as? String)
            .flatMap(URL.init(string:))
        let proposalID = (userInfo[AlertNotificationContent.userInfoProposalID] as? String)
            .flatMap(UUID.init(uuidString:))

        switch response.actionIdentifier {
        case AlertNotificationActionID.yes:
            resolve(
                proposalID,
                action: .accept,
                pairing: pairing,
                completionHandler: completionHandler
            )
        case AlertNotificationActionID.no:
            resolve(
                proposalID,
                action: .decline,
                pairing: pairing,
                completionHandler: completionHandler
            )
        case AlertNotificationActionID.speak,
            AlertNotificationActionID.legacyAlternative:
            let text = (response as? UNTextInputNotificationResponse)?.userText ?? ""
            let command = SpeakCommand.compose(
                userText: text,
                proposalID: proposalID,
                title: userInfo[AlertNotificationContent.userInfoDecisionTitle] as? String,
                proposedAction: userInfo[
                    AlertNotificationContent.userInfoDecisionAction
                ] as? String
            )
            Task { @MainActor in
                AppRouter.shared.openAgentCommandDock(prefill: command)
                completionHandler()
            }
        case UNNotificationDefaultActionIdentifier:
            // Tap-through = the §8.5 "why this?" link when the alert has a
            // decision record; home otherwise.
            Task { @MainActor in
                if let decisionURL {
                    AppRouter.shared.openDecision(decisionURL)
                } else {
                    AppRouter.shared.showHome()
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
        pairing: Pairing,
        completionHandler: @escaping () -> Void
    ) {
        Task {
            defer { completionHandler() }
            guard let proposalID else {
                await postOutcome(
                    title: String(localized: "Nothing to apply"),
                    body: String(localized: "This alert has no pending proposal attached."),
                    pairing: pairing
                )
                return
            }
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
                let pending = try await api.getProposal(
                    proposalID,
                    pairing: pairing
                )
                guard pending.isActionable else {
                    let stillProposed = pending.status == .proposed
                    await postOutcome(
                        title: stillProposed
                            ? String(localized: "Proposal expired")
                            : String(localized: "Already resolved"),
                        body: stillProposed
                            ? String(localized: "The decision window closed without a change.")
                            : String(localized: "This proposal is no longer pending."),
                        pairing: pairing
                    )
                    return
                }
                let proposal = try await api.resolveProposal(
                    pending,
                    action: action,
                    // A mirrored Watch action is delivered through the iPhone
                    // delegate, which cannot reliably distinguish the device.
                    surface: "apple_notification",
                    pairing: pairing
                )
                await postOutcome(
                    title: ProposalStatusPresentation.label(for: proposal.status),
                    body: ProposalStatusPresentation.detail(for: proposal.status),
                    pairing: pairing
                )
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                guard PairingStore.shared.load() == pairing else { return }
                await postOutcome(
                    title: String(localized: "Already resolved"),
                    body: String(
                        localized: "This proposal was already decided (\(error.alreadyResolvedStatus ?? "resolved"))."
                    ),
                    pairing: pairing
                )
            } catch let error as HealthMesAPIError where error.isProposalExpired {
                guard PairingStore.shared.load() == pairing else { return }
                await postOutcome(
                    title: String(localized: "Proposal expired"),
                    body: String(localized: "The decision window closed without a change."),
                    pairing: pairing
                )
            } catch {
                guard PairingStore.shared.load() == pairing else { return }
                await postOutcome(
                    title: String(localized: "Could not reach your instance"),
                    body: String(localized: "Open the app and retry from the Home tab."),
                    pairing: pairing
                )
            }
        }
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
