import Foundation
import SwiftUI
import UIKit

/// What the user is logging. Food uses the nutrition interaction engine;
/// medication/symptom continue to use the medical record endpoint.
enum CaptureTarget: String, CaseIterable, Identifiable {
    case food
    case medication
    case symptom

    var id: String { rawValue }
}

enum CaptureAttachment: Equatable {
    case photo(jpegData: Data)
    case voice(m4aData: Data, duration: TimeInterval)

    var mediaType: CaptureMediaType {
        switch self {
        case .photo: return .jpeg
        case .voice: return .m4a
        }
    }

    var data: Data {
        switch self {
        case .photo(let data): return data
        case .voice(let data, _): return data
        }
    }
}

/// Two-step submission (upload → create) with offline-friendly retry: a
/// successful upload's `media_path` is kept, so retrying after a network
/// failure never re-uploads the bytes, and the user's text is never lost.
@MainActor
final class CaptureModel: ObservableObject {
    enum Phase: Equatable {
        case idle
        case uploading
        case analyzing
        case saving
        case reviewing
        case resolving(IntakeOutcomeStatus)
        case saved(kind: CaptureTarget, outcome: IntakeOutcomeStatus?)
        case failed(message: String)
    }

    @Published var target: CaptureTarget = .food
    @Published var descriptionText: String = ""
    @Published var mealType: String?
    @Published var transcript: String = ""
    @Published var attachment: CaptureAttachment?
    @Published var allowRemoteAnalysis = false
    @Published private(set) var savedIntakeItems: [String] = []
    @Published private(set) var reviewObservation: NutritionObservationResult?
    @Published private(set) var reviewInteraction: IntakeInteractionResult?
    @Published var foodCorrections: [NutritionItemCorrectionDraft] = []
    @Published private(set) var outcomeError: String?
    @Published var phase: Phase = .idle

    /// Medical captures retain their upload across create retries. Nutrition
    /// captures keep the equivalent value in `foodDraft`.
    private var uploadedMediaPath: String?
    private var uploadedForAttachment: CaptureAttachment?
    private var foodDraft: NutritionCaptureDraft?
    private var foodDraftKey: FoodDraftKey?

    private let api = HealthMesAPI()

    static let mealTypes = ["breakfast", "lunch", "dinner", "snack"]

    var isFoodReviewLocked: Bool {
        reviewInteraction != nil
            || phase == .analyzing
            || phase == .uploading
            || isResolving
    }

    private var isResolving: Bool {
        if case .resolving = phase { return true }
        return false
    }

    var canSubmit: Bool {
        let hasText = !descriptionText.trimmingCharacters(
            in: .whitespacesAndNewlines
        ).isEmpty
        let hasFoodCapture = target == .food && (hasText || attachment != nil)
        return (hasFoodCapture || (target != .food && hasText))
            && phase != .uploading
            && phase != .analyzing
            && phase != .saving
            && !isResolving
            && reviewInteraction == nil
    }

    func setPhoto(_ image: UIImage) {
        // Re-encode to JPEG: uniform, always in the server allowlist, and
        // strips camera metadata (only the pixels leave the device — and
        // only to the user's own instance).
        guard let data = image.jpegData(compressionQuality: 0.85) else { return }
        attachment = .photo(jpegData: data)
        resetUploadIfAttachmentChanged()
    }

    func setVoice(data: Data, duration: TimeInterval) {
        attachment = .voice(m4aData: data, duration: duration)
        resetUploadIfAttachmentChanged()
    }

    func removeAttachment() {
        attachment = nil
        uploadedMediaPath = nil
        uploadedForAttachment = nil
        invalidateFoodDraft()
    }

    private func resetUploadIfAttachmentChanged() {
        if uploadedForAttachment != attachment {
            uploadedMediaPath = nil
            uploadedForAttachment = nil
        }
        invalidateFoodDraft()
    }

    func submit() async {
        let text = descriptionText.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard canSubmit else { return }

        savedIntakeItems = []
        outcomeError = nil
        if target == .food {
            await analyzeFood(text: text)
        } else {
            await saveMedical(text: text)
        }
    }

