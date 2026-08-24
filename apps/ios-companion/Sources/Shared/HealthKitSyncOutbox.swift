import CryptoKit
import Foundation
import Security

public enum HealthKitSyncOutboxError: Error, Equatable, LocalizedError {
    case invalidDestinationFingerprint
    case invalidBody
    case invalidEntry
    case duplicatePayloadConflict
    case entryTooLarge(maxBytes: Int)
    case queueTooLarge(maxEntries: Int)
    case fileTooLarge(maxBytes: Int)
    case storedEntryCountExceeded(maxEntries: Int)
    case invalidFile
    case unsupportedFileVersion(Int)
    case unsupportedPayloadVersion(Int)
    case encryptionFailed
    case decryptionFailed
    case persistenceFailed
    case keyUnavailable
    case invalidKey
    case keychainReadFailed(OSStatus)
    case keychainWriteFailed(OSStatus)
    case keychainDeleteFailed(OSStatus)

    public var errorDescription: String? {
        switch self {
        case .invalidDestinationFingerprint:
            return "HealthKit outbox destination fingerprint is empty or invalid."
        case .invalidBody:
            return "HealthKit outbox body is empty or invalid."
        case .invalidEntry:
            return "HealthKit outbox entry failed validation."
        case .duplicatePayloadConflict:
            return "The same HealthKit payload was enqueued with different anchors."
        case .entryTooLarge(let maxBytes):
            return "The HealthKit outbox entry exceeds \(maxBytes) bytes."
        case .queueTooLarge(let maxEntries):
            return "The HealthKit outbox exceeds \(maxEntries) entries."
        case .fileTooLarge(let maxBytes):
            return "The encrypted HealthKit outbox exceeds \(maxBytes) bytes."
        case .storedEntryCountExceeded(let maxEntries):
            return "The encrypted HealthKit outbox contains more than \(maxEntries) entries."
        case .invalidFile:
            return "The encrypted HealthKit outbox file is invalid."
        case .unsupportedFileVersion(let version):
            return "The encrypted HealthKit outbox file version \(version) is unsupported."
        case .unsupportedPayloadVersion(let version):
            return "The HealthKit outbox payload version \(version) is unsupported."
        case .encryptionFailed:
            return "HealthMes could not encrypt the HealthKit outbox."
        case .decryptionFailed:
            return "HealthMes could not decrypt the HealthKit outbox."
        case .persistenceFailed:
            return "HealthMes could not persist the HealthKit outbox."
        case .keyUnavailable:
            return "The HealthKit outbox encryption key is unavailable."
        case .invalidKey:
            return "The HealthKit outbox encryption key is invalid."
        case .keychainReadFailed(let status):
            return "The HealthKit outbox keychain read failed (\(status))."
        case .keychainWriteFailed(let status):
            return "The HealthKit outbox keychain write failed (\(status))."
        case .keychainDeleteFailed(let status):
            return "The HealthKit outbox keychain delete failed (\(status))."
        }
    }
}

public protocol HealthKitSyncOutboxKeyProviding: Sendable {
    func loadKey() throws -> SymmetricKey?
    func loadOrCreateKey() throws -> SymmetricKey
    func deleteKey() throws
}

/// The key is device-bound and remains available after the first unlock so
/// background refresh can drain the outbox without weakening data protection.
public struct HealthKitSyncOutboxKeychainProvider:
    HealthKitSyncOutboxKeyProviding,
    Sendable
{
    public static let defaultService =
        "com.healthmes.companion.healthkit-sync-outbox"
    public static let defaultAccount = "encryption-key-v1"

    private let service: String
    private let account: String
    private let accessGroup: String?

    public init(
        service: String = HealthKitSyncOutboxKeychainProvider.defaultService,
        account: String = HealthKitSyncOutboxKeychainProvider.defaultAccount,
        accessGroup: String? = nil
    ) {
        self.service = service
        self.account = account
        self.accessGroup = accessGroup
    }

    public func loadOrCreateKey() throws -> SymmetricKey {
        if let existing = try loadKey() {
            return existing
        }

        let key = SymmetricKey(size: .bits256)
        let keyData = key.withUnsafeBytes { Data($0) }
        var attributes = baseQuery()
        attributes[kSecValueData as String] = keyData
        attributes[kSecAttrAccessible as String] =
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecSuccess {
            return key
        }
        if status == errSecDuplicateItem {
            guard let existing = try loadKey() else {
                throw HealthKitSyncOutboxError.keychainReadFailed(status)
            }
            return existing
        }
        throw HealthKitSyncOutboxError.keychainWriteFailed(status)
    }

    public func loadKey() throws -> SymmetricKey? {
        guard let data = try readData() else { return nil }
        return try makeKey(from: data)
    }

    public func deleteKey() throws {
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw HealthKitSyncOutboxError.keychainDeleteFailed(status)
        }
    }

    private func readData() throws -> Data? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(
            query as CFDictionary,
            &item
        )
        switch status {
        case errSecSuccess:
            guard let data = item as? Data else {
                throw HealthKitSyncOutboxError.invalidKey
            }
            return data
        case errSecItemNotFound:
            return nil
        default:
            throw HealthKitSyncOutboxError.keychainReadFailed(status)
        }
    }

    private func makeKey(from data: Data) throws -> SymmetricKey {
        guard data.count == 32 else {
            throw HealthKitSyncOutboxError.invalidKey
        }
        return SymmetricKey(data: data)
    }

    private func baseQuery() -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup, !accessGroup.isEmpty {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}

