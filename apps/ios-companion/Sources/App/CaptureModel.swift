import Foundation
import SwiftUI
import UIKit

/// What the user is logging. Food goes to `POST /v1/food-logs`;
/// medication/symptom go to `POST /v1/medical-records` (the server attaches
/// the deterministic health snapshot itself — the app sends capture
/// metadata only, never health data).
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
        case saving
        case saved(kind: CaptureTarget)
        case failed(message: String)
    }

    @Published var target: CaptureTarget = .food
    @Published var descriptionText: String = ""
    @Published var mealType: String?
    @Published var transcript: String = ""
    @Published var attachment: CaptureAttachment?
    @Published var phase: Phase = .idle

    /// Survives a failed create step so retry skips the upload.
    private var uploadedMediaPath: String?
    private var uploadedForAttachment: CaptureAttachment?

    private let api = HealthMesAPI()

    static let mealTypes = ["breakfast", "lunch", "dinner", "snack"]

    var canSubmit: Bool {
        !descriptionText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && phase != .uploading && phase != .saving
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
    }

    private func resetUploadIfAttachmentChanged() {
        if uploadedForAttachment != attachment {
            uploadedMediaPath = nil
            uploadedForAttachment = nil
        }
    }

    func submit() async {
        let text = descriptionText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }

        // Step 1 — media upload (skipped when there is no attachment or a
        // retry already uploaded it).
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

        // Step 2 — the record itself.
        phase = .saving
        do {
            switch target {
            case .food:
                _ = try await api.createFoodLog(
                    FoodLogCreateBody(
                        description: text,
                        mediaPath: mediaPath,
                        mealType: mealType,
                        source: "ios-app"
                    )
                )
            case .medication, .symptom:
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
            }
            let saved = target
            reset()
            phase = .saved(kind: saved)
        } catch {
            // The description, attachment and any uploaded media_path are
            // all still here — retry is one tap and never loses data.
            phase = .failed(message: BriefingHomeModel.describe(error))
        }
    }

    private var sourceTag: String {
        switch attachment {
        case .photo: return "ios-app-photo"
        case .voice: return "ios-app-voice"
        case nil: return "ios-app-text"
        }
    }

    private func reset() {
        descriptionText = ""
        transcript = ""
        mealType = nil
        attachment = nil
        uploadedMediaPath = nil
        uploadedForAttachment = nil
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