    func recordFoodOutcome(_ status: IntakeOutcomeStatus) async {
        guard var draft = foodDraft, let interaction = draft.interaction else {
            return
        }
        if status == .consumed,
            foodCorrections.contains(where: { !$0.isValid })
        {
            outcomeError = "Fix the highlighted intake correction before recording it."
            return
        }

        let correctedItems = foodCorrections.contains(where: \.isChanged)
            ? foodCorrections.compactMap(\.correctedItem)
            : nil
        let submittedItems = status == .consumed ? correctedItems : nil
        let note = status == .consumed
            ? mealType.map { "meal_type=\($0)" }
            : nil
        let pending = draft.outcome(
            for: status,
            correctedItems: submittedItems,
            note: note
        )
        foodDraft = draft
        outcomeError = nil
        phase = .resolving(status)
        do {
            let result = try await api.confirmIntake(
                interactionID: interaction.interactionID,
                body: IntakeOutcomeBody(
                    operationID: pending.operationID,
                    status: status,
                    source: "ios-app",
                    consumedAt: status == .consumed ? pending.actedAt : nil,
                    correctedItems: pending.correctedItems,
                    note: pending.note
                )
            )
            savedIntakeItems = result.resolvedItems.map(\.name)
            resetContent(preserveSavedItems: true)
            phase = .saved(kind: .food, outcome: status)
        } catch {
            // Keep the interaction and pending outcome identity. Retrying the
            // same button sends byte-for-byte equivalent idempotency inputs.
            foodDraft = draft
            phase = .reviewing
            outcomeError = BriefingHomeModel.describe(error)
        }
    }

    var hasInvalidFoodCorrections: Bool {
        foodCorrections.contains(where: { !$0.isValid })
    }

    private func analyzeFood(text: String) async {
        var draft = prepareFoodDraft(text: text)

        // Upload once per attachment. The returned path belongs to the draft
        // and survives failures in all later analysis/review stages.
        if let attachment, draft.uploadedMediaPath == nil {
            phase = .uploading
            do {
                let upload = try await api.uploadMedia(
                    data: attachment.data,
                    mediaType: attachment.mediaType
                )
                draft.uploadedMediaPath = upload.mediaPath
                foodDraft = draft
            } catch {
                phase = .failed(message: Self.describeUpload(error))
                return
            }
        }

        phase = .analyzing
        do {
            if draft.interaction == nil {
                switch draft.modality {
                case .photo:
                    guard let mediaPath = draft.uploadedMediaPath else {
                        throw HealthMesAPIError.httpStatus(422)
                    }
                    if draft.observation == nil {
                        draft.observation = try await api.analyzeNutritionPhoto(
                            NutritionPhotoAnalysisBody(
                                mediaPath: mediaPath,
                                capturedAt: draft.observedAt,
                                timezone: draft.timezone,
                                source: draft.source,
                                allowRemoteVision: allowRemoteAnalysis
                            )
                        )
                        foodDraft = draft
                    }
                    guard let observation = draft.observation else {
                        throw HealthMesAPIError.httpStatus(422)
                    }
                    draft.interaction = try await api.createPhotoIntake(
                        PhotoIntakeInteractionBody(
                            operationID: draft.interactionOperationID,
                            source: draft.source,
                            sourceText: text.isEmpty ? nil : text,
                            nutritionObservationID: observation.observationID
                        )
                    )
                case .voice:
                    guard let mediaPath = draft.uploadedMediaPath else {
                        throw HealthMesAPIError.httpStatus(422)
                    }
                    draft.interaction = try await api.analyzeIntake(
                        IntakeInteractionAnalysisBody(
                            operationID: draft.interactionOperationID,
                            modality: draft.modality.rawValue,
                            observedAt: draft.observedAt,
                            timezone: draft.timezone,
                            source: draft.source,
                            sourceText: nil,
                            mediaPath: mediaPath,
                            allowRemoteAnalysis: allowRemoteAnalysis
                        )
                    )
                case .text:
                    draft.interaction = try await api.analyzeIntake(
                        IntakeInteractionAnalysisBody(
                            operationID: draft.interactionOperationID,
                            modality: draft.modality.rawValue,
                            observedAt: draft.observedAt,
                            timezone: draft.timezone,
                            source: draft.source,
                            sourceText: text,
                            mediaPath: nil,
                            allowRemoteAnalysis: allowRemoteAnalysis
                        )
                    )
                }
                foodDraft = draft
            }

            reviewObservation = draft.observation
            reviewInteraction = draft.interaction
            foodCorrections = draft.interaction?.resolvedItems.map {
                NutritionItemCorrectionDraft(item: $0)
            } ?? []
            phase = .reviewing
        } catch {
            foodDraft = draft
            phase = .failed(message: BriefingHomeModel.describe(error))
        }
    }

