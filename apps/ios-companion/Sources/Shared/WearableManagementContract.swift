import Foundation

/// Secret-free projection returned by HealthMes' wearable management adapter.
/// Provider credentials and Open Wearables user identities never cross this
/// boundary into the companion apps.
public struct WearableProviderDescriptor: Codable, Equatable, Identifiable {
    public let provider: String
    public let name: String
    public let hasCloudAPI: Bool
    public let isEnabled: Bool
    public let catalogAvailable: Bool
    public let managementSupported: Bool
    public let iconURL: String?
    public let liveSyncMode: String?
    public let liveSyncConfigurable: Bool
    public let clientSDK: Bool
    public let fileImport: Bool
    public let restPull: Bool
    public let webhookStream: Bool
    public let webhookPing: Bool
    public let webhookCallback: Bool
    public let webhookRegistrationAPI: Bool
    public let webhookInboundSecret: Bool
    public let maxHistoricalDays: Int?

    public var id: String { provider }

    public init(
        provider: String,
        name: String,
        hasCloudAPI: Bool,
        isEnabled: Bool,
        catalogAvailable: Bool = true,
        managementSupported: Bool = false,
        iconURL: String? = nil,
        liveSyncMode: String? = nil,
        liveSyncConfigurable: Bool,
        clientSDK: Bool = false,
        fileImport: Bool = false,
        restPull: Bool = false,
        webhookStream: Bool = false,
        webhookPing: Bool = false,
        webhookCallback: Bool = false,
        webhookRegistrationAPI: Bool = false,
        webhookInboundSecret: Bool = false,
        maxHistoricalDays: Int? = nil
    ) {
        self.provider = provider
        self.name = name
        self.hasCloudAPI = hasCloudAPI
        self.isEnabled = isEnabled
        self.catalogAvailable = catalogAvailable
        self.managementSupported = managementSupported
        self.iconURL = iconURL
        self.liveSyncMode = liveSyncMode
        self.liveSyncConfigurable = liveSyncConfigurable
        self.clientSDK = clientSDK
        self.fileImport = fileImport
        self.restPull = restPull
        self.webhookStream = webhookStream
        self.webhookPing = webhookPing
        self.webhookCallback = webhookCallback
        self.webhookRegistrationAPI = webhookRegistrationAPI
        self.webhookInboundSecret = webhookInboundSecret
        self.maxHistoricalDays = maxHistoricalDays
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            provider: try container.decode(String.self, forKey: .provider),
            name: try container.decode(String.self, forKey: .name),
            hasCloudAPI: try container.decode(Bool.self, forKey: .hasCloudAPI),
            isEnabled: try container.decode(Bool.self, forKey: .isEnabled),
            catalogAvailable: try container.decodeIfPresent(
                Bool.self,
                forKey: .catalogAvailable
            ) ?? true,
            managementSupported: try container.decodeIfPresent(
                Bool.self,
                forKey: .managementSupported
            ) ?? false,
            iconURL: try container.decodeIfPresent(String.self, forKey: .iconURL),
            liveSyncMode: try container.decodeIfPresent(String.self, forKey: .liveSyncMode),
            liveSyncConfigurable: try container.decode(Bool.self, forKey: .liveSyncConfigurable),
            clientSDK: try container.decodeIfPresent(Bool.self, forKey: .clientSDK) ?? false,
            fileImport: try container.decodeIfPresent(Bool.self, forKey: .fileImport) ?? false,
            restPull: try container.decodeIfPresent(Bool.self, forKey: .restPull) ?? false,
            webhookStream: try container.decodeIfPresent(Bool.self, forKey: .webhookStream) ?? false,
            webhookPing: try container.decodeIfPresent(Bool.self, forKey: .webhookPing) ?? false,
            webhookCallback: try container.decodeIfPresent(Bool.self, forKey: .webhookCallback) ?? false,
            webhookRegistrationAPI: try container.decodeIfPresent(Bool.self, forKey: .webhookRegistrationAPI) ?? false,
            webhookInboundSecret: try container.decodeIfPresent(Bool.self, forKey: .webhookInboundSecret) ?? false,
            maxHistoricalDays: try container.decodeIfPresent(Int.self, forKey: .maxHistoricalDays)
        )
    }

    enum CodingKeys: String, CodingKey {
        case provider
        case name
        case hasCloudAPI = "has_cloud_api"
        case isEnabled = "is_enabled"
        case catalogAvailable = "catalog_available"
        case managementSupported = "management_supported"
        case iconURL = "icon_url"
        case liveSyncMode = "live_sync_mode"
        case liveSyncConfigurable = "live_sync_configurable"
        case clientSDK = "client_sdk"
        case fileImport = "file_import"
        case restPull = "rest_pull"
        case webhookStream = "webhook_stream"
        case webhookPing = "webhook_ping"
        case webhookCallback = "webhook_callback"
        case webhookRegistrationAPI = "webhook_registration_api"
        case webhookInboundSecret = "webhook_inbound_secret"
        case maxHistoricalDays = "max_historical_days"
    }
}

