import SwiftUI
import UserNotifications

struct WatchDecisionDetail: Identifiable {
    let id = UUID()
    let prompt: String
    let target: String
    let observation: String?
    let evidence: String?
    let action: String?
    let before: Date?
    let after: Date?
    let endsAt: Date?
}

@MainActor
final class WatchDecisionInbox: ObservableObject {
    static let shared = WatchDecisionInbox()

    @Published var detail: WatchDecisionDetail?

    func present(content: UNNotificationContent) {
        let info = content.userInfo
        let formatter = ISO8601DateFormatter()
        detail = WatchDecisionDetail(
            prompt: content.title,
            target: content.body,
            observation: info[AlertNotificationContent.userInfoDecisionObservation] as? String,
            evidence: info[AlertNotificationContent.userInfoDecisionEvidence] as? String,
            action: info[AlertNotificationContent.userInfoDecisionAction] as? String,
            before: (info[AlertNotificationContent.userInfoDecisionBefore] as? String)
                .flatMap(formatter.date(from:)),
            after: (info[AlertNotificationContent.userInfoDecisionAfter] as? String)
                .flatMap(formatter.date(from:)),
            endsAt: (info[AlertNotificationContent.userInfoDecisionEndsAt] as? String)
                .flatMap(formatter.date(from:))
        )
    }
}

struct WatchDecisionDetailView: View {
    let detail: WatchDecisionDetail

    private var changeLine: String? {
        guard let after = detail.after else { return nil }
        let time = DateFormatter()
        time.locale = .autoupdatingCurrent
        time.timeStyle = .short
        time.dateStyle = .none

        let destination: String
        if let endsAt = detail.endsAt {
            destination = "\(time.string(from: after))–\(time.string(from: endsAt))"
        } else {
            destination = time.string(from: after)
        }
        guard let before = detail.before else { return destination }
        return "\(time.string(from: before)) → \(destination)"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 9) {
                Text(detail.prompt)
                    .font(.headline)
                Text(detail.target)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                Divider()

                if let observation = detail.observation {
                    detailSection(String(localized: "Why this?"), value: observation)
                }
                if let evidence = detail.evidence {
                    Text(evidence)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                if let changeLine {
                    detailSection(String(localized: "What changes"), value: changeLine)
                }
                if let action = detail.action {
                    Text(action)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func detailSection(_ title: String, value: String) -> some View {
        Text(title.uppercased())
            .font(.caption2)
            .foregroundStyle(.tertiary)
        Text(value)
            .font(.footnote)
    }
}
