import Foundation

public struct StorageRetentionPolicy: Codable, Equatable, Identifiable {
    public var id: String { dataClass }
    public let dataClass: String
    public let preset: String
    public let retentionDays: Int?
    public let enabled: Bool

    enum CodingKeys: String, CodingKey {
        case dataClass = "data_class"
        case preset
        case retentionDays = "retention_days"
        case enabled
    }
}

public struct StorageBackupStatus: Codable, Equatable {
    public let provider: String
    public let directory: String
    public let snapshotCount: Int
    public let latestSnapshot: String?
    public let encryptionConfigured: Bool

    enum CodingKeys: String, CodingKey {
        case provider
        case directory
        case snapshotCount = "snapshot_count"
        case latestSnapshot = "latest_snapshot"
        case encryptionConfigured = "encryption_configured"
    }
}

public struct StorageSettingsSnapshot: Codable, Equatable {
    public let dataDir: String
    public let diskTotalBytes: Int64
    public let diskFreeBytes: Int64
    public let usage: [String: [String: Int64]]
    public let policies: [StorageRetentionPolicy]
    public let backup: StorageBackupStatus

    enum CodingKeys: String, CodingKey {
        case dataDir = "data_dir"
        case diskTotalBytes = "disk_total_bytes"
        case diskFreeBytes = "disk_free_bytes"
        case usage
        case policies
        case backup
    }
}

public struct StorageRetentionUpdate: Codable, Equatable {
    public let preset: String
}

public struct StorageMaintenanceReport: Codable, Equatable {
    public let jobID: UUID
    public let dryRun: Bool
    public let candidates: Int
    public let deleted: Int
    public let bytesReclaimed: Int64
    public let errors: [String]

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case dryRun = "dry_run"
        case candidates
        case deleted
        case bytesReclaimed = "bytes_reclaimed"
        case errors
    }
}
