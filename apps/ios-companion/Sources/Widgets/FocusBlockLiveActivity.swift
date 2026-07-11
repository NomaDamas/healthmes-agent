#if canImport(ActivityKit)
    import ActivityKit
    import SwiftUI
    import WidgetKit

    // Live Activity for the current focus block (issue #10; #7 deferred
    // item). Updates are POLLING-DERIVED only (foreground + BGAppRefreshTask;
    // no push token — local-first), so the timer ranges below do the
    // real-time work and `staleDate` handles abandonment.
    //
    // PLACEHOLDER VISUALS: layout/colors are engineering placeholders
    // proving the ActivityKit plumbing; the actual progressive-surface design
    // is the domain expert's deliverable (docs/design/WATCH-NOTIFICATIONS.ko.md,
    // grammar: docs/PLAN.md §8.5).

    /// Countdown range with a clamped lower bound. The activity routinely
    /// outlives `end` (polling-only updates; `staleDate == end` forces a
    /// re-render exactly at block end, and Always-On/luminance changes
    /// re-render any time), and `Date()...end` traps with "Range requires
    /// lowerBound <= upperBound" once `Date() > end` — crashing the widget
    /// extension instead of graying out. Clamping renders 0:00 on the stale
    /// surface, which is exactly the honest state.
    private func countdownInterval(to end: Date) -> ClosedRange<Date> {
        min(Date(), end)...end
    }

    struct FocusBlockLiveActivity: Widget {
        var body: some WidgetConfiguration {
            ActivityConfiguration(for: FocusBlockActivityAttributes.self) { context in
                // Lock-screen banner.
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(verbatim: "HEALTHMES · FOCUS")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Spacer()
                        if let demand = context.state.energyDemand {
                            Text(verbatim: demand)
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(.quaternary, in: Capsule())
                        }
                    }
                    Text(verbatim: context.state.title)
                        .font(.headline)
                        .lineLimit(1)
                    ProgressView(
                        timerInterval: context.state.start...context.state.end,
                        countsDown: false
                    )
                    .tint(.accentColor)
                    HStack {
                        Text(context.state.start, style: .time)
                        Spacer()
                        Text(
                            timerInterval: countdownInterval(to: context.state.end),
                            countsDown: true
                        )
                        .monospacedDigit()
                        Spacer()
                        Text(context.state.end, style: .time)
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                .padding(12)
                .activityBackgroundTint(nil)
                .widgetURL(URL(string: "healthmes://home"))
                .accessibilityElement(children: .combine)
            } dynamicIsland: { context in
                DynamicIsland {
                    DynamicIslandExpandedRegion(.leading) {
                        Text(verbatim: "HM")
                            .font(.headline)
                    }
                    DynamicIslandExpandedRegion(.trailing) {
                        Text(
                            timerInterval: countdownInterval(to: context.state.end),
                            countsDown: true
                        )
                        .monospacedDigit()
                        .font(.callout)
                    }
                    DynamicIslandExpandedRegion(.bottom) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: context.state.title)
                                .font(.headline)
                                .lineLimit(1)
                            ProgressView(
                                timerInterval: context.state.start...context.state.end,
                                countsDown: false
                            )
                        }
                    }
                } compactLeading: {
                    Image(systemName: "brain.head.profile")
                } compactTrailing: {
                    Text(
                        timerInterval: countdownInterval(to: context.state.end),
                        countsDown: true
                    )
                    .monospacedDigit()
                    .frame(maxWidth: 44)
                } minimal: {
                    Image(systemName: "brain.head.profile")
                }
                .widgetURL(URL(string: "healthmes://home"))
            }
        }
    }
#endif