public struct HealthKitSyncRetryPolicy: Equatable, Sendable {
    public let initialDelay: TimeInterval
    public let maximumDelay: TimeInterval
    public let maximumAutomaticAttempts: Int

    public static let `default` = HealthKitSyncRetryPolicy(
        initialDelay: 60,
        maximumDelay: 6 * 60 * 60,
        maximumAutomaticAttempts: 8
    )

    public init(
        initialDelay: TimeInterval,
        maximumDelay: TimeInterval,
        maximumAutomaticAttempts: Int = 8
    ) {
        self.initialDelay = max(0, initialDelay)
        self.maximumDelay = max(self.initialDelay, maximumDelay)
        self.maximumAutomaticAttempts =
            max(1, maximumAutomaticAttempts)
    }

    public func nextAttemptDate(
        afterFailedAttempts failedAttempts: Int,
        now: Date
    ) -> Date {
        let exponent = max(0, min(failedAttempts - 1, 30))
        let multiplier = pow(2.0, Double(exponent))
        let delay = min(maximumDelay, initialDelay * multiplier)
        return now.addingTimeInterval(delay)
    }

    public func shouldPauseAutomaticRetry(
        afterFailedAttempts failedAttempts: Int
    ) -> Bool {
        failedAttempts >= maximumAutomaticAttempts
    }
}

public enum HealthKitSyncOutboxIdentity {
    public static let idempotencyPrefix = "hm-ios-hk-v1-"

    public static func idempotencyKey(for body: Data) -> String {
        idempotencyPrefix + sha256Hex(body)
    }

    public static func destinationFingerprint(
        baseURL: URL,
        token: String?
    ) -> String {
        var material = Data(baseURL.absoluteString.utf8)
        material.append(0)
        material.append(Data((token ?? "").utf8))
        return sha256Hex(material)
    }

    public static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }
}

public struct HealthKitSyncOutboxEntry: Codable, Equatable, Sendable {
    public let idempotencyKey: String
    public let body: Data
    public let anchors: [String: Data]
    public let destinationFingerprint: String
    public let enqueuedAt: Date
    public var failedAttempts: Int
    public var nextAttemptAt: Date
    public var terminalFailure: String?
    public var terminalAnchorsCommitted: Bool? = nil
    public var terminalAnchorCommitPending: Bool? = nil
    public var forwardingFailedAttempts: Int? = nil
    public var uploadAccepted: Bool? = nil
    /// `true` marks a permanent failure written by the fail-closed queue.
    /// Missing values belong to older queue versions and are migrated.
    public var terminalFailureIsPermanent: Bool? = nil

    public var laneKeys: Set<String> {
        Set(anchors.keys)
    }

    public var isDue: Bool {
        isDue(at: Date())
    }

    public func isDue(at now: Date) -> Bool {
        uploadAccepted != true
            && terminalFailure == nil
            && nextAttemptAt <= now
    }
}

public enum HealthKitSyncQueueSelection: Equatable, Sendable {
    case upload(HealthKitSyncOutboxEntry)
    case finalize(HealthKitSyncOutboxEntry)
    case deferred
    case drained
}

public enum HealthKitSyncQueuePolicy {
    static let legacyGlobalLane = "__healthmes_legacy_global__"

    public static func select(
        from entries: [HealthKitSyncOutboxEntry],
        forceRetry: Bool,
        includeTerminalFailures: Bool,
        attemptedTerminalKeys: Set<String> = [],
        now: Date
    ) -> HealthKitSyncQueueSelection {
        var blockedLanes = Set<String>()
        var hasDeferredEntry = false
        for entry in entries {
            let lanes = effectiveLanes(for: entry)
            if conflicts(lanes, with: blockedLanes) {
                blockedLanes.formUnion(lanes)
                hasDeferredEntry = true
                continue
            }
            if entry.uploadAccepted == true {
                return .finalize(entry)
            }
            if entry.terminalFailure != nil {
                if attemptedTerminalKeys.contains(entry.idempotencyKey) {
                    blockedLanes.formUnion(lanes)
                    hasDeferredEntry = true
                    continue
                }
                if includeTerminalFailures, forceRetry {
                    return .upload(entry)
                }
                if entry.terminalAnchorsCommitted == true {
                    continue
                }
                blockedLanes.formUnion(lanes)
                hasDeferredEntry = true
                continue
            }
            if attemptedTerminalKeys.contains(entry.idempotencyKey) {
                blockedLanes.formUnion(lanes)
                hasDeferredEntry = true
                continue
            }
            if forceRetry || entry.isDue(at: now) {
                return .upload(entry)
            }
            blockedLanes.formUnion(lanes)
            hasDeferredEntry = true
        }
        return hasDeferredEntry ? .deferred : .drained
    }