public struct WearableConnectionDescriptor: Codable, Equatable, Identifiable {
    public let provider: String
    public let status: String
    public let lastSyncedAt: Date?
    public let createdAt: Date?
    public let updatedAt: Date?
    public let maxHistoricalDays: Int?
    public let restPull: Bool
    public let webhookStream: Bool
    public let webhookPing: Bool
    public let webhookCallback: Bool
    public let webhookRegistrationAPI: Bool
    public let webhookInboundSecret: Bool
    public let liveSyncMode: String?

    public var id: String { provider }

    public init(
        provider: String,
        status: String,
        lastSyncedAt: Date? = nil,
        createdAt: Date? = nil,
        updatedAt: Date? = nil,
        maxHistoricalDays: Int? = nil,
        restPull: Bool = false,
        webhookStream: Bool = false,
        webhookPing: Bool = false,
        webhookCallback: Bool = false,
        webhookRegistrationAPI: Bool = false,
        webhookInboundSecret: Bool = false,
        liveSyncMode: String? = nil
    ) {
        self.provider = provider
        self.status = status
        self.lastSyncedAt = lastSyncedAt
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.maxHistoricalDays = maxHistoricalDays
        self.restPull = restPull
        self.webhookStream = webhookStream
        self.webhookPing = webhookPing
        self.webhookCallback = webhookCallback
        self.webhookRegistrationAPI = webhookRegistrationAPI
        self.webhookInboundSecret = webhookInboundSecret
        self.liveSyncMode = liveSyncMode
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            provider: try container.decode(String.self, forKey: .provider),
            status: try container.decode(String.self, forKey: .status),
            lastSyncedAt: try container.decodeIfPresent(Date.self, forKey: .lastSyncedAt),
            createdAt: try container.decodeIfPresent(Date.self, forKey: .createdAt),
            updatedAt: try container.decodeIfPresent(Date.self, forKey: .updatedAt),
            maxHistoricalDays: try container.decodeIfPresent(Int.self, forKey: .maxHistoricalDays),
            restPull: try container.decodeIfPresent(Bool.self, forKey: .restPull) ?? false,
            webhookStream: try container.decodeIfPresent(Bool.self, forKey: .webhookStream) ?? false,
            webhookPing: try container.decodeIfPresent(Bool.self, forKey: .webhookPing) ?? false,
            webhookCallback: try container.decodeIfPresent(Bool.self, forKey: .webhookCallback) ?? false,
            webhookRegistrationAPI: try container.decodeIfPresent(Bool.self, forKey: .webhookRegistrationAPI) ?? false,
            webhookInboundSecret: try container.decodeIfPresent(Bool.self, forKey: .webhookInboundSecret) ?? false,
            liveSyncMode: try container.decodeIfPresent(String.self, forKey: .liveSyncMode)
        )
    }

    enum CodingKeys: String, CodingKey {
        case provider
        case status
        case lastSyncedAt = "last_synced_at"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case maxHistoricalDays = "max_historical_days"
        case restPull = "rest_pull"
        case webhookStream = "webhook_stream"
        case webhookPing = "webhook_ping"
        case webhookCallback = "webhook_callback"
        case webhookRegistrationAPI = "webhook_registration_api"
        case webhookInboundSecret = "webhook_inbound_secret"
        case liveSyncMode = "live_sync_mode"
    }
}

public struct WearableDataSourceDescriptor: Codable, Equatable, Identifiable {
    public let id: String
    public let provider: String
    public let displayName: String?
    public let deviceModel: String?
    public let softwareVersion: String?
    public let deviceType: String?
    public let source: String?
    public let originalSourceName: String?

    public init(
        id: String,
        provider: String,
        displayName: String? = nil,
        deviceModel: String? = nil,
        softwareVersion: String? = nil,
        deviceType: String? = nil,
        source: String? = nil,
        originalSourceName: String? = nil
    ) {
        self.id = id
        self.provider = provider
        self.displayName = displayName
        self.deviceModel = deviceModel
        self.softwareVersion = softwareVersion
        self.deviceType = deviceType
        self.source = source
        self.originalSourceName = originalSourceName
    }

    enum CodingKeys: String, CodingKey {
        case id
        case provider
        case displayName = "display_name"
        case deviceModel = "device_model"
        case softwareVersion = "software_version"
        case deviceType = "device_type"
        case source
        case originalSourceName = "original_source_name"
    }

    public var title: String {
        displayName
            ?? deviceModel
            ?? originalSourceName
            ?? source
            ?? deviceType
            ?? "Registered data source"
    }

