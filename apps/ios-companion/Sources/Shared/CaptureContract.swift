import Foundation

// Codable contracts for the native capture flow (issue #10):
//
//   1. `POST /v1/media` (multipart, field `file`)  → media_path token
//   2. `POST /v1/food-logs` or `POST /v1/medical-records` with that token
//
// healthmes/api/media.py + food.py + medical.py. Only the media_path string
// is ever stored server-side; the server attaches its own deterministic
// health snapshot to medical records (`context.health`) — the app must NEVER
// send health data in `context` (capture metadata only).

/// Response of `POST /v1/media`.
public struct MediaUpload: Codable, Equatable {
    /// Data-dir-relative token, e.g. "media/2026/07/<uuid>.jpg". Pass it
    /// verbatim to the food/medical create call, and to
    /// `GET /v1/media/{media_path}` to serve the bytes back.
    public let mediaPath: String
    /// Canonical stored type (client aliases normalised server-side).
    public let contentType: String
    public let bytes: Int

    enum CodingKeys: String, CodingKey {
        case mediaPath = "media_path"
        case contentType = "content_type"
        case bytes
    }
}

/// Upload content types the app produces, from the server's canonical
/// allowlist (healthmes/api/media.py CANONICAL_CONTENT_TYPES). The app
/// re-encodes camera/library photos to JPEG and records voice memos as
/// AAC-in-m4a, so only these two are ever sent.
public enum CaptureMediaType: String {
    case jpeg = "image/jpeg"
    case m4a = "audio/mp4"

    public var fileExtension: String {
        switch self {
        case .jpeg: return "jpg"
        case .m4a: return "m4a"
        }
    }
}

/// Body of `POST /v1/food-logs` (healthmes/api/food.py FoodLogCreate).
public struct FoodLogCreateBody: Codable, Equatable {
    public let description: String
    public let mediaPath: String?
    public let mealType: String?
    /// Capture channel; this app always sends "ios-app".
    public let source: String

    public init(description: String, mediaPath: String?, mealType: String?, source: String) {
        self.description = description
        self.mediaPath = mediaPath
        self.mealType = mealType
        self.source = source
    }

    enum CodingKeys: String, CodingKey {
        case description
        case mediaPath = "media_path"
        case mealType = "meal_type"
        case source
    }
}

public struct FoodLogItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let loggedAt: Date
    public let description: String
    public let mediaPath: String?
    public let mealType: String?

    enum CodingKeys: String, CodingKey {
        case id
        case loggedAt = "logged_at"
        case description
        case mediaPath = "media_path"
        case mealType = "meal_type"
    }
}

/// Mirror of healthmes.store.enums.MedicalRecordKind.
public enum MedicalCaptureKind: String, Codable, CaseIterable {
    case medication
    case symptom
}

/// Body of `POST /v1/medical-records` (healthmes/api/medical.py
/// MedicalRecordCreate — the same contract the Telegram capture skill uses).
public struct MedicalRecordCreateBody: Codable, Equatable {
    public let kind: MedicalCaptureKind
    public let description: String
    public let mediaPath: String?
    public let transcript: String?
    /// Capture metadata ONLY (e.g. {"source": "ios-app-photo"}); the server
    /// attaches the health snapshot itself under context.health.
    public let context: [String: JSONValue]?

    public init(
        kind: MedicalCaptureKind,
        description: String,
        mediaPath: String?,
        transcript: String?,
        context: [String: JSONValue]?
    ) {
        self.kind = kind
        self.description = description
        self.mediaPath = mediaPath
        self.transcript = transcript
        self.context = context
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case description
        case mediaPath = "media_path"
        case transcript
        case context
    }
}

public struct MedicalRecordItem: Codable, Equatable, Identifiable {
    public let id: UUID
    public let kind: MedicalCaptureKind
    public let description: String
    public let mediaPath: String?
    public let transcript: String?
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case description
        case mediaPath = "media_path"
        case transcript
        case createdAt = "created_at"
    }
}
