import Foundation

struct MacSetupEvent: Decodable, Equatable, Identifiable {
    let schema: String
    let action: String
    let step: String
    let state: String
    let message: String
    let detail: String?
    let expiresAt: Int?

    enum CodingKeys: String, CodingKey {
        case schema
        case action
        case step
        case state
        case message
        case detail
        case expiresAt = "expires_at"
    }

    var id: String {
        "\(action):\(step):\(state)"
    }

    var isFailure: Bool {
        state == "failed" || state == "action_required"
    }
}

enum MacSetupSupport {
    static let officialRepositoryURL = URL(
        string: "https://github.com/NomaDamas/healthmes-agent.git"
    )!

    static func decodeEvents(_ output: String) -> [MacSetupEvent] {
        output.split(whereSeparator: \.isNewline).compactMap { line in
            try? JSONDecoder().decode(
                MacSetupEvent.self,
                from: Data(line.utf8)
            )
        }
    }

    static func repositoryRoot(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        currentDirectory: URL = URL(
            fileURLWithPath: FileManager.default.currentDirectoryPath
        ),
        applicationSupportDirectory: URL? = nil
    ) -> URL? {
        let candidates = [
            environment["HEALTHMES_REPO_ROOT"].map {
                URL(fileURLWithPath: $0, isDirectory: true)
            },
            Optional(currentDirectory),
            Optional(URL(fileURLWithPath: #filePath)),
            Optional(
                managedRepositoryRoot(
                    applicationSupportDirectory: applicationSupportDirectory
                )
            )
        ].compactMap { $0 }
        for candidate in candidates {
            var cursor = candidate
            for _ in 0..<10 {
                if FileManager.default.fileExists(
                    atPath: cursor
                        .appendingPathComponent("scripts/healthmes_setup.py")
                        .path
                ) {
                    return cursor
                }
                cursor.deleteLastPathComponent()
            }
        }
        return nil
    }

    static func managedRepositoryRoot(
        applicationSupportDirectory: URL? = nil
    ) -> URL {
        let base = applicationSupportDirectory
            ?? FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support")
        return base
            .appendingPathComponent("HealthMes", isDirectory: true)
            .appendingPathComponent("runtime-source", isDirectory: true)
    }

    static func isPairingGrantExpired(
        expiresAt: Date?,
        now: Date = Date()
    ) -> Bool {
        guard let expiresAt else { return false }
        return now > expiresAt
    }
}
