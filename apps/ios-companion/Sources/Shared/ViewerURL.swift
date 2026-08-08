import CryptoKit
import Foundation

/// Builds browser URLs with the server's derived read-only viewer credential.
/// The full bearer token remains in Keychain and is never placed in a URL.
public enum ViewerURL {
    private static let context = Data("healthmes-viewer:".utf8)

    public static func token(from apiToken: String) -> String {
        let digest = SHA256.hash(data: context + Data(apiToken.utf8))
        return digest.prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    public static func make(
        pairing: Pairing,
        pathComponents: [String],
        fragment: String? = nil
    ) -> URL {
        var url = pairing.baseURL
        for component in pathComponents {
            url.appendPathComponent(component)
        }
        guard let apiToken = pairing.token else {
            return adding(fragment: fragment, token: nil, to: url)
        }
        return adding(fragment: fragment, token: token(from: apiToken), to: url)
    }

    public static func authenticate(_ url: URL, pairing: Pairing) -> URL {
        guard let apiToken = pairing.token else { return url }
        return adding(
            fragment: URLComponents(url: url, resolvingAgainstBaseURL: false)?.fragment,
            token: token(from: apiToken),
            to: url
        )
    }

    public static func hasSameOrigin(_ url: URL, as baseURL: URL) -> Bool {
        guard
            let candidate = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let base = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        else { return false }
        return candidate.scheme?.lowercased() == base.scheme?.lowercased()
            && candidate.host?.lowercased() == base.host?.lowercased()
            && effectivePort(candidate) == effectivePort(base)
            && pathIsWithinBase(candidate.path, basePath: base.path)
    }

    private static func adding(fragment: String?, token: String?, to url: URL) -> URL {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        var query = components.queryItems?.filter { $0.name != "token" } ?? []
        if let token {
            query.append(URLQueryItem(name: "token", value: token))
        }
        components.queryItems = query.isEmpty ? nil : query
        components.fragment = fragment
        return components.url!
    }

    private static func effectivePort(_ components: URLComponents) -> Int? {
        if let port = components.port { return port }
        switch components.scheme?.lowercased() {
        case "http": return 80
        case "https": return 443
        default: return nil
        }
    }

    private static func pathIsWithinBase(_ path: String, basePath: String) -> Bool {
        let normalizedBase =
            basePath.isEmpty || basePath == "/"
            ? "/"
            : basePath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if normalizedBase == "/" { return path.hasPrefix("/") }
        let prefix = "/\(normalizedBase)"
        return path == prefix || path.hasPrefix(prefix + "/")
    }
}
