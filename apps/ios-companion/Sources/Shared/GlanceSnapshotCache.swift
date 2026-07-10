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
    /// Strong ETag exactly as received (quoted sha-256 hex).
    public let etag: String?
    /// When this cache entry was last fetched or revalidated.
    public let fetchedAt: Date
    /// Server Cache-Control max-age at that time.
    public let maxAgeSeconds: Int
    /// Verbatim response body (decodable via GlanceJSON.decodePayload).
    public let payloadData: Data

    public init(etag: String?, fetchedAt: Date, maxAgeSeconds: Int, payloadData: Data) {
        self.etag = etag
        self.fetchedAt = fetchedAt
        self.maxAgeSeconds = maxAgeSeconds
        self.payloadData = payloadData
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

    public func store(_ cached: CachedGlance) {
        try? FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        guard let data = try? JSONEncoder().encode(cached) else { return }
        try? data.write(to: fileURL, options: .atomic)
    }

    public func clear() {
        try? FileManager.default.removeItem(at: fileURL)
    }

    public func decodedPayload() -> GlancePayload? {
        guard let cached = load() else { return nil }
        return try? GlanceJSON.decodePayload(cached.payloadData)
    }
}
