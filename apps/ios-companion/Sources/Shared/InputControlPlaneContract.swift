import Foundation

public enum InputConnectionState: String, Codable, CaseIterable, Equatable {
    case notConfigured = "not_configured"
    case configured
    case connected
    case unavailable
}

public enum InputCollectionState: String, Codable, CaseIterable, Equatable {
    case notApplicable = "not_applicable"
    case idle
    case collecting
    case paused
    case blocked
    case unavailable
}

public struct InputActionDescriptor: Codable, Equatable, Identifiable {
    public var id: String {
        [
            action,
            execution,
            method ?? "",
            endpoint ?? "",
            requiresInstance ? "instance" : "source",
        ].joined(separator: ":")
    }

    public let action: String
    public let execution: String
    public let method: String?
    public let endpoint: String?
    public let requiresInstance: Bool
    public let description: String

    enum CodingKeys: String, CodingKey {
        case action
        case execution
        case method
        case endpoint
        case requiresInstance = "requires_instance"
        case description
    }
}

public struct InputSettingDefinition: Codable, Equatable, Identifiable {
    public var id: String { "\(scope):\(key)" }

    public let key: String
    public let valueType: String
    public let scope: String
    public let allowedValues: [String]
    public let description: String

    enum CodingKeys: String, CodingKey {
        case key
        case valueType = "value_type"
        case scope
        case allowedValues = "allowed_values"
        case description
    }
}

public struct InputRetentionPolicy: Codable, Equatable, Identifiable {
    public var id: String { dataClass }

    public let dataClass: String
    public let preset: String
    public let retentionDays: Int?
    public let enabled: Bool
    public let effectivePreset: String
    public let sharedAcrossSourceInstances: Bool

    enum CodingKeys: String, CodingKey {
        case dataClass = "data_class"
        case preset
        case retentionDays = "retention_days"
        case enabled
        case effectivePreset = "effective_preset"
        case sharedAcrossSourceInstances = "shared_across_source_instances"
    }
}

public struct InputPrivacyProfile: Codable, Equatable {
    public let localFirst: Bool
    public let rawContentCollected: Bool
    public let sourceSideExclusions: Bool
    public let defaultLLMExposure: String
    public let notes: [String]

    enum CodingKeys: String, CodingKey {
        case localFirst = "local_first"
        case rawContentCollected = "raw_content_collected"
        case sourceSideExclusions = "source_side_exclusions"
        case defaultLLMExposure = "default_llm_exposure"
        case notes
    }
}

public struct InputInstance: Codable, Equatable, Identifiable {
    public var id: String { instanceID }

    public let instanceID: String
    public let platform: String
    public let enabled: Bool
    public let effectiveCollecting: Bool
    public let permissionStatus: String
    public let capability: String
    public let blockedReason: String?
    public let statusReason: String?
    public let statusObservedAt: Date?
    public let lastCollectedAt: Date?
    public let lastUploadedAt: Date?
    public let coverage: Double?
    public let excludedApps: [String]
    public let pausedUntil: Date?
    public let configRevision: Int

    enum CodingKeys: String, CodingKey {
        case instanceID = "instance_id"
        case platform
        case enabled
        case effectiveCollecting = "effective_collecting"
        case permissionStatus = "permission_status"
        case capability
        case blockedReason = "blocked_reason"
        case statusReason = "status_reason"
        case statusObservedAt = "status_observed_at"
        case lastCollectedAt = "last_collected_at"
        case lastUploadedAt = "last_uploaded_at"
        case coverage
        case excludedApps = "excluded_apps"
        case pausedUntil = "paused_until"
        case configRevision = "config_revision"
    }
}

public struct InputSourceDescriptor: Codable, Equatable, Identifiable {
    public var id: String { sourceID }

    public let sourceID: String
    public let domain: String
    public let displayName: String
    public let platforms: [String]
    public let capabilities: [String]
    public let connectionState: InputConnectionState
    public let collectionState: InputCollectionState
    public let decisionAccessEnabled: Bool
    public let instances: [InputInstance]
    public let retention: [InputRetentionPolicy]
    public let settings: [InputSettingDefinition]
    public let actions: [InputActionDescriptor]
    public let privacy: InputPrivacyProfile
    public let limitations: [String]
    public let revision: String

    enum CodingKeys: String, CodingKey {
        case sourceID = "source_id"
        case domain
        case displayName = "display_name"
        case platforms
        case capabilities
        case connectionState = "connection_state"
        case collectionState = "collection_state"
        case decisionAccessEnabled = "decision_access_enabled"
        case instances
        case retention
        case settings
        case actions
        case privacy
        case limitations
        case revision
    }

    public func supports(setting key: String, scope: String? = nil) -> Bool {
        settings.contains { setting in
            setting.key == key && (scope == nil || setting.scope == scope)
        }
    }

    public var retentionAllowedValues: [String] {
        settings.first(where: { $0.key == "retention" })?.allowedValues ?? []
    }

    public var isWearable: Bool { domain == "wearable" }

