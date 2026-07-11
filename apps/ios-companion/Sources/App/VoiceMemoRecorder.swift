import AVFoundation
import Foundation

/// AAC voice-memo recorder for the capture flow. Output is an .m4a file
/// (AAC in an MPEG-4 container) — uploaded as `audio/mp4`, which is in the
/// server's canonical allowlist (healthmes/api/media.py).
@MainActor
final class VoiceMemoRecorder: NSObject, ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var elapsed: TimeInterval = 0
    @Published var permissionDenied = false

    private var recorder: AVAudioRecorder?
    private var timer: Timer?
    private var fileURL: URL?

    /// Ask for microphone permission and start recording; false when either
    /// step fails.
    @discardableResult
    func start() async -> Bool {
        guard await AVAudioApplication.requestRecordPermission() else {
            permissionDenied = true
            return false
        }
        permissionDenied = false
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord, mode: .default)
            try session.setActive(true)
        } catch {
            return false
        }

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-memo-\(UUID().uuidString).m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 44_100,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
        ]
        do {
            let recorder = try AVAudioRecorder(url: url, settings: settings)
            guard recorder.record() else { return false }
            self.recorder = recorder
            fileURL = url
            elapsed = 0
            isRecording = true
            timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) {
                [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self, let recorder = self.recorder else { return }
                    self.elapsed = recorder.currentTime
                }
            }
            return true
        } catch {
            return false
        }
    }

    /// Stop and return the recorded bytes (nil when nothing was recorded).
    func stop() -> (data: Data, duration: TimeInterval)? {
        timer?.invalidate()
        timer = nil
        guard let recorder, let fileURL else {
            isRecording = false
            return nil
        }
        let duration = recorder.currentTime
        recorder.stop()
        self.recorder = nil
        isRecording = false
        try? AVAudioSession.sharedInstance().setActive(false, options: [])
        defer { try? FileManager.default.removeItem(at: fileURL) }
        guard let data = try? Data(contentsOf: fileURL), !data.isEmpty else { return nil }
        return (data, duration)
    }

    func cancel() {
        timer?.invalidate()
        timer = nil
        recorder?.stop()
        recorder = nil
        isRecording = false
        if let fileURL {
            try? FileManager.default.removeItem(at: fileURL)
        }
        try? AVAudioSession.sharedInstance().setActive(false, options: [])
    }
}
