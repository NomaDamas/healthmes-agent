import PhotosUI
import SwiftUI

/// Capture shortcuts (issue #10): camera / photo-picker / voice memo →
/// `POST /v1/media` → `POST /v1/food-logs` or `/v1/medical-records`, with a
/// description the user edits before submitting. Matches the Telegram
/// capture skill's contract; the server attaches the health snapshot.
struct CaptureView: View {
    @StateObject private var model = CaptureModel()
    @StateObject private var recorder = VoiceMemoRecorder()
    @State private var photoItem: PhotosPickerItem?
    @State private var showCamera = false

    var body: some View {
        Form {
            targetSection
            attachmentSection
            descriptionSection
            submitSection
        }
        .navigationTitle(Text("Capture"))
        .sheet(isPresented: $showCamera) {
            CameraPicker { image in
                model.setPhoto(image)
            }
            .ignoresSafeArea()
        }
        .onChange(of: photoItem) { _, item in
            guard let item else { return }
            Task {
                if let data = try? await item.loadTransferable(type: Data.self),
                    let image = UIImage(data: data)
                {
                    model.setPhoto(image)
                }
                photoItem = nil
            }
        }
    }

    // MARK: Sections

    private var targetSection: some View {
        Section {
            Picker(selection: $model.target) {
                Text("Food").tag(CaptureTarget.food)
                Text("Medication").tag(CaptureTarget.medication)
                Text("Symptom").tag(CaptureTarget.symptom)
            } label: {
                Text("What are you logging?")
            }
            .pickerStyle(.segmented)
            .accessibilityLabel(Text("What are you logging?"))

            if model.target == .food {
                Picker(selection: $model.mealType) {
                    Text("Not set").tag(String?.none)
                    Text("Breakfast").tag(String?.some("breakfast"))
                    Text("Lunch").tag(String?.some("lunch"))
                    Text("Dinner").tag(String?.some("dinner"))
                    Text("Snack").tag(String?.some("snack"))
                } label: {
                    Text("Meal")
                }
            }
        } footer: {
            if model.target != .food {
                Text(
                    "Medical captures stay on your own instance. Your server attaches its health snapshot; this app never guesses drug names or diagnoses."
                )
            }
        }
    }

    private var attachmentSection: some View {
        Section {
            HStack(spacing: 12) {
                if CameraPicker.isAvailable {
                    Button {
                        showCamera = true
                    } label: {
                        Label("Camera", systemImage: "camera")
                    }
                    .buttonStyle(.bordered)
                }
                PhotosPicker(selection: $photoItem, matching: .images) {
                    Label("Photo", systemImage: "photo.on.rectangle")
                }
                .buttonStyle(.bordered)
                recordButton
            }
            .labelStyle(.titleAndIcon)
            .font(.callout)

            if recorder.isRecording {
                HStack {
                    Image(systemName: "waveform")
                        .symbolEffect(.variableColor.iterative)
                        .foregroundStyle(.red)
                    Text("Recording… \(Int(recorder.elapsed))s")
                        .font(.callout)
                    Spacer()
                    Button(role: .cancel) {
                        recorder.cancel()
                    } label: {
                        Text("Cancel")
                    }
                }
                .accessibilityElement(children: .combine)
            }

            if recorder.permissionDenied {
                Label {
                    Text("Microphone access is off — enable it in iOS Settings to record voice memos.")
                } icon: {
                    Image(systemName: "mic.slash")
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
            }

            attachmentPreview
        } header: {
            Text("Attachment (optional)")
        }
    }

    private var recordButton: some View {
        Button {
            if recorder.isRecording {
                if let memo = recorder.stop() {
                    model.setVoice(data: memo.data, duration: memo.duration)
                }
            } else {
                Task { await recorder.start() }
            }
        } label: {
            Label(
                recorder.isRecording
                    ? String(localized: "Stop")
                    : String(localized: "Voice"),
                systemImage: recorder.isRecording ? "stop.circle.fill" : "mic"
            )
        }
        .buttonStyle(.bordered)
        .tint(recorder.isRecording ? .red : nil)
        .accessibilityLabel(
            recorder.isRecording
                ? Text("Stop recording")
                : Text("Record voice memo")
        )
    }

    @ViewBuilder
    private var attachmentPreview: some View {
        switch model.attachment {
        case .photo(let data):
            HStack {
                if let image = UIImage(data: data) {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 56, height: 56)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .accessibilityLabel(Text("Attached photo"))
                }
                Text("Photo attached (\(data.count / 1024) KB)")
                    .font(.footnote)
                Spacer()
                removeButton
            }
        case .voice(let data, let duration):
            HStack {
                Image(systemName: "waveform.circle.fill")
                    .font(.title2)
                    .foregroundStyle(Color.accentColor)
                Text("Voice memo, \(Int(duration))s (\(data.count / 1024) KB)")
                    .font(.footnote)
                Spacer()
                removeButton
            }
            .accessibilityElement(children: .combine)
        case nil:
            EmptyView()
        }
    }

    private var removeButton: some View {
        Button(role: .destructive) {
            model.removeAttachment()
        } label: {
            Image(systemName: "trash")
        }
        .buttonStyle(.borderless)
        .accessibilityLabel(Text("Remove attachment"))
    }

    private var descriptionSection: some View {
        Section {
            TextField(
                text: $model.descriptionText,
                axis: .vertical
            ) {
                Text("Describe it — you can edit before saving")
            }
            .lineLimit(3...8)
            .accessibilityLabel(Text("Description"))

            if case .voice = model.attachment, model.target != .food {
                TextField(
                    text: $model.transcript,
                    axis: .vertical
                ) {
                    Text("Transcript (optional)")
                }
                .lineLimit(2...6)
                .accessibilityLabel(Text("Voice transcript"))
            }
        } header: {
            Text("Description")
        }
    }

    private var submitSection: some View {
        Section {
            Button {
                Task { await model.submit() }
            } label: {
                HStack {
                    Spacer()
                    switch model.phase {
                    case .uploading:
                        ProgressView()
                        Text("Uploading media…")
                    case .saving:
                        ProgressView()
                        Text("Saving…")
                    default:
                        Text("Save to my instance")
                    }
                    Spacer()
                }
            }
            .disabled(!model.canSubmit)

            switch model.phase {
            case .saved(let kind):
                Label {
                    Text(savedMessage(kind))
                } icon: {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
                .font(.footnote)
            case .failed(let message):
                VStack(alignment: .leading, spacing: 6) {
                    Label {
                        Text(verbatim: message)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    Text("Nothing was lost — your text and attachment are still here.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Button {
                        Task { await model.submit() }
                    } label: {
                        Text("Retry")
                    }
                    .buttonStyle(.bordered)
                }
            default:
                EmptyView()
            }
        }
    }

    private func savedMessage(_ kind: CaptureTarget) -> LocalizedStringKey {
        switch kind {
        case .food: return "Food log saved."
        case .medication: return "Medication record saved."
        case .symptom: return "Symptom record saved."
        }
    }
}
