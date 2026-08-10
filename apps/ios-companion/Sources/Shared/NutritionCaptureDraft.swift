import Foundation

public enum NutritionCaptureModality: String, Codable {
    case photo
    case text
    case voice
}

public struct PendingIntakeOutcome: Equatable {
    public let operationID: UUID
    public let status: IntakeOutcomeStatus
    public let actedAt: Date
    public let correctedItems: [IntakeItemResult]?
    public let note: String?
}

public struct PendingNutritionReview: Equatable {
    public let operationID: UUID
    public let status: NutritionReviewStatus
    public let items: [ReviewedNutritionItemBody]
}

public struct NutritionItemCorrectionDraft: Equatable, Identifiable {
    public let id: UUID
    public let original: IntakeItemResult
    public var name: String
    public var exactAmount: String
    public var unit: String
    public var isExcluded: Bool

    public init(
        id: UUID = UUID(),
        item: IntakeItemResult
    ) {
        self.id = id
        self.original = item
        self.name = item.name
        self.exactAmount = item.serving.exact.map {
            $0.rounded() == $0 ? String(Int($0)) : String($0)
        } ?? ""
        self.unit = item.serving.unit
        self.isExcluded = false
    }

    public var isChanged: Bool {
        isExcluded
            || name.trimmingCharacters(in: .whitespacesAndNewlines)
                != original.name
            || exactAmount.trimmingCharacters(in: .whitespacesAndNewlines)
                != originalExactAmount
            || unit.trimmingCharacters(in: .whitespacesAndNewlines)
                != original.serving.unit
    }

    public var validationMessage: String? {
        guard !isExcluded else { return nil }
        guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return "Enter a food or drink name."
        }
        let normalizedUnit = unit.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard !normalizedUnit.isEmpty else {
            return "Enter a serving unit."
        }
        let normalizedAmount = exactAmount
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ",", with: ".")
        if normalizedAmount.isEmpty {
            return original.serving.kind == "exact"
                    || normalizedUnit != original.serving.unit
                ? "Enter a serving amount greater than zero."
                : nil
        }
        guard let amount = Double(normalizedAmount), amount > 0 else {
            return "Enter a serving amount greater than zero."
        }
        return nil
    }

    public var isValid: Bool {
        validationMessage == nil
    }

    public var correctedItem: IntakeItemResult? {
        guard !isExcluded, isValid else { return nil }
        let normalizedName = name.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard
            !normalizedName.isEmpty,
            let serving = correctedServing
        else {
            return nil
        }
        let servingChanged = serving != original.serving
        return IntakeItemResult(
            name: normalizedName,
            intakeType: original.intakeType,
            serving: serving,
            nutrients: servingChanged ? [] : original.nutrients,
            confidence: original.confidence,
            warnings: servingChanged
                ? original.warnings
                    + ["Serving corrected by user; nutrients require recalculation."]
                : original.warnings
        )
    }

    /// Complete photo-review value. The review API requires every analyzed
    /// nutrient to remain present even when the user corrects the visible
    /// name or serving; downstream normalization keeps those estimates
    /// distinct from the user-edited serving.
    public var reviewedItem: IntakeItemResult? {
        guard !isExcluded, isValid else { return nil }
        let normalizedName = name.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard
            !normalizedName.isEmpty,
            let serving = correctedServing
        else {
            return nil
        }
        return IntakeItemResult(
            name: normalizedName,
            intakeType: original.intakeType,
            serving: serving,
            nutrients: original.nutrients,
            confidence: original.confidence,
            warnings: original.warnings
        )
    }

    private var originalExactAmount: String {
        original.serving.exact.map {
            $0.rounded() == $0 ? String(Int($0)) : String($0)
        } ?? ""
    }

    private var correctedServing: IntakeServingResult? {
        let normalizedUnit = unit.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        let normalizedAmount = exactAmount
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ",", with: ".")
        if normalizedAmount.isEmpty, original.serving.kind != "exact" {
            return normalizedUnit == original.serving.unit
                ? original.serving
                : nil
        }
        guard
            let amount = Double(normalizedAmount),
            amount > 0,
            !normalizedUnit.isEmpty
        else {
            return nil
        }
        if original.serving.kind == "exact",
            original.serving.exact == amount,
            original.serving.unit == normalizedUnit
        {
            return original.serving
        }
        return IntakeServingResult(
            kind: "exact",
            unit: normalizedUnit,
            exact: amount,
            minimum: nil,
            maximum: nil,
            evidenceText: "user correction",
            estimationBasis: "user"
        )
    }
}

/// Stable request identity for one user-visible nutrition draft.
///
/// UI models may keep the attachment bytes separately, but everything that
/// participates in server idempotency stays here and is reused after a
/// transport or server failure.
public struct NutritionCaptureDraft: Equatable {
    public let interactionOperationID: UUID
    public let modality: NutritionCaptureModality
    public let observedAt: Date
    public let timezone: String
    public let source: String
    public var uploadedMediaPath: String?
    public var observation: NutritionObservationResult?
    public var review: NutritionObservationReviewResult?
    public var pendingReview: PendingNutritionReview?
    public var interaction: IntakeInteractionResult?
    public var pendingOutcome: PendingIntakeOutcome?

    public init(
        interactionOperationID: UUID = UUID(),
        modality: NutritionCaptureModality,
        observedAt: Date = Date(),
        timezone: String = TimeZone.current.identifier,
        source: String
    ) {
        self.interactionOperationID = interactionOperationID
        self.modality = modality
        self.observedAt = observedAt
        self.timezone = timezone
        self.source = source
        self.uploadedMediaPath = nil
        self.observation = nil
        self.review = nil
        self.pendingReview = nil
        self.interaction = nil
        self.pendingOutcome = nil
    }

    public mutating func nutritionReview(
        status: NutritionReviewStatus,
        items: [ReviewedNutritionItemBody]
    ) -> PendingNutritionReview {
        if let pendingReview,
            pendingReview.status == status,
            pendingReview.items == items
        {
            return pendingReview
        }
        let pending = PendingNutritionReview(
            operationID: UUID(),
            status: status,
            items: items
        )
        pendingReview = pending
        return pending
    }

    /// Reuses the exact same operation ID and action timestamp when the same
    /// outcome is retried. Choosing a different outcome starts a new
    /// idempotent operation instead of conflicting with the failed one.
    public mutating func outcome(
        for status: IntakeOutcomeStatus,
        correctedItems: [IntakeItemResult]? = nil,
        note: String? = nil,
        now: Date = Date()
    ) -> PendingIntakeOutcome {
        if let pendingOutcome,
            pendingOutcome.status == status,
            pendingOutcome.correctedItems == correctedItems,
            pendingOutcome.note == note
        {
            return pendingOutcome
        }
        let pending = PendingIntakeOutcome(
            operationID: UUID(),
            status: status,
            actedAt: now,
            correctedItems: correctedItems,
            note: note
        )
        pendingOutcome = pending
        return pending
    }
}
