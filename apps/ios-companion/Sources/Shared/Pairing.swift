import CryptoKit
import Foundation
import Security

public extension Notification.Name {
    /// Posted whenever the active HealthMes account or credential changes.
    static let healthmesPairingChanged = Notification.Name("healthmes.pairing.changed")
}

public struct PairingCacheIdentity: Codable, Equatable {
    public let fingerprint: String
    public let generation: UInt64

    public init(fingerprint: String, generation: UInt64) {
        self.fingerprint = fingerprint
        self.generation = generation
    }
}

// Pairing = the base URL + bearer token of the user's OWN healthmes
// instance. Local-first contract (issue #7): this URL is the only network
// destination any target in this project ever talks to.
//
// Storage split:
//   - base URL   -> App Group UserDefaults (shared with the widget process)
//   - API token  -> Keychain, using the App Group identifier as the keychain
//                   access group so the widget extension can read it too.
//
// Unsigned simulator and local Mac builds have no access-group entitlement.
// They use the app's default keychain directly so startup never waits on an
// invalid group query. Signed builds use the shared group only when the
// entitlement is actually present.

public enum AppGroup {
    public static let identifier = "group.com.healthmes.companion"
    public static var keychainIdentifier: String {
        Bundle.main.object(forInfoDictionaryKey: "HealthMesKeychainAccessGroup") as? String
            ?? identifier
    }

    public static var keychainAccessGroupForCurrentPlatform: String? {
        #if os(macOS)
            // The Mac companion project intentionally builds unsigned.
            return nil
        #else
            return keychainIdentifier
        #endif
    }

    public static var userDefaults: UserDefaults {
        UserDefaults(suiteName: identifier) ?? .standard
    }

    public static var containerURL: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier)
    }
}

public enum PairingError: LocalizedError, Equatable {
    case invalidBaseURL
    case insecureBaseURL
    case tokenRequired
    case invalidPairingCode
    case pairingCodeExpired
    case pairingCodeConsumed
    case originMismatch
    case transport
    case invalidResponse
    case exchangeFailed(Int)
    case credentialStorageFailed

    public var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "Enter an HTTPS URL, or localhost for same-device development."
        case .insecureBaseURL:
            return "Pairing requires HTTPS. Plain HTTP is allowed only on this device."
        case .tokenRequired:
            return "Remote HealthMes instances require an API token."
        case .invalidPairingCode:
            return "The pairing code is invalid or incomplete."
        case .pairingCodeExpired:
            return "The pairing code expired. Generate a new QR on your Mac and scan it again."
        case .pairingCodeConsumed:
            return "The pairing code was already used. Generate a new QR on your Mac."
        case .originMismatch:
            return "The pairing server identity did not match the QR code."
        case .transport:
            return "The HealthMes service could not be reached."
        case .invalidResponse:
            return "The pairing server returned an invalid response."
        case .exchangeFailed(let status):
            return "The one-time pairing code was rejected (HTTP \(status))."
        case .credentialStorageFailed:
            return "HealthMes could not store the pairing credential securely."
        }
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

    public var cacheFingerprint: String {
        let material = baseURL.absoluteString + "\u{0}" + (token ?? "")
        return SHA256.hash(data: Data(material.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }
}

public enum PairingContextApplication {
    @discardableResult
    public static func apply(
        baseURLString: String,
        token: String,
        pairingStore: PairingStore = .shared
    ) -> Bool {
        if baseURLString.isEmpty {
            pairingStore.clear()
            return true
        }
        do {
            _ = try pairingStore.save(baseURLString: baseURLString, token: token)
            return true
        } catch {
            return false
        }
    }
}

public struct PairingDeepLink: Equatable {
    public let baseURL: URL
    public let code: String

    public static func parse(_ url: URL) throws -> PairingDeepLink {
        guard
            url.scheme?.lowercased() == "healthmes",
            url.host?.lowercased() == "pair",
            let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
            ),
            let rawBaseURL = components.queryItems?
                .first(where: { $0.name == "url" })?
                .value
        else {
            throw PairingError.invalidBaseURL
        }
        let baseURL = try PairingStore.normalizeBaseURL(rawBaseURL)
        guard PairingStore.isSecurePairingOrigin(baseURL) else {
            throw PairingError.insecureBaseURL
        }
        guard
            let code = components.queryItems?
            .first(where: { $0.name == "code" })?
            .value?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !code.isEmpty
        else {
            throw PairingError.invalidPairingCode
        }
        return PairingDeepLink(
            baseURL: baseURL,
            code: code
        )
    }
}

private struct PairingExchangeBody: Encodable {
    let code: String
}

private struct PairingExchangeResponse: Decodable {
    let baseURL: String
    let token: String

    enum CodingKeys: String, CodingKey {
        case baseURL = "base_url"
        case token
    }
}

public final class PairingExchangeClient {
    private let session: URLSession

    public init(
        session: URLSession = URLSession(
            configuration: .ephemeral
        )
    ) {
        self.session = session
    }