    public var detail: String {
        [
            deviceType.map(WearableManagementPresentation.humanize),
            softwareVersion.map { "v\($0)" },
            source.map(WearableManagementPresentation.humanize),
        ]
        .compactMap { $0 }
        .joined(separator: " · ")
    }
}

public struct WearableSyncStatusDescriptor: Codable, Equatable, Identifiable {
    public let runID: String
    public let provider: String
    public let source: String
    public let stage: String
    public let status: String
    public let message: String?
    public let progress: Double?
    public let itemsProcessed: Int?
    public let itemsTotal: Int?
    public let error: String?
    public let startedAt: Date?
    public let endedAt: Date?
    public let lastUpdate: Date?

    public var id: String { runID }

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"
        case provider
        case source
        case stage
        case status
        case message
        case progress
        case itemsProcessed = "items_processed"
        case itemsTotal = "items_total"
        case error
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case lastUpdate = "last_update"
    }
}

public struct WearableProviderActionPolicyDescriptor: Codable, Equatable, Identifiable {
    public let provider: String
    public let connectionStatus: String
    public let lastSyncedAt: Date?
    public let canAuthorize: Bool
    public let canSync: Bool
    public let canHistoricalSync: Bool
    public let canDisconnect: Bool
    public let historicalLimit: Int
    public let defaultHistoricalDays: Int

    public var id: String { provider }

    enum CodingKeys: String, CodingKey {
        case provider
        case connectionStatus = "connection_status"
        case lastSyncedAt = "last_synced_at"
        case canAuthorize = "can_authorize"
        case canSync = "can_sync"
        case canHistoricalSync = "can_historical_sync"
        case canDisconnect = "can_disconnect"
        case historicalLimit = "historical_limit"
        case defaultHistoricalDays = "default_historical_days"
    }

    public func policy() -> WearableProviderActionPolicy {
        WearableProviderActionPolicy(
            connectionStatus: connectionStatus,
            isActive: connectionStatus.lowercased() == "active",
            canAuthorize: canAuthorize,
            canSync: canSync,
            canHistoricalSync: canHistoricalSync,
            canDisconnect: canDisconnect,
            historicalLimit: historicalLimit
        )
    }
}

public struct WearablesManagementSnapshot: Codable, Equatable {
    public let apiConfigured: Bool
    public let userConfigured: Bool
    public let providers: [WearableProviderDescriptor]
    public let connections: [WearableConnectionDescriptor]
    public let dataSources: [WearableDataSourceDescriptor]
    public let coverage: JSONValue?
    public let recentSync: [WearableSyncStatusDescriptor]
    public let syncRuns: [WearableSyncStatusDescriptor]
    /// Optional components that were unavailable while the snapshot was built.
    /// Missing on older HealthMes responses, so decoding defaults to empty.
    public let degradedComponents: [String]
    /// Present on `/v1/settings/hub`; absent on the legacy wearable endpoint.
    public let controls: [WearableProviderActionPolicyDescriptor]?

    enum CodingKeys: String, CodingKey {
        case apiConfigured = "api_configured"
        case userConfigured = "user_configured"
        case providers
        case connections
        case dataSources = "data_sources"
        case coverage
        case recentSync = "recent_sync"
        case syncRuns = "sync_runs"
        case degradedComponents = "degraded_components"
        case controls
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        apiConfigured = try container.decode(Bool.self, forKey: .apiConfigured)
        userConfigured = try container.decode(Bool.self, forKey: .userConfigured)
        providers = try container.decode(
            [WearableProviderDescriptor].self,
            forKey: .providers
        )
        connections = try container.decode(
            [WearableConnectionDescriptor].self,
            forKey: .connections
        )
        dataSources = try container.decode(
            [WearableDataSourceDescriptor].self,
            forKey: .dataSources
        )
        coverage = try container.decodeIfPresent(JSONValue.self, forKey: .coverage)
        recentSync = try container.decode(
            [WearableSyncStatusDescriptor].self,
            forKey: .recentSync
        )
        syncRuns = try container.decode(
            [WearableSyncStatusDescriptor].self,
            forKey: .syncRuns
        )
        degradedComponents = try container.decodeIfPresent(
            [String].self,
            forKey: .degradedComponents
        ) ?? []
        controls = try container.decodeIfPresent(
            [WearableProviderActionPolicyDescriptor].self,
            forKey: .controls
        )
    }

    public func connection(for provider: String) -> WearableConnectionDescriptor? {
        connections.first { $0.provider == provider }
    }

    public func dataSources(for provider: String) -> [WearableDataSourceDescriptor] {
        dataSources.filter { $0.provider == provider }
    }

