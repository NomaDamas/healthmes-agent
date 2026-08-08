import Foundation

@MainActor
final class MacSetupCoordinator: ObservableObject {
    @Published private(set) var events: [MacSetupEvent] = []
    @Published private(set) var isRunning = false
    @Published private(set) var failure: String?
    @Published private(set) var phonePairingURL: URL?
    @Published private(set) var phonePairingExpiresAt: Date?

    func install(glanceStore: GlanceStore) async {
        await run(.install, glanceStore: glanceStore)
    }

    func runAdvanced(_ action: Action) async {
        await run(action, glanceStore: nil)
    }

    func refreshPhonePairing() async {
        await run(.pair, glanceStore: nil)
    }

    func verifyPairing() async {
        guard !isRunning else { return }
        isRunning = true
        failure = nil
        defer { isRunning = false }
        do {
            let readiness = try await HealthMesAPI().setupReadiness()
            events = [
                MacSetupEvent(
                    schema: "healthmes.setup.v1",
                    action: "verify",
                    step: "readiness",
                    state: readiness.overall == .ready
                        ? "ready" : "action_required",
                    message: readiness.overall == .ready
                        ? "All connected services are ready."
                        : "Setup is connected; some services still need attention.",
                    detail: nil,
                    expiresAt: nil
                )
            ]
        } catch {
            failure = error.localizedDescription
        }
    }

    enum Action: String {
        case install
        case pair
        case repair
        case update
        case diagnostics
        case uninstall
    }

    private func run(_ action: Action, glanceStore: GlanceStore?) async {
        guard !isRunning else { return }
        isRunning = true
        failure = nil
        events = []
        if action == .install || action == .pair {
            phonePairingURL = nil
            phonePairingExpiresAt = nil
        }
        defer { isRunning = false }

        do {
            let root = try await Self.ensureRepository()
            let script = root.appendingPathComponent("scripts/healthmes_setup.py")
            let result = try await Self.execute(
                executable: try Self.pythonExecutable(),
                arguments: [
                    script.path,
                    action.rawValue,
                    "--json",
                ],
                currentDirectory: root
            )
            events = MacSetupSupport.decodeEvents(result.output)
            if result.status != 0 {
                failure = events.last(where: \.isFailure)?.detail
                    ?? events.last(where: \.isFailure)?.message
                    ?? "Setup stopped before completion."
                return
            }
            if action == .install {
                try await completePairing(glanceStore: glanceStore)
            } else if action == .pair {
                updatePhonePairing()
            }
        } catch {
            failure = error.localizedDescription
        }
    }

    private static func ensureRepository() async throws -> URL {
        if let existing = MacSetupSupport.repositoryRoot() {
            return existing
        }
        let destination = MacSetupSupport.managedRepositoryRoot()
        let fileManager = FileManager.default
        let parent = destination.deletingLastPathComponent()
        try fileManager.createDirectory(
            at: parent,
            withIntermediateDirectories: true
        )
        guard !fileManager.fileExists(atPath: destination.path) else {
            throw SetupError.invalidManagedCheckout(destination)
        }
        let temporary = parent.appendingPathComponent(
            ".runtime-source-\(UUID().uuidString)",
            isDirectory: true
        )
        defer { try? fileManager.removeItem(at: temporary) }
        let result = try await execute(
            executable: URL(fileURLWithPath: "/usr/bin/git"),
            arguments: [
                "clone",
                "--depth",
                "1",
                "--branch",
                "main",
                "--single-branch",
                MacSetupSupport.officialRepositoryURL.absoluteString,
                temporary.path,
            ],
            currentDirectory: parent
        )
        guard result.status == 0 else {
            throw SetupError.cloneFailed(
                result.output.split(whereSeparator: \.isNewline).last.map(String.init)
            )
        }
        guard MacSetupSupport.repositoryRoot(currentDirectory: temporary) != nil else {
            throw SetupError.invalidManagedCheckout(temporary)
        }
        try fileManager.moveItem(at: temporary, to: destination)
        return destination
    }

    private static func pythonExecutable(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) throws -> URL {
        let pathCandidates = [
            environment["HEALTHMES_PYTHON"],
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ].compactMap { $0 }
        if let path = pathCandidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) {
            return URL(fileURLWithPath: path)
        }
        throw SetupError.pythonMissing
    }

    private func completePairing(glanceStore: GlanceStore?) async throws {
        guard
            let macLink = events.first(where: { $0.step == "pair_mac" })?.detail,
            let macURL = URL(string: macLink)
        else {
            throw SetupError.missingPairingGrant
        }
        let pairing = try await PairingExchangeClient().exchange(macURL)
        if let glanceStore {
            try await glanceStore.pair(
                baseURLString: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
        } else {
            _ = try PairingStore.shared.save(
                baseURLString: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
        }
        updatePhonePairing()
    }

    private func updatePhonePairing() {
        let event = events.first(where: { $0.step == "pair_phone" })
        phonePairingURL = event?.detail.flatMap(URL.init(string:))
        phonePairingExpiresAt = event?.expiresAt.map {
            Date(timeIntervalSince1970: TimeInterval($0))
        }
    }

    private static func execute(
        executable: URL,
        arguments: [String],
        currentDirectory: URL
    ) async throws -> (status: Int32, output: String) {
        try await Task.detached(priority: .userInitiated) {
            let process = Process()
            let output = Pipe()
            process.executableURL = executable
            process.arguments = arguments
            process.currentDirectoryURL = currentDirectory
            process.standardOutput = output
            process.standardError = output
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (
                process.terminationStatus,
                String(decoding: data, as: UTF8.self)
            )
        }.value
    }
}

private enum SetupError: LocalizedError {
    case missingPairingGrant
    case cloneFailed(String?)
    case invalidManagedCheckout(URL)
    case pythonMissing

    var errorDescription: String? {
        switch self {
        case .missingPairingGrant:
            return "Setup completed, but the one-time pairing grant was missing."
        case .cloneFailed(let detail):
            return detail.map { "Could not download HealthMes: \($0)" }
                ?? "Could not download HealthMes."
        case .invalidManagedCheckout(let url):
            return "The managed HealthMes runtime is incomplete at \(url.path)."
        case .pythonMissing:
            return "Python 3 is required. Install it with Homebrew, then try again."
        }
    }
}
