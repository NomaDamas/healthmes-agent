import AVFoundation
import Speech
import SwiftUI

extension Notification.Name {
    static let healthmesPlanChanged = Notification.Name("healthmes.plan.changed")
}

enum VoiceCommandDestination: String, CaseIterable, Identifiable {
    case task
    case weeklyGoal

    var id: String { rawValue }

    var title: LocalizedStringKey {
        switch self {
        case .task: return "Task"
        case .weeklyGoal: return "Weekly goal"
        }
    }
}

@MainActor
final class VoiceCommandModel: NSObject, ObservableObject {
    @Published var destination: VoiceCommandDestination = .task
    @Published var transcript = ""
    @Published var isListening = false
    @Published var isSaving = false
    @Published var message: String?

    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let recognizer = SFSpeechRecognizer(locale: .autoupdatingCurrent)
    private let api = HealthMesAPI()

    var canSave: Bool {
        !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isListening
            && !isSaving
    }

    func toggleListening() async {
        if isListening {
            stopListening()
        } else {
            await startListening()
        }
    }

    func startListening() async {
        message = nil
        transcript = ""
        guard await speechPermission() else {
            message = String(localized: "Speech recognition permission is off.")
            return
        }
        guard await AVAudioApplication.requestRecordPermission() else {
            message = String(localized: "Microphone permission is off.")
            return
        }
        guard let recognizer, recognizer.isAvailable else {
            message = String(localized: "Voice recognition is unavailable right now.")
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            message = String(localized: "On-device voice recognition is unavailable on this device.")
            return
        }

        task?.cancel()
        task = nil
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        self.request = request

        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .measurement, options: [.duckOthers])
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            let input = audioEngine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1_024, format: format) {
                [weak request] buffer, _ in
                request?.append(buffer)
            }
            audioEngine.prepare()
            try audioEngine.start()
        } catch {
            message = String(localized: "Could not start listening.")
            stopListening()
            return
        }

        isListening = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let result {
                    transcript = result.bestTranscription.formattedString
                    if result.isFinal {
                        stopListening()
                    }
                }
                if error != nil {
                    stopListening()
                }
            }
        }
    }

    func stopListening() {
        guard isListening || audioEngine.isRunning else { return }
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        request = nil
        isListening = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func save() async {
        let title = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            switch destination {
            case .task:
                _ = try await api.createTask(TaskCreateBody(title: title))
                message = String(localized: "Task added to Plan.")
            case .weeklyGoal:
                _ = try await api.createGoal(
                    WeeklyGoalCreateBody(
                        weekStart: ProductDateFormat.weekStart(containing: Date()),
                        title: title
                    )
                )
                message = String(localized: "Weekly goal added to Plan.")
            }
            NotificationCenter.default.post(name: .healthmesPlanChanged, object: nil)
            transcript = ""
        } catch {
            message = BriefingHomeModel.describe(error)
        }
    }

    func reset() {
        stopListening()
        task?.cancel()
        task = nil
    }

    private func speechPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }
}

struct SpeakView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model = VoiceCommandModel()

    var body: some View {
        VStack(spacing: 22) {
            Picker(selection: $model.destination) {
                ForEach(VoiceCommandDestination.allCases) { destination in
                    Text(destination.title).tag(destination)
                }
            } label: {
                Text("Save voice as")
            }
            .pickerStyle(.segmented)

            Spacer()

            Button {
                Task { await model.toggleListening() }
            } label: {
                ZStack {
                    Circle()
                        .fill(
                            model.isListening
                                ? Color.red.gradient
                                : Color(red: 0.02, green: 0.34, blue: 0.25).gradient
                        )
                        .frame(width: 116, height: 116)
                        .shadow(color: .black.opacity(0.18), radius: 16, y: 7)
                    Image(systemName: model.isListening ? "stop.fill" : "waveform")
                        .font(.system(size: 38, weight: .semibold))
                        .foregroundStyle(.white)
                        .symbolEffect(.variableColor.iterative, isActive: model.isListening)
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Text(model.isListening ? "Stop listening" : "Tap to speak"))

            VStack(spacing: 8) {
                Text(model.isListening ? "Listening…" : "Speak one clear goal or task")
                    .font(.title3.weight(.semibold))
                if model.transcript.isEmpty {
                    Text("There is no text command box. Your voice becomes the Plan item.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                } else {
                    Text(verbatim: model.transcript)
                        .font(.body)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                        .accessibilityLabel(Text("Recognized voice"))
                }
            }

            Spacer()

            if let message = model.message {
                Text(verbatim: message)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            Button {
                Task { await model.save() }
            } label: {
                HStack {
                    if model.isSaving {
                        ProgressView()
                    }
                    Text(model.destination == .task ? "Add task" : "Add weekly goal")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.02, green: 0.34, blue: 0.25))
            .disabled(!model.canSave)
        }
        .padding(20)
        .navigationTitle(Text("Speak"))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Done") { dismiss() }
            }
        }
        .onDisappear { model.reset() }
    }
}
