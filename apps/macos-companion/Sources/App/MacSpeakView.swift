import AppKit
import SwiftUI

private enum MacNutritionPhase: Equatable {
    case idle
    case uploading
    case analyzing
    case reviewing
    case resolving(IntakeOutcomeStatus)
}

private struct MacNutritionDraftKey: Equatable {
    let text: String
    let modality: NutritionCaptureModality
    let mediaData: Data?
    let allowRemoteAnalysis: Bool
}

struct MacSpeakView: View {
    @ObservedObject var dashboardStore: MacDashboardStore
    let onNavigate: (MacAppSection) -> Void
    let onRefresh: () -> Void

    @EnvironmentObject private var router: MacAppRouter
    @StateObject private var speech = MacSpeechController()
    @State private var lastHandledRequest = 0
    @State private var commandText = ""
    @State private var mealPhoto: Data?
    @State private var intakeMessage: String?
    @State private var allowRemoteNutrition = false
    @State private var nutritionPhase: MacNutritionPhase = .idle
    @State private var nutritionDraft: NutritionCaptureDraft?
    @State private var nutritionDraftKey: MacNutritionDraftKey?
    @State private var nutritionObservation: NutritionObservationResult?
    @State private var nutritionInteraction: IntakeInteractionResult?
    @State private var nutritionCorrections: [NutritionItemCorrectionDraft] = []
    @State private var nutritionOutcomeError: String?
    @State private var typedIntent: MacVoiceIntent?

