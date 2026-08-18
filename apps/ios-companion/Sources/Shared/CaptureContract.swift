import Foundation

// Codable contracts for the native capture flow:
//
//   1. `POST /v1/media` (multipart, field `file`)  → media_path token
//   2. nutrition captures use `/v1/nutrition-observations` and
//      `/v1/intake-interactions`; medical captures use `/v1/medical-records`
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

/// Upload content types the Apple apps produce from the server's canonical
/// allowlist. iPhone records AAC-in-m4a; Mac records PCM WAV while also
/// running on-device speech recognition.
public enum CaptureMediaType: String {
    case jpeg = "image/jpeg"
    case m4a = "audio/mp4"
    case wav = "audio/wav"

    public var fileExtension: String {
        switch self {
        case .jpeg: return "jpg"
        case .m4a: return "m4a"
        case .wav: return "wav"
        }
    }
}

/// An aware timestamp encoded with the offset of its declared IANA timezone.
///
/// Encoding a `Date` with JSONEncoder's default ISO-8601 strategy always
/// emits UTC (`Z`). The nutrition API intentionally rejects that when the
/// adjacent timezone says, for example, `Asia/Seoul`. This wrapper keeps the
/// same instant while emitting the matching local offset.
public struct NutritionCaptureTimestamp: Codable, Equatable {
    public let date: Date
    public let timezone: String

    public init(date: Date, timezone: String) {
        self.date = date
        self.timezone = timezone
    }

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        guard let decoded = formatter.date(from: value)
            ?? ISO8601DateFormatter().date(from: value)
        else {
            throw DecodingError.dataCorruptedError(
                in: try decoder.singleValueContainer(),
                debugDescription: "Expected an ISO-8601 timestamp."
            )
        }
        date = decoded
        timezone = "UTC"
    }

    public func encode(to encoder: Encoder) throws {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        formatter.timeZone = TimeZone(identifier: timezone) ?? .gmt
        var container = encoder.singleValueContainer()
        try container.encode(formatter.string(from: date))
    }
}

public struct NutritionPhotoAnalysisBody: Codable, Equatable {
    public let mediaPath: String?
    public let capturedAt: NutritionCaptureTimestamp
    public let timezone: String
    public let source: String
    public let metadataProvenance: [String: String]
    public let allowRemoteVision: Bool

    public init(
        mediaPath: String,
        capturedAt: Date,
        timezone: String,
        source: String,
        allowRemoteVision: Bool
    ) {
        self.mediaPath = mediaPath
        self.capturedAt = NutritionCaptureTimestamp(
            date: capturedAt,
            timezone: timezone
        )
        self.timezone = timezone
        self.source = source
        self.metadataProvenance = [
            "captured_at": "app",
            "timezone": "app",
            "location": "unavailable",
        ]
        self.allowRemoteVision = allowRemoteVision
    }

    enum CodingKeys: String, CodingKey {
        case mediaPath = "media_path"
        case capturedAt = "captured_at"
        case timezone
        case source
        case metadataProvenance = "metadata_provenance"
        case allowRemoteVision = "allow_remote_vision"
    }
}

public struct NutritionObservationNutrientResult: Codable, Equatable {
    public let nutrient: String
    public let amount: IntakeServingResult
    public let confidence: String
}

public struct NutritionObservationItemResult: Codable, Equatable {
    public let intakeType: String
    public let nameCandidates: [String]
    public let category: String?
    public let serving: IntakeServingResult
    public let caffeine: IntakeServingResult
    public let nutrients: [NutritionObservationNutrientResult]
    public let confidence: String
    public let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case intakeType = "intake_type"
        case nameCandidates = "name_candidates"
        case category
        case serving
        case caffeine
        case nutrients
        case confidence
        case warnings
    }

    public var reviewCandidate: IntakeItemResult {
        IntakeItemResult(
            name: nameCandidates.first
                ?? category
                ?? "Unidentified food or drink",
            intakeType: intakeType,
            serving: serving,
            nutrients: nutrients.map {
                IntakeNutrientFactResult(
                    nutrient: $0.nutrient,
                    amount: $0.amount,
                    confidence: $0.confidence,
                    origin: "agent",
                    evidenceText: nil
                )
            },
            confidence: confidence,
            warnings: warnings
        )
    }
}

