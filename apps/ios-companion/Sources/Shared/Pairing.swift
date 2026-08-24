import Darwin
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
// Unsigned simulator, watchOS, and local Mac builds use the app's default
// keychain directly. iOS extensions use the shared group so they can read the
// credential without creating a private copy.

public enum AppGroup {
    public static var identifier: String {
        Bundle.main.object(forInfoDictionaryKey: "HealthMesAppGroupIdentifier") as? String
            ?? "group.com.healthmes.companion"
    }
    public static var keychainIdentifier: String {
        Bundle.main.object(forInfoDictionaryKey: "HealthMesKeychainAccessGroup") as? String
            ?? identifier
    }

    public static var keychainAccessGroupForCurrentPlatform: String? {
        #if os(macOS) || os(watchOS)
            // These targets do not declare a keychain access-group entitlement.
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
    case storageLockFailed
    case transitionInProgress

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
        case .storageLockFailed:
            return "HealthMes could not safely access the pairing storage."
        case .transitionInProgress:
            return "Another HealthMes connection change is still in progress."
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

public struct PairingSyncState: Equatable {
    public let pairing: Pairing?
    public let sequence: UInt64

    public init(pairing: Pairing?, sequence: UInt64) {
        self.pairing = pairing
        self.sequence = sequence
    }
}

public enum PairingTransitionKind: String, Codable, Equatable {
    case replacement
    case unpair
}

private enum PairingTransitionPhase: String, Codable {
    case staging
    case prepared
    case cleanupStarted
}

public struct PairingTransition: Equatable {
    public let kind: PairingTransitionKind
    public let previousFingerprint: String?
    public let candidate: Pairing?

    public init(
        kind: PairingTransitionKind,
        previousFingerprint: String?,
        candidate: Pairing?
    ) {
        self.kind = kind
        self.previousFingerprint = previousFingerprint
        self.candidate = candidate
    }
}

public struct PairingStagingRecovery: Equatable {
    public let pairing: Pairing?

    public init(pairing: Pairing?) {
        self.pairing = pairing
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
            guard !pairingStore.hasPendingTransition else {
                return false
            }
            return pairingStore.clear()
        }
        do {
            _ = try pairingStore.save(baseURLString: baseURLString, token: token)
            return true
        } catch {
            return false
        }
    }
}

public enum PairingContextApplyResult: Equatable {
    case applied(previous: Pairing?, current: Pairing?)
    case unchanged
    case ignoredStale
    case failed
}

/// Applies phone-to-watch pairing contexts in source-sequence order. The
/// sequence is persisted separately from the watch's local pairing generation
/// so delayed A -> B -> A messages cannot cross account sessions.
public actor PairingContextCoordinator {
    public static let shared = PairingContextCoordinator()

    private static let sequenceDefaultsKey =
        "healthmes.watch.pairing-source-sequence"
    private static let payloadDefaultsKey =
        "healthmes.watch.pairing-source-payload"
    private static let pendingSequenceDefaultsKey =
        "healthmes.watch.pairing-source-pending-sequence"
    private static let pendingPayloadDefaultsKey =
        "healthmes.watch.pairing-source-pending-payload"

    private let defaults: UserDefaults
    private let pairingStore: PairingStore
    private var isApplying = false
    private var queuedRequests: [QueuedRequest] = []

    private struct QueuedRequest {
        let context: [String: Any]
        let cleanup: @Sendable (Pairing?, Pairing?) async -> Bool
        let continuation: CheckedContinuation<
            PairingContextApplyResult,
            Never
        >
    }

    public init(
        defaults: UserDefaults = AppGroup.userDefaults,
        pairingStore: PairingStore = .shared
    ) {
        self.defaults = defaults
        self.pairingStore = pairingStore
    }

    /// Applies contexts in delivery order. Cleanup runs before the pairing
    /// journal is started, so a failed notification barrier leaves the old
    /// pairing intact and retryable.
    public func apply(
        context: [String: Any],
        cleanup: @escaping @Sendable (Pairing?, Pairing?) async -> Bool
    ) async -> PairingContextApplyResult {
        await withCheckedContinuation { continuation in
            queuedRequests.append(
                QueuedRequest(
                    context: context,
                    cleanup: cleanup,
                    continuation: continuation
                )
            )
            drainQueue()
        }
    }

    private func drainQueue() {
        guard !isApplying, let request = queuedRequests.first else {
            return
        }
        queuedRequests.removeFirst()
        isApplying = true
        Task { [weak self] in
            guard let self else { return }
            let result = await self.applyOne(
                context: request.context,
                cleanup: request.cleanup
            )
            await self.finish(
                result: result,
                continuation: request.continuation
            )
        }
    }

    private func finish(
        result: PairingContextApplyResult,
        continuation: CheckedContinuation<
            PairingContextApplyResult,
            Never
        >
    ) {
        isApplying = false
        continuation.resume(returning: result)
        drainQueue()
    }

    private func applyOne(
        context: [String: Any],
        cleanup: @Sendable (Pairing?, Pairing?) async -> Bool
    ) async -> PairingContextApplyResult {
        guard recoverPendingSourceTransition() else {
            return .failed
        }
        guard
            let baseURLString = context[PairingSyncKeys.baseURL] as? String
        else {
            return .failed
        }
        let token = context[PairingSyncKeys.token] as? String ?? ""
        let sequence = PairingScope.generation(
            from: context[PairingSyncKeys.sequence]
        )

        let candidate: Pairing?
        if baseURLString.isEmpty {
            candidate = nil
        } else {
            guard
                let validated = try? PairingStore.validatedPairing(
                    baseURLString: baseURLString,
                    token: token
                )
            else {
                return .failed
            }
            candidate = validated
        }

        let payloadIdentity = candidate?.cacheFingerprint ?? "unpaired"
        if let sequence {
            guard
                let fingerprint =
                    context[PairingSyncKeys.fingerprint] as? String,
                fingerprint == payloadIdentity,
                candidate == nil || sequence > 0
            else {
                return .failed
            }
        } else if let fingerprint =
            context[PairingSyncKeys.fingerprint] as? String
        {
            guard fingerprint == payloadIdentity else {
                return .failed
            }
        }

        let appliedSequence = (
            defaults.object(forKey: Self.sequenceDefaultsKey) as? NSNumber
        )?.uint64Value
        if let sequence {
            if let appliedSequence, sequence < appliedSequence {
                return .ignoredStale
            }
            if sequence == appliedSequence {
                return defaults.string(forKey: Self.payloadDefaultsKey)
                    == payloadIdentity
                    ? .unchanged
                    : .failed
            }
        } else if appliedSequence != nil {
            // Once a sequenced context has been accepted, an adjacent older
            // build must not be able to restore an unsequenced account.
            return .ignoredStale
        }

        let previous = pairingStore.load()
        guard !pairingStore.hasPendingTransition else {
            return .failed
        }
        await PairingRelayGate.shared.fenceAndWait()
        guard await cleanup(previous, candidate) else {
            PairingRelayGate.shared.reopenIfStable(store: pairingStore)
            return .failed
        }
        guard
            !pairingStore.hasPendingTransition,
            pairingStore.load() == previous
        else {
            PairingRelayGate.shared.reopenIfStable(store: pairingStore)
            return .failed
        }

        if previous == candidate {
            if let sequence {
                promotePendingSourceState(
                    sequence: sequence,
                    payloadIdentity: payloadIdentity
                )
            }
            PairingRelayGate.shared.reopenIfStable(store: pairingStore)
            return .applied(previous: previous, current: candidate)
        }

        do {
            if let sequence {
                defaults.set(
                    NSNumber(value: sequence),
                    forKey: Self.pendingSequenceDefaultsKey
                )
                defaults.set(
                    payloadIdentity,
                    forKey: Self.pendingPayloadDefaultsKey
                )
            }
            if let candidate {
                _ = try pairingStore.beginReplacement(with: candidate)
            } else {
                _ = try pairingStore.beginUnpair()
            }
            try pairingStore.markPendingTransitionCleanupStarted()
            _ = try pairingStore.commitPendingTransition()
        } catch {
            if !pairingStore.hasPendingTransition {
                clearPendingSourceState()
            }
            PairingRelayGate.shared.reopenIfStable(store: pairingStore)
            return .failed
        }

        let current = pairingStore.load()
        if let sequence {
            promotePendingSourceState(
                sequence: sequence,
                payloadIdentity: payloadIdentity
            )
        }
        PairingRelayGate.shared.reopenIfStable(store: pairingStore)
        return .applied(previous: previous, current: current)
    }

    public func currentSourceIdentity() -> PairingCacheIdentity? {
        guard recoverPendingSourceTransition() else {
            return nil
        }
        return Self.persistedSourceIdentity(
            defaults: defaults,
            pairingStore: pairingStore
        )
    }

    public static func persistedSourceIdentity(
        defaults: UserDefaults = AppGroup.userDefaults,
        pairingStore: PairingStore = .shared
    ) -> PairingCacheIdentity? {
        guard
            let pairing = pairingStore.load(),
            let sequence = (
                defaults.object(forKey: sequenceDefaultsKey)
                    as? NSNumber
            )?.uint64Value,
            defaults.string(forKey: payloadDefaultsKey)
                == pairing.cacheFingerprint
        else {
            return nil
        }
        return PairingCacheIdentity(
            fingerprint: pairing.cacheFingerprint,
            generation: sequence
        )
    }

    public static func matchingSourcePairing(
        fingerprint: String?,
        generation: UInt64?,
        defaults: UserDefaults = AppGroup.userDefaults,
        pairingStore: PairingStore = .shared
    ) -> Pairing? {
        guard
            let generation,
            let identity = persistedSourceIdentity(
                defaults: defaults,
                pairingStore: pairingStore
            ),
            identity.fingerprint == fingerprint,
            identity.generation == generation
        else {
            return nil
        }
        return pairingStore.load()
    }

    public func matchesCurrentSourceIdentity(
        fingerprint: String?,
        generation: UInt64?
    ) -> Bool {
        guard
            let generation,
            let identity = currentSourceIdentity()
        else {
            return false
        }
        return identity.fingerprint == fingerprint
            && identity.generation == generation
    }

    private func recoverPendingSourceTransition() -> Bool {
        guard
            let pendingSequence = (
                defaults.object(
                    forKey: Self.pendingSequenceDefaultsKey
                ) as? NSNumber
            )?.uint64Value,
            let pendingPayload = defaults.string(
                forKey: Self.pendingPayloadDefaultsKey
            )
        else {
            return !pairingStore.hasPendingTransition
        }

        if pairingStore.hasPendingTransition {
            guard
                let transition = pairingStore.pendingTransition(),
                (transition.candidate?.cacheFingerprint ?? "unpaired")
                    == pendingPayload
            else {
                return false
            }
            do {
                try pairingStore.markPendingTransitionCleanupStarted()
                _ = try pairingStore.commitPendingTransition()
            } catch {
                return false
            }
        }

        let currentPayload =
            pairingStore.load()?.cacheFingerprint ?? "unpaired"
        guard currentPayload == pendingPayload else {
            clearPendingSourceState()
            return false
        }
        promotePendingSourceState(
            sequence: pendingSequence,
            payloadIdentity: pendingPayload
        )
        return true
    }

    private func promotePendingSourceState(
        sequence: UInt64,
        payloadIdentity: String
    ) {
        defaults.set(
            NSNumber(value: sequence),
            forKey: Self.sequenceDefaultsKey
        )
        defaults.set(
            payloadIdentity,
            forKey: Self.payloadDefaultsKey
        )
        clearPendingSourceState()
    }

    private func clearPendingSourceState() {
        defaults.removeObject(forKey: Self.pendingSequenceDefaultsKey)
        defaults.removeObject(forKey: Self.pendingPayloadDefaultsKey)
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
    public static let fingerprint = "pairing_fingerprint"
    public static let sequence = "pairing_sequence"

    public static func context(for pairing: Pairing?) -> [String: Any] {
        [
            baseURL: pairing?.baseURL.absoluteString ?? "",
            token: pairing?.token ?? "",
        ]
    }

    public static func context(for state: PairingSyncState) -> [String: Any] {
        [
            baseURL: state.pairing?.baseURL.absoluteString ?? "",
            token: state.pairing?.token ?? "",
            fingerprint:
                state.pairing?.cacheFingerprint ?? "unpaired",
            sequence: NSNumber(value: state.sequence),
        ]
    }
}

final class LockedLatestValue<Value> {
    private let lock = NSLock()
    private var value: Value?

    init(_ value: Value? = nil) {
        self.value = value
    }

    func replace(with value: Value) {
        lock.lock()
        defer { lock.unlock() }
        self.value = value
    }

    @discardableResult
    func deliver(
        _ operation: (Value) throws -> Void
    ) rethrows -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let value else { return false }
        try operation(value)
        self.value = nil
        return true
    }

    func snapshot() -> Value? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

public struct PairingRelayLease: Hashable {
    fileprivate let id: UUID
}

/// Serializes account-changing transitions against delayed network relays.
/// A transition closes the gate before writing its journal and waits until
/// every relay that already acquired a lease has finished.
public final class PairingRelayGate: @unchecked Sendable {
    public static let shared = PairingRelayGate()

    private let lock = NSLock()
    private var acceptsNewRelays = true
    private var activeLeases: Set<UUID> = []
    private var quiescenceWaiters: [
        CheckedContinuation<Void, Never>
    ] = []

    public init() {}

    public func begin(
        pairing: Pairing,
        store: PairingStore = .shared
    ) -> PairingRelayLease? {
        lock.lock()
        defer { lock.unlock() }
        guard
            acceptsNewRelays,
            !store.hasPendingTransition,
            store.load() == pairing
        else {
            return nil
        }
        let lease = PairingRelayLease(id: UUID())
        activeLeases.insert(lease.id)
        return lease
    }

    public func end(_ lease: PairingRelayLease) {
        let waiters: [CheckedContinuation<Void, Never>]
        lock.lock()
        activeLeases.remove(lease.id)
        if activeLeases.isEmpty, !quiescenceWaiters.isEmpty {
            waiters = quiescenceWaiters
            quiescenceWaiters.removeAll(keepingCapacity: true)
        } else {
            waiters = []
        }
        lock.unlock()
        for waiter in waiters {
            waiter.resume()
        }
    }

    public func fenceAndWait() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            acceptsNewRelays = false
            if activeLeases.isEmpty {
                lock.unlock()
                continuation.resume()
            } else {
                quiescenceWaiters.append(continuation)
                lock.unlock()
            }
        }
    }

