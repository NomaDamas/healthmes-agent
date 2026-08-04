import UIKit
import UserNotifications
import UserNotificationsUI

final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private let badgeLabel = InsetLabel()
    private let statusLabel = UILabel()
    private let actionLabel = UILabel()
    private let timeLabel = UILabel()
    private let expiryLabel = UILabel()
    private let hintLabel = UILabel()
    private let noButton = UIButton(type: .system)
    private let yesButton = UIButton(type: .system)
    private var proposalID: UUID?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.systemBackground

        badgeLabel.text = String(localized: "Health-based schedule proposal")
        badgeLabel.font = .preferredFont(forTextStyle: .caption1)
        badgeLabel.adjustsFontForContentSizeCategory = true
        badgeLabel.textColor = UIColor(red: 0.02, green: 0.34, blue: 0.25, alpha: 1)
        badgeLabel.backgroundColor = UIColor(red: 0.84, green: 0.95, blue: 0.89, alpha: 1)
        badgeLabel.layer.cornerRadius = 8
        badgeLabel.clipsToBounds = true

        statusLabel.font = .preferredFont(forTextStyle: .headline)
        statusLabel.adjustsFontForContentSizeCategory = true
        statusLabel.textColor = .secondaryLabel
        statusLabel.numberOfLines = 2

        actionLabel.font = .preferredFont(forTextStyle: .title2)
        actionLabel.adjustsFontForContentSizeCategory = true
        actionLabel.textColor = .label
        actionLabel.numberOfLines = 3

        timeLabel.font = .preferredFont(forTextStyle: .subheadline)
        timeLabel.adjustsFontForContentSizeCategory = true
        timeLabel.textColor = .label
        timeLabel.numberOfLines = 2

        expiryLabel.font = .preferredFont(forTextStyle: .caption1)
        expiryLabel.adjustsFontForContentSizeCategory = true
        expiryLabel.textColor = .secondaryLabel

        hintLabel.text = String(localized: "Choose No or Yes below")
        hintLabel.font = .preferredFont(forTextStyle: .footnote)
        hintLabel.adjustsFontForContentSizeCategory = true
        hintLabel.textColor = UIColor(red: 0.02, green: 0.34, blue: 0.25, alpha: 1)

        var noConfiguration = UIButton.Configuration.tinted()
        noConfiguration.title = String(localized: "No")
        noConfiguration.image = UIImage(systemName: "xmark.circle")
        noConfiguration.imagePadding = 6
        noConfiguration.cornerStyle = .large
        noButton.configuration = noConfiguration
        noButton.addTarget(self, action: #selector(declineProposal), for: .touchUpInside)

        var yesConfiguration = UIButton.Configuration.filled()
        yesConfiguration.title = String(localized: "Yes")
        yesConfiguration.image = UIImage(systemName: "checkmark.circle.fill")
        yesConfiguration.imagePadding = 6
        yesConfiguration.cornerStyle = .large
        yesConfiguration.baseBackgroundColor = UIColor(
            red: 0.02, green: 0.34, blue: 0.25, alpha: 1
        )
        yesButton.configuration = yesConfiguration
        yesButton.addTarget(self, action: #selector(acceptProposal), for: .touchUpInside)

        let buttonRow = UIStackView(arrangedSubviews: [noButton, yesButton])
        buttonRow.axis = .horizontal
        buttonRow.distribution = .fillEqually
        buttonRow.spacing = 10

        let divider = UIView()
        divider.backgroundColor = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false
        divider.heightAnchor.constraint(equalToConstant: 1 / UIScreen.main.scale).isActive = true

        let stack = UIStackView(arrangedSubviews: [
            badgeLabel,
            statusLabel,
            actionLabel,
            timeLabel,
            expiryLabel,
            divider,
            hintLabel,
            buttonRow,
        ])
        stack.axis = .vertical
        stack.spacing = 8
        stack.setCustomSpacing(12, after: badgeLabel)
        stack.setCustomSpacing(12, after: statusLabel)
        stack.setCustomSpacing(12, after: actionLabel)
        stack.setCustomSpacing(12, after: expiryLabel)
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -16),
        ])
        preferredContentSize = CGSize(width: 0, height: 292)
    }

    func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        let info = content.userInfo
        installDecisionActions()
        proposalID = (info["healthmes_proposal_id"] as? String).flatMap(UUID.init(uuidString:))
        statusLabel.text =
            info["healthmes_decision_observation"] as? String ?? content.title
        actionLabel.text =
            info["healthmes_decision_action"] as? String ?? content.body

        let formatter = ISO8601DateFormatter()
        let display = DateFormatter()
        display.locale = .autoupdatingCurrent
        display.dateStyle = .none
        display.timeStyle = .short
        if
            let startText = info["healthmes_decision_after"] as? String,
            let endText = info["healthmes_decision_ends_at"] as? String,
            let start = formatter.date(from: startText),
            let end = formatter.date(from: endText)
        {
            timeLabel.text = String(
                format: String(localized: "New time: %@ – %@"),
                display.string(from: start),
                display.string(from: end)
            )
        } else {
            timeLabel.text = nil
        }

        if
            let expiryText = info["healthmes_decision_expires_at"] as? String,
            let expiry = formatter.date(from: expiryText)
        {
            expiryLabel.text = String(
                format: String(localized: "Available until %@"),
                display.string(from: expiry)
            )
        } else {
            expiryLabel.text = nil
        }
    }

    @objc private func declineProposal() {
        resolve(.decline)
    }

    @objc private func acceptProposal() {
        resolve(.accept)
    }

    private func resolve(_ action: DecisionAction) {
        guard let proposalID else {
            showFailure(String(localized: "This alert has no pending proposal attached."))
            return
        }
        setResolving(true, action: action)
        Task {
            do {
                let status = try await NotificationDecisionResolver().resolve(
                    proposalID: proposalID,
                    action: action
                )
                await MainActor.run {
                    hintLabel.text =
                        status == "accepted"
                        ? String(localized: "Yes recorded. Calendar sync will apply the change.")
                        : String(localized: "No recorded. Your calendar stays unchanged.")
                    hintLabel.textColor = UIColor(
                        red: 0.02, green: 0.34, blue: 0.25, alpha: 1
                    )
                    noButton.isHidden = true
                    yesButton.isHidden = true
                }
            } catch {
                await MainActor.run {
                    showFailure(String(localized: "Could not decide. Try again from the iPhone app."))
                }
            }
        }
    }

    private func setResolving(_ resolving: Bool, action: DecisionAction) {
        noButton.isEnabled = !resolving
        yesButton.isEnabled = !resolving
        hintLabel.text =
            action == .accept
            ? String(localized: "Recording Yes…")
            : String(localized: "Recording No…")
    }

    private func showFailure(_ message: String) {
        hintLabel.text = message
        hintLabel.textColor = .systemRed
        noButton.isEnabled = true
        yesButton.isEnabled = true
    }

    private func installDecisionActions() {
        #if targetEnvironment(simulator)
            let protectedOptions: UNNotificationActionOptions = []
        #else
            let protectedOptions: UNNotificationActionOptions = [.authenticationRequired]
        #endif
        let no = UNNotificationAction(
            identifier: "HEALTHMES_NO",
            title: String(localized: "No"),
            options: protectedOptions,
            icon: UNNotificationActionIcon(systemImageName: "xmark.circle")
        )
        let yes = UNNotificationAction(
            identifier: "HEALTHMES_YES",
            title: String(localized: "Yes"),
            options: protectedOptions,
            icon: UNNotificationActionIcon(systemImageName: "checkmark.circle.fill")
        )
        extensionContext?.notificationActions = [no, yes]
    }
}

