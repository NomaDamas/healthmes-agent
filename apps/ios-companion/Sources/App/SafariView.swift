import SafariServices
import SwiftUI

/// In-app decision viewer (issue #10): SFSafariViewController over the
/// tokenized decision/report URLs. Native chrome comes for free — Done
/// (back), the share sheet, reader sizing, and Dynamic Type — and the page
/// keeps using the derived read-only viewer token the server embedded in
/// the link; the bearer token never leaves the Keychain for web views.
///
/// Local-first: only URLs from the paired instance's own payloads (glance /
/// alerts / reports) or deep links that pass AppRouter's host check are
/// ever presented here.
struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let configuration = SFSafariViewController.Configuration()
        configuration.entersReaderIfAvailable = false
        let controller = SFSafariViewController(url: url, configuration: configuration)
        controller.dismissButtonStyle = .done
        return controller
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