    public var isAcceptingNewRelays: Bool {
        lock.lock()
        defer { lock.unlock() }
        return acceptsNewRelays
    }

    @discardableResult
    public func reopenIfStable(
        store: PairingStore = .shared
    ) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !store.hasPendingTransition else {
            acceptsNewRelays = false
            return false
        }
        acceptsNewRelays = true
        return true
    }
}

public protocol PairingTokenStoring {
    func readToken(identifier: String) -> String?
    func readTokenForRecovery(identifier: String) throws -> String?
    func writeToken(_ token: String, identifier: String) throws
    func deleteToken(identifier: String)
    func deleteTokenForCleanup(identifier: String) throws
}

public extension PairingTokenStoring {
    func readTokenForRecovery(identifier: String) throws -> String? {
        readToken(identifier: identifier)
    }

    func deleteTokenForCleanup(identifier: String) throws {
        deleteToken(identifier: identifier)
    }
}

public final class PairingStore {
    public static let shared = PairingStore()

    /// Holds the cross-process pairing storage fence while a delayed action
    /// performs its network request. Pairing replacement/unpair waits for the
    /// lease, then publishes its transition journal before a new lease can
    /// be acquired.
    public final class PairingLease: @unchecked Sendable {
        public let cacheIdentity: PairingCacheIdentity