    public static func request(for payload: PairingDeepLink) throws -> URLRequest {
        var request = URLRequest(
            url: payload.baseURL.appendingPathComponent(
                "v1/setup/pairing/exchange"
            )
        )
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            PairingExchangeBody(code: payload.code)
        )
        return request
    }

    public func exchange(_ url: URL) async throws -> Pairing {
        let payload = try PairingDeepLink.parse(url)
        let request = try Self.request(for: payload)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw PairingError.transport
        }
        guard let http = response as? HTTPURLResponse else {
            throw PairingError.exchangeFailed(-1)
        }
        guard (200...299).contains(http.statusCode) else {
            switch http.statusCode {
            case 409:
                throw PairingError.pairingCodeConsumed
            case 410:
                throw PairingError.pairingCodeExpired
            default:
                throw PairingError.exchangeFailed(http.statusCode)
            }
        }
        let exchange: PairingExchangeResponse
        do {
            exchange = try JSONDecoder().decode(
                PairingExchangeResponse.self,
                from: data
            )
        } catch {
            throw PairingError.invalidResponse
        }
        let returnedBaseURL = try PairingStore.normalizeBaseURL(exchange.baseURL)
        guard Self.sameOrigin(payload.baseURL, returnedBaseURL) else {
            throw PairingError.originMismatch
        }
        return Pairing(baseURL: returnedBaseURL, token: exchange.token)
    }

    private static func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        lhs.scheme?.lowercased() == rhs.scheme?.lowercased()
            && lhs.host?.lowercased() == rhs.host?.lowercased()
            && effectivePort(lhs) == effectivePort(rhs)
    }

    private static func effectivePort(_ url: URL) -> Int? {
        url.port ?? (url.scheme?.lowercased() == "https" ? 443 : 80)
    }
}

/// Keys of the WatchConnectivity application context used to push the
/// pairing from the iPhone app to the watch app.
public enum PairingSyncKeys {
    public static let baseURL = "base_url"
    public static let token = "token"

    public static func context(for pairing: Pairing?) -> [String: Any] {
        [
            baseURL: pairing?.baseURL.absoluteString ?? "",
            token: pairing?.token ?? "",
        ]
    }
}

public protocol PairingTokenStoring {
    func readToken() -> String?
    func writeToken(_ token: String) throws
    func deleteToken()
}

public final class PairingStore {
    public static let shared = PairingStore()

    private static let baseURLDefaultsKey = "healthmes.pairing.baseURL"
    public static let fingerprintDefaultsKey = "healthmes.pairing.fingerprint"
    public static let generationDefaultsKey = "healthmes.pairing.generation"

    private let defaults: UserDefaults
    private let keychain: PairingTokenStoring

