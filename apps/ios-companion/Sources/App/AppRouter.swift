import Foundation
import SwiftUI

extension Notification.Name {
    /// Posted by the pairing screen after save/unpair so every surface
    /// (root gate, home model, watch sync) reloads its pairing-derived state.
    static let healthmesPairingChanged = Notification.Name("healthmes.pairing.changed")
}

/// A URL wrapped for `.sheet(item:)` presentation.
struct DecisionSheetTarget: Identifiable {
    let id = UUID()
    let url: URL
}

enum AppTab: Hashable {
    case home
    case report
    case capture
    case settings
}

/// Central navigation state: tab selection, the in-app decision viewer
/// sheet, and the proposal-detail sheet. Notification taps and
/// `healthmes://` deep links (widgets, Live Activity) land here.
@MainActor
final class AppRouter: ObservableObject {
    static let shared = AppRouter()

    @Published var tab: AppTab = .home
    @Published var decisionSheet: DecisionSheetTarget?
    @Published var proposalSheetID: UUID?

    /// Open a tokenized decision/report URL in the in-app viewer. Only URLs
    /// that come from server payloads (glance/alerts/reports) or pass the
    /// deep-link host check reach this point.
    func openDecision(_ url: URL) {
        decisionSheet = DecisionSheetTarget(url: url)
    }

    func openProposalDetail(_ id: UUID) {
        tab = .home
        proposalSheetID = id
    }

    /// Route a `healthmes://` deep link (widget tap, Live Activity tap,
    /// notification "why?"): `healthmes://decision?url=<pct-encoded>`,
    /// `healthmes://proposal?id=<uuid>`, `healthmes://capture`,
    /// `healthmes://report`, anything else → home.
    func handle(_ url: URL) {
        guard url.scheme?.lowercased() == "healthmes" else { return }
        switch url.host?.lowercased() {
        case "decision":
            guard
                let target = Self.queryValue(of: url, name: "url"),
                let targetURL = URL(string: target),
                Self.isAllowedViewerURL(targetURL)
            else {
                tab = .home
                return
            }
            openDecision(targetURL)
        case "proposal":
            guard
                let raw = Self.queryValue(of: url, name: "id"),
                let id = UUID(uuidString: raw)
            else {
                tab = .home
                return
            }
            openProposalDetail(id)
        case "capture":
            tab = .capture
        case "report":
            tab = .report
        default:
            tab = .home
        }
    }

    static func queryValue(of url: URL, name: String) -> String? {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first(where: { $0.name == name })?
            .value
    }

    /// Deep links arrive from OUTSIDE the app (any installed app can open
    /// `healthmes://`), so unlike server-payload URLs they are validated:
    /// http(s) only, and the host must match the paired instance. Local-first
    /// stays intact — the in-app viewer never opens a third-party host.
    static func isAllowedViewerURL(_ url: URL) -> Bool {
        guard
            let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            let pairing = PairingStore.shared.load()
        else { return false }
        return url.host?.lowercased() == pairing.baseURL.host?.lowercased()
    }
}