        private let descriptor: Int32
        private let releaseLock = NSLock()
        private var released = false

        fileprivate init(
            descriptor: Int32,
            cacheIdentity: PairingCacheIdentity
        ) {
            self.descriptor = descriptor
            self.cacheIdentity = cacheIdentity
        }

        public func release() {
            releaseLock.lock()
            defer { releaseLock.unlock() }
            guard !released else { return }
            released = true
            _ = flock(descriptor, LOCK_UN)
            close(descriptor)
        }

        deinit {
            release()
        }
    }

    private static let baseURLDefaultsKey = "healthmes.pairing.baseURL"
    public static let fingerprintDefaultsKey = "healthmes.pairing.fingerprint"
    public static let generationDefaultsKey = "healthmes.pairing.generation"
    private static let recordDefaultsKey = "healthmes.pairing.record.v1"
    private static let transitionDefaultsKey =
        "healthmes.pairing.transition.v1"
    private static let legacyCredentialIdentifier = "api-token"
    private static let storageVersion = 1

    private struct PersistedRecord: Codable {
        let version: Int
        let baseURL: String?
        let credentialIdentifier: String?
        let fingerprint: String?
        let generation: UInt64
    }

    private struct PersistedTransition: Codable {
        let version: Int
        let kind: PairingTransitionKind
        let phase: PairingTransitionPhase
        let previousFingerprint: String?
        let previousStateWasVerifiedUnpaired: Bool?
        let previousCredentialIdentifier: String?
        let candidateBaseURL: String?
        let candidateCredentialIdentifier: String?
        let candidateFingerprint: String?
        let generation: UInt64
    }

    private struct StorageSnapshot {
        let pairing: Pairing?
        let fingerprint: String?
        let credentialIdentifier: String?
        let generation: UInt64
        let usesRecord: Bool
    }

    private static let processStorageLock = NSRecursiveLock()
    private static let storageLockFileName = "healthmes.pairing.lock"
    private static let storageLockTimeout: TimeInterval = 5
    private static let storageLockRetryMicroseconds: useconds_t = 10_000

    private let defaults: UserDefaults
    private let keychain: PairingTokenStoring
    private let storageLockURL: URL

    public init(
        defaults: UserDefaults = AppGroup.userDefaults,
        keychain: PairingTokenStoring = KeychainTokenStore(),
        lockURL: URL? = nil
    ) {
        self.defaults = defaults
        self.keychain = keychain
        self.storageLockURL = lockURL ?? Self.defaultStorageLockURL
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
        do {
            return try withStorageLock {
                loadUnlocked()
            }
        } catch {
            return nil
        }
    }

    @discardableResult
    public func save(baseURLString: String, token: String) throws -> Pairing {
        try withStorageLock {
            try saveUnlocked(baseURLString: baseURLString, token: token)
        }
    }

    public static func validatedPairing(
        baseURLString: String,
        token: String
    ) throws -> Pairing {
        let url = try Self.normalizeBaseURL(baseURLString)
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isSecurePairingOrigin(url) else {
            throw PairingError.insecureBaseURL
        }
        if !Self.isLoopbackOrigin(url), trimmedToken.isEmpty {
            throw PairingError.tokenRequired
        }
        return Pairing(baseURL: url, token: trimmedToken)
    }

    public var hasPendingTransition: Bool {
        do {
            return try withStorageLock {
                defaults.data(forKey: Self.transitionDefaultsKey) != nil
            }
        } catch {
            // A lock failure must not expose an old account to another
            // process. Treat the store as fenced until it becomes readable.
            return true
        }
    }

    /// True when a connection record, legacy URL, fingerprint, or credential
    /// still exists, even if the credential is unreadable. This lets unpair
    /// finish cleanup instead of treating a damaged account as already gone.
    public var hasPersistedPairingState: Bool {
        do {
            return try withStorageLock {
                hasPersistedPairingStateUnlocked()
            }
        } catch {
            return true
        }
    }

    public func withStablePairing<T>(
        _ operation: (Pairing?) throws -> T
    ) throws -> T {
        try withStorageLock {
            guard
                defaults.data(
                    forKey: Self.transitionDefaultsKey
                ) == nil
            else {
                throw PairingError.transitionInProgress
            }
            return try operation(loadUnlocked())
        }
    }

    public func stableSyncState() throws -> PairingSyncState {
        try withStorageLock {
            guard
                defaults.data(
                    forKey: Self.transitionDefaultsKey
                ) == nil
            else {
                throw PairingError.transitionInProgress
            }
            let pairing = loadUnlocked()
            let snapshot = storageSnapshot()
            return PairingSyncState(
                pairing: pairing,
                sequence: snapshot.generation
            )
        }
    }

    /// Acquires the same cross-process fence used by pairing mutations and
    /// verifies that the expected pairing is still active. Callers must hold
    /// the returned lease until every request for that pairing has finished.
    public func acquirePairingLease(
        for pairing: Pairing
    ) throws -> PairingLease {
        guard Self.processStorageLock.try() else {
            throw PairingError.storageLockFailed
        }
        defer { Self.processStorageLock.unlock() }

        do {
            try FileManager.default.createDirectory(
                at: storageLockURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            throw PairingError.storageLockFailed
        }

        let descriptor = open(
            storageLockURL.path,
            O_CREAT | O_RDWR,
            S_IRUSR | S_IWUSR
        )
        guard descriptor >= 0 else {
            throw PairingError.storageLockFailed
        }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            close(descriptor)
            throw PairingError.storageLockFailed
        }

        defaults.synchronize()
        guard
            defaults.data(forKey: Self.transitionDefaultsKey) == nil,
            loadUnlocked() == pairing,
            let cacheIdentity =
                Self.persistedCacheIdentityUnlocked(defaults: defaults)
        else {
            _ = flock(descriptor, LOCK_UN)
            close(descriptor)
            throw PairingError.transitionInProgress
        }
        defaults.synchronize()
        return PairingLease(
            descriptor: descriptor,
            cacheIdentity: cacheIdentity
        )
    }

    public func withPairingLease<T>(
        for pairing: Pairing,
        _ operation: () async throws -> T
    ) async throws -> T {
        let lease = try acquirePairingLease(for: pairing)
        defer { lease.release() }
        return try await operation()
    }

