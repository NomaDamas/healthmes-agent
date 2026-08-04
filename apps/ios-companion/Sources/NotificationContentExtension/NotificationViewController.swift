import UIKit
import UserNotifications
import UserNotificationsUI

final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private let statusLabel = UILabel()
    private let actionLabel = UILabel()
    private let timeLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.93, green: 0.97, blue: 0.94, alpha: 1)

        statusLabel.font = .preferredFont(forTextStyle: .headline)
        statusLabel.textColor = UIColor(red: 0.05, green: 0.36, blue: 0.30, alpha: 1)
        statusLabel.numberOfLines = 2

        actionLabel.font = .preferredFont(forTextStyle: .title3)
        actionLabel.textColor = UIColor(red: 0.10, green: 0.16, blue: 0.13, alpha: 1)
        actionLabel.numberOfLines = 3

        timeLabel.font = .preferredFont(forTextStyle: .footnote)
        timeLabel.textColor = .secondaryLabel
        timeLabel.numberOfLines = 2

        let hint = UILabel()
        hint.text = String(localized: "Decide in 3 seconds: Yes or No")
        hint.font = .preferredFont(forTextStyle: .caption1)
        hint.textColor = .secondaryLabel

        let stack = UIStackView(arrangedSubviews: [statusLabel, actionLabel, timeLabel, hint])
        stack.axis = .vertical
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -16),
        ])
    }

    func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        let info = content.userInfo
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
            timeLabel.text = "\(display.string(from: start)) – \(display.string(from: end))"
        } else {
            timeLabel.text = nil
        }
    }
}
