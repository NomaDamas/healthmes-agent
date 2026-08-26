import Foundation

public enum HealthMesConnectionTransport: Equatable {
    case disconnected
    case sameDevice
    case tailscaleDNS
    case tailscaleIP
    case remoteHTTPS

    public var isTailscale: Bool {
        self == .tailscaleDNS || self == .tailscaleIP
    }
}

public struct TailscalePairingStep: Equatable, Identifiable {
    public let number: Int
    public let title: String
    public let detail: String

    public var id: Int { number }
}

public enum TailscalePairingPresentation {
    public static let downloadURL = URL(string: "https://tailscale.com/download")!

    public static let steps = [
        TailscalePairingStep(
            number: 1,
            title: "Install Tailscale",
            detail: "Install it on the Mac or Linux host and on iPhone."
        ),
        TailscalePairingStep(
            number: 2,
            title: "Use the same Tailnet",
            detail: "Sign in to Tailscale with the same account on both devices."
        ),
        TailscalePairingStep(
            number: 3,
            title: "Select Connect iPhone",
            detail: "HealthMes prepares a short-lived, one-time pairing QR."
        ),
        TailscalePairingStep(
            number: 4,
            title: "Scan the QR",
            detail: "Use the iPhone Camera. HealthMes opens automatically."
        ),
        TailscalePairingStep(
            number: 5,
            title: "Done",
            detail: "The iPhone securely passes the connection to Apple Watch."
        ),
    ]

    public static func transport(for pairing: Pairing?) -> HealthMesConnectionTransport {
        guard let pairing else { return .disconnected }
        guard let host = pairing.baseURL.host?.lowercased() else {
            return .remoteHTTPS
        }
        if PairingStore.isLoopbackHost(host) {
            return .sameDevice
        }
        if host == "ts.net" || host.hasSuffix(".ts.net") {
            return .tailscaleDNS
        }
        if isTailscaleIPv4(host) {
            return .tailscaleIP
        }
        return .remoteHTTPS
    }

    private static func isTailscaleIPv4(_ host: String) -> Bool {
        let octets = host.split(separator: ".", omittingEmptySubsequences: false)
        guard
            octets.count == 4,
            let first = Int(octets[0]),
            let second = Int(octets[1]),
            octets.dropFirst(2).allSatisfy({ Int($0).map { (0...255).contains($0) } == true })
        else {
            return false
        }
        return first == 100 && (64...127).contains(second)
    }
}
