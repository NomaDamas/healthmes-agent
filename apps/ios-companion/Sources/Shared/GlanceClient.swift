import Foundation

/// What a fetch produced: the decoded payload plus refresh guidance derived
/// from the server's `Cache-Control: private, max-age=N`.
public struct GlanceSnapshot {
    public let payload: GlancePayload
    public let fetchedAt: Date
    /// True when the server answered `304 Not Modified` and the cached body
    /// was re-served (data unchanged; polling stayed cheap).
    public let revalidated: Bool
    /// Earliest sensible next poll (`fetchedAt + max-age`).
    public let nextRefresh: Date
}

public enum GlanceClientError: Error {
    case notPaired
    /// 401/403 — token missing/rejected by the instance.
    case unauthorized(statusCode: Int)
    case httpStatus(Int)
    case transport(underlying: Error)
    case decoding(underlying: Error)
    /// Internal: 304 received but the cached body vanished; retried once.
    case staleCacheMiss
}

/// Minimal client for `GET /v1/briefing/glance`, shared by the iOS app, the
/// widget extensions and the watch app.
///
/// HTTP behaviour (mirrors the server contract in healthmes/api/briefing.py):
/// - `Authorization: Bearer <token>` when a token is paired.
/// - `If-None-Match` with the cached ETag on every poll; a `304` re-serves
///   the cached body (widgets keep their payload without re-downloading).
/// - `Cache-Control: max-age` is parsed into `GlanceSnapshot.nextRefresh` so
///   timeline providers can schedule WidgetKit-budget-friendly reloads.
/// - Foundation's transparent URLCache is disabled: this client owns
///   conditional-GET semantics end to end (the raw 304 must be observable).
public final class GlanceClient {
    public static let glancePath = "v1/briefing/glance"
    /// Fallback when the server omits/mangles Cache-Control (contract says
    /// it never does; matches CACHE_MAX_AGE_SECONDS server-side).
    public static let defaultMaxAgeSeconds = 300

    public let cache: GlanceSnapshotCache
    private let session: URLSession
    private let pairingStore: PairingStore

    public init(
        session: URLSession = GlanceClient.makeSession(),
        cache: GlanceSnapshotCache = .shared,
        pairingStore: PairingStore = .shared
    ) {
        self.session = session
        self.cache = cache
        self.pairingStore = pairingStore
    }

    public static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 15
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }

    public static func makeRequest(pairing: Pairing, ifNoneMatch: String?) -> URLRequest {
        var request = URLRequest(url: pairing.baseURL.appendingPathComponent(glancePath))
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let ifNoneMatch {
            request.setValue(ifNoneMatch, forHTTPHeaderField: "If-None-Match")
        }
        return request
    }

    /// `max-age` seconds out of a Cache-Control header, nil when absent.
    public static func maxAgeSeconds(fromCacheControl header: String?) -> Int? {
        guard let header else { return nil }
        for directive in header.split(separator: ",") {
            let trimmed = directive.trimmingCharacters(in: .whitespaces).lowercased()
            if trimmed.hasPrefix("max-age=") {
                return Int(trimmed.dropFirst("max-age=".count))
            }
        }
        return nil
    }

    /// Fetch using the stored pairing (app + widget path).
    public func fetch(now: Date = Date()) async throws -> GlanceSnapshot {
        guard let pairing = pairingStore.load() else {
            throw GlanceClientError.notPaired
        }
        return try await fetch(pairing: pairing, now: now)
    }

    /// Conditional GET honoring ETag/Cache-Control.
    public func fetch(pairing: Pairing, now: Date = Date()) async throws -> GlanceSnapshot {
        do {
            return try await performFetch(pairing: pairing, cached: cache.load(), now: now)
        } catch GlanceClientError.staleCacheMiss {
            // The server said 304 but our cached body was gone (evicted or
            // corrupted): one unconditional retry fetches a full body.
            return try await performFetch(pairing: pairing, cached: nil, now: now)
        }
    }

    private func performFetch(
        pairing: Pairing,
        cached: CachedGlance?,
        now: Date
    ) async throws -> GlanceSnapshot {
        let request = Self.makeRequest(pairing: pairing, ifNoneMatch: cached?.etag)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw GlanceClientError.transport(underlying: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw GlanceClientError.httpStatus(-1)
        }

        let maxAge =
            Self.maxAgeSeconds(fromCacheControl: http.value(forHTTPHeaderField: "Cache-Control"))
            ?? Self.defaultMaxAgeSeconds
        let nextRefresh = now.addingTimeInterval(TimeInterval(maxAge))

        switch http.statusCode {
        case 200:
            let payload: GlancePayload
            do {
                payload = try GlanceJSON.decodePayload(data)
            } catch {
                throw GlanceClientError.decoding(underlying: error)
            }
            cache.store(
                CachedGlance(
                    etag: http.value(forHTTPHeaderField: "ETag"),
                    fetchedAt: now,
                    maxAgeSeconds: maxAge,
                    payloadData: data
                )
            )
            return GlanceSnapshot(
                payload: payload, fetchedAt: now, revalidated: false, nextRefresh: nextRefresh
            )

        case 304:
            guard
                let cached,
                let payload = try? GlanceJSON.decodePayload(cached.payloadData)
            else {
                throw GlanceClientError.staleCacheMiss
            }
            // Same data, refreshed validity window (304 carries the same
            // ETag/Cache-Control per the endpoint contract).
            cache.store(
                CachedGlance(
                    etag: cached.etag,
                    fetchedAt: now,
                    maxAgeSeconds: maxAge,
                    payloadData: cached.payloadData
                )
            )
            return GlanceSnapshot(
                payload: payload, fetchedAt: now, revalidated: true, nextRefresh: nextRefresh
            )

        case 401, 403:
            throw GlanceClientError.unauthorized(statusCode: http.statusCode)

        default:
            throw GlanceClientError.httpStatus(http.statusCode)
        }
    }
}