public struct NutritionObservationResult: Codable, Equatable {
    public let observationID: UUID
    public let status: String
    public let confidence: String
    public let warnings: [String]
    public let items: [NutritionObservationItemResult]
    public let confirmationStatus: String

    enum CodingKeys: String, CodingKey {
        case observationID = "observation_id"
        case status
        case confidence
        case warnings
        case items
        case confirmationStatus = "confirmation_status"
    }
}

public enum NutritionReviewStatus: String, Codable {
    case confirmed
    case corrected
    case rejected
}

public struct ReviewedEstimateBody: Codable, Equatable {
    public let kind: String
    public let unit: String
    public let exact: Double?
    public let minimum: Double?
    public let maximum: Double?
    public let estimationBasis: String?

    public init(_ value: IntakeServingResult) {
        kind = value.kind
        unit = value.unit
        exact = value.exact
        minimum = value.minimum
        maximum = value.maximum
        estimationBasis = value.estimationBasis
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case unit
        case exact
        case minimum
        case maximum
        case estimationBasis = "estimation_basis"
    }
}

public struct ReviewedNutrientBody: Codable, Equatable {
    public let nutrient: String
    public let amount: ReviewedEstimateBody
    public let confidence: String

    public init(_ value: IntakeNutrientFactResult) {
        nutrient = value.nutrient
        amount = ReviewedEstimateBody(value.amount)
        confidence = value.confidence
    }
}

public struct ReviewedNutritionItemBody: Codable, Equatable {
    public let itemIndex: Int
    public let name: String
    public let intakeType: String
    public let serving: ReviewedEstimateBody
    public let nutrients: [ReviewedNutrientBody]
    public let confidence: String
    public let warnings: [String]

    public init(itemIndex: Int, item: IntakeItemResult) {
        self.itemIndex = itemIndex
        name = item.name
        intakeType = item.intakeType
        serving = ReviewedEstimateBody(item.serving)
        nutrients = item.nutrients.map(ReviewedNutrientBody.init)
        confidence = item.confidence
        warnings = item.warnings
    }

    enum CodingKeys: String, CodingKey {
        case itemIndex = "item_index"
        case name
        case intakeType = "intake_type"
        case serving
        case nutrients
        case confidence
        case warnings
    }
}

public struct NutritionObservationReviewBody: Codable, Equatable {
    public let operationID: UUID
    public let status: NutritionReviewStatus
    public let source: String
    public let items: [ReviewedNutritionItemBody]

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case status
        case source
        case items
    }
}

public struct NutritionObservationReviewResult: Codable, Equatable {
    public let reviewID: UUID
    public let observationID: UUID
    public let status: NutritionReviewStatus

    enum CodingKeys: String, CodingKey {
        case reviewID = "review_id"
        case observationID = "observation_id"
        case status
    }
}

public struct IntakeInteractionAnalysisBody: Codable, Equatable {
    public let operationID: UUID
    public let intent = "log_consumed"
    public let modality: String
    public let observedAt: NutritionCaptureTimestamp
    public let timezone: String
    public let source: String
    public let sourceText: String?
    public let mediaPath: String?
    public let allowRemoteAnalysis: Bool