    public func actionPolicy(
        for provider: WearableProviderDescriptor
    ) -> WearableProviderActionPolicy {
        if let control = controls?.first(where: {
            $0.provider == provider.provider
        }) {
            return control.policy()
        }
        let connection = connection(for: provider.provider)
        let dataSourceConnected = !dataSources(for: provider.provider).isEmpty
        let status: String
        if let connection {
            status = connection.status.lowercased()
        } else if provider.clientSDK && dataSourceConnected {
            status = "active"
        } else if dataSourceConnected {
            status = "data_available"
        } else {
            status = "not_connected"
        }
        let isActive = status == "active"
        let hasConnection = connection != nil
        let limits = [
            provider.maxHistoricalDays,
            connection?.maxHistoricalDays,
            365,
        ].compactMap { $0 }

        return WearableProviderActionPolicy(
            connectionStatus: status,
            isActive: isActive,
            canAuthorize: provider.catalogAvailable
                && provider.managementSupported
                && provider.hasCloudAPI
                && provider.isEnabled
                && !isActive,
            canSync: provider.isEnabled && hasConnection
                && provider.managementSupported
                && isActive && provider.restPull,
            canHistoricalSync: provider.isEnabled && hasConnection && isActive
                && provider.managementSupported
                && (provider.restPull || provider.webhookCallback),
            canDisconnect: provider.managementSupported
                && hasConnection && isActive && provider.hasCloudAPI,
            historicalLimit: limits.min() ?? 365
        )
    }
}

public struct WearableAuthorizationResponse: Codable, Equatable {
    public let provider: String
    public let authorizationURL: URL

    enum CodingKeys: String, CodingKey {
        case provider
        case authorizationURL = "authorization_url"
    }
}

public struct WearableMutationResponse: Codable, Equatable {
    public let provider: String
    public let operation: String
    public let success: Bool
    public let isAsync: Bool?
    public let taskID: String?
    public let method: String?
    public let message: String?
    public let days: Int?
    public let startDate: String?
    public let endDate: String?

    enum CodingKeys: String, CodingKey {
        case provider
        case operation
        case success
        case isAsync = "async"
        case taskID = "task_id"
        case method
        case message
        case days
        case startDate = "start_date"
        case endDate = "end_date"
    }
}

public struct WearableProviderActionPolicy: Equatable {
    public let connectionStatus: String
    public let isActive: Bool
    public let canAuthorize: Bool
    public let canSync: Bool
    public let canHistoricalSync: Bool
    public let canDisconnect: Bool
    public let historicalLimit: Int
}

public enum WearableManagementJSON {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let value = try decoder.singleValueContainer()
            if let raw = try? value.decode(String.self) {
                if let parsed = parseDate(raw) {
                    return parsed
                }
            }
            if let number = try? value.decode(Double.self) {
                let seconds = abs(number) > 100_000_000_000
                    ? number / 1_000
                    : number
                return Date(timeIntervalSince1970: seconds)
            }
            throw DecodingError.dataCorruptedError(
                in: value,
                debugDescription: "Invalid wearable timestamp."
            )
        }
        return decoder
    }

    public static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    private static func parseDate(_ raw: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        if let date = fractional.date(from: raw) {
            return date
        }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: raw)
    }
}

public enum WearableManagementPresentation {
    public static func status(_ raw: String) -> String {
        switch raw.lowercased() {
        case "active", "connected", "ready", "success", "succeeded":
            return "Connected"
        case "pending", "queued", "running", "syncing":
            return "Syncing"
        case "expired":
            return "Reconnect required"
        case "revoked", "disconnected", "not_connected":
            return "Disconnected"
        case "data_available":
            return "Data available"
        case "error", "failed", "failure":
            return "Needs attention"
        case "not_configured":
            return "Server setup required"
        default:
            return humanize(raw)
        }
    }

    public static func humanize(_ raw: String) -> String {
        let words = raw
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
        guard let first = words.first else { return words }
        return first.uppercased() + words.dropFirst()
    }

    public static func deviceSymbol(
        deviceType: String?,
        deviceModel: String?,
        source: String?
    ) -> String {
        let text = [
            deviceType,
            deviceModel,
            source,
        ]
        .compactMap { $0?.lowercased() }
        .joined(separator: " ")

        if text.contains("watch") {
            return "applewatch"
        }
        if text.contains("ring") {
            return "circle.dotted"
        }
        if text.contains("scale") {
            return "scalemass"
        }
        if text.contains("phone") || text.contains("iphone") {
            return "iphone"
        }
        if text.contains("tablet") || text.contains("ipad") {
            return "ipad"
        }
        if text.contains("band") {
            return "rectangle.and.hand.point.up.left"
        }
        if text.contains("heart") || text.contains("health") {
            return "heart.text.square"
        }
        return "sensor"
    }
}