    @discardableResult
    public func beginReplacement(
        with candidate: Pairing
    ) throws -> PairingTransition {
        try withStorageLock {
            try beginReplacementUnlocked(with: candidate)
        }
    }

    @discardableResult
    public func beginUnpair() throws -> PairingTransition {
        try withStorageLock {
            try beginUnpairUnlocked()
        }
    }

    public func pendingTransition() -> PairingTransition? {
        do {
            return try withStorageLock {
                pendingTransitionUnlocked()
            }
        } catch {
            return nil
        }
    }

    public func markPendingTransitionCleanupStarted() throws {
        try withStorageLock {
            try markPendingTransitionCleanupStartedUnlocked()
        }
    }

    @discardableResult
    public func abortStagingTransitionIfSafe()
        -> PairingStagingRecovery?
    {
        do {
            return try withStorageLock {
                guard
                    let transition = persistedTransition(),
                    transition.phase == .staging
                else {
                    return nil
                }
                let recovery = PairingStagingRecovery(
                    pairing: try verifiedStagingRecoveryPairing(
                        for: transition
                    )
                )
                abortPendingTransitionUnlocked()
                return recovery
            }
        } catch {
            return nil
        }
    }

    @discardableResult
    public func commitPendingTransition() throws -> Pairing? {
        try withStorageLock {
            try commitPendingTransitionUnlocked()
        }
    }

    public func abortPendingTransition() {
        do {
            try withStorageLock {
                abortPendingTransitionUnlocked()
            }
        } catch {
            // Best effort API retained for staging rollback. A lock failure
            // keeps the journal intact so a later lifecycle pass can recover.
        }
    }

    @discardableResult
    public func clear() -> Bool {
        do {
            try withStorageLock {
                guard
                    defaults.data(
                        forKey: Self.transitionDefaultsKey
                    ) == nil
                else {
                    throw PairingError.transitionInProgress
                }
                _ = try beginUnpairUnlocked()
                try markPendingTransitionCleanupStartedUnlocked()
                _ = try commitPendingTransitionUnlocked()
            }
            return true
        } catch {
            // Clearing is intentionally fail-closed when another process owns
            // the journal lock.
            return false
        }
    }

    public func cacheIdentity(for pairing: Pairing) -> PairingCacheIdentity? {
        do {
            return try withStorageLock {
                guard loadUnlocked() == pairing else { return nil }
                return Self.persistedCacheIdentityUnlocked(defaults: defaults)
            }
        } catch {
            return nil
        }
    }

    private func loadUnlocked() -> Pairing? {
        guard defaults.data(forKey: Self.transitionDefaultsKey) == nil else {
            return nil
        }
        let snapshot = storageSnapshot()
        guard let pairing = snapshot.pairing else {
            if !snapshot.usesRecord,
                defaults.object(forKey: Self.baseURLDefaultsKey) != nil
                    || snapshot.credentialIdentifier != nil
            {
                clearUnlocked()
            }
            return nil
        }
        if !snapshot.usesRecord {
            defaults.set(
                pairing.baseURL.absoluteString,
                forKey: Self.baseURLDefaultsKey
            )
            _ = ensureLegacyCacheIdentity(for: pairing)
        }
        return pairing
    }

    private func saveUnlocked(
        baseURLString: String,
        token: String
    ) throws -> Pairing {
        guard defaults.data(forKey: Self.transitionDefaultsKey) == nil else {
            throw PairingError.transitionInProgress
        }
        let pairing = try Self.validatedPairing(
            baseURLString: baseURLString,
            token: token
        )
        let previous = storageSnapshot()
        if previous.pairing == pairing, previous.usesRecord {
            mirrorActivePairing(
                pairing,
                generation: max(previous.generation, 1)
            )
            return pairing
        }

        let credentialIdentifier = try prepareCredential(for: pairing)
        let nextGeneration =
            previous.pairing == pairing
            ? max(previous.generation, 1)
            : incrementedGeneration()
        do {
            try persistActivePairing(
                pairing,
                credentialIdentifier: credentialIdentifier,
                generation: nextGeneration
            )
        } catch {
            deleteCredential(credentialIdentifier)
            throw error
        }
        if previous.credentialIdentifier != credentialIdentifier {
            deleteCredential(previous.credentialIdentifier)
        }
        return pairing
    }