private enum DecisionAction: String {
    case accept
    case decline
}

private struct NotificationDecisionResolver {
    private struct Proposal: Decodable {
        let status: String
        let acceptResolutionToken: String?
        let declineResolutionToken: String?

        enum CodingKeys: String, CodingKey {
            case status
            case acceptResolutionToken = "accept_resolution_token"
            case declineResolutionToken = "decline_resolution_token"
        }

        func token(for action: DecisionAction) -> String? {
            switch action {
            case .accept: acceptResolutionToken
            case .decline: declineResolutionToken
            }
        }
    }

    func resolve(proposalID: UUID, action: DecisionAction) async throws -> String {
        guard let pairing = PairingStore.shared.load() else {
            throw ResolutionError.notPaired
        }
        let proposalURL = pairing.baseURL.appendingPathComponent(
            "v1/schedule/proposals/\(proposalID.uuidString.lowercased())"
        )
        var getRequest = URLRequest(url: proposalURL)
        authorize(&getRequest, token: pairing.token)
        let (proposalData, proposalResponse) = try await URLSession.shared.data(for: getRequest)
        try requireSuccess(proposalResponse)
        let proposal = try JSONDecoder().decode(Proposal.self, from: proposalData)
        guard proposal.status == "proposed", let token = proposal.token(for: action) else {
            throw ResolutionError.notActionable
        }

        var postRequest = URLRequest(
            url: proposalURL.appendingPathComponent(action.rawValue)
        )
        postRequest.httpMethod = "POST"
        postRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&postRequest, token: pairing.token)
        postRequest.httpBody = try JSONSerialization.data(withJSONObject: [
            "resolution_token": token,
            "surface": "ios_notification",
        ])
        let (resolvedData, resolvedResponse) = try await URLSession.shared.data(for: postRequest)
        try requireSuccess(resolvedResponse)
        return try JSONDecoder().decode(Proposal.self, from: resolvedData).status
    }

    private func authorize(_ request: inout URLRequest, token: String?) {
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    private func requireSuccess(_ response: URLResponse) throws {
        guard
            let http = response as? HTTPURLResponse,
            (200...299).contains(http.statusCode)
        else {
            throw ResolutionError.requestFailed
        }
    }

    private enum ResolutionError: Error {
        case notPaired
        case notActionable
        case requestFailed
    }
}

private final class InsetLabel: UILabel {
    private let insets = UIEdgeInsets(top: 5, left: 9, bottom: 5, right: 9)

    override var intrinsicContentSize: CGSize {
        let size = super.intrinsicContentSize
        return CGSize(
            width: size.width + insets.left + insets.right,
            height: size.height + insets.top + insets.bottom
        )
    }

    override func drawText(in rect: CGRect) {
        super.drawText(in: rect.inset(by: insets))
    }
}
