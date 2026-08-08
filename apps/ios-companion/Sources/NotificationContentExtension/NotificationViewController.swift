import LocalAuthentication
import UIKit
import UserNotifications
import UserNotificationsUI

final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private let signalIconView = UIImageView()
    private let signalLabel = UILabel()
    private let actionLabel = UILabel()
    private let reasonLabel = UILabel()
    private let timeLabel = UILabel()
    private let hintLabel = UILabel()
    private let detailScrollView = UIScrollView()
    private let detailLabel = UILabel()
    private let noButton = UIButton(type: .system)
    private let yesButton = UIButton(type: .system)
    private var proposalID: UUID?
    private var expiresAt: Date?
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

        actionLabel.font = .preferredFont(forTextStyle: .title3)
        actionLabel.adjustsFontForContentSizeCategory = true
        actionLabel.textColor = .label
        actionLabel.numberOfLines = 1
        actionLabel.adjustsFontSizeToFitWidth = true
        actionLabel.minimumScaleFactor = 0.78

        reasonLabel.font = .preferredFont(forTextStyle: .subheadline)
        reasonLabel.adjustsFontForContentSizeCategory = true
        reasonLabel.textColor = healthGreen
        reasonLabel.numberOfLines = 1
        reasonLabel.adjustsFontSizeToFitWidth = true
        reasonLabel.minimumScaleFactor = 0.8

        timeLabel.font = .preferredFont(forTextStyle: .subheadline)
        timeLabel.adjustsFontForContentSizeCategory = true
        timeLabel.textColor = .secondaryLabel
        timeLabel.numberOfLines = 1

        hintLabel.text = String(localized: "Details")
        hintLabel.font = .preferredFont(forTextStyle: .footnote)
        hintLabel.adjustsFontForContentSizeCategory = true
        hintLabel.textColor = .tertiaryLabel

        detailScrollView.alwaysBounceVertical = true
        detailScrollView.showsVerticalScrollIndicator = true
        detailLabel.numberOfLines = 0
        detailLabel.adjustsFontForContentSizeCategory = true
        detailLabel.accessibilityIdentifier = "healthmes-decision-details"
        detailLabel.translatesAutoresizingMaskIntoConstraints = false
        detailScrollView.addSubview(detailLabel)
        NSLayoutConstraint.activate([
            detailLabel.leadingAnchor.constraint(equalTo: detailScrollView.contentLayoutGuide.leadingAnchor),
            detailLabel.trailingAnchor.constraint(equalTo: detailScrollView.contentLayoutGuide.trailingAnchor),
            detailLabel.topAnchor.constraint(equalTo: detailScrollView.contentLayoutGuide.topAnchor),
            detailLabel.bottomAnchor.constraint(equalTo: detailScrollView.contentLayoutGuide.bottomAnchor),
            detailLabel.widthAnchor.constraint(equalTo: detailScrollView.frameLayoutGuide.widthAnchor),
        ])

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
            actionLabel,
            reasonLabel,
            timeLabel,
            buttonRow,
            hintLabel,
            detailScrollView,
        ])
        stack.axis = .vertical
        stack.spacing = 7
        stack.setCustomSpacing(10, after: signalRow)
        stack.setCustomSpacing(4, after: actionLabel)
        stack.setCustomSpacing(4, after: reasonLabel)
        stack.setCustomSpacing(12, after: timeLabel)
        stack.setCustomSpacing(12, after: buttonRow)
        stack.setCustomSpacing(4, after: hintLabel)
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
            detailScrollView.heightAnchor.constraint(equalToConstant: 104),
        ])
        preferredContentSize = CGSize(width: 0, height: 326)
    }

    func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        let info = content.userInfo
        // The category actions still mirror to Apple Watch. On iPhone this
        // custom card owns the interaction, so suppress the duplicate native
        // action list below it.
        extensionContext?.notificationActions = []
        proposalID = (info["healthmes_proposal_id"] as? String).flatMap(UUID.init(uuidString:))
        actionLabel.text = content.title
        reasonLabel.text = content.subtitle
        reasonLabel.isHidden = content.subtitle.isEmpty
        timeLabel.text = content.body
        detailLabel.attributedText = detailText(info: info)
        detailScrollView.setContentOffset(.zero, animated: false)

        let formatter = ISO8601DateFormatter()
        let timeDisplay = DateFormatter()
        timeDisplay.locale = .autoupdatingCurrent
        timeDisplay.dateStyle = .none
        timeDisplay.timeStyle = .short

        if
            let expiryText = info["healthmes_decision_expires_at"] as? String,
            let expiry = formatter.date(from: expiryText)
        {
            expiresAt = expiry
            hintLabel.text = String(
                format: String(localized: "Details · decide by %@"),
                timeDisplay.string(from: expiry)
            )
            if expiry <= Date() {
                showTerminal(
                    String(localized: "Decision expired"),
                    color: .secondaryLabel
                )
                return
            }
        } else {
            expiresAt = nil
            hintLabel.text = String(localized: "Details")
        }

        validateCurrentState()
    }

    private func detailText(info: [AnyHashable: Any]) -> NSAttributedString {
        let result = NSMutableAttributedString()
        let sectionFont = UIFont.preferredFont(forTextStyle: .caption1)
        let bodyFont = UIFont.preferredFont(forTextStyle: .footnote)
        let sectionAttributes: [NSAttributedString.Key: Any] = [
            .font: sectionFont,
            .foregroundColor: UIColor.tertiaryLabel,
        ]
        let bodyAttributes: [NSAttributedString.Key: Any] = [
            .font: bodyFont,
            .foregroundColor: UIColor.secondaryLabel,
        ]

        func appendSection(_ title: String, lines: [String]) {
            let visibleLines = lines.filter { !$0.isEmpty }
            guard !visibleLines.isEmpty else { return }
            if result.length > 0 {
                result.append(NSAttributedString(string: "\n\n"))
            }
            result.append(
                NSAttributedString(string: title.uppercased() + "\n", attributes: sectionAttributes)
            )
            result.append(
                NSAttributedString(
                    string: visibleLines.joined(separator: "\n"),
                    attributes: bodyAttributes
                )
            )
        }

        appendSection(
            String(localized: "Why this?"),
            lines: [
                info[NotificationUserInfoKey.observation] as? String ?? "",
                info[NotificationUserInfoKey.evidence] as? String ?? "",
            ]
        )

        let formatter = ISO8601DateFormatter()
        let time = DateFormatter()
        time.locale = .autoupdatingCurrent
        time.dateStyle = .none
        time.timeStyle = .short
        let before = (info[NotificationUserInfoKey.before] as? String)
            .flatMap(formatter.date(from:))
        let after = (info[NotificationUserInfoKey.after] as? String)
            .flatMap(formatter.date(from:))
        let endsAt = (info[NotificationUserInfoKey.endsAt] as? String)
            .flatMap(formatter.date(from:))
        var scheduleLine = ""
        if let after {
            let destination =
                endsAt.map { "\(time.string(from: after))–\(time.string(from: $0))" }
                ?? time.string(from: after)
            scheduleLine =
                before.map { "\(time.string(from: $0)) → \(destination)" }
                ?? destination
        }
        appendSection(
            String(localized: "What changes"),
            lines: [
                scheduleLine,
                info[NotificationUserInfoKey.action] as? String ?? "",
            ]
        )
        return result
    }

    @objc private func declineProposal() {
        resolve(.decline)
    }

    @objc private func acceptProposal() {
        resolve(.accept)
    }

    private func resolve(_ action: DecisionAction) {
        if let expiresAt, expiresAt <= Date() {
            showTerminal(String(localized: "Decision expired"), color: .secondaryLabel)
            return
        }
        guard let proposalID else {
            showFailure(String(localized: "This alert has no pending proposal attached."))
            return
        }
        authenticateAndResolve(proposalID: proposalID, action: action)
    }

    private func authenticateAndResolve(proposalID: UUID, action: DecisionAction) {
        setResolving(true, action: action)
        let context = LAContext()
        context.localizedCancelTitle = String(localized: "Cancel")
        var authorizationError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &authorizationError) else {
            showFailure(String(localized: "Unlock the iPhone to decide."))
            return
        }
        context.evaluatePolicy(
            .deviceOwnerAuthentication,
            localizedReason: String(localized: "Confirm this HealthMes calendar decision.")
        ) { [weak self] approved, _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                guard approved else {
                    showFailure(String(localized: "Decision not changed."))
                    return
                }
                performResolution(proposalID: proposalID, action: action)
            }
        }
    }

    private func performResolution(proposalID: UUID, action: DecisionAction) {
        Task {
            do {
                let status = try await NotificationDecisionResolver().resolve(
                    proposalID: proposalID,
                    action: action
                )
                await MainActor.run {
                    guard let proposalStatus = NotificationProposalStatus(rawValue: status) else {
                        showFailure(
                            String(localized: "Could not decide. Try again from the iPhone app.")
                        )
                        return
                    }
                    hintLabel.text = proposalStatus.message
                    hintLabel.textColor =
                        proposalStatus == .declined || proposalStatus == .invalidated
                        ? .secondaryLabel
                        : healthGreen
                    noButton.isHidden = true
                    yesButton.isHidden = true
                }
            } catch {
                await MainActor.run {
                    if let error = error as? NotificationResolutionError {
                        showResolutionError(error)
                    } else {
                        showFailure(
                            String(localized: "Could not decide. Try again from the iPhone app.")
                        )
                    }
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

    private func showTerminal(_ message: String, color: UIColor) {
        hintLabel.text = message
        hintLabel.textColor = color
        noButton.isHidden = true
        yesButton.isHidden = true
    }

    private func showResolutionError(_ error: NotificationResolutionError) {
        switch error {
        case .notPaired, .requestFailed:
            showFailure(String(localized: "Could not decide. Try again from the iPhone app."))
        case .notActionable(let status):
            switch status {
            case "accepted":
                showTerminal(String(localized: "Already approved"), color: healthGreen)
            case "pushed":
                showTerminal(String(localized: "Applied to calendar"), color: healthGreen)
            case "declined":
                showTerminal(String(localized: "Already declined"), color: .secondaryLabel)
            default:
                showTerminal(String(localized: "Decision expired"), color: .secondaryLabel)
            }
        case .expired:
            showTerminal(String(localized: "Decision expired"), color: .secondaryLabel)
        }
    }

    private func validateCurrentState() {
        guard let proposalID else {
            noButton.isHidden = true
            yesButton.isHidden = true
            return
        }
        Task {
            do {
                let proposal = try await NotificationDecisionResolver().currentProposal(
                    proposalID: proposalID
                )
                guard !proposal.isActionable else { return }
                await MainActor.run {
                    showResolutionError(.notActionable(status: proposal.status))
                }
            } catch {
                // Keep buttons on transient validation failure. The action
                // handler revalidates against the server before mutation.
            }
        }
    }

}

private enum NotificationUserInfoKey {
    static let observation = "healthmes_decision_observation"
    static let evidence = "healthmes_decision_evidence"
    static let action = "healthmes_decision_action"
    static let before = "healthmes_decision_before"
    static let after = "healthmes_decision_after"
    static let endsAt = "healthmes_decision_ends_at"
}

private enum DecisionAction: String {
    case accept
    case decline
}

private enum NotificationProposalStatus: String {
    case proposed
    case accepted
    case pushed
    case declined
    case invalidated

    var message: String {
        switch self {
        case .proposed:
            return String(localized: "Decision not confirmed. Refresh and try again.")
        case .accepted:
            return String(localized: "Approved. Calendar sync is pending.")
        case .pushed:
            return String(localized: "Applied to calendar.")
        case .declined:
            return String(localized: "Declined. Your calendar stays unchanged.")
        case .invalidated:
            return String(localized: "Expired. Your calendar stays unchanged.")
        }
    }
}

private enum NotificationResolutionError: Error {
    case notPaired
    case notActionable(status: String)
    case expired
    case requestFailed
}

private struct NotificationDecisionResolver {
    fileprivate struct Proposal: Decodable {
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

        var isActionable: Bool {
            status == "proposed"
                && acceptResolutionToken != nil
                && declineResolutionToken != nil
        }
    }

    func resolve(proposalID: UUID, action: DecisionAction) async throws -> String {
        guard let pairing = PairingStore.shared.load() else {
            throw NotificationResolutionError.notPaired
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
            throw NotificationResolutionError.notActionable(status: proposal.status)
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
        try requireSuccess(resolvedResponse, data: resolvedData)
        return try JSONDecoder().decode(Proposal.self, from: resolvedData).status
    }

    fileprivate func currentProposal(proposalID: UUID) async throws -> Proposal {
        guard let pairing = PairingStore.shared.load() else {
            throw NotificationResolutionError.notPaired
        }
        let proposalURL = pairing.baseURL.appendingPathComponent(
            "v1/schedule/proposals/\(proposalID.uuidString.lowercased())"
        )
        var request = URLRequest(url: proposalURL)
        authorize(&request, token: pairing.token)
        let (data, response) = try await URLSession.shared.data(for: request)
        try requireSuccess(response, data: data)
        return try JSONDecoder().decode(Proposal.self, from: data)
    }

    private func authorize(_ request: inout URLRequest, token: String?) {
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    private func requireSuccess(_ response: URLResponse, data: Data = Data()) throws {
        guard
            let http = response as? HTTPURLResponse,
            (200...299).contains(http.statusCode)
        else {
            if
                let http = response as? HTTPURLResponse,
                http.statusCode == 409,
                let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let error = object["error"] as? [String: Any],
                let code = error["code"] as? String
            {
                if code == "proposal_expired" {
                    throw NotificationResolutionError.expired
                }
                if
                    code == "invalid_transition",
                    let detail = error["detail"] as? [String: Any],
                    let current = detail["current"] as? String
                {
                    throw NotificationResolutionError.notActionable(status: current)
                }
            }
            throw NotificationResolutionError.requestFailed
        }
    }
}
