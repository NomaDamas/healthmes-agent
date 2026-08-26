import Foundation

public struct WellnessDecisionHints: Codable, Equatable {
    public let localDate: String?
    public let start: Date?
    public let end: Date?
    public let lookbackDays: Int?
    public let relatedRecordIDs: [String: String]

    public init(
        localDate: String? = nil,
        start: Date? = nil,
        end: Date? = nil,
        lookbackDays: Int? = nil,
        relatedRecordIDs: [String: String] = [:]
    ) {
        self.localDate = localDate
        self.start = start
        self.end = end
        self.lookbackDays = lookbackDays
        self.relatedRecordIDs = relatedRecordIDs
    }

    enum CodingKeys: String, CodingKey {
        case localDate = "local_date"
        case start
        case end
        case lookbackDays = "lookback_days"
        case relatedRecordIDs = "related_record_ids"
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let localDate {
            try container.encode(localDate, forKey: .localDate)
        } else {
            try container.encodeNil(forKey: .localDate)
        }
        if let start {
            try container.encode(start, forKey: .start)
        } else {
            try container.encodeNil(forKey: .start)
        }
        if let end {
            try container.encode(end, forKey: .end)
        } else {
            try container.encodeNil(forKey: .end)
        }
        if let lookbackDays {
            try container.encode(lookbackDays, forKey: .lookbackDays)
        } else {
            try container.encodeNil(forKey: .lookbackDays)
        }
        try container.encode(relatedRecordIDs, forKey: .relatedRecordIDs)
    }
}

public struct WellnessDecisionInput: Codable, Equatable {
    public let question: String
    public let persistenceRequested: Bool
    public let hints: WellnessDecisionHints

    public init(
        question: String,
        persistenceRequested: Bool = false,
        hints: WellnessDecisionHints = WellnessDecisionHints()
    ) {
        self.question = question
        self.persistenceRequested = persistenceRequested
        self.hints = hints
    }

    enum CodingKeys: String, CodingKey {
        case question
        case persistenceRequested = "persistence_requested"
        case hints
    }
}

public enum WellnessDecisionStatus: String, Codable {
    case completed
    case needsClarification = "needs_clarification"
    case blocked
    case failed
}

public enum WellnessDecisionPersistenceStatus: String, Codable {
    case notRequired = "not_required"
    case persisted
    case failed
    case unknown
}

public enum WellnessDecisionActionKind: String, Codable {
    case walk
    case drinkWater = "drink_water"
    case sleepPreparation = "sleep_preparation"
}

public enum WellnessDecisionActionState: String, Codable {
    case recommended
    case offered
    case selected
}

public struct WellnessDecisionAction: Codable, Equatable {
    public let kind: WellnessDecisionActionKind
    public let state: WellnessDecisionActionState
    public let durationMinutes: Int?
    public let advanceMinutes: Int?

    public init(
        kind: WellnessDecisionActionKind,
        state: WellnessDecisionActionState,
        durationMinutes: Int? = nil,
        advanceMinutes: Int? = nil
    ) {
        self.kind = kind
        self.state = state
        self.durationMinutes = durationMinutes
        self.advanceMinutes = advanceMinutes
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case state
        case durationMinutes = "duration_minutes"
        case advanceMinutes = "advance_minutes"
    }
}

public enum WellnessDecisionSourceFreshness: String, Codable {
    case current
    case stale
    case unknown
    case unavailable
}

public struct WellnessDecisionSourceRef: Codable, Equatable {
    public let referenceID: String
    public let domain: String
    public let resourceType: String
    public let recordID: String
    public let sourceProvider: String
    public let observedStart: Date
    public let observedEnd: Date?
    public let collectedAt: Date?
    public let schemaVersion: Int
    public let derivedBy: String?
    public let freshness: WellnessDecisionSourceFreshness
    public let coverage: Double?
    public let contentDigest: String?
    public let sensitivity: String

    public init(
        referenceID: String,
        domain: String,
        resourceType: String,
        recordID: String,
        sourceProvider: String,
        observedStart: Date,
        observedEnd: Date? = nil,
        collectedAt: Date? = nil,
        schemaVersion: Int = 1,
        derivedBy: String? = nil,
        freshness: WellnessDecisionSourceFreshness = .unknown,
        coverage: Double? = nil,
        contentDigest: String? = nil,
        sensitivity: String = "wellness"
    ) {
        self.referenceID = referenceID
        self.domain = domain
        self.resourceType = resourceType
        self.recordID = recordID
        self.sourceProvider = sourceProvider
        self.observedStart = observedStart
        self.observedEnd = observedEnd
        self.collectedAt = collectedAt
        self.schemaVersion = schemaVersion
        self.derivedBy = derivedBy
        self.freshness = freshness
        self.coverage = coverage
        self.contentDigest = contentDigest
        self.sensitivity = sensitivity
    }

    enum CodingKeys: String, CodingKey {
        case referenceID = "reference_id"
        case domain
        case resourceType = "resource_type"
        case recordID = "record_id"
        case sourceProvider = "source_provider"
        case observedStart = "observed_start"
        case observedEnd = "observed_end"
        case collectedAt = "collected_at"
        case schemaVersion = "schema_version"
        case derivedBy = "derived_by"
        case freshness
        case coverage
        case contentDigest = "content_digest"
        case sensitivity
    }
}