    public var strongETag: String? {
        InputControlPlaneContract.strongETag(for: revision)
    }
}

public struct InputSourcesResponse: Codable, Equatable {
    public let sources: [InputSourceDescriptor]
}

public struct InputControlPlaneSettingsUpdate: Encodable, Equatable {
    public let instanceID: String?
    public let platform: String?
    public let enabled: Bool?
    public let decisionAccessEnabled: Bool?
    public let retention: [String: String]

    public init(
        instanceID: String? = nil,
        platform: String? = nil,
        enabled: Bool? = nil,
        decisionAccessEnabled: Bool? = nil,
        retention: [String: String] = [:]
    ) {
        self.instanceID = instanceID
        self.platform = platform
        self.enabled = enabled
        self.decisionAccessEnabled = decisionAccessEnabled
        self.retention = retention
    }

    enum CodingKeys: String, CodingKey {
        case instanceID = "instance_id"
        case platform
        case enabled
        case decisionAccessEnabled = "decision_access_enabled"
        case retention
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(instanceID, forKey: .instanceID)
        try container.encodeIfPresent(platform, forKey: .platform)
        try container.encodeIfPresent(enabled, forKey: .enabled)
        try container.encodeIfPresent(
            decisionAccessEnabled,
            forKey: .decisionAccessEnabled
        )
        if !retention.isEmpty {
            try container.encode(retention, forKey: .retention)
        }
    }
}

public enum InputControlPlaneContract {
    public static func isValidRevision(_ revision: String) -> Bool {
        guard revision.hasPrefix("sha256:"), revision.utf8.count == 71 else {
            return false
        }
        return revision.dropFirst("sha256:".count).utf8.allSatisfy { byte in
            (48...57).contains(byte) || (97...102).contains(byte)
        }
    }

    public static func strongETag(for revision: String) -> String? {
        guard isValidRevision(revision) else { return nil }
        return "\"\(revision)\""
    }

    public static func revision(fromStrongETag etag: String) -> String? {
        guard etag.first == "\"", etag.last == "\"", etag.utf8.count == 73 else {
            return nil
        }
        let revision = String(etag.dropFirst().dropLast())
        return isValidRevision(revision) ? revision : nil
    }
}

public enum InputControlPlaneJSON {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = parseDate(raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Unparseable input datetime: \(raw)"
                )
            }
            return date
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
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: raw) { return date }

        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        return fractional.date(from: raw)
    }
}

public enum InputControlPlanePresentation {
    public static func connection(_ state: InputConnectionState) -> String {
        switch state {
        case .notConfigured:
            return "Not configured"
        case .configured:
            return "Configured"
        case .connected:
            return "Connected"
        case .unavailable:
            return "Unavailable"
        }
    }

    public static func collection(_ state: InputCollectionState) -> String {
        switch state {
        case .notApplicable:
            return "Not applicable"
        case .idle:
            return "Idle"
        case .collecting:
            return "Collecting"
        case .paused:
            return "Paused"
        case .blocked:
            return "Blocked"
        case .unavailable:
            return "Unavailable"
        }
    }

    public static func sourceSummary(_ source: InputSourceDescriptor) -> String {
        let connection = connection(source.connectionState)
        guard source.collectionState != .notApplicable else {
            return connection
        }
        return "\(connection) · \(collection(source.collectionState))"
    }

    public static func instanceSummary(_ source: InputSourceDescriptor) -> String {
        let count = source.instances.count
        if source.isWearable {
            return count == 1 ? "Reported instances: 1" : "Reported instances: \(count)"
        }
        return count == 1 ? "Reported instances: 1" : "Reported instances: \(count)"
    }

    public static func instanceStatus(_ instance: InputInstance) -> String {
        if let blockedReason = instance.blockedReason, !blockedReason.isEmpty {
            return "Blocked: \(humanize(blockedReason))"
        }
        if let statusReason = instance.statusReason, !statusReason.isEmpty {
            return humanize(statusReason)
        }
        if instance.effectiveCollecting {
            return "Collecting"
        }
        if !instance.enabled {
            return "Disabled"
        }
        return humanize(instance.permissionStatus)
    }

    public static func limitation(_ code: String) -> String {
        switch code {
        case "wearable_provider_device_inventory_not_available":
            return "Individual wearable providers and devices are not listed by this endpoint."
        case "wearable_provider_device_crud_not_available":
            return "Provider and device connections cannot be added or removed here because the server has no CRUD contract yet."
        case "open_wearables_configuration_is_server_managed":
            return "Open Wearables credentials are managed on the HealthMes server."
        case "healthkit_collection_requires_healthmes_ios_companion":
            return "Apple Health collection runs in the paired HealthMes iPhone app."
        case "healthkit_delivery_freshness_is_not_observed":
            return "The aggregate descriptor does not report HealthKit delivery freshness."
        default:
            return humanize(code)
        }
    }

    public static func humanize(_ value: String) -> String {
        let words = value
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
        guard let first = words.first else { return words }
        return first.uppercased() + words.dropFirst()
    }
}
