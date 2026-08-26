import Foundation

public enum SetupReadinessState: String, Codable, Equatable {
    case ready
    case actionRequired = "action_required"
    case blocked
}

public struct SetupReadinessCheck: Codable, Equatable, Identifiable {
    public let key: String
    public let label: String
    public let state: SetupReadinessState
    public let detail: String

    public var id: String { key }
}

public struct SetupReadiness: Codable, Equatable {
    public let overall: SetupReadinessState
    public let checks: [SetupReadinessCheck]

    public func check(_ key: String) -> SetupReadinessCheck? {
        checks.first { $0.key == key }
    }
}