    private func hasPersistedPairingStateUnlocked() -> Bool {
        if let data = defaults.data(forKey: Self.recordDefaultsKey) {
            guard
                let record = try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: data
                ),
                record.version == Self.storageVersion
            else {
                return true
            }
            return record.baseURL != nil
                || record.fingerprint != nil
                || record.credentialIdentifier != nil
        }
        return defaults.object(forKey: Self.baseURLDefaultsKey) != nil
            || defaults.object(forKey: Self.fingerprintDefaultsKey) != nil
            || keychain.readToken(
                identifier: Self.legacyCredentialIdentifier
            ) != nil
    }

    private func beginReplacementUnlocked(
        with candidate: Pairing
    ) throws -> PairingTransition {
        guard defaults.data(forKey: Self.transitionDefaultsKey) == nil else {
            throw PairingError.transitionInProgress
        }
        let normalizedCandidate = try Self.validatedPairing(
            baseURLString: candidate.baseURL.absoluteString,
            token: candidate.token ?? ""
        )
        let previous = storageSnapshot()
        let previousStateWasVerifiedUnpaired: Bool
        if previous.fingerprint == nil {
            guard !hasPersistedPairingStateUnlocked() else {
                throw PairingError.credentialStorageFailed
            }
            previousStateWasVerifiedUnpaired = true
        } else {
            previousStateWasVerifiedUnpaired = false
        }
        let credentialIdentifier = normalizedCandidate.token == nil
            ? nil
            : "credential-\(UUID().uuidString.lowercased())"
        let transition = PersistedTransition(
            version: Self.storageVersion,
            kind: .replacement,
            phase: .staging,
            previousFingerprint: previous.fingerprint,
            previousStateWasVerifiedUnpaired:
                previousStateWasVerifiedUnpaired,
            previousCredentialIdentifier:
                previous.credentialIdentifier,
            candidateBaseURL:
                normalizedCandidate.baseURL.absoluteString,
            candidateCredentialIdentifier: credentialIdentifier,
            candidateFingerprint:
                normalizedCandidate.cacheFingerprint,
            generation: incrementedGeneration()
        )
        try persistTransition(transition)
        do {
            try writeCredential(
                normalizedCandidate.token,
                identifier: credentialIdentifier
            )
            try persistTransition(
                PersistedTransition(
                    version: transition.version,
                    kind: transition.kind,
                    phase: .prepared,
                    previousFingerprint:
                        transition.previousFingerprint,
                    previousStateWasVerifiedUnpaired:
                        transition.previousStateWasVerifiedUnpaired,
                    previousCredentialIdentifier:
                        transition.previousCredentialIdentifier,
                    candidateBaseURL: transition.candidateBaseURL,
                    candidateCredentialIdentifier:
                        transition.candidateCredentialIdentifier,
                    candidateFingerprint:
                        transition.candidateFingerprint,
                    generation: transition.generation
                )
            )
        } catch {
            abortPendingTransitionUnlocked()
            throw error
        }
        return PairingTransition(
            kind: .replacement,
            previousFingerprint: transition.previousFingerprint,
            candidate: normalizedCandidate
        )
    }

    private func beginUnpairUnlocked() throws -> PairingTransition {
        guard defaults.data(forKey: Self.transitionDefaultsKey) == nil else {
            throw PairingError.transitionInProgress
        }
        let previous = storageSnapshot()
        let transition = PersistedTransition(
            version: Self.storageVersion,
            kind: .unpair,
            phase: .prepared,
            previousFingerprint: previous.fingerprint,
            previousStateWasVerifiedUnpaired: nil,
            previousCredentialIdentifier:
                previous.credentialIdentifier,
            candidateBaseURL: nil,
            candidateCredentialIdentifier: nil,
            candidateFingerprint: nil,
            generation: incrementedGeneration()
        )
        try persistTransition(transition)
        return PairingTransition(
            kind: .unpair,
            previousFingerprint: transition.previousFingerprint,
            candidate: nil
        )
    }

    private func pendingTransitionUnlocked() -> PairingTransition? {
        guard
            let transition = persistedTransition(),
            transition.phase == .prepared
                || transition.phase == .cleanupStarted
        else {
            return nil
        }
        switch transition.kind {
        case .unpair:
            let verifiedPreviousFingerprint =
                storageSnapshot().fingerprint
            return PairingTransition(
                kind: .unpair,
                previousFingerprint:
                    transition.previousFingerprint
                        == verifiedPreviousFingerprint
                    ? verifiedPreviousFingerprint
                    : nil,
                candidate: nil
            )
        case .replacement:
            let previous = storageSnapshot()
            if
                let candidateFingerprint = transition.candidateFingerprint,
                previous.fingerprint == candidateFingerprint,
                previous.credentialIdentifier
                    == transition.candidateCredentialIdentifier
            {
                guard
                    let rawBaseURL = transition.candidateBaseURL,
                    let url = try? Self.normalizeBaseURL(rawBaseURL)
                else {
                    return nil
                }
                let token = transition.candidateCredentialIdentifier.flatMap {
                    keychain.readToken(identifier: $0)
                }
                let candidate = Pairing(baseURL: url, token: token)
                guard
                    Self.isSecurePairingOrigin(url),
                    Self.isLoopbackOrigin(url) || candidate.token != nil,
                    candidate.cacheFingerprint == candidateFingerprint
                else {
                    return nil
                }
                // The candidate record was already published before a crash.
                // Recovery only needs to remove the old credential and journal.
                return PairingTransition(
                    kind: .replacement,
                    previousFingerprint: transition.previousFingerprint,
                    candidate: candidate
                )
            }
            if let expectedFingerprint = transition.previousFingerprint {
                guard
                    PairingScope.isValidFingerprint(expectedFingerprint),
                    expectedFingerprint == previous.fingerprint
                else {
                    return nil
                }
            } else {
                guard
                    transition.previousStateWasVerifiedUnpaired == true,
                    transition.previousCredentialIdentifier == nil,
                    !hasPersistedPairingStateUnlocked()
                else {
                    return nil
                }
            }
            guard
                let rawBaseURL = transition.candidateBaseURL,
                let url = try? Self.normalizeBaseURL(rawBaseURL)
            else {
                return nil
            }
            let token = transition.candidateCredentialIdentifier.flatMap {
                keychain.readToken(identifier: $0)
            }
            let candidate = Pairing(baseURL: url, token: token)
            guard
                Self.isSecurePairingOrigin(url),
                Self.isLoopbackOrigin(url) || candidate.token != nil,
                candidate.cacheFingerprint
                    == transition.candidateFingerprint
            else {
                return nil
            }
            return PairingTransition(
                kind: .replacement,
                previousFingerprint: transition.previousFingerprint,
                candidate: candidate
            )
        }
    }

    private func markPendingTransitionCleanupStartedUnlocked() throws {
        guard
            let transition = persistedTransition(),
            transition.phase == .prepared
                || transition.phase == .cleanupStarted
        else {
            throw PairingError.transitionInProgress
        }
        guard pendingTransitionUnlocked() != nil else {
            throw PairingError.credentialStorageFailed
        }
        guard transition.phase != .cleanupStarted else { return }
        try persistTransition(
            PersistedTransition(
                version: transition.version,
                kind: transition.kind,
                phase: .cleanupStarted,
                previousFingerprint:
                    transition.previousFingerprint,
                previousStateWasVerifiedUnpaired:
                    transition.previousStateWasVerifiedUnpaired,
                previousCredentialIdentifier:
                    transition.previousCredentialIdentifier,
                candidateBaseURL: transition.candidateBaseURL,
                candidateCredentialIdentifier:
                    transition.candidateCredentialIdentifier,
                candidateFingerprint:
                    transition.candidateFingerprint,
                generation: transition.generation
            )
        )
    }

    private func commitPendingTransitionUnlocked() throws -> Pairing? {
        guard let transition = persistedTransition() else {
            throw PairingError.transitionInProgress
        }
        guard transition.phase == .cleanupStarted else {
            throw PairingError.transitionInProgress
        }
        guard let validated = pendingTransitionUnlocked() else {
            throw PairingError.credentialStorageFailed
        }
        switch validated.kind {
        case .replacement:
            guard let candidate = validated.candidate else {
                throw PairingError.credentialStorageFailed
            }
            try persistActivePairing(
                candidate,
                credentialIdentifier:
                    transition.candidateCredentialIdentifier,
                generation: transition.generation
            )
        case .unpair:
            try persistUnpaired(generation: transition.generation)
        }
        if transition.previousCredentialIdentifier
            != transition.candidateCredentialIdentifier
        {
            try deleteCredentialForCleanup(
                transition.previousCredentialIdentifier
            )
        }
        defaults.removeObject(forKey: Self.transitionDefaultsKey)
        return validated.candidate
    }

    private func abortPendingTransitionUnlocked() {
        guard
            let transition = persistedTransition(),
            transition.phase != .cleanupStarted
        else { return }
        deleteCredential(transition.candidateCredentialIdentifier)
        defaults.removeObject(forKey: Self.transitionDefaultsKey)
    }

    private func clearUnlocked() {
        guard defaults.data(forKey: Self.transitionDefaultsKey) == nil else {
            return
        }
        let previous = storageSnapshot()
        let hadPairingState =
            previous.pairing != nil
            || previous.fingerprint != nil
            || previous.credentialIdentifier != nil
            || defaults.object(forKey: Self.baseURLDefaultsKey) != nil
            || defaults.object(forKey: Self.fingerprintDefaultsKey) != nil
        let generation = hadPairingState
            ? incrementedGeneration()
            : max(previous.generation, 1)
        try? persistUnpaired(generation: generation)
        deleteCredential(previous.credentialIdentifier)
        if previous.credentialIdentifier
            != Self.legacyCredentialIdentifier
        {
            deleteCredential(Self.legacyCredentialIdentifier)
        }
    }

    public static func persistedCacheIdentity(
        defaults: UserDefaults = AppGroup.userDefaults
    ) -> PairingCacheIdentity? {
        do {
            return try withStorageLock(
                at: defaultStorageLockURL,
                defaults: defaults
            ) {
                persistedCacheIdentityUnlocked(defaults: defaults)
            }
        } catch {
            return nil
        }
    }

    private static func persistedCacheIdentityUnlocked(
        defaults: UserDefaults
    ) -> PairingCacheIdentity? {
        guard defaults.data(forKey: transitionDefaultsKey) == nil else {
            return nil
        }
        if let data = defaults.data(forKey: recordDefaultsKey) {
            guard
                let record = try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: data
                ),
                record.version == storageVersion,
                record.baseURL != nil,
                let fingerprint = record.fingerprint,
                !fingerprint.isEmpty,
                record.generation > 0
            else {
                return nil
            }
            return PairingCacheIdentity(
                fingerprint: fingerprint,
                generation: record.generation
            )
        }
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

    public static func hasPersistedPairing(
        defaults: UserDefaults = AppGroup.userDefaults
    ) -> Bool {
        do {
            return try withStorageLock(
                at: defaultStorageLockURL,
                defaults: defaults
            ) {
                hasPersistedPairingUnlocked(defaults: defaults)
            }
        } catch {
            return false
        }
    }

    private static func hasPersistedPairingUnlocked(
        defaults: UserDefaults
    ) -> Bool {
        guard defaults.data(forKey: transitionDefaultsKey) == nil else {
            return false
        }
        if let data = defaults.data(forKey: recordDefaultsKey) {
            guard
                let record = try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: data
                ),
                record.version == storageVersion
            else {
                return false
            }
            return record.baseURL != nil
        }
        return defaults.string(forKey: baseURLDefaultsKey) != nil
    }

    private static var defaultStorageLockURL: URL {
        #if os(macOS)
            // The unsigned menu-bar app, widget tests, and legacy
            // ScreenSaver host cannot reliably open an App Group container.
            // They still run as the same user, so Application Support provides
            // one cross-process lock without an entitlement boundary.
            let root =
                FileManager.default.urls(
                    for: .applicationSupportDirectory,
                    in: .userDomainMask
                ).first?
                .appendingPathComponent("HealthMes", isDirectory: true)
                ?? FileManager.default.temporaryDirectory
                    .appendingPathComponent("HealthMes", isDirectory: true)
        #else
        let root =
            AppGroup.containerURL
            ?? FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first?
            .appendingPathComponent("HealthMes", isDirectory: true)
            ?? FileManager.default.temporaryDirectory
                .appendingPathComponent("HealthMes", isDirectory: true)
        #endif
        return root.appendingPathComponent(storageLockFileName)
    }

    private func withStorageLock<T>(
        _ operation: () throws -> T
    ) throws -> T {
        try Self.withStorageLock(
            at: storageLockURL,
            defaults: defaults,
            operation
        )
    }

    private static func withStorageLock<T>(
        at lockURL: URL,
        defaults: UserDefaults,
        _ operation: () throws -> T
    ) throws -> T {
        let deadline =
            DispatchTime.now().uptimeNanoseconds
            + UInt64(storageLockTimeout * 1_000_000_000)
        while !processStorageLock.try() {
            guard DispatchTime.now().uptimeNanoseconds < deadline else {
                throw PairingError.storageLockFailed
            }
            usleep(storageLockRetryMicroseconds)
        }
        defer { processStorageLock.unlock() }

        do {
            try FileManager.default.createDirectory(
                at: lockURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            throw PairingError.storageLockFailed
        }
        let descriptor = open(
            lockURL.path,
            O_CREAT | O_RDWR,
            S_IRUSR | S_IWUSR
        )
        guard descriptor >= 0 else {
            throw PairingError.storageLockFailed
        }
        defer { close(descriptor) }
        while flock(descriptor, LOCK_EX | LOCK_NB) != 0 {
            guard
                errno == EWOULDBLOCK || errno == EAGAIN,
                DispatchTime.now().uptimeNanoseconds < deadline
            else {
                throw PairingError.storageLockFailed
            }
            usleep(storageLockRetryMicroseconds)
        }
        defer { flock(descriptor, LOCK_UN) }

        // CFPrefs caches per process. Refresh after acquiring the cross-process
        // lock and flush before releasing it so every target sees one journal.
        defaults.synchronize()
        defer { defaults.synchronize() }
        return try operation()
    }

    private func ensureLegacyCacheIdentity(
        for pairing: Pairing
    ) -> PairingCacheIdentity {
        if let identity = Self.persistedCacheIdentityUnlocked(defaults: defaults),
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

    private func incrementedGeneration() -> UInt64 {
        let mirrored =
            (defaults.object(
                forKey: Self.generationDefaultsKey
            ) as? NSNumber)?.uint64Value ?? 0
        let recorded = defaults.data(forKey: Self.recordDefaultsKey)
            .flatMap {
                try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: $0
                )
            }?
            .generation ?? 0
        let current = max(mirrored, recorded)
        return current == UInt64.max ? 1 : current + 1
    }

    private func storageSnapshot() -> StorageSnapshot {
        if let data = defaults.data(forKey: Self.recordDefaultsKey) {
            guard
                let record = try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: data
                ),
                record.version == Self.storageVersion
            else {
                return StorageSnapshot(
                    pairing: nil,
                    fingerprint: nil,
                    credentialIdentifier: nil,
                    generation: incrementedGeneration(),
                    usesRecord: true
                )
            }
            guard
                let rawBaseURL = record.baseURL,
                let url = try? Self.normalizeBaseURL(rawBaseURL)
            else {
                return StorageSnapshot(
                    pairing: nil,
                    fingerprint: nil,
                    credentialIdentifier:
                        record.credentialIdentifier,
                    generation: record.generation,
                    usesRecord: true
                )
            }
            let token = record.credentialIdentifier.flatMap {
                keychain.readToken(identifier: $0)
            }
            let pairing = Pairing(baseURL: url, token: token)
            guard
                Self.isSecurePairingOrigin(url),
                Self.isLoopbackOrigin(url) || pairing.token != nil,
                pairing.cacheFingerprint == record.fingerprint
            else {
                return StorageSnapshot(
                    pairing: nil,
                    fingerprint: nil,
                    credentialIdentifier:
                        record.credentialIdentifier,
                    generation: record.generation,
                    usesRecord: true
                )
            }
            return StorageSnapshot(
                pairing: pairing,
                fingerprint: pairing.cacheFingerprint,
                credentialIdentifier: record.credentialIdentifier,
                generation: record.generation,
                usesRecord: true
            )
        }

        guard
            let raw = defaults.string(forKey: Self.baseURLDefaultsKey),
            let url = try? Self.normalizeBaseURL(raw)
        else {
            return StorageSnapshot(
                pairing: nil,
                fingerprint: nil,
                credentialIdentifier: nil,
                generation: mirroredGeneration(),
                usesRecord: false
            )
        }
        let token = keychain.readToken(
            identifier: Self.legacyCredentialIdentifier
        )?
        .trimmingCharacters(in: .whitespacesAndNewlines)
        let pairing = Pairing(baseURL: url, token: token)
        guard
            Self.isSecurePairingOrigin(url),
            Self.isLoopbackOrigin(url) || pairing.token != nil
        else {
            return StorageSnapshot(
                pairing: nil,
                fingerprint: nil,
                credentialIdentifier:
                    Self.legacyCredentialIdentifier,
                generation: mirroredGeneration(),
                usesRecord: false
            )
        }
        return StorageSnapshot(
            pairing: pairing,
            fingerprint: pairing.cacheFingerprint,
            credentialIdentifier: pairing.token == nil
                ? nil
                : Self.legacyCredentialIdentifier,
            generation: mirroredGeneration(),
            usesRecord: false
        )
    }

    private func verifiedStagingRecoveryPairing(
        for transition: PersistedTransition
    ) throws -> Pairing? {
        if transition.previousFingerprint == nil {
            guard transition.previousStateWasVerifiedUnpaired == true else {
                throw PairingError.credentialStorageFailed
            }
        }
        if let data = defaults.data(forKey: Self.recordDefaultsKey) {
            guard
                let record = try? JSONDecoder().decode(
                    PersistedRecord.self,
                    from: data
                ),
                record.version == Self.storageVersion,
                record.credentialIdentifier
                    == transition.previousCredentialIdentifier
            else {
                throw PairingError.credentialStorageFailed
            }
            guard let expectedFingerprint = transition.previousFingerprint
            else {
                guard
                    record.baseURL == nil,
                    record.fingerprint == nil,
                    record.credentialIdentifier == nil
                else {
                    throw PairingError.credentialStorageFailed
                }
                return nil
            }
            guard
                PairingScope.isValidFingerprint(expectedFingerprint),
                record.fingerprint == expectedFingerprint,
                let rawBaseURL = record.baseURL,
                let baseURL = try? Self.normalizeBaseURL(rawBaseURL)
            else {
                throw PairingError.credentialStorageFailed
            }
            let token: String?
            if let identifier = record.credentialIdentifier {
                token = try keychain.readTokenForRecovery(
                    identifier: identifier
                )
            } else {
                token = nil
            }
            let pairing = Pairing(baseURL: baseURL, token: token)
            guard
                Self.isSecurePairingOrigin(baseURL),
                Self.isLoopbackOrigin(baseURL) || pairing.token != nil,
                pairing.cacheFingerprint == expectedFingerprint
            else {
                throw PairingError.credentialStorageFailed
            }
            return pairing
        }

        guard let expectedFingerprint = transition.previousFingerprint else {
            guard
                defaults.object(forKey: Self.baseURLDefaultsKey) == nil,
                defaults.object(forKey: Self.fingerprintDefaultsKey) == nil,
                transition.previousCredentialIdentifier == nil,
                try keychain.readTokenForRecovery(
                    identifier: Self.legacyCredentialIdentifier
                ) == nil
            else {
                throw PairingError.credentialStorageFailed
            }
            return nil
        }
        guard
            PairingScope.isValidFingerprint(expectedFingerprint),
            let rawBaseURL = defaults.string(
                forKey: Self.baseURLDefaultsKey
            ),
            let baseURL = try? Self.normalizeBaseURL(rawBaseURL),
            transition.previousCredentialIdentifier == nil
                || transition.previousCredentialIdentifier
                    == Self.legacyCredentialIdentifier
        else {
            throw PairingError.credentialStorageFailed
        }
        let token: String?
        if transition.previousCredentialIdentifier != nil {
            token = try keychain.readTokenForRecovery(
                identifier: Self.legacyCredentialIdentifier
            )
        } else {
            token = nil
        }
        let pairing = Pairing(baseURL: baseURL, token: token)
        guard
            Self.isSecurePairingOrigin(baseURL),
            Self.isLoopbackOrigin(baseURL) || pairing.token != nil,
            pairing.cacheFingerprint == expectedFingerprint
        else {
            throw PairingError.credentialStorageFailed
        }
        return pairing
    }

    private func mirroredGeneration() -> UInt64 {
        (defaults.object(
            forKey: Self.generationDefaultsKey
        ) as? NSNumber)?.uint64Value ?? 0
    }

    private func prepareCredential(
        for pairing: Pairing
    ) throws -> String? {
        guard pairing.token != nil else { return nil }
        let identifier = "credential-\(UUID().uuidString.lowercased())"
        do {
            try writeCredential(pairing.token, identifier: identifier)
            return identifier
        } catch {
            deleteCredential(identifier)
            throw error
        }
    }

    private func writeCredential(
        _ token: String?,
        identifier: String?
    ) throws {
        guard let token, let identifier else {
            if token != nil || identifier != nil {
                throw PairingError.credentialStorageFailed
            }
            return
        }
        do {
            try keychain.writeToken(token, identifier: identifier)
            guard keychain.readToken(identifier: identifier) == token else {
                throw PairingError.credentialStorageFailed
            }
        } catch {
            throw PairingError.credentialStorageFailed
        }
    }

    private func deleteCredential(_ identifier: String?) {
        guard let identifier else { return }
        keychain.deleteToken(identifier: identifier)
    }

    private func deleteCredentialForCleanup(
        _ identifier: String?
    ) throws {
        guard let identifier else { return }
        try keychain.deleteTokenForCleanup(identifier: identifier)
    }

    private func persistActivePairing(
        _ pairing: Pairing,
        credentialIdentifier: String?,
        generation: UInt64
    ) throws {
        let record = PersistedRecord(
            version: Self.storageVersion,
            baseURL: pairing.baseURL.absoluteString,
            credentialIdentifier: credentialIdentifier,
            fingerprint: pairing.cacheFingerprint,
            generation: max(generation, 1)
        )
        let data: Data
        do {
            data = try JSONEncoder().encode(record)
        } catch {
            throw PairingError.credentialStorageFailed
        }
        defaults.set(data, forKey: Self.recordDefaultsKey)
        mirrorActivePairing(pairing, generation: record.generation)
    }

    private func persistUnpaired(generation: UInt64) throws {
        let record = PersistedRecord(
            version: Self.storageVersion,
            baseURL: nil,
            credentialIdentifier: nil,
            fingerprint: nil,
            generation: max(generation, 1)
        )
        let data: Data
        do {
            data = try JSONEncoder().encode(record)
        } catch {
            throw PairingError.credentialStorageFailed
        }
        defaults.set(data, forKey: Self.recordDefaultsKey)
        defaults.removeObject(forKey: Self.baseURLDefaultsKey)
        defaults.removeObject(forKey: Self.fingerprintDefaultsKey)
        defaults.set(
            NSNumber(value: record.generation),
            forKey: Self.generationDefaultsKey
        )
    }

    private func mirrorActivePairing(
        _ pairing: Pairing,
        generation: UInt64
    ) {
        defaults.set(
            pairing.baseURL.absoluteString,
            forKey: Self.baseURLDefaultsKey
        )
        defaults.set(
            pairing.cacheFingerprint,
            forKey: Self.fingerprintDefaultsKey
        )
        defaults.set(
            NSNumber(value: max(generation, 1)),
            forKey: Self.generationDefaultsKey
        )
    }

    private func persistTransition(
        _ transition: PersistedTransition
    ) throws {
        let data: Data
        do {
            data = try JSONEncoder().encode(transition)
        } catch {
            throw PairingError.credentialStorageFailed
        }
        defaults.set(data, forKey: Self.transitionDefaultsKey)
    }

    private func persistedTransition() -> PersistedTransition? {
        guard
            let data = defaults.data(
                forKey: Self.transitionDefaultsKey
            ),
            let transition = try? JSONDecoder().decode(
                PersistedTransition.self,
                from: data
            ),
            transition.version == Self.storageVersion
        else {
            return nil
        }
        return transition
    }
}

