import Foundation

// Codable contract for `GET /v1/briefing/glance` (healthmes/api/briefing.py).
// The field names and value vocabularies are pinned verbatim to the server
// schema; tests/api/test_briefing.py (server side) and Tests/Fixtures/
// glance.json (this app) hold the same reference payload. Decoding is strict
// on purpose — an unknown enum value or missing key is a contract break we
// want to see, not silently render.

/// Response of `GET /v1/briefing/glance`.
public struct GlancePayload: Codable, Equatable {
    /// Server-side generation instant (aware UTC).
    public let generatedAt: Date
    /// User timezone string (IANA name when HEALTHMES_TIMEZONE is set).
    public let timezone: String
    public let energy: GlanceEnergy
    /// 0..3 upcoming/ongoing blocks, soonest first.
    public let nextBlocks: [GlanceBlock]
    public let alerts: GlanceAlerts
    public let latestDecision: GlanceDecision?

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case timezone
        case energy
        case nextBlocks = "next_blocks"
        case alerts
        case latestDecision = "latest_decision"
    }
}

/// Today's persisted cognitive-energy picture. Missing hours are honest
/// nulls — the server never computes energy on demand for a widget poll.
public struct GlanceEnergy: Codable, Equatable {
    /// Latest persisted window at/before now; null when nothing persisted.
    public let score: Int?
    /// Freshness ladder over the latest window's age.
    public let confidence: GlanceConfidence
    /// Exactly 24 entries, local wall-clock hour ascending.
    public let curve24h: [GlanceCurvePoint]

    enum CodingKeys: String, CodingKey {
        case score
        case confidence
        case curve24h = "curve_24h"
    }
}

public enum GlanceConfidence: String, Codable {
    case high
    case medium
    case low
}

public struct GlanceCurvePoint: Codable, Equatable {
    /// Hour of the user's local day, 0-23.
    public let hour: Int
    public let score: Int?
}

/// One upcoming block: a mirrored calendar event or an accepted proposal.
public struct GlanceBlock: Codable, Equatable {
    public let start: Date
    public let end: Date
    public let title: String?
    public let energyDemand: GlanceEnergyDemand?
    public let source: GlanceBlockSource

    enum CodingKeys: String, CodingKey {
        case start
        case end
        case title
        case energyDemand = "energy_demand"
        case source
    }
}

/// Mirror of healthmes.store.enums.EnergyDemand ("low"/"med"/"high").
public enum GlanceEnergyDemand: String, Codable {
    case low
    case med
    case high
}

public enum GlanceBlockSource: String, Codable {
    case calendar
    case proposal
}

public struct GlanceAlerts: Codable, Equatable {
    /// Recent pushed alerts (server-side placeholder policy: recency stands
    /// in for resolution until the domain expert defines one).
    public let unresolvedCount: Int
    public let top: GlanceTopAlert?

    enum CodingKeys: String, CodingKey {
        case unresolvedCount = "unresolved_count"
        case top
    }
}

public struct GlanceTopAlert: Codable, Equatable {
    public let ruleId: String
    public let summary: String
    /// Browser-tappable decision-viewer link (read-only ?token= embedded
    /// server-side when the instance is token-gated); null when the alert
    /// has no recorded decision yet.
    public let decisionUrl: String?

    enum CodingKeys: String, CodingKey {
        case ruleId = "rule_id"
        case summary
        case decisionUrl = "decision_url"
    }
}

public struct GlanceDecision: Codable, Equatable {
    public let id: UUID
    public let url: String
}

/// JSON (de)coding pinned to the server's timestamp shapes.
public enum GlanceJSON {
    /// Decoder accepting both `2026-07-09T14:23:00Z` (what pydantic emits
    /// for whole seconds) and fractional-second / numeric-offset variants.
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = parseISO8601(raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Unparseable ISO-8601 datetime: \(raw)"
                )
            }
            return date
        }
        return decoder
    }

    public static func parseISO8601(_ raw: String) -> Date? {
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: raw) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: raw) { return date }
        return parseNaiveUTC(raw)
    }

    /// Store-backed endpoints (schedule proposals, food logs) serialize
    /// sqlite's naive datetimes verbatim — `2026-07-11T14:23:10.355753`,
    /// no zone designator. Every persisted datetime in the healthmes store
    /// is UTC by contract, so naive parses as UTC. (Found live: the
    /// proposals list failed to decode against a real instance without
    /// this; glance/alerts always send "Z".)
    private static func parseNaiveUTC(_ raw: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        if let date = formatter.date(from: raw) { return date }
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter.date(from: raw)
    }

    public static func decodePayload(_ data: Data) throws -> GlancePayload {
        try decoder().decode(GlancePayload.self, from: data)
    }
}