    public static func blockedLaneKeys(
        in entries: [HealthKitSyncOutboxEntry]
    ) -> Set<String> {
        entries.reduce(into: Set<String>()) { blocked, entry in
            guard
                !(
                    entry.terminalFailure != nil
                        && entry.terminalAnchorsCommitted == true
                )
            else {
                return
            }
            blocked.formUnion(effectiveLanes(for: entry))
        }
    }

    private static func effectiveLanes(
        for entry: HealthKitSyncOutboxEntry
    ) -> Set<String> {
        entry.laneKeys.isEmpty
            ? [legacyGlobalLane]
            : entry.laneKeys
    }

    private static func conflicts(
        _ lanes: Set<String>,
        with blockedLanes: Set<String>
    ) -> Bool {
        blockedLanes.contains(legacyGlobalLane)
            || lanes.contains(legacyGlobalLane)
                && !blockedLanes.isEmpty
            || !lanes.isDisjoint(with: blockedLanes)
    }
}

enum HealthKitObserverLifecycle {
    static func synchronizeAndAcknowledge(
        completion: () -> Void,
        synchronize: () async -> Void
    ) async {
        defer { completion() }
        await synchronize()
    }
}

/// Owns one BGTask execution and guarantees that expiration and normal
/// completion race through a single completion gate.
final class HealthKitBackgroundTaskRunner: @unchecked Sendable {
    typealias Operation = @Sendable () async -> Bool
    typealias Completion = @Sendable (Bool) -> Void

    private let operation: Operation
    private let lock = NSLock()
    private var completion: Completion?
    private var work: Task<Void, Never>?

    init(
        operation: @escaping Operation,
        completion: @escaping Completion
    ) {
        self.operation = operation
        self.completion = completion
    }

    func start() {
        lock.lock()
        guard work == nil, completion != nil else {
            lock.unlock()
            return
        }
        let operation = operation
        let task = Task { [self] in
            let success = await operation()
            finish(
                success: success && !Task.isCancelled,
                cancelWork: false
            )
        }
        work = task
        lock.unlock()
    }

    func expire() {
        finish(success: false, cancelWork: true)
    }

    private func finish(
        success: Bool,
        cancelWork: Bool
    ) {
        lock.lock()
        guard let completion else {
            lock.unlock()
            return
        }
        self.completion = nil
        let work = work
        self.work = nil
        lock.unlock()

        if cancelWork {
            work?.cancel()
        }
        completion(success)
    }

    deinit {
        lock.lock()
        let work = work
        self.work = nil
        completion = nil
        lock.unlock()
        work?.cancel()
    }
}

public struct HealthKitSyncQueueSummary: Equatable, Sendable {
    public let pendingCount: Int
    public let terminalFailureCount: Int
    public let latestTerminalFailure: String?
    public let nextRetryAt: Date?

    public init(entries: [HealthKitSyncOutboxEntry]) {
        pendingCount = entries.count
        let terminalEntries = entries.filter {
            $0.terminalFailure != nil
        }
        terminalFailureCount = terminalEntries.count
        latestTerminalFailure = terminalEntries.last?.terminalFailure
        var retryAt: Date?
        for entry in entries {
            if entry.terminalFailure != nil {
                if entry.terminalAnchorsCommitted == true {
                    continue
                }
                break
            }
            retryAt = entry.nextAttemptAt
            break
        }
        nextRetryAt = retryAt
    }
}

public enum HealthKitSyncLocalState {
    private static let namespacePrefix = "healthmes.healthkit."
    private static let anchorPrefix = "\(namespacePrefix)anchor."
    private static let lastUploadPrefix =
        "\(namespacePrefix)lastUploadAt."
    private static let pausePrefix = "\(namespacePrefix)paused."

    public static func clear(
        fingerprint: String,
        defaults: UserDefaults
    ) {
        defaults.removeObject(
            forKey: "\(lastUploadPrefix)\(fingerprint)"
        )
        defaults.removeObject(
            forKey: "\(pausePrefix)\(fingerprint)"
        )
        let scopedAnchorPrefix = "\(anchorPrefix)\(fingerprint)."
        for key in defaults.dictionaryRepresentation().keys
        where key.hasPrefix(scopedAnchorPrefix) {
            defaults.removeObject(forKey: key)
        }
    }

