import Foundation

#if canImport(ActivityKit)
    import ActivityKit
#endif

/// Starts/updates/ends the focus-block Live Activity from glance payloads
/// (issue #10, carried over from #7's deferred list).
///
/// Polling only, by design: no push token is ever requested (APNs is out of
/// scope, local-first). Updates arrive when the app comes to the foreground
/// or a BGAppRefreshTask runs; `staleDate = block end` lets iOS gray the
/// surface out by itself when no budget arrives. ActivityKit also forbids
/// *starting* activities from the background — starts are foreground-only.
final class LiveActivityController {
    static let shared = LiveActivityController()

    private init() {}

    func sync(payload: GlancePayload, isForeground: Bool, now: Date = Date()) async {
        #if canImport(ActivityKit)
            guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }

            let currentBlock = FocusBlockSelector.current(in: payload.nextBlocks, now: now)
            let running = Activity<FocusBlockActivityAttributes>.activities

            guard let block = currentBlock else {
                // No block running: end whatever is still on screen.
                for activity in running {
                    await activity.end(activity.content, dismissalPolicy: .immediate)
                }
                return
            }

            let state = FocusBlockActivityAttributes.ContentState(
                title: block.title ?? String(localized: "Focus block"),
                start: block.start,
                end: block.end,
                energyDemand: block.energyDemand?.rawValue
            )
            let content = ActivityContent(state: state, staleDate: block.end)

            if let activity = running.first {
                await activity.update(content)
                // Never more than one focus activity.
                for stray in running.dropFirst() {
                    await stray.end(stray.content, dismissalPolicy: .immediate)
                }
            } else if isForeground {
                // Starting is a foreground-only ActivityKit capability.
                _ = try? Activity.request(
                    attributes: FocusBlockActivityAttributes(),
                    content: content
                )
            }
        #endif
    }
}