public struct WellnessDecisionRuntime: Codable, Equatable {
    public let runtime: String
    public let model: String?
    public let provider: String?
    public let inputTokens: Int?
    public let outputTokens: Int?

    public init(
        runtime: String,
        model: String? = nil,
        provider: String? = nil,
        inputTokens: Int? = nil,
        outputTokens: Int? = nil
    ) {
        self.runtime = runtime
        self.model = model
        self.provider = provider
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
    }

    enum CodingKeys: String, CodingKey {
        case runtime
        case model
        case provider
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
    }
}

public struct WellnessDecisionOutput: Codable, Equatable {
    public let requestID: UUID
    public let turnID: UUID
    public let status: WellnessDecisionStatus
    public let answer: String?
    public let proposedAction: Bool
    public let actions: [WellnessDecisionAction]
    public let sourceRefs: [WellnessDecisionSourceRef]
    public let relatedRecordIDs: [String: String]
    public let limitations: [String]
    public let clarificationQuestion: String?
    public let confidence: Double?
    public let uncertainty: String?
    public let followUpQuestion: String?
    public let persistenceStatus: WellnessDecisionPersistenceStatus
    public let decisionRecordID: UUID?
    public let runtime: WellnessDecisionRuntime

    public init(
        requestID: UUID,
        turnID: UUID,
        status: WellnessDecisionStatus,
        answer: String? = nil,
        proposedAction: Bool = false,
        actions: [WellnessDecisionAction] = [],
        sourceRefs: [WellnessDecisionSourceRef] = [],
        relatedRecordIDs: [String: String] = [:],
        limitations: [String] = [],
        clarificationQuestion: String? = nil,
        confidence: Double? = nil,
        uncertainty: String? = nil,
        followUpQuestion: String? = nil,
        persistenceStatus: WellnessDecisionPersistenceStatus = .notRequired,
        decisionRecordID: UUID? = nil,
        runtime: WellnessDecisionRuntime
    ) {
        self.requestID = requestID
        self.turnID = turnID
        self.status = status
        self.answer = answer
        self.proposedAction = proposedAction
        self.actions = actions
        self.sourceRefs = sourceRefs
        self.relatedRecordIDs = relatedRecordIDs
        self.limitations = limitations
        self.clarificationQuestion = clarificationQuestion
        self.confidence = confidence
        self.uncertainty = uncertainty
        self.followUpQuestion = followUpQuestion
        self.persistenceStatus = persistenceStatus
        self.decisionRecordID = decisionRecordID
        self.runtime = runtime
    }

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case turnID = "turn_id"
        case status
        case answer
        case proposedAction = "proposed_action"
        case actions
        case sourceRefs = "source_refs"
        case relatedRecordIDs = "related_record_ids"
        case limitations
        case clarificationQuestion = "clarification_question"
        case confidence
        case uncertainty
        case followUpQuestion = "follow_up_question"
        case persistenceStatus = "persistence_status"
        case decisionRecordID = "decision_record_id"
        case runtime
    }
}

public struct WellnessDecisionPresentation: Equatable {
    public let output: WellnessDecisionOutput
    public let scene: WellnessScene

    public init(output: WellnessDecisionOutput, scene: WellnessScene) {
        self.output = output
        self.scene = scene
    }
}

public enum WellnessDecisionIdempotencyKey {
    public static func make() -> String {
        UUID().uuidString.lowercased()
    }
}

public enum WellnessDecisionWatchRelay {
    public static func question(
        from command: String,
        proposalID: UUID?
    ) -> String {
        let lines = command.split(
            separator: "\n",
            maxSplits: 1,
            omittingEmptySubsequences: false
        )
        let header = lines.first.map(String.init) ?? ""
        let spokenText = lines.count > 1 ? String(lines[1]) : ""
        var safeContext: [String] = []

        if header.hasPrefix("Spoken instruction ["), header.hasSuffix("]") {
            let start = header.index(
                header.startIndex,
                offsetBy: "Spoken instruction [".count
            )
            let end = header.index(before: header.endIndex)
            safeContext = header[start..<end]
                .split(separator: "|")
                .map {
                    $0.trimmingCharacters(in: .whitespacesAndNewlines)
                }
                .filter { !$0.lowercased().hasPrefix("proposal:") }
        }

        let knownID = proposalID?.uuidString.lowercased()
        func withoutKnownID(_ value: String) -> String {
            guard let knownID else { return value }
            return value
                .replacingOccurrences(
                    of: knownID,
                    with: "",
                    options: [.caseInsensitive]
                )
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        let text = withoutKnownID(
            spokenText.isEmpty && !header.hasPrefix("Spoken instruction")
                ? header
                : spokenText
        )
        let context = safeContext
            .map(withoutKnownID)
            .filter { !$0.isEmpty }
            .joined(separator: " · ")

        if !text.isEmpty, !context.isEmpty {
            return "\(text)\nContext: \(context)"
        }
        if !text.isEmpty {
            return text
        }
        if !context.isEmpty {
            return "Spoken wellness instruction\nContext: \(context)"
        }
        return "Spoken wellness instruction"
    }
}
