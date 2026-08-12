import Foundation
import SwiftUI

/// A URL wrapped for `.sheet(item:)` presentation.
struct DecisionSheetTarget: Identifiable {
    let id = UUID()
    let url: URL
}

enum AppTab: Hashable {
    case today
    case plan
    case decisions
}

enum AppModal: String, Identifiable {
    case settings
    case report
    case capture

    var id: String { rawValue }
}

/// Central navigation state: core product selection, modal tools, the
/// in-app decision viewer sheet, and the proposal-detail sheet. Notification taps and
/// `healthmes://` deep links (widgets, Live Activity) land here.
@MainActor
final class AppRouter: ObservableObject {
    static let shared = AppRouter()

    @Published var tab: AppTab = .today
    @Published var modal: AppModal?
    @Published var decisionSheet: DecisionSheetTarget?
    @Published var proposalSheetID: UUID?
    @Published private(set) var commandFocusRequest = 0
    @Published private(set) var pendingCommand: String?
    @Published private(set) var homeRequest = 0
    @Published private(set) var pairingImportMessage: String?

    func focusCommandDock() {
        commandFocusRequest += 1
    }

    func focusCommandDock(prefill: String) {
        let clean = prefill.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else {
            focusCommandDock()
            return
        }
        pendingCommand = clean
        commandFocusRequest += 1
    }

    func consumePendingCommand() -> String? {
        defer { pendingCommand = nil }
        return pendingCommand
    }

    func showHome() {
        tab = .today
        modal = nil
        decisionSheet = nil
        proposalSheetID = nil
        homeRequest += 1
    }

    func dismissPairingImportMessage() {
        pairingImportMessage = nil
    }

    /// Open a tokenized decision/report URL in the in-app viewer. Only URLs
    /// that come from server payloads (glance/alerts/reports) or pass the
    /// deep-link host check reach this point.
    func openDecision(_ url: URL) {
        guard
            let pairing = PairingStore.shared.load(),
            Self.isAllowedViewerURL(url)
        else { return }
        decisionSheet = DecisionSheetTarget(
            url: ViewerURL.authenticate(url, pairing: pairing)
        )
    }

    func openProposalDetail(_ id: UUID) {
        tab = .decisions
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
                showHome()
                return
            }
            openDecision(targetURL)
        case "proposal":
            guard
                let raw = Self.queryValue(of: url, name: "id"),
                let id = UUID(uuidString: raw)
            else {
                showHome()
                return
            }
            openProposalDetail(id)
        case "capture":
            modal = .capture
        case "report":
            modal = .report
        case "speak":
            focusCommandDock()
        case "pair":
            Task { await importPairing(url) }
        default:
            showHome()
        }
    }

    private func importPairing(_ url: URL) async {
        do {
            let exchanged = try await PairingExchangeClient().exchange(url)
            let pairing = try PairingStore.shared.save(
                baseURLString: exchanged.baseURL.absoluteString,
                token: exchanged.token ?? ""
            )
            PhoneWatchSync.shared.pushPairing(
                baseURL: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
            pairingImportMessage = "Connected to \(pairing.baseURL.host ?? "HealthMes")."
            NotificationCenter.default.post(
                name: .healthmesPairingChanged,
                object: nil
            )
            showHome()
            Task {
                _ = await NotificationManager.shared.requestAuthorization()
                BackgroundRefreshManager.shared.schedule()
                await RefreshCoordinator.shared.sync(isForeground: true)
                await HealthKitSyncManager.shared.requestAuthorizationAndSync()
            }
        } catch let error as PairingError {
            pairingImportMessage = error.localizedDescription
            modal = .settings
        } catch {
            pairingImportMessage = "HealthMes could not complete pairing. Try again."
            modal = .settings
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
        return ViewerURL.hasSameOrigin(url, as: pairing.baseURL)
    }
}