    var body: some View {
        VStack(spacing: 28) {
            MacPageHeader(
                eyebrow: "Speak",
                title: "Say what should change.",
                subtitle: "HealthMes listens locally when macOS supports it. Nothing mutates until you confirm."
            )
            .frame(maxWidth: 720, alignment: .leading)

            VStack(spacing: 22) {
                Button {
                    Task { await speech.toggle() }
                } label: {
                    ZStack {
                        Circle()
                            .fill(
                                speech.isListening
                                    ? MacHealthMesStyle.amber
                                    : MacHealthMesStyle.graphite
                            )
                            .frame(width: 116, height: 116)
                            .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
                        Image(systemName: speech.isListening ? "stop.fill" : "waveform")
                            .font(.system(size: 34, weight: .semibold))
                            .foregroundStyle(.white)
                            .symbolEffect(.variableColor.iterative, isActive: speech.isListening)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(Text(speech.isListening ? "Stop listening" : "Start speaking"))
                .keyboardShortcut(" ", modifiers: [.command, .shift])

                Text(verbatim: phaseTitle)
                    .font(.title3.weight(.semibold))
                Text("Keyboard: ⇧⌘Space")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if !speech.transcript.isEmpty {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("I heard")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .textCase(.uppercase)
                        Text(verbatim: speech.transcript)
                            .font(.system(size: 23, weight: .medium, design: .rounded))
                            .frame(maxWidth: .infinity, alignment: .leading)

                        if let intent = MacVoiceIntentParser.parse(speech.transcript) {
                            intentAction(intent)
                        } else {
                            nutritionAction(text: speech.transcript)
                        }

                        Button("Try again") {
                            dashboardStore.clearPlanSaveMessage()
                            speech.reset()
                        }
                        .buttonStyle(.link)
                        .disabled(isNutritionBusy || nutritionInteraction != nil)
                    }
                    .padding(22)
                    .frame(maxWidth: 680)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
                    .overlay {
                        RoundedRectangle(cornerRadius: 20)
                            .stroke(MacHealthMesStyle.line)
                    }
                } else if speech.capturedAudio != nil {
                    nutritionAction(text: "")
                } else {
                    examplePrompts
                }

                commandCanvas

                if let message = dashboardStore.planSaveMessage {
                    Label {
                        Text(verbatim: message)
                    } icon: {
                        Image(
                            systemName: dashboardStore.planSaveSucceeded
                                ? "checkmark.circle.fill"
                                : "exclamationmark.triangle.fill"
                        )
                    }
                    .font(.callout)
                    .foregroundStyle(
                        dashboardStore.planSaveSucceeded ? MacHealthMesStyle.moss : .red
                    )
                }
            }
            .frame(maxWidth: .infinity)

            Spacer(minLength: 0)
        }
        .padding(32)
        .onReceive(router.$speakRequest) { request in
            guard request > lastHandledRequest else { return }
            lastHandledRequest = request
            Task { await speech.toggle() }
        }
    }

    private var commandCanvas: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Command canvas")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)

            if nutritionInteraction != nil {
                nutritionReview
            } else {
                if let typedIntent {
                    intentAction(typedIntent)
                }
                TextField(
                    "Type a command or describe what you consumed",
                    text: $commandText
                )
                .textFieldStyle(.roundedBorder)
                .disabled(isNutritionBusy)
                .onSubmit {
                    submitTypedInput()
                }

                HStack {
                    Button {
                        chooseMealPhoto()
                    } label: {
                        Label(
                            mealPhoto == nil ? "Meal photo" : "Photo ready",
                            systemImage: mealPhoto == nil ? "photo" : "checkmark.circle"
                        )
                    }
                    .disabled(isNutritionBusy)
                    Toggle("Remote model", isOn: $allowRemoteNutrition)
                        .toggleStyle(.switch)
                        .disabled(isNutritionBusy)
                        .help(
                            "Allows the nutrition provider configured on your HealthMes instance."
                        )
                    Spacer()
                    Button("Analyze intake") {
                        submitTypedInput()
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.moss)
                    .disabled(
                        isNutritionBusy
                            || (mealPhoto == nil
                                && commandText.trimmingCharacters(
                                    in: .whitespacesAndNewlines
                                ).isEmpty)
                    )
                }

                if isNutritionBusy {
                    ProgressView(nutritionProgressTitle)
                        .controlSize(.small)
                }
                if let intakeMessage {
                    Text(verbatim: intakeMessage)
                        .font(.callout)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: 680)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private func submitTypedInput() {
        let text = commandText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || mealPhoto != nil else { return }
        if mealPhoto == nil, let intent = MacVoiceIntentParser.parse(text) {
            typedIntent = intent
            return
        }
        typedIntent = nil
        Task { await analyzeIntake(text: text) }
    }

    private var phaseTitle: String {
        switch speech.phase {
        case .idle: return String(localized: "Press and speak")
        case .requestingPermission: return String(localized: "Checking microphone access…")
        case .listening: return String(localized: "Listening…")
        case .ready: return String(localized: "Review before acting")
        case .denied: return String(localized: "Microphone or speech access is off")
        case .failed(let message): return message
        }
    }

    private var examplePrompts: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Try")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            Text("“Show my plan”")
            Text("“What decisions are waiting?”")
            Text("“Task: prepare the live QA checklist”")
            Text("“Weekly goal: protect three focus blocks”")
        }
        .font(.callout)
        .foregroundStyle(.secondary)
        .padding(18)
        .frame(maxWidth: 420, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    private func intentAction(_ intent: MacVoiceIntent) -> some View {
        switch intent {
        case .showToday:
            confirmationButton("Open Today", systemImage: "sun.max") {
                onNavigate(.today)
            }
        case .showPlan:
            confirmationButton("Open Plan", systemImage: "calendar") {
                onNavigate(.plan)
            }
        case .showDecisions:
            confirmationButton("Open Decisions", systemImage: "checkmark.bubble") {
                onNavigate(.decisions)
            }
        case .showSettings:
            confirmationButton("Open Settings", systemImage: "gearshape") {
                onNavigate(.settings)
            }
        case .refresh:
            confirmationButton("Refresh HealthMes", systemImage: "arrow.clockwise") {
                onRefresh()
            }
        case .taskDraft(let title):
            saveButton("Add task to Plan", systemImage: "checklist") {
                await dashboardStore.createTask(title: title)
            }
        case .goalDraft(let title):
            saveButton("Add weekly goal", systemImage: "scope") {
                await dashboardStore.createGoal(title: title)
            }
        }
    }

    private func saveButton(
        _ title: LocalizedStringKey,
        systemImage: String,
        save: @escaping () async -> Bool
    ) -> some View {
        Button {
            Task {
                if await save() {
                    speech.reset()
                    onNavigate(.plan)
                }
            }
        } label: {
            if dashboardStore.isSavingPlanItem {
                ProgressView()
            } else {
                Label(title, systemImage: systemImage)
            }
        }
        .buttonStyle(.borderedProminent)
        .tint(MacHealthMesStyle.moss)
        .controlSize(.large)
        .disabled(dashboardStore.isSavingPlanItem)
    }

    private func confirmationButton(
        _ title: LocalizedStringKey,
        systemImage: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
        }
        .buttonStyle(.borderedProminent)
        .tint(MacHealthMesStyle.moss)
        .controlSize(.large)
    }

    private func nutritionAction(text: String) -> some View {
        Button {
            Task {
                await analyzeIntake(
                    text: text,
                    voiceData: speech.capturedAudio
                )
            }
        } label: {
            if isNutritionBusy {
                ProgressView()
            } else {
                Label("Analyze voice intake", systemImage: "fork.knife")
            }
        }
        .buttonStyle(.borderedProminent)
        .tint(MacHealthMesStyle.moss)
        .disabled(
            isNutritionBusy
                || speech.capturedAudio == nil
                || nutritionInteraction != nil
        )
        .help(
            speech.capturedAudio == nil
                ? "Finish the recording before analyzing nutrition."
                : "Uploads the recorded audio and asks your instance to transcribe it."
        )
    }

    private func chooseMealPhoto() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard
            let image = NSImage(contentsOf: url),
            let tiff = image.tiffRepresentation,
            let bitmap = NSBitmapImageRep(data: tiff),
            let jpeg = bitmap.representation(
                using: .jpeg,
                properties: [.compressionFactor: 0.85]
            )
        else {
            intakeMessage = "The selected image could not be prepared."
            return
        }
        resetNutritionDraft()
        mealPhoto = jpeg
        intakeMessage = "Photo ready for analysis."
    }