/// Fail-closed account boundary for delayed notifications, WatchConnectivity
/// payloads, and other work that can outlive the pairing that created it.
public enum PairingScope {
    public static func isValidFingerprint(_ fingerprint: String?) -> Bool {
        guard let fingerprint, fingerprint.utf8.count == 64 else {
            return false
        }
        return fingerprint.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (97...102).contains(byte)
        }
    }

    public static func matches(
        fingerprint: String?,
        pairing: Pairing?
    ) -> Bool {
        guard
            isValidFingerprint(fingerprint),
            let fingerprint,
            let pairing
        else {
            return false
        }
        return fingerprint == pairing.cacheFingerprint
    }

    public static func matchingPairing(
        fingerprint: String?,
        store: PairingStore = .shared
    ) -> Pairing? {
        guard let pairing = store.load() else { return nil }
        return matches(fingerprint: fingerprint, pairing: pairing)
            ? pairing
            : nil
    }

    public static func generation(from value: Any?) -> UInt64? {
        if let number = value as? NSNumber {
            return number.uint64Value
        }
        if let string = value as? String,
            let generation = UInt64(string)
        {
            return generation
        }
        return nil
    }

    public static func matchingPairing(
        fingerprint: String?,
        generation: UInt64?,
        store: PairingStore = .shared
    ) -> Pairing? {
        guard
            let generation,
            let pairing = store.load(),
            let identity = store.cacheIdentity(for: pairing),
            identity.fingerprint == fingerprint,
            identity.generation == generation
        else {
            return nil
        }
        return pairing
    }

    #if DEBUG && targetEnvironment(simulator)
        public static let debugSimulatorLoopbackBaseURLUserInfoKey =
            "healthmes_debug_simulator_loopback_base_url"

        /// Unsigned simulator app extensions cannot share App Group defaults
        /// with their host. Reconstruct only a token-less loopback pairing
        /// whose payload fingerprint proves the exact origin.
        public static func debugSimulatorLoopbackPairing(
            baseURLString: String?,
            fingerprint: String?
        ) -> Pairing? {
            guard
                let baseURLString,
                isValidFingerprint(fingerprint),
                let fingerprint,
                let baseURL = try? PairingStore.normalizeBaseURL(
                    baseURLString
                ),
                PairingStore.isLoopbackOrigin(baseURL)
            else {
                return nil
            }
            let pairing = Pairing(baseURL: baseURL, token: nil)
            return pairing.cacheFingerprint == fingerprint
                ? pairing
                : nil
        }
    #endif
}

