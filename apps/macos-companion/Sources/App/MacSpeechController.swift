import AVFoundation
import Speech

@MainActor
final class MacSpeechController: ObservableObject {
    enum Phase: Equatable {
        case idle
        case requestingPermission
        case listening
        case ready
        case denied
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var transcript = ""
    @Published private(set) var capturedAudio: Data?
    @Published private(set) var capturedAudioDuration: TimeInterval = 0

    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var recordingFile: AVAudioFile?
    private var recordingURL: URL?
    private var hasInputTap = false

    var isListening: Bool {
        phase == .listening
    }

    func toggle() async {
        if isListening {
            stop()
        } else {
            await start()
        }
    }

    func start() async {
        guard !isListening else { return }
        phase = .requestingPermission
        guard await hasPermissions() else {
            phase = .denied
            return
        }
        let speechAuthorized = await speechAuthorization()
        let recognizer = SFSpeechRecognizer(locale: .autoupdatingCurrent)
        let canTranscribeLocally = speechAuthorized
            && recognizer?.isAvailable == true
            && recognizer?.supportsOnDeviceRecognition == true

        cleanupRecognition(discardAudio: true)
        transcript = ""
        let request: SFSpeechAudioBufferRecognitionRequest? = if canTranscribeLocally {
            SFSpeechAudioBufferRecognitionRequest()
        } else {
            nil
        }
        request?.shouldReportPartialResults = true
        request?.requiresOnDeviceRecognition = true
        recognitionRequest = request

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            phase = .failed(String(localized: "No microphone input is available."))
            return
        }
        let audioURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-mac-voice-\(UUID().uuidString).wav")
        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(
                forWriting: audioURL,
                settings: format.settings
            )
        } catch {
            phase = .failed(String(localized: "Could not prepare the voice recording."))
            return
        }
        recordingFile = audioFile
        recordingURL = audioURL
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
            request?.append(buffer)
            try? audioFile.write(from: buffer)
        }
        hasInputTap = true

        if let recognizer, let request {
            recognitionTask = recognizer.recognitionTask(with: request) {
                [weak self] result, error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if let result {
                        transcript = result.bestTranscription.formattedString
                        if result.isFinal {
                            finishCapture(preserveAudio: true)
                            phase = transcript.isEmpty && capturedAudio == nil
                                ? .idle
                                : .ready
                        }
                    }
                    if error != nil, phase == .listening {
                        finishCapture(preserveAudio: true)
                        phase = capturedAudio == nil
                            ? .failed(
                                String(localized: "Could not record that. Try again.")
                            )
                            : .ready
                    }
                }
            }
        }

        do {
            audioEngine.prepare()
            try audioEngine.start()
            phase = .listening
        } catch {
            cleanupRecognition(discardAudio: true)
            phase = .failed(String(localized: "Could not start the microphone."))
        }
    }

    func stop() {
        guard isListening else { return }
        finishCapture(preserveAudio: true)
        phase = transcript.isEmpty && capturedAudio == nil ? .idle : .ready
    }

    func reset() {
        cleanupRecognition(discardAudio: true)
        transcript = ""
        phase = .idle
    }

    private func hasPermissions() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }

    private func speechAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    private func finishCapture(preserveAudio: Bool) {
        recognitionRequest?.endAudio()
        audioEngine.stop()
        if hasInputTap {
            audioEngine.inputNode.removeTap(onBus: 0)
            hasInputTap = false
        }

        let duration: TimeInterval
        if let recordingFile, recordingFile.processingFormat.sampleRate > 0 {
            duration = Double(recordingFile.length)
                / recordingFile.processingFormat.sampleRate
        } else {
            duration = 0
        }
        let url = recordingURL
        recordingFile = nil
        recordingURL = nil

        if preserveAudio, let url,
            let data = try? Data(contentsOf: url), !data.isEmpty
        {
            capturedAudio = data
            capturedAudioDuration = duration
        } else if !preserveAudio {
            capturedAudio = nil
            capturedAudioDuration = 0
        }
        if let url {
            try? FileManager.default.removeItem(at: url)
        }
    }

    private func cleanupRecognition(discardAudio: Bool) {
        finishCapture(preserveAudio: !discardAudio)
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
    }
}