    public static func clearAll(defaults: UserDefaults) {
        for key in defaults.dictionaryRepresentation().keys
        where key.hasPrefix(namespacePrefix) {
            defaults.removeObject(forKey: key)
        }
    }
}

public enum HealthKitSyncRemovalScope: Equatable, Sendable {
    case none
    case destination(String)
    case all
}

public enum HealthKitSyncRemovalPolicy {
    public static func scope(
        fingerprint: String?,
        userDirectedRemoval: Bool
    ) -> HealthKitSyncRemovalScope {
        if let fingerprint {
            return .destination(fingerprint)
        }
        return userDirectedRemoval ? .all : .none
    }
}

public actor HealthKitSyncOutbox {
    private struct StoredFile: Codable {
        let version: Int
        let sealedData: Data
    }

    private struct PlaintextEnvelope: Codable {
        let version: Int
        let entries: [HealthKitSyncOutboxEntry]
    }

    private enum Constants {
        static let fileVersion = 1
        static let payloadVersion = 1
        static let authenticatedData =
            Data("HealthMes.HealthKitSyncOutbox.v1".utf8)
        static let minimumSealedDataBytes = 12 + 16
    }

    public static let defaultMaximumEntries = 32
    public static let defaultMaximumTerminalEntries = 8
    public static let defaultMaximumBytes = 32 * 1_024 * 1_024

    public static let shared = HealthKitSyncOutbox(
        fileURL: defaultFileURL(),
        keyProvider: HealthKitSyncOutboxKeychainProvider()
    )

    private let fileURL: URL
    private let keyProvider: any HealthKitSyncOutboxKeyProviding
    private let maximumEntries: Int
    private let maximumTerminalEntries: Int
    private let maximumBytes: Int
    private let retryPolicy: HealthKitSyncRetryPolicy
    private let fileManager: FileManager
    private var entries: [HealthKitSyncOutboxEntry] = []
    private var didLoad = false

    public init(
        fileURL: URL,
        keyProvider: any HealthKitSyncOutboxKeyProviding =
            HealthKitSyncOutboxKeychainProvider(),
        maximumEntries: Int = HealthKitSyncOutbox.defaultMaximumEntries,
        maximumTerminalEntries: Int? = nil,
        maximumBytes: Int = HealthKitSyncOutbox.defaultMaximumBytes,
        retryPolicy: HealthKitSyncRetryPolicy = .default,
        fileManager: FileManager = .default
    ) {
        self.fileURL = fileURL
        self.keyProvider = keyProvider
        self.maximumEntries = max(1, maximumEntries)
        self.maximumTerminalEntries = max(
            1,
            maximumTerminalEntries ?? maximumEntries
        )
        self.maximumBytes = max(1, maximumBytes)
        self.retryPolicy = retryPolicy
        self.fileManager = fileManager
    }

    public static func defaultFileURL(
        fileManager: FileManager = .default
    ) -> URL {
        let root = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.temporaryDirectory
        return root
            .appendingPathComponent("HealthMes/HealthKit", isDirectory: true)
            .appendingPathComponent("outbox-v1.enc")
    }

    @discardableResult
    public func enqueue(
        body: Data,
        anchors: [String: Data],
        destinationFingerprint: String,
        enqueuedAt: Date = Date()
    ) throws -> HealthKitSyncOutboxEntry {
        try ensureLoaded()
        let entry = try makeEntry(
            body: body,
            anchors: anchors,
            destinationFingerprint: destinationFingerprint,
            enqueuedAt: enqueuedAt
        )

        if let existingIndex = entries.firstIndex(where: {
            $0.idempotencyKey == entry.idempotencyKey
                && $0.destinationFingerprint == entry.destinationFingerprint
        }) {
            guard entries[existingIndex].anchors == entry.anchors else {
                throw HealthKitSyncOutboxError.duplicatePayloadConflict
            }
            return entries[existingIndex]
        }

        var candidate = entries
        candidate.append(entry)
        candidate.sort(by: entryOrder)
        candidate = compactCommittedTerminalEntries(candidate)
        try validateQueue(candidate)
        while true {
            do {
                try persist(candidate)
                break
            } catch HealthKitSyncOutboxError.fileTooLarge(_) {
                guard removeOldestCommittedTerminal(from: &candidate) else {
                    throw HealthKitSyncOutboxError.fileTooLarge(
                        maxBytes: maximumBytes
                    )
                }
            }
        }
        entries = candidate
        return entry
    }

    public func pendingCount(
        destinationFingerprint: String
    ) throws -> Int {
        try ensureLoaded()
        return entries.filter {
            $0.destinationFingerprint == destinationFingerprint
        }.count
    }

    public func pendingCount() throws -> Int {
        try ensureLoaded()
        return entries.count
    }

    public func pendingEntries(
        destinationFingerprint: String
    ) throws -> [HealthKitSyncOutboxEntry] {
        try ensureLoaded()
        return entries
            .filter {
                $0.destinationFingerprint == destinationFingerprint
            }
            .sorted(by: entryOrder)
    }

    public func nextPending(
        destinationFingerprint: String,
        now: Date = Date()
    ) throws -> HealthKitSyncOutboxEntry? {
        try ensureLoaded()
        return entries
            .filter {
                $0.destinationFingerprint == destinationFingerprint
                    && $0.isDue(at: now)
            }
            .min(by: entryOrder)
    }

    public func markSucceeded(
        idempotencyKey: String,
        destinationFingerprint: String
    ) throws {
        try ensureLoaded()
        let candidate = entries.filter {
            !(
                $0.idempotencyKey == idempotencyKey
                    && $0.destinationFingerprint == destinationFingerprint
            )
        }
        guard candidate.count != entries.count else { return }
        try persist(candidate)
        entries = candidate
    }

    @discardableResult
    public func markUploadAccepted(
        idempotencyKey: String,
        destinationFingerprint: String
    ) throws -> HealthKitSyncOutboxEntry? {
        try ensureLoaded()
        guard let index = entries.firstIndex(where: {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
        }) else {
            return nil
        }
        guard entries[index].uploadAccepted != true else {
            return entries[index]
        }

        var candidate = entries
        candidate[index].uploadAccepted = true
        try validateQueue(candidate)
        try persist(candidate)
        entries = candidate
        return candidate[index]
    }

    @discardableResult
    public func markFailed(
        idempotencyKey: String,
        destinationFingerprint: String,
        countsTowardAutomaticQuarantine: Bool = false,
        preserveTerminalFailure: Bool = false,
        now: Date = Date()
    ) throws -> HealthKitSyncOutboxEntry? {
        try ensureLoaded()
        guard let index = entries.firstIndex(where: {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
        }) else {
            return nil
        }

        var candidate = entries
        candidate[index].failedAttempts += 1
        if countsTowardAutomaticQuarantine {
            candidate[index].forwardingFailedAttempts =
                (candidate[index].forwardingFailedAttempts ?? 0) + 1
        }
        candidate[index].nextAttemptAt = retryPolicy.nextAttemptDate(
            afterFailedAttempts: candidate[index].failedAttempts,
            now: now
        )
        if !preserveTerminalFailure {
            candidate[index].terminalFailure = nil
        }
        try persist(candidate)
        entries = candidate
        return candidate[index]
    }

    /// Old builds could pause a retryable forwarding failure and prepare its
    /// anchor for commit. Restore every uncommitted legacy entry to retryable
    /// state; a permanent failure written by this build is explicitly marked.
    @discardableResult
    public func migrateLegacyTerminalEntries(
        destinationFingerprint: String,
        now: Date = Date()
    ) throws -> Int {
        try ensureLoaded()
        var candidate = entries
        var migrated = 0
        for index in candidate.indices
        where candidate[index].destinationFingerprint
            == destinationFingerprint
            && candidate[index].terminalFailure != nil
            && candidate[index].terminalAnchorsCommitted != true
            && candidate[index].terminalFailureIsPermanent != true
        {
            candidate[index].terminalFailure = nil
            candidate[index].terminalAnchorsCommitted = nil
            candidate[index].terminalAnchorCommitPending = nil
            candidate[index].nextAttemptAt = now
            migrated += 1
        }
        guard migrated > 0 else { return 0 }
        try validateQueue(candidate)
        try persist(candidate)
        entries = candidate
        return migrated
    }

    @discardableResult
    public func markTerminal(
        idempotencyKey: String,
        destinationFingerprint: String,
        reason: String,
        anchorsCommitted: Bool = false,
        anchorCommitPending: Bool = false
    ) throws -> HealthKitSyncOutboxEntry? {
        try ensureLoaded()
        guard let index = entries.firstIndex(where: {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
        }) else {
            return nil
        }

        var candidate = entries
        candidate[index].terminalFailure = String(reason.prefix(256))
        candidate[index].terminalAnchorsCommitted = anchorsCommitted
        candidate[index].terminalAnchorCommitPending =
            !anchorsCommitted && anchorCommitPending
        candidate[index].terminalFailureIsPermanent = true
        candidate = compactCommittedTerminalEntries(candidate)
        try validateQueue(candidate)
        try persist(candidate)
        entries = candidate
        return candidate.first {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
        }
    }

    @discardableResult
    public func markTerminalAnchorsCommitted(
        idempotencyKey: String,
        destinationFingerprint: String
    ) throws -> HealthKitSyncOutboxEntry? {
        try ensureLoaded()
        guard let index = entries.firstIndex(where: {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
                && $0.terminalFailure != nil
        }) else {
            return nil
        }
        guard entries[index].terminalAnchorsCommitted != true else {
            return entries[index]
        }

        var candidate = entries
        candidate[index].terminalAnchorsCommitted = true
        candidate[index].terminalAnchorCommitPending = false
        candidate = compactCommittedTerminalEntries(candidate)
        try validateQueue(candidate)
        try persist(candidate)
        entries = candidate
        return candidate.first {
            $0.idempotencyKey == idempotencyKey
                && $0.destinationFingerprint == destinationFingerprint
        }
    }

    /// Removes only the selected destination while retaining other pairings.
    @discardableResult
    public func purge(
        destinationFingerprint: String
    ) throws -> Int {
        try ensureLoaded()
        let candidate = entries.filter {
            $0.destinationFingerprint != destinationFingerprint
        }
        let removed = entries.count - candidate.count
        guard removed > 0 else { return 0 }
        try persist(candidate)
        entries = candidate
        return removed
    }

    /// User-directed removal remains possible when selective purge cannot
    /// decrypt or rewrite the queue. The fallback removes the local ciphertext;
    /// deleting an orphaned random key is best effort.
    @discardableResult
    public func purgeForUserRemoval(
        destinationFingerprint: String
    ) throws -> Int {
        do {
            return try purge(
                destinationFingerprint: destinationFingerprint
            )
        } catch let error as HealthKitSyncOutboxError {
            guard Self.requiresFullPurgeForUserRemoval(error) else {
                throw error
            }
            return try purgeAll(requireKeyDeletion: false)
        }
    }

    /// Explicit crypto-erasure for the entire outbox. This bypasses loading so
    /// a corrupt ciphertext can still be removed by a user-directed action.
    @discardableResult
    public func purgeAll() throws -> Int {
        try purgeAll(requireKeyDeletion: true)
    }

    /// User-directed privacy removal succeeds once the ciphertext is gone.
    /// Deleting the now-orphaned random key remains best effort.
    @discardableResult
    public func purgeAllForUserRemoval() throws -> Int {
        try purgeAll(requireKeyDeletion: false)
    }

    @discardableResult
    private func purgeAll(
        requireKeyDeletion: Bool
    ) throws -> Int {
        let removed = didLoad ? entries.count : 0
        if fileManager.fileExists(atPath: fileURL.path) {
            do {
                try fileManager.removeItem(at: fileURL)
            } catch {
                throw HealthKitSyncOutboxError.persistenceFailed
            }
        }
        // Once the ciphertext is gone, do not retain uploadable health data
        // in memory even if best-effort Keychain crypto-erasure fails.
        entries = []
        didLoad = true
        do {
            try keyProvider.deleteKey()
        } catch let error as HealthKitSyncOutboxError {
            if requireKeyDeletion {
                throw error
            }
        } catch {
            if requireKeyDeletion {
                throw HealthKitSyncOutboxError.keychainDeleteFailed(
                    errSecInteractionNotAllowed
                )
            }
        }
        return removed
    }

    private func ensureLoaded() throws {
        guard !didLoad else { return }
        entries = try loadEntries()
        didLoad = true
    }

    private static func requiresFullPurgeForUserRemoval(
        _ error: HealthKitSyncOutboxError
    ) -> Bool {
        switch error {
        case .fileTooLarge,
            .storedEntryCountExceeded,
            .invalidFile,
            .unsupportedFileVersion,
            .unsupportedPayloadVersion,
            .decryptionFailed,
            .keyUnavailable,
            .invalidKey,
            .queueTooLarge,
            .entryTooLarge,
            .invalidEntry:
            return true
        case .invalidDestinationFingerprint,
            .invalidBody,
            .duplicatePayloadConflict,
            .encryptionFailed,
            .persistenceFailed,
            .keychainReadFailed,
            .keychainWriteFailed,
            .keychainDeleteFailed:
            return false
        }
    }

    private func makeEntry(
        body: Data,
        anchors: [String: Data],
        destinationFingerprint: String,
        enqueuedAt: Date
    ) throws -> HealthKitSyncOutboxEntry {
        guard !body.isEmpty else {
            throw HealthKitSyncOutboxError.invalidBody
        }
        guard
            !destinationFingerprint.isEmpty,
            destinationFingerprint.count <= 256
        else {
            throw HealthKitSyncOutboxError.invalidDestinationFingerprint
        }
        let entry = HealthKitSyncOutboxEntry(
            idempotencyKey: HealthKitSyncOutboxIdentity.idempotencyKey(
                for: body
            ),
            body: body,
            anchors: anchors,
            destinationFingerprint: destinationFingerprint,
            enqueuedAt: enqueuedAt,
            failedAttempts: 0,
            nextAttemptAt: enqueuedAt,
            terminalFailure: nil,
            terminalAnchorsCommitted: nil,
            terminalAnchorCommitPending: nil,
            forwardingFailedAttempts: nil,
            uploadAccepted: nil,
            terminalFailureIsPermanent: nil
        )
        try validateEntry(entry)
        return entry
    }

    private func loadEntries() throws -> [HealthKitSyncOutboxEntry] {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return []
        }
        guard
            let attributes = try? fileManager.attributesOfItem(
                atPath: fileURL.path
            ),
            let size = attributes[.size] as? NSNumber,
            size.uint64Value <= UInt64(maximumBytes)
        else {
            throw HealthKitSyncOutboxError.fileTooLarge(
                maxBytes: maximumBytes
            )
        }

        let storedData: Data
        do {
            storedData = try Data(contentsOf: fileURL)
        } catch {
            throw HealthKitSyncOutboxError.persistenceFailed
        }
        guard storedData.count <= maximumBytes else {
            throw HealthKitSyncOutboxError.fileTooLarge(
                maxBytes: maximumBytes
            )
        }

        let stored: StoredFile
        do {
            stored = try decoder().decode(StoredFile.self, from: storedData)
        } catch {
            throw HealthKitSyncOutboxError.invalidFile
        }
        guard stored.version == Constants.fileVersion else {
            throw HealthKitSyncOutboxError.unsupportedFileVersion(
                stored.version
            )
        }
        guard
            stored.sealedData.count >= Constants.minimumSealedDataBytes,
            stored.sealedData.count <= maximumBytes
        else {
            throw HealthKitSyncOutboxError.invalidFile
        }

        let key = try loadExistingKey()
        let sealedBox: AES.GCM.SealedBox
        do {
            guard let box = try? AES.GCM.SealedBox(
                combined: stored.sealedData
            ) else {
                throw HealthKitSyncOutboxError.decryptionFailed
            }
            sealedBox = box
        }
        let plaintext: Data
        do {
            plaintext = try AES.GCM.open(
                sealedBox,
                using: key,
                authenticating: Constants.authenticatedData
            )
        } catch {
            throw HealthKitSyncOutboxError.decryptionFailed
        }
        guard plaintext.count <= maximumBytes else {
            throw HealthKitSyncOutboxError.fileTooLarge(
                maxBytes: maximumBytes
            )
        }

        let envelope: PlaintextEnvelope
        do {
            envelope = try decoder().decode(
                PlaintextEnvelope.self,
                from: plaintext
            )
        } catch {
            throw HealthKitSyncOutboxError.invalidFile
        }
        guard envelope.version == Constants.payloadVersion else {
            throw HealthKitSyncOutboxError.unsupportedPayloadVersion(
                envelope.version
            )
        }
        let activeEntries = envelope.entries.filter {
            !isCommittedTerminal($0)
        }
        let committedTerminalEntries = envelope.entries.filter(
            isCommittedTerminal
        )
        guard
            activeEntries.count <= maximumEntries,
            committedTerminalEntries.count <= maximumTerminalEntries
        else {
            throw HealthKitSyncOutboxError.storedEntryCountExceeded(
                maxEntries: maximumEntries
            )
        }
        try validateQueue(envelope.entries)
        return envelope.entries.sorted(by: entryOrder)
    }

    private func loadExistingKey() throws -> SymmetricKey {
        do {
            guard let key = try keyProvider.loadKey() else {
                throw HealthKitSyncOutboxError.keyUnavailable
            }
            return key
        } catch let error as HealthKitSyncOutboxError {
            throw error
        } catch {
            throw HealthKitSyncOutboxError.keyUnavailable
        }
    }

    private func loadOrCreateKey() throws -> SymmetricKey {
        do {
            return try keyProvider.loadOrCreateKey()
        } catch let error as HealthKitSyncOutboxError {
            throw error
        } catch {
            throw HealthKitSyncOutboxError.keyUnavailable
        }
    }

    private func persist(
        _ candidate: [HealthKitSyncOutboxEntry]
    ) throws {
        let plaintext: Data
        do {
            plaintext = try encoder().encode(
                PlaintextEnvelope(
                    version: Constants.payloadVersion,
                    entries: candidate
                )
            )
        } catch {
            throw HealthKitSyncOutboxError.persistenceFailed
        }
        guard plaintext.count <= maximumBytes else {
            throw HealthKitSyncOutboxError.fileTooLarge(
                maxBytes: maximumBytes
            )
        }

        let key = try loadOrCreateKey()
        let sealedData: Data
        do {
            let sealedBox = try AES.GCM.seal(
                plaintext,
                using: key,
                authenticating: Constants.authenticatedData
            )
            guard let combined = sealedBox.combined else {
                throw HealthKitSyncOutboxError.encryptionFailed
            }
            sealedData = combined
        } catch let error as HealthKitSyncOutboxError {
            throw error
        } catch {
            throw HealthKitSyncOutboxError.encryptionFailed
        }

        let storedData: Data
        do {
            storedData = try encoder().encode(
                StoredFile(
                    version: Constants.fileVersion,
                    sealedData: sealedData
                )
            )
        } catch {
            throw HealthKitSyncOutboxError.persistenceFailed
        }
        guard storedData.count <= maximumBytes else {
            throw HealthKitSyncOutboxError.fileTooLarge(
                maxBytes: maximumBytes
            )
        }

        do {
            let directoryURL = fileURL.deletingLastPathComponent()
            try fileManager.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true
            )
            try excludeFromBackup(directoryURL)
            try storedData.write(to: fileURL, options: .atomic)
            try excludeFromBackup(fileURL)
        } catch {
            throw HealthKitSyncOutboxError.persistenceFailed
        }
    }

    private func validateQueue(
        _ candidate: [HealthKitSyncOutboxEntry]
    ) throws {
        let activeEntries = candidate.filter {
            !isCommittedTerminal($0)
        }
        let committedTerminalEntries = candidate.filter(
            isCommittedTerminal
        )
        guard
            activeEntries.count <= maximumEntries,
            committedTerminalEntries.count <= maximumTerminalEntries
        else {
            throw HealthKitSyncOutboxError.queueTooLarge(
                maxEntries: maximumEntries
            )
        }
        var identities = Set<String>()
        for entry in candidate {
            try validateEntry(entry)
            let identity = "\(entry.destinationFingerprint)\u{0}\(entry.idempotencyKey)"
            guard identities.insert(identity).inserted else {
                throw HealthKitSyncOutboxError.invalidEntry
            }
        }
    }

    private func compactCommittedTerminalEntries(
        _ candidate: [HealthKitSyncOutboxEntry]
    ) -> [HealthKitSyncOutboxEntry] {
        var compacted = candidate.sorted(by: entryOrder)
        while compacted.filter(isCommittedTerminal).count
            > maximumTerminalEntries
        {
            guard removeOldestCommittedTerminal(from: &compacted) else {
                break
            }
        }
        return compacted
    }

    private func removeOldestCommittedTerminal(
        from candidate: inout [HealthKitSyncOutboxEntry]
    ) -> Bool {
        guard let index = candidate.firstIndex(where: isCommittedTerminal)
        else {
            return false
        }
        candidate.remove(at: index)
        return true
    }

    private func isCommittedTerminal(
        _ entry: HealthKitSyncOutboxEntry
    ) -> Bool {
        entry.terminalFailure != nil
            && entry.terminalAnchorsCommitted == true
    }

    private func validateEntry(
        _ entry: HealthKitSyncOutboxEntry
    ) throws {
        guard
            !entry.body.isEmpty,
            entry.idempotencyKey
                == HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: entry.body
                ),
            !entry.destinationFingerprint.isEmpty,
            entry.destinationFingerprint.count <= 256,
            entry.failedAttempts >= 0,
            (entry.forwardingFailedAttempts ?? 0) >= 0,
            (entry.terminalFailure?.count ?? 0) <= 256,
            entry.terminalAnchorCommitPending != true
                || (
                    entry.terminalFailure != nil
                        && entry.terminalAnchorsCommitted != true
                ),
            entry.enqueuedAt.timeIntervalSinceReferenceDate.isFinite,
            entry.nextAttemptAt.timeIntervalSinceReferenceDate.isFinite
        else {
            throw HealthKitSyncOutboxError.invalidEntry
        }
        let anchorBytes = entry.anchors.reduce(into: 0) {
            $0 += $1.key.utf8.count + $1.value.count
        }
        guard
            entry.body.count <= maximumBytes,
            anchorBytes <= maximumBytes,
            entry.body.count + anchorBytes <= maximumBytes
        else {
            throw HealthKitSyncOutboxError.entryTooLarge(
                maxBytes: maximumBytes
            )
        }
    }

    private func entryOrder(
        _ lhs: HealthKitSyncOutboxEntry,
        _ rhs: HealthKitSyncOutboxEntry
    ) -> Bool {
        if lhs.enqueuedAt != rhs.enqueuedAt {
            return lhs.enqueuedAt < rhs.enqueuedAt
        }
        if lhs.destinationFingerprint != rhs.destinationFingerprint {
            return lhs.destinationFingerprint < rhs.destinationFingerprint
        }
        return lhs.idempotencyKey < rhs.idempotencyKey
    }

    private func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .millisecondsSince1970
        return encoder
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        return decoder
    }

    private func excludeFromBackup(_ sourceURL: URL) throws {
        var url = sourceURL
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try url.setResourceValues(values)
    }
}
