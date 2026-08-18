#if canImport(ActivityKit)
    import ActivityKit
    import Foundation
    import SwiftUI
    import WidgetKit

    struct DecisionLiveActivity: Widget {
        private let brand = Color(red: 0.89, green: 0.29, blue: 0.15)
        private let decisionBlue = Color(red: 0.24, green: 0.44, blue: 0.84)

        var body: some WidgetConfiguration {
            ActivityConfiguration(for: DecisionActivityAttributes.self) { context in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Image(systemName: "sun.max.fill")
                        Text(verbatim: "HEALTHMES")
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
                    .foregroundStyle(brand)

                    Text(verbatim: context.state.title)
                        .font(.headline)
                        .lineLimit(2)
                        .minimumScaleFactor(0.88)

                    if isActionable(context) {
                        Label {
                            Text(verbatim: context.state.target)
                                .lineLimit(1)
                                .minimumScaleFactor(0.85)
                        } icon: {
                            Image(systemName: "calendar.badge.clock")
                        }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(decisionBlue)

                        Text(verbatim: context.state.reason)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)

                        HStack(spacing: 8) {
                            Button(
                                intent: DeclineDecisionIntent(
                                    proposalID: context.attributes.proposalID
                                )
                            ) {
                                Text("No")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .tint(.secondary)

                            Button(
                                intent: AcceptDecisionIntent(
                                    proposalID: context.attributes.proposalID
                                )
                            ) {
                                Text("Yes")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(decisionBlue)

                            Link(destination: speakURL(context.attributes.proposalID)) {
                                Label("Speak", systemImage: "microphone.fill")
                                    .labelStyle(.titleAndIcon)
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)
                            .tint(brand)
                        }
                        .font(.caption.weight(.semibold))
                        .controlSize(.small)
                    } else {
                        Text(verbatim: statusReason(context.state, isStale: context.isStale))
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(
                                context.state.status == .failed ? Color.red : decisionBlue
                            )
                            .lineLimit(2)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .activityBackgroundTint(nil)
                .widgetURL(URL(string: "healthmes://proposal?id=\(context.attributes.proposalID)"))
                .accessibilityElement(children: .contain)
            } dynamicIsland: { context in
                DynamicIsland {
                    DynamicIslandExpandedRegion(.leading) {
                        Image(systemName: "sun.max.fill")
                            .foregroundStyle(brand)
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
                                .lineLimit(2)
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
                                    Link(
                                        destination: speakURL(
                                            context.attributes.proposalID
                                        )
                                    ) {
                                        Label("Speak", systemImage: "microphone.fill")
                                    }
                                }
                                .buttonStyle(.bordered)
                            }
                        }
                    }
                } compactLeading: {
                    Image(systemName: "sun.max.fill")
                        .foregroundStyle(brand)
                } compactTrailing: {
                    Image(systemName: "questionmark.circle.fill")
                } minimal: {
                    Image(systemName: "sun.max.fill")
                        .foregroundStyle(brand)
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

        private func speakURL(_ proposalID: String) -> URL {
            URL(
                string:
                    "healthmes://speak?proposal=\(proposalID)&autostart=1"
            )!
        }

    }
#endif