    public init(
        operationID: UUID,
        modality: String,
        observedAt: Date,
        timezone: String,
        source: String,
        sourceText: String?,
        mediaPath: String?,
        allowRemoteAnalysis: Bool
    ) {
        self.operationID = operationID
        self.modality = modality
        self.observedAt = NutritionCaptureTimestamp(
            date: observedAt,
            timezone: timezone
        )
        self.timezone = timezone
        self.source = source
        self.sourceText = sourceText
        self.mediaPath = mediaPath
        self.allowRemoteAnalysis = allowRemoteAnalysis
    }

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case intent
        case modality
        case observedAt = "observed_at"
        case timezone
        case source
        case sourceText = "source_text"
        case mediaPath = "media_path"
        case allowRemoteAnalysis = "allow_remote_analysis"
    }
}

public struct PhotoIntakeInteractionBody: Codable, Equatable {
    public let operationID: UUID
    public let intent = "log_consumed"
    public let modality = "photo"
    public let source: String
    public let sourceText: String?
    public let nutritionObservationID: UUID

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case intent
        case modality
        case source
        case sourceText = "source_text"
        case nutritionObservationID = "nutrition_observation_id"
    }
}

public struct IntakeServingResult: Codable, Equatable {
    public let kind: String
    public let unit: String
    public let exact: Double?
    public let minimum: Double?
    public let maximum: Double?
    public let evidenceText: String?
    public let estimationBasis: String?

    enum CodingKeys: String, CodingKey {
        case kind
        case unit
        case exact
        case minimum
        case maximum
        case evidenceText = "evidence_text"
        case estimationBasis = "estimation_basis"
    }

    public var summary: String {
        switch kind {
        case "exact":
            guard let exact else { return unit }
            return "\(Self.number(exact)) \(unit)"
        case "range":
            guard let minimum, let maximum else { return unit }
            return "\(Self.number(minimum))–\(Self.number(maximum)) \(unit)"
        default:
            return "Amount unknown"
        }
    }

    private static func number(_ value: Double) -> String {
        value.rounded() == value
            ? String(Int(value))
            : String(format: "%.1f", value)
    }
}

public struct IntakeNutrientFactResult: Codable, Equatable {
    public let nutrient: String
    public let amount: IntakeServingResult
    public let confidence: String
    public let origin: String
    public let evidenceText: String?

    enum CodingKeys: String, CodingKey {
        case nutrient
        case amount
        case confidence
        case origin
        case evidenceText = "evidence_text"
    }
}

public struct IntakeItemResult: Codable, Equatable {
    public let name: String
    public let intakeType: String
    public let serving: IntakeServingResult
    public let nutrients: [IntakeNutrientFactResult]
    public let confidence: String
    public let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case name
        case intakeType = "intake_type"
        case serving
        case nutrients
        case confidence
        case warnings
    }
}

public struct IntakeInteractionResult: Codable, Equatable {
    public let interactionID: UUID
    public let resolvedItems: [IntakeItemResult]
    public let warnings: [String]
    public let isConfirmedIntake: Bool
    public let modality: String
    public let sourceText: String?

    enum CodingKeys: String, CodingKey {
        case interactionID = "interaction_id"
        case resolvedItems = "resolved_items"
        case warnings
        case isConfirmedIntake = "is_confirmed_intake"
        case modality
        case sourceText = "source_text"
    }
}

public enum IntakeOutcomeStatus: String, Codable, CaseIterable {
    case consumed
    case notConsumed = "not_consumed"
    case cancelled
}

public struct IntakeOutcomeBody: Codable, Equatable {
    public let operationID: UUID
    public let status: IntakeOutcomeStatus
    public let source: String
    public let consumedAt: Date?
    public let correctedItems: [IntakeItemResult]?
    public let note: String?

    public init(
        operationID: UUID,
        status: IntakeOutcomeStatus,
        source: String,
        consumedAt: Date?,
        correctedItems: [IntakeItemResult]? = nil,
        note: String?
    ) {
        self.operationID = operationID
        self.status = status
        self.source = source
        self.consumedAt = consumedAt
        self.correctedItems = correctedItems
        self.note = note
    }

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case status
        case source
        case consumedAt = "consumed_at"
        case correctedItems = "corrected_items"
        case note
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