    private func saveMedical(text: String) async {
        // Media upload is skipped when a retry already completed it.
        var mediaPath: String? = uploadedMediaPath
        if let attachment, mediaPath == nil {
            phase = .uploading
            do {
                let upload = try await api.uploadMedia(
                    data: attachment.data, mediaType: attachment.mediaType
                )
                mediaPath = upload.mediaPath
                uploadedMediaPath = upload.mediaPath
                uploadedForAttachment = attachment
            } catch {
                phase = .failed(message: Self.describeUpload(error))
                return
            }
        }

        phase = .saving
        do {
            let voiceTranscript = transcript.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            _ = try await api.createMedicalRecord(
                MedicalRecordCreateBody(
                    kind: target == .medication ? .medication : .symptom,
                    description: text,
                    mediaPath: mediaPath,
                    transcript: voiceTranscript.isEmpty ? nil : voiceTranscript,
                    // Capture metadata ONLY — the server owns the health
                    // snapshot (context.health).
                    context: ["source": .string(sourceTag)]
                )
            )
            let saved = target
            resetContent()
            phase = .saved(kind: saved, outcome: nil)
        } catch {
            // The description, attachment and any uploaded media_path are
            // all still here — retry is one tap and never loses data.
            phase = .failed(message: BriefingHomeModel.describe(error))
        }
    }

    private struct FoodDraftKey: Equatable {
        let text: String
        let mealType: String?
        let attachment: CaptureAttachment?
        let allowRemoteAnalysis: Bool
    }

    private func prepareFoodDraft(text: String) -> NutritionCaptureDraft {
        let key = FoodDraftKey(
            text: text,
            mealType: mealType,
            attachment: attachment,
            allowRemoteAnalysis: allowRemoteAnalysis
        )
        if key != foodDraftKey || foodDraft == nil {
            let sameAttachment = foodDraftKey.map {
                $0.attachment == attachment
            } ?? false
            let reusableMediaPath = sameAttachment
                ? foodDraft?.uploadedMediaPath
                : nil
            let modality: NutritionCaptureModality
            switch attachment {
            case .photo: modality = .photo
            case .voice: modality = .voice
            case nil: modality = .text
            }
            var draft = NutritionCaptureDraft(
                modality: modality,
                source: "ios-app-\(modality.rawValue)"
            )
            draft.uploadedMediaPath = reusableMediaPath
            foodDraft = draft
            foodDraftKey = key
            reviewObservation = nil
            reviewInteraction = nil
            foodCorrections = []
            outcomeError = nil
        }
        return foodDraft!
    }

    private var sourceTag: String {
        switch attachment {
        case .photo: return "ios-app-photo"
        case .voice: return "ios-app-voice"
        case nil: return "ios-app-text"
        }
    }

    private func invalidateFoodDraft() {
        foodDraft = nil
        foodDraftKey = nil
        reviewObservation = nil
        reviewInteraction = nil
        foodCorrections = []
        outcomeError = nil
    }

    private func resetContent(preserveSavedItems: Bool = false) {
        descriptionText = ""
        transcript = ""
        mealType = nil
        attachment = nil
        uploadedMediaPath = nil
        uploadedForAttachment = nil
        invalidateFoodDraft()
        if !preserveSavedItems {
            savedIntakeItems = []
        }
    }

    static func describeUpload(_ error: Error) -> String {
        if case HealthMesAPIError.server(let status, let code, _, _) = error {
            switch (status, code) {
            case (413, _):
                return String(
                    localized: "The file is too large for your instance's upload cap.")
            case (415, _):
                return String(localized: "This file type is not accepted by the server.")
            default:
                break
            }
        }
        return BriefingHomeModel.describe(error)
    }
}
