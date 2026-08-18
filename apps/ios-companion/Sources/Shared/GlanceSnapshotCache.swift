import Darwin
import Foundation

/// Last-known-good `/v1/briefing/glance` response plus its validator.
///
/// Lives in the App Group container so the app and its widget extension
/// share one cache per device (the watch has its own container — pairing
/// and cache are per-device by design). The raw response bytes are kept so
/// a `304 Not Modified` revalidation can re-serve exactly what the server
/// sent, and so widgets can render the last snapshot when the instance is
/// unreachable (marked stale — never silently).
public struct CachedGlance: Codable, Equatable {
    /// SHA-256 of the normalized instance origin and credential. The raw
    /// bearer token is never persisted in the cache.
    public let pairingFingerprint: String
    /// Monotonic account generation captured when the request started.
    public let pairingGeneration: UInt64
    /// Strong ETag exactly as received (quoted sha-256 hex).
    public let etag: String?
    /// When this cache entry was last fetched or revalidated.
    public let fetchedAt: Date
    /// Server Cache-Control max-age at that time.
    public let maxAgeSeconds: Int
    /// Verbatim response body (decodable via GlanceJSON.decodePayload).
    public let payloadData: Data

    public init(
        pairingFingerprint: String,
        pairingGeneration: UInt64,
        etag: String?,
        fetchedAt: Date,
        maxAgeSeconds: Int,
        payloadData: Data
    ) {
        self.pairingFingerprint = pairingFingerprint
        self.pairingGeneration = pairingGeneration
        self.etag = etag
        self.fetchedAt = fetchedAt
        self.maxAgeSeconds = maxAgeSeconds
        self.payloadData = payloadData
    }

    enum CodingKeys: String, CodingKey {
        case pairingFingerprint
        case pairingGeneration
        case etag
        case fetchedAt
        case maxAgeSeconds
        case payloadData
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pairingFingerprint = try container.decode(String.self, forKey: .pairingFingerprint)
        pairingGeneration =
            try container.decodeIfPresent(UInt64.self, forKey: .pairingGeneration) ?? 0
        etag = try container.decodeIfPresent(String.self, forKey: .etag)
        fetchedAt = try container.decode(Date.self, forKey: .fetchedAt)
        maxAgeSeconds = try container.decode(Int.self, forKey: .maxAgeSeconds)
        payloadData = try container.decode(Data.self, forKey: .payloadData)
    }

    public var pairingIdentity: PairingCacheIdentity {
        PairingCacheIdentity(
            fingerprint: pairingFingerprint,
            generation: pairingGeneration
        )
    }
}

public final class GlanceSnapshotCache {
    public static let shared = GlanceSnapshotCache()

    private let fileURL: URL

    public init(fileURL: URL = GlanceSnapshotCache.defaultFileURL()) {
        self.fileURL = fileURL
    }

    public static func defaultFileURL() -> URL {
        let base =
            AppGroup.containerURL?.appendingPathComponent("Library/Caches", isDirectory: true)
            ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("healthmes-glance-snapshot.json")
    }

    public func load() -> CachedGlance? {
        guard let data = try? Data(contentsOf: fileURL) else { return nil }
        return try? JSONDecoder().decode(CachedGlance.self, from: data)
    }

    public func load(for identity: PairingCacheIdentity) -> CachedGlance? {
        guard
            let cached = load(),
            cached.pairingIdentity == identity
        else { return nil }
        return cached
    }

    public func load(for pairing: Pairing, pairingStore: PairingStore = .shared) -> CachedGlance? {
        guard let identity = pairingStore.cacheIdentity(for: pairing) else { return nil }
        return load(for: identity)
    }

    @discardableResult
    public func store(_ cached: CachedGlance) -> Bool {
        try? FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        return withExclusiveFileLock {
            if let existing = load() {
                guard existing.pairingGeneration <= cached.pairingGeneration else {
                    return false
                }
                guard
                    existing.pairingGeneration != cached.pairingGeneration
                        || existing.pairingFingerprint == cached.pairingFingerprint
                else {
                    return false
                }
                guard
                    existing.pairingGeneration != cached.pairingGeneration
                        || existing.fetchedAt <= cached.fetchedAt
                else {
                    return false
                }
            }
            guard let data = try? JSONEncoder().encode(cached) else { return false }
            do {
                try data.write(to: fileURL, options: .atomic)
                return true
            } catch {
                return false
            }
        }
    }

    public func clear() {
        try? FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        _ = withExclusiveFileLock {
            try? FileManager.default.removeItem(at: fileURL)
            return true
        }
    }

    public func decodedPayload(for identity: PairingCacheIdentity) -> GlancePayload? {
        guard let cached = load(for: identity) else { return nil }
        return try? GlanceJSON.decodePayload(cached.payloadData)
    }

    public func decodedPayload(
        for pairing: Pairing,
        pairingStore: PairingStore = .shared
    ) -> GlancePayload? {
        guard let cached = load(for: pairing, pairingStore: pairingStore) else { return nil }
        return try? GlanceJSON.decodePayload(cached.payloadData)
    }

    private func withExclusiveFileLock(_ operation: () -> Bool) -> Bool {
        let lockURL = fileURL.appendingPathExtension("lock")
        let descriptor = open(
            lockURL.path,
            O_CREAT | O_RDWR,
            S_IRUSR | S_IWUSR
        )
        guard descriptor >= 0 else { return false }
        defer { close(descriptor) }
        guard flock(descriptor, LOCK_EX) == 0 else { return false }
        defer { flock(descriptor, LOCK_UN) }
        return operation()
    }
}
