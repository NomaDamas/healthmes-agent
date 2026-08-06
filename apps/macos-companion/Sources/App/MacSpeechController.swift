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

    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
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
        guard let recognizer = SFSpeechRecognizer(locale: .autoupdatingCurrent),
            recognizer.isAvailable
        else {
            phase = .failed(String(localized: "Speech recognition is unavailable."))
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            phase = .failed(
                String(localized: "On-device voice recognition is unavailable on this Mac.")
            )
            return
        }

        cleanupRecognition()
        transcript = ""
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        recognitionRequest = request

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            phase = .failed(String(localized: "No microphone input is available."))
            return
        }
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
            request.append(buffer)
        }
        hasInputTap = true

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let result {
                    transcript = result.bestTranscription.formattedString
                    if result.isFinal {
                        finishCapture()
                        phase = transcript.isEmpty ? .idle : .ready
                    }
                }
                if error != nil, phase == .listening {
                    finishCapture()
                    phase = transcript.isEmpty
                        ? .failed(String(localized: "Could not understand that. Try again."))
                        : .ready
                }
            }
        }

        do {
            audioEngine.prepare()
            try audioEngine.start()
            phase = .listening
        } catch {
            cleanupRecognition()
            phase = .failed(String(localized: "Could not start the microphone."))
        }
    }

    func stop() {
        guard isListening else { return }
        finishCapture()
        phase = transcript.isEmpty ? .idle : .ready
    }

    func reset() {
        cleanupRecognition()
        transcript = ""
        phase = .idle
    }

    private func hasPermissions() async -> Bool {
        let microphone = await AVCaptureDevice.requestAccess(for: .audio)
        guard microphone else { return false }
        return await speechAuthorization()
    }

    private func speechAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    private func finishCapture() {
        recognitionRequest?.endAudio()
        audioEngine.stop()
        if hasInputTap {
            audioEngine.inputNode.removeTap(onBus: 0)
            hasInputTap = false
        }
    }

    private func cleanupRecognition() {
        finishCapture()
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
    }
}
