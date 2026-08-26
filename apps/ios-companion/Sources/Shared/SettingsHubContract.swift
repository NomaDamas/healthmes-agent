import Foundation

/// One server-backed read model for the iPhone and macOS settings surfaces.
/// Secrets never cross this contract; provider credentials remain on HealthMes.
public struct SettingsHubSnapshot: Codable, Equatable {
    public let schema: String
    public let generatedAt: Date
    public let readiness: SetupReadiness
    public let inputs: [InputSourceDescriptor]
    public let wearables: WearablesManagementSnapshot?
    public let wearablesState: SettingsHubWearablesState
    public let wearablesError: String?

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case readiness
        case inputs
        case wearables
        case wearablesState = "wearables_state"
        case wearablesError = "wearables_error"
    }

    public func source(_ sourceID: String) -> InputSourceDescriptor? {
        inputs.first { $0.sourceID == sourceID }
    }

    public func wearableControl(
        for provider: String
    ) -> WearableProviderActionPolicyDescriptor? {
        wearables?.controls?.first { $0.provider == provider }
    }
}

public enum SettingsHubWearablesState: String, Codable, Equatable {
    case ready
    case notConfigured = "not_configured"
    case degraded
}
