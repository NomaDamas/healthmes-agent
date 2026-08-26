import Foundation

@MainActor
final class MacSetupCoordinator: ObservableObject {
    typealias ReadinessLoader = (Pairing) async throws -> SetupReadiness

    @Published private(set) var events: [MacSetupEvent] = []
    @Published private(set) var isRunning = false
    @Published private(set) var failure: String?
    @Published private(set) var phonePairingURL: URL?
    @Published private(set) var phonePairingExpiresAt: Date?
    @Published private(set) var requiresDeveloperTools = false
    @Published private(set) var requiresHomebrew = false

    private let pairingStore: PairingStore
    private let readinessLoader: ReadinessLoader
    private var operationGeneration: UInt = 0
    private var activeAction: Action?
    private var expectedInstallPairing: Pairing?

    init(
        pairingStore: PairingStore = .shared,
        readinessLoader: ReadinessLoader? = nil
    ) {
        self.pairingStore = pairingStore
        if let readinessLoader {
            self.readinessLoader = readinessLoader
        } else {
            let api = HealthMesAPI(pairingStore: pairingStore)
            self.readinessLoader = { pairing in
                try await api.setupReadiness(pairing: pairing)
            }
        }
    }

    func resetForPairingChange() {
        if activeAction == .install,
            let expectedInstallPairing,
            pairingStore.load() == expectedInstallPairing
        {
            self.expectedInstallPairing = nil
            return
        }
        operationGeneration &+= 1
        activeAction = nil
        expectedInstallPairing = nil
        events = []
        failure = nil
        phonePairingURL = nil
        phonePairingExpiresAt = nil
        requiresDeveloperTools = false
        requiresHomebrew = false
        isRunning = false
    }

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
        operationGeneration &+= 1
        let operation = operationGeneration
        activeAction = nil
        expectedInstallPairing = nil
        isRunning = true
        failure = nil
        defer {
            if operationGeneration == operation {
                isRunning = false
            }
        }
        guard
            let pairing = pairingStore.load(),
            let identity = pairingStore.cacheIdentity(for: pairing)
        else {
            events = []
            failure = "HealthMes is not paired."
            return
        }
        do {
            let readiness = try await readinessEvents(for: pairing)
            guard isCurrent(operation, pairing: pairing, identity: identity) else {
                return
            }
            events = readiness
        } catch {
            guard isCurrent(operation, pairing: pairing, identity: identity) else {
                return
            }
            failure = error.localizedDescription
        }
    }

    func requestDeveloperToolsInstallation() async {
        guard !isRunning else { return }
        operationGeneration &+= 1
        let operation = operationGeneration
        activeAction = nil
        expectedInstallPairing = nil
        isRunning = true
        failure = nil
        defer {
            if operationGeneration == operation {
                isRunning = false
            }
        }
        do {
            let result = try await Self.execute(
                executable: URL(fileURLWithPath: "/usr/bin/xcode-select"),
                arguments: ["--install"],
                currentDirectory: URL(fileURLWithPath: "/")
            )
            guard operationGeneration == operation else { return }
            if result.status == 0 {
                failure = "Complete Apple's installation, then select Set up this Mac again."
            } else {
                let detail = result.output
                    .split(whereSeparator: \.isNewline)
                    .last
                    .map(String.init)
                failure = detail
                    ?? "Apple Developer Tools could not be requested automatically."
            }
        } catch {
            guard operationGeneration == operation else { return }
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
        operationGeneration &+= 1
        let operation = operationGeneration
        activeAction = action
        expectedInstallPairing = nil
        isRunning = true
        failure = nil
        requiresDeveloperTools = false
        requiresHomebrew = false
        events = []
        if action == .install || action == .pair {
            phonePairingURL = nil
            phonePairingExpiresAt = nil
        }
        defer {
            if operationGeneration == operation {
                activeAction = nil
                expectedInstallPairing = nil
                isRunning = false
            }
        }

        do {
            let python = try Self.pythonExecutable()
            let root = try await Self.ensureRepository()
            let script = root.appendingPathComponent("scripts/healthmes_setup.py")
            let result = try await Self.execute(
                executable: python,
                arguments: [
                    script.path,
                    action.rawValue,
                    "--json",
                ],
                currentDirectory: root,
                environment: Self.managedRuntimeEnvironment()
            )
            guard operationGeneration == operation else { return }
            events = MacSetupSupport.decodeEvents(result.output)
            requiresDeveloperTools = events.contains {
                $0.step == "tool_python3" && $0.isFailure
            }
            requiresHomebrew = events.contains {
                $0.step == "tool_brew" && $0.isFailure
            }
            if result.status != 0 {
                failure = events.last(where: \.isFailure)?.detail
                    ?? events.last(where: \.isFailure)?.message
                    ?? "Setup stopped before completion."
                return
            }
            if action == .install {
                try await completePairing(glanceStore: glanceStore)
                guard operationGeneration == operation else { return }
                guard
                    let pairing = pairingStore.load(),
                    let identity = pairingStore.cacheIdentity(for: pairing)
                else {
                    throw SetupError.missingPairing
                }
                let readiness = try await readinessEvents(for: pairing)
                guard isCurrent(operation, pairing: pairing, identity: identity) else {
                    return
                }
                events.append(contentsOf: readiness)
            } else if action == .pair {
                updatePhonePairing()
            }
        } catch SetupError.pythonMissing {
            requiresDeveloperTools = true
            failure = SetupError.pythonMissing.localizedDescription
        } catch {
            failure = error.localizedDescription
        }
    }

    private static func ensureRepository() async throws -> URL {
        let destination = MacSetupSupport.managedRepositoryRoot()
        if let existing = MacSetupSupport.repositoryRoot() {
            if existing.standardizedFileURL == destination.standardizedFileURL {
                try await checkoutRuntimeRevision(in: existing)
            }
            return existing
        }
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
        let clone = try await execute(
            executable: URL(fileURLWithPath: "/usr/bin/git"),
            arguments: [
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                MacSetupSupport.officialRepositoryURL.absoluteString,
                temporary.path,
            ],
            currentDirectory: parent
        )
        guard clone.status == 0 else {
            throw SetupError.cloneFailed(
                clone.output.split(whereSeparator: \.isNewline).last.map(String.init)
            )
        }
        try await checkoutRuntimeRevision(in: temporary)
        guard MacSetupSupport.repositoryRoot(currentDirectory: temporary) != nil else {
            throw SetupError.invalidManagedCheckout(temporary)
        }
        try fileManager.moveItem(at: temporary, to: destination)
        return destination
    }

    private static func checkoutRuntimeRevision(in repository: URL) async throws {
        let revision = MacSetupSupport.runtimeRevision()
        let current = try await execute(
            executable: URL(fileURLWithPath: "/usr/bin/git"),
            arguments: ["rev-parse", "HEAD"],
            currentDirectory: repository
        )
        if current.status == 0,
            current.output.trimmingCharacters(in: .whitespacesAndNewlines) == revision
        {
            return
        }
        let fetch = try await execute(
            executable: URL(fileURLWithPath: "/usr/bin/git"),
            arguments: [
                "fetch",
                "--depth",
                "1",
                "origin",
                revision,
            ],
            currentDirectory: repository
        )
        guard fetch.status == 0 else {
            throw SetupError.revisionUnavailable(revision)
        }
        let checkout = try await execute(
            executable: URL(fileURLWithPath: "/usr/bin/git"),
            arguments: ["checkout", "--detach", "FETCH_HEAD"],
            currentDirectory: repository
        )
        guard checkout.status == 0 else {
            throw SetupError.revisionUnavailable(revision)
        }
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
            expectedInstallPairing = pairing
            try await glanceStore.pair(
                baseURLString: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
        } else {
            let previous = pairingStore.load()
            let saved = try await MacPairingTransitionCoordinator.replace(
                baseURLString: pairing.baseURL.absoluteString,
                token: pairing.token ?? "",
                store: pairingStore,
                cleanup: {
                    await MacNotificationManager.shared.clearAccountSurfaces()
                }
            )
            if previous != saved {
                GlanceSnapshotCache().clear()
                SeenAlertsStore.shared.resetForPairingChange()
            }
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

    private func readinessEvents(for pairing: Pairing) async throws -> [MacSetupEvent] {
        let readiness = try await readinessLoader(pairing)
        return MacSetupSupport.readinessEvents(readiness)
    }

    private func isCurrent(
        _ operation: UInt,
        pairing: Pairing,
        identity: PairingCacheIdentity
    ) -> Bool {
        operationGeneration == operation
            && pairingStore.load() == pairing
            && pairingStore.cacheIdentity(for: pairing) == identity
    }

    private static func execute(
        executable: URL,
        arguments: [String],
        currentDirectory: URL,
        environment: [String: String]? = nil
    ) async throws -> (status: Int32, output: String) {
        try await Task.detached(priority: .userInitiated) {
            let process = Process()
            let output = Pipe()
            process.executableURL = executable
            process.arguments = arguments
            process.currentDirectoryURL = currentDirectory
            process.environment = environment
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

    private static func managedRuntimeEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["HEALTHMES_MANAGED_RUNTIME"] = "1"
        return environment
    }
}

private enum SetupError: LocalizedError {
    case missingPairingGrant
    case missingPairing
    case cloneFailed(String?)
    case revisionUnavailable(String)
    case invalidManagedCheckout(URL)
    case pythonMissing

    var errorDescription: String? {
        switch self {
        case .missingPairingGrant:
            return "Setup completed, but the one-time pairing grant was missing."
        case .missingPairing:
            return "Setup completed, but the Mac pairing was not saved."
        case .cloneFailed(let detail):
            return detail.map { "Could not download HealthMes: \($0)" }
                ?? "Could not download HealthMes."
        case .revisionUnavailable(let revision):
            return "Could not download the compatible HealthMes runtime revision \(revision)."
        case .invalidManagedCheckout(let url):
            return "The managed HealthMes runtime is incomplete at \(url.path)."
        case .pythonMissing:
            return "Apple Developer Tools are required once before HealthMes can install its local runtime."
        }
    }
}