struct KeychainSecurityOperations {
    let copyMatching:
        (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus
    let update: (CFDictionary, CFDictionary) -> OSStatus
    let add:
        (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus
    let delete: (CFDictionary) -> OSStatus

    static let live = KeychainSecurityOperations(
        copyMatching: { SecItemCopyMatching($0, $1) },
        update: { SecItemUpdate($0, $1) },
        add: { SecItemAdd($0, $1) },
        delete: { SecItemDelete($0) }
    )
}

public struct KeychainTokenStore: PairingTokenStoring {
    private let service = "com.healthmes.companion.pairing"
    private let accessGroup: String?
    private let security: KeychainSecurityOperations
    private let allowsUngroupedFallback: Bool

    public init() {
        accessGroup = AppGroup.keychainAccessGroupForCurrentPlatform
        security = .live
        #if targetEnvironment(simulator)
            allowsUngroupedFallback =
                AppGroup.keychainIdentifier == AppGroup.identifier
        #else
            allowsUngroupedFallback = false
        #endif
    }

    init(
        accessGroup: String?,
        security: KeychainSecurityOperations,
        allowsUngroupedFallback: Bool
    ) {
        self.accessGroup = accessGroup
        self.security = security
        self.allowsUngroupedFallback = allowsUngroupedFallback
    }

    public func readToken(identifier: String) -> String? {
        try? readTokenForRecovery(identifier: identifier)
    }

    public func readTokenForRecovery(
        identifier: String
    ) throws -> String? {
        if let accessGroup {
            switch readTokenResult(
                identifier: identifier,
                accessGroup: accessGroup
            ) {
            case .found(let token):
                return token
            case .missing:
                return nil
            case .failed(let status):
                guard
                    allowsUngroupedFallback,
                    status == errSecMissingEntitlement
                else {
                    throw PairingError.credentialStorageFailed
                }
            }
        }
        switch readTokenResult(
            identifier: identifier,
            accessGroup: nil
        ) {
        case .found(let token):
            return token
        case .missing:
            return nil
        case .failed:
            throw PairingError.credentialStorageFailed
        }
    }

    public func writeToken(
        _ token: String,
        identifier: String
    ) throws {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw PairingError.credentialStorageFailed }
        if let accessGroup {
            let status = upsert(
                token: trimmed,
                identifier: identifier,
                accessGroup: accessGroup
            )
            if status == errSecSuccess {
                return
            }
            // Unsigned simulator fallback is allowed only after Security
            // reports that the shared access group entitlement is unavailable.
            guard
                allowsUngroupedFallback,
                status == errSecMissingEntitlement
            else {
                throw PairingError.credentialStorageFailed
            }
        }
        guard
            upsert(
                token: trimmed,
                identifier: identifier,
                accessGroup: nil
            ) == errSecSuccess
        else {
            throw PairingError.credentialStorageFailed
        }
    }

    public func deleteToken(identifier: String) {
        try? deleteTokenForCleanup(identifier: identifier)
    }

    public func deleteTokenForCleanup(identifier: String) throws {
        if let accessGroup {
            let status = security.delete(
                baseQuery(
                    identifier: identifier,
                    accessGroup: accessGroup
                ) as CFDictionary
            )
            if status == errSecSuccess || status == errSecItemNotFound {
                guard allowsUngroupedFallback else { return }
            } else {
                guard
                    allowsUngroupedFallback,
                    status == errSecMissingEntitlement
                else {
                    throw PairingError.credentialStorageFailed
                }
            }
        }
        let status = security.delete(
            baseQuery(
                identifier: identifier,
                accessGroup: nil
            ) as CFDictionary
        )
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw PairingError.credentialStorageFailed
        }
    }

