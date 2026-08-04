import UIKit
import UserNotifications
import UserNotificationsUI

final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private let signalIconView = UIImageView()
    private let signalLabel = UILabel()
    private let statusLabel = UILabel()
    private let actionLabel = UILabel()
    private let timeLabel = UILabel()
    private let hintLabel = UILabel()
    private let noButton = UIButton(type: .system)
    private let yesButton = UIButton(type: .system)
    private var proposalID: UUID?
    private let healthGreen = UIColor(red: 0.02, green: 0.34, blue: 0.25, alpha: 1)

    override func viewDidLoad() {
        super.viewDidLoad()
        // The notification container already supplies Liquid Glass. Keeping
        // this view clear avoids stacking an opaque card inside that material.
        view.backgroundColor = .clear

        signalIconView.image = UIImage(systemName: "waveform.path.ecg")
        signalIconView.preferredSymbolConfiguration = UIImage.SymbolConfiguration(
            pointSize: 13,
            weight: .semibold
        )
        signalIconView.tintColor = healthGreen
        signalIconView.contentMode = .scaleAspectFit
        signalIconView.setContentHuggingPriority(.required, for: .horizontal)

        signalLabel.text = String(localized: "HEALTHMES · DECISION")
        signalLabel.font = .preferredFont(forTextStyle: .caption1)
        signalLabel.adjustsFontForContentSizeCategory = true
        signalLabel.textColor = healthGreen

        statusLabel.font = .preferredFont(forTextStyle: .subheadline)
        statusLabel.adjustsFontForContentSizeCategory = true
        statusLabel.textColor = .secondaryLabel
        statusLabel.numberOfLines = 1

        actionLabel.font = .preferredFont(forTextStyle: .title3)
        actionLabel.adjustsFontForContentSizeCategory = true
        actionLabel.textColor = .label
        actionLabel.numberOfLines = 3

        timeLabel.font = .preferredFont(forTextStyle: .subheadline)
        timeLabel.adjustsFontForContentSizeCategory = true
        timeLabel.textColor = .secondaryLabel
        timeLabel.numberOfLines = 2

        hintLabel.text = String(localized: "Choose without opening the app")
        hintLabel.font = .preferredFont(forTextStyle: .footnote)
        hintLabel.adjustsFontForContentSizeCategory = true
        hintLabel.textColor = .tertiaryLabel

        var noConfiguration: UIButton.Configuration
        var yesConfiguration: UIButton.Configuration
        if #available(iOS 26.0, *) {
            noConfiguration = .glass()
            yesConfiguration = .prominentGlass()
        } else {
            noConfiguration = .tinted()
            yesConfiguration = .filled()
        }
        noConfiguration.title = String(localized: "No")
        noConfiguration.image = UIImage(systemName: "xmark")
        noConfiguration.imagePadding = 6
        noConfiguration.cornerStyle = .large
        noConfiguration.baseForegroundColor = .label
        noButton.configuration = noConfiguration
        noButton.accessibilityIdentifier = "healthmes-decision-no"
        noButton.addTarget(self, action: #selector(declineProposal), for: .touchUpInside)

        yesConfiguration.title = String(localized: "Yes")
        yesConfiguration.image = UIImage(systemName: "checkmark")
        yesConfiguration.imagePadding = 6
        yesConfiguration.cornerStyle = .large
        yesConfiguration.baseBackgroundColor = healthGreen
        yesConfiguration.baseForegroundColor = .white
        yesButton.configuration = yesConfiguration
        yesButton.accessibilityIdentifier = "healthmes-decision-yes"
        yesButton.addTarget(self, action: #selector(acceptProposal), for: .touchUpInside)

        let signalRow = UIStackView(arrangedSubviews: [signalIconView, signalLabel])
        signalRow.axis = .horizontal
        signalRow.alignment = .center
        signalRow.spacing = 6

        let buttonRow = UIStackView(arrangedSubviews: [noButton, yesButton])
        buttonRow.axis = .horizontal
        buttonRow.distribution = .fillEqually
        buttonRow.spacing = 10

        let stack = UIStackView(arrangedSubviews: [
            signalRow,
            statusLabel,
            actionLabel,
            timeLabel,
            hintLabel,
            buttonRow,
        ])
        stack.axis = .vertical
        stack.spacing = 7
        stack.setCustomSpacing(10, after: signalRow)
        stack.setCustomSpacing(5, after: statusLabel)
        stack.setCustomSpacing(10, after: actionLabel)
        stack.setCustomSpacing(14, after: hintLabel)
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -14),
            signalIconView.widthAnchor.constraint(equalToConstant: 18),
            signalIconView.heightAnchor.constraint(equalToConstant: 18),
            noButton.heightAnchor.constraint(greaterThanOrEqualToConstant: 48),
            yesButton.heightAnchor.constraint(greaterThanOrEqualToConstant: 48),
        ])
        preferredContentSize = CGSize(width: 0, height: 244)
    }

    func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        let info = content.userInfo
        // The category actions still mirror to Apple Watch. On iPhone this
        // custom card owns the interaction, so suppress the duplicate native
        // action list below it.
        extensionContext?.notificationActions = []
        proposalID = (info["healthmes_proposal_id"] as? String).flatMap(UUID.init(uuidString:))
        statusLabel.text =
            info["healthmes_decision_observation"] as? String ?? content.title
        actionLabel.text =
            info["healthmes_decision_action"] as? String ?? content.body

        let formatter = ISO8601DateFormatter()
        let dayDisplay = DateFormatter()
        dayDisplay.locale = .autoupdatingCurrent
        dayDisplay.dateStyle = .medium
        dayDisplay.timeStyle = .none
        dayDisplay.doesRelativeDateFormatting = true
        let timeDisplay = DateFormatter()
        timeDisplay.locale = .autoupdatingCurrent
        timeDisplay.dateStyle = .none
        timeDisplay.timeStyle = .short
        if
            let startText = info["healthmes_decision_after"] as? String,
            let endText = info["healthmes_decision_ends_at"] as? String,
            let start = formatter.date(from: startText),
            let end = formatter.date(from: endText)
        {
            timeLabel.text = String(
                format: String(localized: "%@ · %@ – %@"),
                dayDisplay.string(from: start),
                timeDisplay.string(from: start),
                timeDisplay.string(from: end)
            )
        } else {
            timeLabel.text = nil
        }

        if
            let expiryText = info["healthmes_decision_expires_at"] as? String,
            let expiry = formatter.date(from: expiryText)
        {
            hintLabel.text = String(
                format: String(localized: "Decide by %@ · no app needed"),
                timeDisplay.string(from: expiry)
            )
        } else {
            hintLabel.text = String(localized: "Choose without opening the app")
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
                    hintLabel.textColor = healthGreen
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
