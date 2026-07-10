import Foundation
import Security

// Pairing = the base URL + bearer token of the user's OWN healthmes
// instance. Local-first contract (issue #7): this URL is the only network
// destination any target in this project ever talks to.
//
// Storage split:
//   - base URL   -> App Group UserDefaults (shared with the widget process)
//   - API token  -> Keychain, using the App Group identifier as the keychain
//                   access group so the widget extension can read it too.
//
// Unsigned simulator builds (CODE_SIGNING_ALLOWED=NO) have no entitlement to
// enforce; SecItem calls that reject the access group (-34018) fall back to
// the app's default keychain so development keeps working. On a signed
// device build the access-group path is the real one — not yet verified on
// hardware (see README).

public enum AppGroup {
    public static let identifier = "group.com.healthmes.companion"

    public static var userDefaults: UserDefaults {
        UserDefaults(suiteName: identifier) ?? .standard
    }

    public static var containerURL: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier)
    }
}

public enum PairingError: LocalizedError, Equatable {
    case invalidBaseURL

    public var errorDescription: String? {
        "Enter a valid http(s) URL, e.g. http://192.168.1.20:8100"
    }
}

public struct Pairing: Equatable {
    /// Normalized (no trailing slash) http(s) base URL of the instance.
    public let baseURL: URL
    /// Bearer token; nil for token-less loopback-open instances.
    public let token: String?

    public init(baseURL: URL, token: String?) {
        self.baseURL = baseURL
        let trimmed = token?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.token = (trimmed?.isEmpty ?? true) ? nil : trimmed
    }
}

/// Keys of the WatchConnectivity application context used to push the
/// pairing from the iPhone app to the watch app.
public enum PairingSyncKeys {
    public static let baseURL = "base_url"
    public static let token = "token"
}

public final class PairingStore {
    public static let shared = PairingStore()

    private static let baseURLDefaultsKey = "healthmes.pairing.baseURL"

    private let defaults: UserDefaults
    private let keychain: KeychainTokenStore

    public init(
        defaults: UserDefaults = AppGroup.userDefaults,
        keychain: KeychainTokenStore = KeychainTokenStore()
    ) {
        self.defaults = defaults
        self.keychain = keychain
    }

    /// Accepts what a human types: whitespace and trailing slashes are
    /// stripped; scheme+host are required. Subpath bases (reverse proxies,
    /// e.g. `https://home.example/healthmes`) are preserved.
    public static func normalizeBaseURL(_ raw: String) throws -> URL {
        var trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while trimmed.hasSuffix("/") { trimmed.removeLast() }
        guard
            let url = URL(string: trimmed),
            let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            url.host != nil
        else {
            throw PairingError.invalidBaseURL
        }
        return url
    }

    public func load() -> Pairing? {
        guard
            let raw = defaults.string(forKey: Self.baseURLDefaultsKey),
            let url = URL(string: raw)
        else {
            return nil
        }
        return Pairing(baseURL: url, token: keychain.readToken())
    }

    @discardableResult
    public func save(baseURLString: String, token: String) throws -> Pairing {
        let url = try Self.normalizeBaseURL(baseURLString)
        defaults.set(url.absoluteString, forKey: Self.baseURLDefaultsKey)
        keychain.writeToken(token)
        return Pairing(baseURL: url, token: token)
    }

    public func clear() {
        defaults.removeObject(forKey: Self.baseURLDefaultsKey)
        keychain.deleteToken()
    }
}

public struct KeychainTokenStore {
    private let service = "com.healthmes.companion.pairing"
    private let account = "api-token"

    public init() {}

    public func readToken() -> String? {
        readToken(accessGroup: AppGroup.identifier) ?? readToken(accessGroup: nil)
    }

    public func writeToken(_ token: String) {
        deleteToken()
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if add(token: trimmed, accessGroup: AppGroup.identifier) { return }
        // Unsigned/simulator fallback: no access-group entitlement available.
        _ = add(token: trimmed, accessGroup: nil)
    }

    public func deleteToken() {
        SecItemDelete(baseQuery(accessGroup: AppGroup.identifier) as CFDictionary)
        SecItemDelete(baseQuery(accessGroup: nil) as CFDictionary)
    }

    private func readToken(accessGroup: String?) -> String? {
        var query = baseQuery(accessGroup: accessGroup)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard
            status == errSecSuccess,
            let data = item as? Data,
            let token = String(data: data, encoding: .utf8),
            !token.isEmpty
        else {
            return nil
        }
        return token
    }

    private func add(token: String, accessGroup: String?) -> Bool {
        var attributes = baseQuery(accessGroup: accessGroup)
        attributes[kSecValueData as String] = Data(token.utf8)
        // AfterFirstUnlock: widget timeline refreshes run in the background;
        // only the pre-first-unlock window after a reboot is excluded.
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
    }

    private func baseQuery(accessGroup: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}