    private enum TokenReadResult {
        case found(String)
        case missing
        case failed(OSStatus)
    }

    private func readTokenResult(
        identifier: String,
        accessGroup: String?
    ) -> TokenReadResult {
        var query = baseQuery(
            identifier: identifier,
            accessGroup: accessGroup
        )
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = security.copyMatching(
            query as CFDictionary,
            &item
        )
        switch status {
        case errSecSuccess:
            guard
                let data = item as? Data,
                let token = String(data: data, encoding: .utf8),
                !token.isEmpty
            else {
                return .failed(errSecDecode)
            }
            return .found(token)
        case errSecItemNotFound:
            return .missing
        default:
            return .failed(status)
        }
    }

    private func upsert(
        token: String,
        identifier: String,
        accessGroup: String?
    ) -> OSStatus {
        let valueAttributes: [String: Any] = [
            kSecValueData as String: Data(token.utf8)
        ]
        let updateStatus = security.update(
            baseQuery(
                identifier: identifier,
                accessGroup: accessGroup
            ) as CFDictionary,
            valueAttributes as CFDictionary
        )
        if updateStatus == errSecSuccess {
            return errSecSuccess
        }
        guard updateStatus == errSecItemNotFound else {
            return updateStatus
        }

        var attributes = baseQuery(
            identifier: identifier,
            accessGroup: accessGroup
        )
        attributes.merge(valueAttributes) { _, new in new }
        // AfterFirstUnlock: widget timeline refreshes run in the background;
        // only the pre-first-unlock window after a reboot is excluded.
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return security.add(
            attributes as CFDictionary,
            nil
        )
    }

    private func baseQuery(
        identifier: String,
        accessGroup: String?
    ) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: identifier,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}
