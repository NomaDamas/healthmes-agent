#if canImport(ActivityKit)
    import ActivityKit
    import Foundation
    import SwiftUI
    import WidgetKit

    struct DecisionLiveActivity: Widget {
        private let healthGreen = Color(red: 0.02, green: 0.34, blue: 0.25)

        var body: some WidgetConfiguration {
            ActivityConfiguration(for: DecisionActivityAttributes.self) { context in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 6) {
                        Image(systemName: "waveform.path.ecg")
                        Text(verbatim: "HEALTHMES · DECISION")
                        Spacer()
                        if isActionable(context) {
                            Text(
                                timerInterval: countdownInterval(to: context.state.expiresAt),
                                countsDown: true
                            )
                                .monospacedDigit()
                        }
                    }
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(healthGreen)

                    Text(verbatim: context.state.title)
                        .font(.headline)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)

                    Text(verbatim: statusReason(context.state, isStale: context.isStale))
                        .font(.subheadline)
                        .foregroundStyle(
                            context.state.status == .failed ? Color.red : healthGreen
                        )
                        .lineLimit(1)

                    if isActionable(context) {
                        Text(verbatim: context.state.target)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)

                        HStack(spacing: 10) {
                            Button(
                                intent: DeclineDecisionIntent(
                                    proposalID: context.attributes.proposalID
                                )
                            ) {
                                Label("No", systemImage: "xmark")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .tint(.secondary)

                            Button(
                                intent: AcceptDecisionIntent(
                                    proposalID: context.attributes.proposalID
                                )
                            ) {
                                Label("Yes", systemImage: "checkmark")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(healthGreen)
                        }
                        .font(.headline)
                    }
                }
                .padding(14)
                .activityBackgroundTint(nil)
                .widgetURL(URL(string: "healthmes://proposal?id=\(context.attributes.proposalID)"))
                .accessibilityElement(children: .contain)
            } dynamicIsland: { context in
                DynamicIsland {
                    DynamicIslandExpandedRegion(.leading) {
                        Image(systemName: "waveform.path.ecg")
                            .foregroundStyle(healthGreen)
                    }
                    DynamicIslandExpandedRegion(.trailing) {
                        if isActionable(context) {
                            Text(
                                timerInterval: countdownInterval(to: context.state.expiresAt),
                                countsDown: true
                            )
                            .monospacedDigit()
                        }
                    }
                    DynamicIslandExpandedRegion(.bottom) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(verbatim: context.state.title)
                                .font(.headline)
                                .lineLimit(1)
                            Text(
                                verbatim: statusReason(
                                    context.state,
                                    isStale: context.isStale
                                )
                            )
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if isActionable(context) {
                                HStack {
                                    Button(
                                        intent: DeclineDecisionIntent(
                                            proposalID: context.attributes.proposalID
                                        )
                                    ) {
                                        Label("No", systemImage: "xmark")
                                    }
                                    Button(
                                        intent: AcceptDecisionIntent(
                                            proposalID: context.attributes.proposalID
                                        )
                                    ) {
                                        Label("Yes", systemImage: "checkmark")
                                    }
                                }
                                .buttonStyle(.bordered)
                            }
                        }
                    }
                } compactLeading: {
                    Image(systemName: "waveform.path.ecg")
                        .foregroundStyle(healthGreen)
                } compactTrailing: {
                    Image(systemName: "questionmark.circle.fill")
                } minimal: {
                    Image(systemName: "waveform.path.ecg")
                        .foregroundStyle(healthGreen)
                }
                .widgetURL(URL(string: "healthmes://proposal?id=\(context.attributes.proposalID)"))
            }
        }

        private func statusReason(
            _ state: DecisionActivityAttributes.ContentState,
            isStale: Bool
        ) -> String {
            if state.status == .pending, isStale {
                return String(localized: "Decision expired · calendar unchanged")
            }

            switch state.status {
            case .pending:
                return state.reason
            case .applying:
                return String(localized: "Applying…")
            case .accepted:
                return String(localized: "Yes recorded · calendar sync pending")
            case .pushed:
                return String(localized: "Applied to calendar")
            case .declined:
                return String(localized: "No recorded · calendar unchanged")
            case .alreadyAccepted:
                return String(localized: "Already approved on another device")
            case .alreadyPushed:
                return String(localized: "Already applied to calendar")
            case .alreadyDeclined:
                return String(localized: "Already declined on another device")
            case .expired:
                return String(localized: "Decision expired · calendar unchanged")
            case .failed:
                return String(localized: "Could not decide · open HealthMes")
            }
        }

        private func isActionable(
            _ context: ActivityViewContext<DecisionActivityAttributes>
        ) -> Bool {
            context.state.status == .pending
                && !context.isStale
                && context.state.expiresAt > Date()
        }

        private func countdownInterval(to end: Date) -> ClosedRange<Date> {
            min(Date(), end)...end
        }
    }
#endif
