import Foundation

#if canImport(ActivityKit)
    import ActivityKit
#endif

/// Keeps at most one actionable decision on the lock screen. ActivityKit can
/// update from the background but may only start while the app is foreground.
final class DecisionLiveActivityController {
    static let shared = DecisionLiveActivityController()

    private init() {}

    func sync(alerts: [AlertItem], isForeground: Bool, now: Date = Date()) async {
        #if canImport(ActivityKit)
            guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }

            let candidate = alerts.first {
                $0.proposalId != nil
                    && $0.decisionCard != nil
                    && ($0.decisionCard?.expiresAt ?? .distantPast) > now
            }
            let running = Activity<DecisionActivityAttributes>.activities

            guard
                let alert = candidate,
                let proposalID = alert.proposalId,
                let card = alert.decisionCard
            else {
                for activity in running {
                    await activity.end(activity.content, dismissalPolicy: .immediate)
                }
                return
            }

            let state = DecisionActivityAttributes.ContentState(
                title: AlertNotificationContent.decisionPrompt(for: card),
                reason: AlertNotificationContent.compactLine(card.observationShort, limit: 34),
                target: AlertNotificationContent.targetLine(after: card.after),
                expiresAt: card.expiresAt
            )
            let content = ActivityContent(state: state, staleDate: card.expiresAt)
            let proposalText = proposalID.uuidString.lowercased()

            if let matching = running.first(where: {
                $0.attributes.proposalID == proposalText
            }) {
                await matching.update(content)
                for stray in running where stray.id != matching.id {
                    await stray.end(stray.content, dismissalPolicy: .immediate)
                }
            } else {
                for activity in running {
                    await activity.end(activity.content, dismissalPolicy: .immediate)
                }
                if isForeground {
                    _ = try? Activity.request(
                        attributes: DecisionActivityAttributes(proposalID: proposalText),
                        content: content
                    )
                }
            }
        #endif
    }

    #if DEBUG
        func startDemo(
            proposalID: UUID,
            title: String,
            reason: String,
            target: String,
            expiresAt: Date
        ) async {
            #if canImport(ActivityKit)
                guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
                for activity in Activity<DecisionActivityAttributes>.activities {
                    await activity.end(activity.content, dismissalPolicy: .immediate)
                }
                let state = DecisionActivityAttributes.ContentState(
                    title: title,
                    reason: reason,
                    target: target,
                    expiresAt: expiresAt
                )
                _ = try? Activity.request(
                    attributes: DecisionActivityAttributes(
                        proposalID: proposalID.uuidString.lowercased()
                    ),
                    content: ActivityContent(state: state, staleDate: expiresAt)
                )
            #endif
        }
    #endif
}
