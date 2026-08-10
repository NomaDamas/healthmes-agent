import PhotosUI
import SwiftUI

/// Capture shortcuts: food photos, voice and text are structured by the
/// nutrition engine; medical captures keep their separate record contract.
struct CaptureView: View {
    let startWithCamera: Bool

    @StateObject private var model = CaptureModel()
    @StateObject private var recorder = VoiceMemoRecorder()
    @State private var photoItem: PhotosPickerItem?
    @State private var showCamera = false
    @State private var didOfferInitialCamera = false

    init(startWithCamera: Bool = false) {
        self.startWithCamera = startWithCamera
    }

    var body: some View {
        Form {
            targetSection
                .disabled(model.isFoodReviewLocked)
            descriptionSection
                .disabled(model.isFoodReviewLocked)
            attachmentSection
                .disabled(model.isFoodReviewLocked)
            if model.reviewInteraction != nil {
                reviewSection
            }
            submitSection
        }
        .accessibilityIdentifier("healthmes-capture-form")
        .navigationTitle(Text("Capture"))
        .task {
            guard
                startWithCamera,
                !didOfferInitialCamera,
                CameraPicker.isAvailable
            else { return }
            didOfferInitialCamera = true
            showCamera = true
        }
        .sheet(isPresented: $showCamera) {
            ZStack(alignment: .topTrailing) {
                CameraPicker(
                    onImage: { image in
                        model.setPhoto(image)
                    },
                    onDismiss: {
                        showCamera = false
                    }
                )
                .ignoresSafeArea()

                Button {
                    showCamera = false
                } label: {
                    Image(systemName: "xmark")
                        .font(.headline)
                        .padding(12)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .padding(12)
                .accessibilityLabel(Text("Close camera"))
                .accessibilityIdentifier("DismissImagePickerButton")
            }
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
                Toggle(
                    "Allow configured remote nutrition model",
                    isOn: $model.allowRemoteAnalysis
                )
                .font(.footnote)
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
            if model.target == .food, case .voice = model.attachment {
                Label(
                    "Your instance transcribes the attached voice memo. Review the structured result before recording an outcome.",
                    systemImage: "waveform"
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            } else {
                TextField(
                    text: $model.descriptionText,
                    axis: .vertical
                ) {
                    Text(
                        model.target == .food && model.attachment != nil
                            ? "Optional context, e.g. half portion"
                            : "What did you consume?"
                    )
                }
                .lineLimit(3...8)
                .accessibilityLabel(Text("Description"))
                .accessibilityIdentifier("healthmes-capture-description")
            }

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

    private var reviewSection: some View {
        Section {
            if let observation = model.reviewObservation {
                HStack(alignment: .firstTextBaseline) {
                    Label("Photo analysis", systemImage: "viewfinder")
                        .font(.callout.weight(.semibold))
                    Spacer()
                    Text("\(observation.status) · \(observation.confidence)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !observation.warnings.isEmpty {
                    warningList(observation.warnings)
                }
            }

            if let interaction = model.reviewInteraction {
                ForEach(model.foodCorrections.indices, id: \.self) { index in
                    correctionEditor(index)
                }

                if interaction.resolvedItems.isEmpty {
                    Label(
                        "No specific item was identified. Choose an outcome only if this capture still represents what happened.",
                        systemImage: "questionmark.circle"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                if !interaction.warnings.isEmpty {
                    warningList(interaction.warnings)
                }
            }

            VStack(spacing: 10) {
                Button {
                    Task { await model.recordFoodOutcome(.consumed) }
                } label: {
                    outcomeLabel(
                        status: .consumed,
                        title: "Consumed",
                        systemImage: "checkmark.circle.fill"
                    )
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .disabled(model.hasInvalidFoodCorrections)

                Button {
                    Task { await model.recordFoodOutcome(.notConsumed) }
                } label: {
                    outcomeLabel(
                        status: .notConsumed,
                        title: "Not consumed",
                        systemImage: "minus.circle"
                    )
                }
                .buttonStyle(.bordered)

                Button(role: .cancel) {
                    Task { await model.recordFoodOutcome(.cancelled) }
                } label: {
                    outcomeLabel(
                        status: .cancelled,
                        title: "Cancel",
                        systemImage: "xmark.circle"
                    )
                }
                .buttonStyle(.borderless)
            }
            .disabled(isResolvingOutcome)

            if let error = model.outcomeError {
                Label {
                    Text(verbatim: error)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                }
                .font(.footnote)
                .foregroundStyle(.red)

                Text("The review is still here. Retry the same action without re-analyzing.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Review analyzed intake")
        } footer: {
            Text(
                "Analysis alone is not a food record. HealthMes stores consumed, not consumed, or cancelled only after you choose here."
            )
        }
    }

    private var isResolvingOutcome: Bool {
        if case .resolving = model.phase { return true }
        return false
    }

    @ViewBuilder
    private func outcomeLabel(
        status: IntakeOutcomeStatus,
        title: LocalizedStringKey,
        systemImage: String
    ) -> some View {
        HStack {
            Spacer()
            if case .resolving(let active) = model.phase, active == status {
                ProgressView()
            } else {
                Label(title, systemImage: systemImage)
            }
            Spacer()
        }
    }

    private func intakeItem(_ item: IntakeItemResult) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text(verbatim: item.name)
                    .font(.headline)
                Spacer()
                Text(item.confidence)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
            }
            Text("\(item.intakeType) · \(item.serving.summary)")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            if !item.nutrients.isEmpty {
                Text(
                    item.nutrients.prefix(4).map {
                        "\($0.nutrient) \($0.amount.summary)"
                    }.joined(separator: " · ")
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            if !item.warnings.isEmpty {
                warningList(item.warnings)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    private func correctionEditor(_ index: Int) -> some View {
        let correction = $model.foodCorrections[index]
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField("Food or drink", text: correction.name)
                    .font(.headline)
                    .disabled(correction.wrappedValue.isExcluded)
                Toggle("Exclude", isOn: correction.isExcluded)
                    .toggleStyle(.button)
                    .font(.caption)
            }
            HStack {
                TextField("Amount", text: correction.exactAmount)
                    .keyboardType(.decimalPad)
                    .textFieldStyle(.roundedBorder)
                TextField("Unit", text: correction.unit)
                    .textFieldStyle(.roundedBorder)
            }
            .disabled(correction.wrappedValue.isExcluded)
            if correction.wrappedValue.isChanged {
                Label(
                    "Your correction will replace this analyzed item.",
                    systemImage: "pencil.circle.fill"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                intakeItem(correction.wrappedValue.original)
            }
            if let message = correction.wrappedValue.validationMessage {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        .padding(.vertical, 4)
    }

    private func warningList(_ warnings: [String]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(warnings.enumerated()), id: \.offset) { _, warning in
                Label {
                    Text(verbatim: warning)
                } icon: {
                    Image(systemName: "exclamationmark.triangle")
                }
            }
        }
        .font(.caption)
        .foregroundStyle(.orange)
    }

    private var submitSection: some View {
        Section {
            if model.reviewInteraction == nil {
                Button {
                    Task { await model.submit() }
                } label: {
                    HStack {
                        Spacer()
                        switch model.phase {
                        case .uploading:
                            ProgressView()
                            Text("Uploading media…")
                        case .analyzing:
                            ProgressView()
                            Text("Analyzing…")
                        case .saving:
                            ProgressView()
                            Text("Saving…")
                        default:
                            Text(
                                model.target == .food
                                    ? "Analyze for review"
                                    : "Save to my instance"
                            )
                        }
                        Spacer()
                    }
                }
                .disabled(!model.canSubmit)
            }

            switch model.phase {
            case .saved(let kind, let outcome):
                VStack(alignment: .leading, spacing: 6) {
                    Label {
                        Text(savedMessage(kind, outcome: outcome))
                    } icon: {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }
                    if kind == .food, !model.savedIntakeItems.isEmpty {
                        Text(model.savedIntakeItems.joined(separator: " · "))
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
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

    private func savedMessage(
        _ kind: CaptureTarget,
        outcome: IntakeOutcomeStatus?
    ) -> LocalizedStringKey {
        switch kind {
        case .food:
            switch outcome {
            case .consumed: return "Recorded as consumed."
            case .notConsumed: return "Recorded as not consumed."
            case .cancelled: return "Capture cancelled."
            case nil: return "Food review completed."
            }
        case .medication: return "Medication record saved."
        case .symptom: return "Symptom record saved."
        }
    }
}