    private func analyzeIntake(
        text: String,
        voiceData: Data? = nil
    ) async {
        let normalized = text.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard voiceData != nil || mealPhoto != nil || !normalized.isEmpty else {
            return
        }

        let modality: NutritionCaptureModality
        let mediaData: Data?
        let mediaType: CaptureMediaType?
        if let voiceData {
            modality = .voice
            mediaData = voiceData
            mediaType = .wav
        } else if let mealPhoto {
            modality = .photo
            mediaData = mealPhoto
            mediaType = .jpeg
        } else {
            modality = .text
            mediaData = nil
            mediaType = nil
        }

        var draft = prepareNutritionDraft(
            text: normalized,
            modality: modality,
            mediaData: mediaData
        )
        let api = HealthMesAPI()
        intakeMessage = nil
        nutritionOutcomeError = nil

        if let mediaData, let mediaType, draft.uploadedMediaPath == nil {
            nutritionPhase = .uploading
            do {
                let upload = try await api.uploadMedia(
                    data: mediaData,
                    mediaType: mediaType
                )
                draft.uploadedMediaPath = upload.mediaPath
                nutritionDraft = draft
            } catch {
                nutritionPhase = .idle
                intakeMessage = describe(error)
                return
            }
        }

        nutritionPhase = .analyzing
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
                                allowRemoteVision: allowRemoteNutrition
                            )
                        )
                        nutritionDraft = draft
                    }
                    guard let observation = draft.observation else {
                        throw HealthMesAPIError.httpStatus(422)
                    }
                    draft.interaction = try await api.createPhotoIntake(
                        PhotoIntakeInteractionBody(
                            operationID: draft.interactionOperationID,
                            source: draft.source,
                            sourceText: normalized.isEmpty ? nil : normalized,
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
                            allowRemoteAnalysis: allowRemoteNutrition
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
                            sourceText: normalized,
                            mediaPath: nil,
                            allowRemoteAnalysis: allowRemoteNutrition
                        )
                    )
                }
                nutritionDraft = draft
            }

            nutritionObservation = draft.observation
            nutritionInteraction = draft.interaction
            nutritionCorrections = draft.interaction?.resolvedItems.map {
                NutritionItemCorrectionDraft(item: $0)
            } ?? []
            nutritionPhase = .reviewing
        } catch {
            nutritionDraft = draft
            nutritionPhase = .idle
            intakeMessage = describe(error)
        }
    }

    private func prepareNutritionDraft(
        text: String,
        modality: NutritionCaptureModality,
        mediaData: Data?
    ) -> NutritionCaptureDraft {
        let key = MacNutritionDraftKey(
            text: text,
            modality: modality,
            mediaData: mediaData,
            allowRemoteAnalysis: allowRemoteNutrition
        )
        if key != nutritionDraftKey || nutritionDraft == nil {
            let canReuseUpload = nutritionDraftKey.map {
                $0.modality == modality && $0.mediaData == mediaData
            } ?? false
            let reusableMediaPath = canReuseUpload
                ? nutritionDraft?.uploadedMediaPath
                : nil
            var draft = NutritionCaptureDraft(
                modality: modality,
                source: "mac-app-\(modality.rawValue)"
            )
            draft.uploadedMediaPath = reusableMediaPath
            nutritionDraft = draft
            nutritionDraftKey = key
            nutritionObservation = nil
            nutritionInteraction = nil
            nutritionOutcomeError = nil
        }
        return nutritionDraft!
    }

    private func resolveNutrition(_ status: IntakeOutcomeStatus) async {
        guard var draft = nutritionDraft,
            let interaction = draft.interaction
        else {
            return
        }
        if status == .consumed,
            nutritionCorrections.contains(where: { !$0.isValid })
        {
            nutritionOutcomeError =
                "Fix the highlighted intake correction before recording it."
            return
        }

        let correctedItems = nutritionCorrections.contains(where: \.isChanged)
            ? nutritionCorrections.compactMap(\.correctedItem)
            : nil
        let submittedItems = status == .consumed ? correctedItems : nil
        let pending = draft.outcome(
            for: status,
            correctedItems: submittedItems,
            note: nil
        )
        nutritionDraft = draft
        nutritionOutcomeError = nil
        nutritionPhase = .resolving(status)
        do {
            let result = try await HealthMesAPI().confirmIntake(
                interactionID: interaction.interactionID,
                body: IntakeOutcomeBody(
                    operationID: pending.operationID,
                    status: status,
                    source: "mac-app",
                    consumedAt: status == .consumed ? pending.actedAt : nil,
                    correctedItems: pending.correctedItems,
                    note: pending.note
                )
            )
            let names = result.resolvedItems.map(\.name).joined(separator: " · ")
            switch status {
            case .consumed:
                intakeMessage = names.isEmpty
                    ? "Recorded as consumed."
                    : "Recorded as consumed: \(names)"
            case .notConsumed:
                intakeMessage = "Recorded as not consumed."
            case .cancelled:
                intakeMessage = "Capture cancelled."
            }
            commandText = ""
            mealPhoto = nil
            speech.reset()
            resetNutritionDraft(keepMessage: true)
            onRefresh()
        } catch {
            nutritionDraft = draft
            nutritionPhase = .reviewing
            nutritionOutcomeError = describe(error)
        }
    }

    private var nutritionReview: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Review analyzed intake")
                    .font(.headline)
                Spacer()
                if let observation = nutritionObservation {
                    Text("\(observation.status) · \(observation.confidence)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if let interaction = nutritionInteraction {
                if interaction.resolvedItems.isEmpty {
                    Label(
                        "No specific item was identified. Choose an outcome only if this capture still represents what happened.",
                        systemImage: "questionmark.circle"
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                } else {
                    ForEach(nutritionCorrections.indices, id: \.self) { index in
                        macCorrectionEditor(index)
                    }
                }
                let warnings = (nutritionObservation?.warnings ?? [])
                    + interaction.warnings
                if !warnings.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(
                            Array(warnings.enumerated()),
                            id: \.offset
                        ) { _, warning in
                            Label(
                                warning,
                                systemImage: "exclamationmark.triangle"
                            )
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            }

            Divider()

            HStack {
                Button {
                    Task { await resolveNutrition(.consumed) }
                } label: {
                    macOutcomeLabel(
                        .consumed,
                        title: "Consumed",
                        systemImage: "checkmark.circle.fill"
                    )
                }
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .disabled(
                    nutritionCorrections.contains(where: { !$0.isValid })
                )

                Button {
                    Task { await resolveNutrition(.notConsumed) }
                } label: {
                    macOutcomeLabel(
                        .notConsumed,
                        title: "Not consumed",
                        systemImage: "minus.circle"
                    )
                }
                .buttonStyle(.bordered)

                Button(role: .cancel) {
                    Task { await resolveNutrition(.cancelled) }
                } label: {
                    macOutcomeLabel(
                        .cancelled,
                        title: "Cancel",
                        systemImage: "xmark.circle"
                    )
                }
                .buttonStyle(.borderless)
            }
            .disabled(isResolvingNutrition)

            if let nutritionOutcomeError {
                Label(
                    nutritionOutcomeError,
                    systemImage: "exclamationmark.triangle"
                )
                .font(.callout)
                .foregroundStyle(.red)
                Text("The review is still here. Retry the same action without re-analyzing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Text(
                "Analysis alone is not a food record. An outcome is stored only after you choose."
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private func macIntakeItem(_ item: IntakeItemResult) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(verbatim: item.name)
                    .font(.title3.weight(.semibold))
                Spacer()
                Text(item.confidence.uppercased())
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            Text("\(item.intakeType) · \(item.serving.summary)")
                .font(.callout)
                .foregroundStyle(.secondary)
            if !item.nutrients.isEmpty {
                Text(
                    item.nutrients.prefix(5).map {
                        "\($0.nutrient) \($0.amount.summary)"
                    }.joined(separator: " · ")
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            if !item.warnings.isEmpty {
                Text(item.warnings.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(12)
        .background(
            MacHealthMesStyle.canvas,
            in: RoundedRectangle(cornerRadius: 12)
        )
    }

    private func macCorrectionEditor(_ index: Int) -> some View {
        let correction = $nutritionCorrections[index]
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField("Food or drink", text: correction.name)
                    .textFieldStyle(.roundedBorder)
                    .disabled(correction.wrappedValue.isExcluded)
                Toggle("Exclude", isOn: correction.isExcluded)
                    .toggleStyle(.button)
            }
            HStack {
                TextField("Amount", text: correction.exactAmount)
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
                macIntakeItem(correction.wrappedValue.original)
            }
            if let message = correction.wrappedValue.validationMessage {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        .padding(12)
        .background(
            MacHealthMesStyle.canvas,
            in: RoundedRectangle(cornerRadius: 12)
        )
    }

    @ViewBuilder
    private func macOutcomeLabel(
        _ status: IntakeOutcomeStatus,
        title: LocalizedStringKey,
        systemImage: String
    ) -> some View {
        if case .resolving(let active) = nutritionPhase, active == status {
            ProgressView()
        } else {
            Label(title, systemImage: systemImage)
        }
    }

    private var isNutritionBusy: Bool {
        switch nutritionPhase {
        case .uploading, .analyzing, .resolving:
            return true
        case .idle, .reviewing:
            return false
        }
    }

    private var isResolvingNutrition: Bool {
        if case .resolving = nutritionPhase { return true }
        return false
    }

    private var nutritionProgressTitle: String {
        switch nutritionPhase {
        case .uploading: return "Uploading media…"
        case .analyzing: return "Analyzing intake…"
        case .resolving: return "Saving outcome…"
        case .idle, .reviewing: return ""
        }
    }

    private func resetNutritionDraft(keepMessage: Bool = false) {
        nutritionPhase = .idle
        nutritionDraft = nil
        nutritionDraftKey = nil
        nutritionObservation = nil
        nutritionInteraction = nil
        nutritionCorrections = []
        nutritionOutcomeError = nil
        if !keepMessage {
            intakeMessage = nil
        }
    }

    private func describe(_ error: Error) -> String {
        if case HealthMesAPIError.server(_, _, let message, _) = error {
            return message
        }
        if case HealthMesAPIError.unauthorized = error {
            return "The paired instance rejected this Mac."
        }
        if case HealthMesAPIError.notPaired = error {
            return "Connect HealthMes first."
        }
        return error.localizedDescription
    }
}