    public init(
        defaults: UserDefaults = AppGroup.userDefaults,
        keychain: PairingTokenStoring = KeychainTokenStore()
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

    public static func isLoopbackHost(_ rawHost: String) -> Bool {
        var host = rawHost.lowercased()
        if host.hasPrefix("["), host.hasSuffix("]") {
            host.removeFirst()
            host.removeLast()
        }
        if host == "localhost" || host == "::1" {
            return true
        }

        let octets = host.split(
            separator: ".",
            omittingEmptySubsequences: false
        )
        guard octets.count == 4 else { return false }
        let parsed = octets.compactMap { octet -> Int? in
            guard
                !octet.isEmpty,
                octet.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
                let value = Int(octet),
                (0...255).contains(value)
            else {
                return nil
            }
            return value
        }
        return parsed.count == 4 && parsed[0] == 127
    }

    public static func isLoopbackOrigin(_ url: URL) -> Bool {
        guard let host = url.host else { return false }
        return isLoopbackHost(host)
    }

    public static func isSecurePairingOrigin(_ url: URL) -> Bool {
        if url.scheme?.lowercased() == "https" {
            return true
        }
        return url.scheme?.lowercased() == "http" && isLoopbackOrigin(url)
    }

    public func load() -> Pairing? {
        guard
            let raw = defaults.string(forKey: Self.baseURLDefaultsKey),
            let url = try? Self.normalizeBaseURL(raw)
        else {
            clear()
            return nil
        }
        let token = keychain.readToken()?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            Self.isSecurePairingOrigin(url),
            Self.isLoopbackOrigin(url) || !(token?.isEmpty ?? true)
        else {
            clear()
            return nil
        }
        defaults.set(url.absoluteString, forKey: Self.baseURLDefaultsKey)
        let pairing = Pairing(baseURL: url, token: token)
        _ = ensureCacheIdentity(for: pairing)
        return pairing
    }

    @discardableResult
    public func save(baseURLString: String, token: String) throws -> Pairing {
        let url = try Self.normalizeBaseURL(baseURLString)
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isSecurePairingOrigin(url) else {
            throw PairingError.insecureBaseURL
        }
        if !Self.isLoopbackOrigin(url), trimmedToken.isEmpty {
            throw PairingError.tokenRequired
        }

        let previousToken = keychain.readToken()
        let pairing = Pairing(baseURL: url, token: trimmedToken)
        let existingURL = defaults.string(forKey: Self.baseURLDefaultsKey)
            .flatMap { try? Self.normalizeBaseURL($0) }
        let existingToken = Pairing(baseURL: url, token: previousToken).token
        if existingURL == url, existingToken == pairing.token {
            defaults.set(url.absoluteString, forKey: Self.baseURLDefaultsKey)
            _ = ensureCacheIdentity(for: pairing)
            return pairing
        }
        if trimmedToken.isEmpty {
            keychain.deleteToken()
        } else {
            do {
                try keychain.writeToken(trimmedToken)
                guard keychain.readToken() == trimmedToken else {
                    throw PairingError.credentialStorageFailed
                }
            } catch {
                restoreToken(previousToken)
                throw PairingError.credentialStorageFailed
            }
        }
        defaults.set(url.absoluteString, forKey: Self.baseURLDefaultsKey)
        let nextGeneration = incrementedGeneration()
        defaults.set(pairing.cacheFingerprint, forKey: Self.fingerprintDefaultsKey)
        defaults.set(NSNumber(value: nextGeneration), forKey: Self.generationDefaultsKey)
        return pairing
    }

    public func clear() {
        let hadPairingState =
            defaults.object(forKey: Self.baseURLDefaultsKey) != nil
            || defaults.object(forKey: Self.fingerprintDefaultsKey) != nil
            || keychain.readToken() != nil
        let nextGeneration = hadPairingState ? incrementedGeneration() : nil
        defaults.removeObject(forKey: Self.baseURLDefaultsKey)
        defaults.removeObject(forKey: Self.fingerprintDefaultsKey)
        if let nextGeneration {
            defaults.set(NSNumber(value: nextGeneration), forKey: Self.generationDefaultsKey)
        }
        keychain.deleteToken()
    }

    public func cacheIdentity(for pairing: Pairing) -> PairingCacheIdentity? {
        guard load() == pairing else { return nil }
        return persistedCacheIdentity()
    }

    public static func persistedCacheIdentity(
        defaults: UserDefaults = AppGroup.userDefaults
    ) -> PairingCacheIdentity? {
        guard
            let fingerprint = defaults.string(forKey: fingerprintDefaultsKey),
            !fingerprint.isEmpty,
            let number = defaults.object(forKey: generationDefaultsKey) as? NSNumber,
            number.uint64Value > 0
        else { return nil }
        return PairingCacheIdentity(
            fingerprint: fingerprint,
            generation: number.uint64Value
        )
    }

    private func ensureCacheIdentity(for pairing: Pairing) -> PairingCacheIdentity {
        if let identity = persistedCacheIdentity(),
            identity.fingerprint == pairing.cacheFingerprint
        {
            return identity
        }
        let identity = PairingCacheIdentity(
            fingerprint: pairing.cacheFingerprint,
            generation: incrementedGeneration()
        )
        defaults.set(identity.fingerprint, forKey: Self.fingerprintDefaultsKey)
        defaults.set(NSNumber(value: identity.generation), forKey: Self.generationDefaultsKey)
        return identity
    }

    private func persistedCacheIdentity() -> PairingCacheIdentity? {
        Self.persistedCacheIdentity(defaults: defaults)
    }

    private func incrementedGeneration() -> UInt64 {
        let current =
            (defaults.object(forKey: Self.generationDefaultsKey) as? NSNumber)?.uint64Value ?? 0
        return current == UInt64.max ? 1 : current + 1
    }

    private func restoreToken(_ token: String?) {
        guard let token, !token.isEmpty else {
            keychain.deleteToken()
            return
        }
        try? keychain.writeToken(token)
    }
}

public struct KeychainTokenStore: PairingTokenStoring {
    private let service = "com.healthmes.companion.pairing"
    private let account = "api-token"

    public init() {}

    public func readToken() -> String? {
        if
            let accessGroup = AppGroup.keychainAccessGroupForCurrentPlatform,
            let token = readToken(accessGroup: accessGroup)
        {
            return token
        }
        return readToken(accessGroup: nil)
    }

    public func writeToken(_ token: String) throws {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw PairingError.credentialStorageFailed }
        if
            let accessGroup = AppGroup.keychainAccessGroupForCurrentPlatform,
            upsert(token: trimmed, accessGroup: accessGroup)
        {
            return
        }
        // Unsigned/simulator fallback: no access-group entitlement available.
        guard upsert(token: trimmed, accessGroup: nil) else {
            throw PairingError.credentialStorageFailed
        }
    }

    public func deleteToken() {
        if let accessGroup = AppGroup.keychainAccessGroupForCurrentPlatform {
            SecItemDelete(baseQuery(accessGroup: accessGroup) as CFDictionary)
        }
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

    private func upsert(token: String, accessGroup: String?) -> Bool {
        let valueAttributes: [String: Any] = [
            kSecValueData as String: Data(token.utf8)
        ]
        let updateStatus = SecItemUpdate(
            baseQuery(accessGroup: accessGroup) as CFDictionary,
            valueAttributes as CFDictionary
        )
        if updateStatus == errSecSuccess {
            return true
        }
        guard updateStatus == errSecItemNotFound else {
            return false
        }

        var attributes = baseQuery(accessGroup: accessGroup)
        attributes.merge(valueAttributes) { _, new in new }
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
