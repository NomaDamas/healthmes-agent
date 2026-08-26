import SwiftUI
import UserNotifications

struct WatchDecisionNotificationModel {
    let heading: String
    let action: String
    let reason: String?
    let evidence: String?
    let before: Date?
    let after: Date?
    let endsAt: Date?

    init(content: UNNotificationContent) {
        let info = content.userInfo
        let formatter = ISO8601DateFormatter()
        heading = content.title
        action =
            (info[AlertNotificationContent.userInfoDecisionAction] as? String)
            ?? content.title
        reason =
            (info[AlertNotificationContent.userInfoDecisionObservation] as? String)
            ?? (content.subtitle.isEmpty ? nil : content.subtitle)
        evidence =
            info[AlertNotificationContent.userInfoDecisionEvidence] as? String
        before =
            (info[AlertNotificationContent.userInfoDecisionBefore] as? String)
            .flatMap(formatter.date(from:))
        after =
            (info[AlertNotificationContent.userInfoDecisionAfter] as? String)
            .flatMap(formatter.date(from:))
        endsAt =
            (info[AlertNotificationContent.userInfoDecisionEndsAt] as? String)
            .flatMap(formatter.date(from:))
    }

    static let placeholder = WatchDecisionNotificationModel(
        heading: String(localized: "Review this change?"),
        action: String(localized: "Review this change?"),
        reason: nil,
        evidence: nil,
        before: nil,
        after: nil,
        endsAt: nil
    )

    private init(
        heading: String,
        action: String,
        reason: String?,
        evidence: String?,
        before: Date?,
        after: Date?,
        endsAt: Date?
    ) {
        self.heading = heading
        self.action = action
        self.reason = reason
        self.evidence = evidence
        self.before = before
        self.after = after
        self.endsAt = endsAt
    }
}

final class WatchDecisionNotificationController:
    WKUserNotificationHostingController<WatchDecisionNotificationView>
{
    private var model = WatchDecisionNotificationModel.placeholder

    override class var isInteractive: Bool { true }
    override class var sashColor: Color? {
        Color(red: 0.89, green: 0.29, blue: 0.15)
    }
    override class var titleColor: Color? { .white }
    override class var subtitleColor: Color? { .white.opacity(0.72) }

    override var body: WatchDecisionNotificationView {
        WatchDecisionNotificationView(model: model)
    }

    override func didReceive(_ notification: UNNotification) {
        model = WatchDecisionNotificationModel(content: notification.request.content)
        notificationActions = Self.decisionActions
    }

    private static var decisionActions: [UNNotificationAction] {
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
        return [no, yes, speak]
    }
}

struct WatchDecisionNotificationView: View {
    let model: WatchDecisionNotificationModel
    @State private var showsWhy = false

    private let brand = Color(red: 1.0, green: 0.38, blue: 0.22)
    private let decisionBlue = Color(red: 0.47, green: 0.66, blue: 1.0)
    private let reasonAmber = Color(red: 1.0, green: 0.73, blue: 0.34)

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Label("HEALTHMES WELLNESS", systemImage: "sun.max.fill")
                .font(.system(size: 10, weight: .bold, design: .rounded))
                .foregroundStyle(brand)

            Text(verbatim: model.heading)
                .font(.system(.headline, design: .rounded).weight(.bold))
                .fixedSize(horizontal: false, vertical: true)

            if model.before != nil || model.after != nil {
                Divider()
                    .overlay(.white.opacity(0.18))

                VStack(alignment: .leading, spacing: 5) {
                    if let before = model.before {
                        scheduleRow(
                            label: String(localized: "FROM"),
                            date: before,
                            tint: .secondary
                        )
                    }
                    if let after = model.after {
                        scheduleRow(
                            label: String(localized: "TO"),
                            date: after,
                            end: model.endsAt,
                            tint: decisionBlue
                        )
                    }
                }
            }

            if let reason = model.reason {
                Divider()
                    .overlay(.white.opacity(0.18))

                Text(verbatim: reason)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)
                    .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(reasonAmber)
            }

            if model.reason != nil || model.evidence != nil {
                Button {
                    withAnimation(.easeInOut(duration: 0.18)) {
                        showsWhy.toggle()
                    }
                } label: {
                    HStack {
                        Label("Why?", systemImage: "info.circle")
                        Spacer()
                        Image(systemName: showsWhy ? "chevron.up" : "chevron.down")
                    }
                    .font(.caption2.weight(.semibold))
                }
                .buttonStyle(.plain)
                .foregroundStyle(brand)

                if showsWhy {
                    VStack(alignment: .leading, spacing: 5) {
                        if let reason = model.reason {
                            Text("WHY THIS")
                                .font(.system(size: 10, weight: .bold, design: .rounded))
                                .foregroundStyle(.secondary)
                            Text(verbatim: reason)
                                .font(.caption2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let evidence = model.evidence {
                            Text(verbatim: evidence)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if model.action != model.heading {
                            Text("PROPOSED CHANGE")
                                .font(.system(size: 10, weight: .bold, design: .rounded))
                                .foregroundStyle(.secondary)
                                .padding(.top, 2)
                            Text(verbatim: model.action)
                                .font(.caption2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        Color.white.opacity(0.07),
                        in: RoundedRectangle(cornerRadius: 9)
                    )
                }
            }

            Divider()
                .overlay(.white.opacity(0.18))

            Label("Scroll for No · Yes · Speak", systemImage: "digitalcrown.arrow.clockwise")
                .font(.system(size: 10, weight: .semibold, design: .rounded))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 2)
        .padding(.bottom, 4)
    }

    private func scheduleRow(
        label: String,
        date: Date,
        end: Date? = nil,
        tint: Color
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            Text(verbatim: label)
                .font(.system(size: 9, weight: .bold, design: .rounded))
                .foregroundStyle(.secondary)
                .frame(width: 29, alignment: .leading)

            Text(verbatim: relativeDay(date))
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)

            Spacer(minLength: 2)

            Text(verbatim: timeRange(start: date, end: end))
                .font(.system(size: 11, weight: .bold, design: .rounded))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
        }
        .accessibilityElement(children: .combine)
    }

    private func relativeDay(_ date: Date) -> String {
        let calendar = Calendar.autoupdatingCurrent
        if calendar.isDateInToday(date) {
            return String(localized: "Today")
        }
        if calendar.isDateInTomorrow(date) {
            return String(localized: "Tomorrow")
        }
        return date.formatted(.dateTime.weekday(.wide))
    }

    private func timeRange(start: Date, end: Date?) -> String {
        let startText = start.formatted(date: .omitted, time: .shortened)
        guard let end else { return startText }
        let endText = end.formatted(date: .omitted, time: .shortened)

        let period = DateFormatter()
        period.locale = .autoupdatingCurrent
        period.dateFormat = "a"
        let startPeriod = period.string(from: start)
        let endPeriod = period.string(from: end)
        guard !startPeriod.isEmpty, startPeriod == endPeriod else {
            return "\(startText)–\(endText)"
        }

        let compactStart = startText
            .replacingOccurrences(of: startPeriod, with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return "\(compactStart)–\(endText)"
    }
}
