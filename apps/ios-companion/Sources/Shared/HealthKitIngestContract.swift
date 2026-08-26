import CryptoKit
import Foundation

public struct HealthKitIngestPayload: Codable, Equatable {
    public struct Source: Codable, Equatable {
        public let appId: String?
        public let name: String?
        public let bundleIdentifier: String?
        public let version: String?
        public let productType: String?
        public let deviceId: String?
        public let deviceName: String?
        public let deviceManufacturer: String?
        public let deviceType: String?
        public let deviceModel: String?
        public let deviceHardwareVersion: String?
        public let deviceSoftwareVersion: String?

        public init(
            appId: String? = nil,
            name: String? = nil,
            bundleIdentifier: String? = nil,
            version: String? = nil,
            productType: String? = nil,
            deviceId: String? = nil,
            deviceName: String? = nil,
            deviceManufacturer: String? = nil,
            deviceType: String? = nil,
            deviceModel: String? = nil,
            deviceHardwareVersion: String? = nil,
            deviceSoftwareVersion: String? = nil
        ) {
            self.appId = appId
            self.name = name
            self.bundleIdentifier = bundleIdentifier
            self.version = version
            self.productType = productType
            self.deviceId = deviceId
            self.deviceName = deviceName
            self.deviceManufacturer = deviceManufacturer
            self.deviceType = deviceType
            self.deviceModel = deviceModel
            self.deviceHardwareVersion = deviceHardwareVersion
            self.deviceSoftwareVersion = deviceSoftwareVersion
        }
    }

    public struct DataSet: Codable, Equatable {
        public let records: [Metric]
        public let sleep: [Sleep]
        public let workouts: [Workout]
        public let deletions: [Deletion]

        public init(
            records: [Metric] = [],
            sleep: [Sleep] = [],
            workouts: [Workout] = [],
            deletions: [Deletion] = []
        ) {
            self.records = records
            self.sleep = sleep
            self.workouts = workouts
            self.deletions = deletions
        }
    }

    public struct Deletion: Codable, Equatable {
        public let id: String
        public let type: String

        public init(id: String, type: String) {
            self.id = id
            self.type = type
        }
    }

    public struct Metric: Codable, Equatable {
        public let id: String
        public let type: String
        public let startDate: Date
        public let endDate: Date
        public let value: Double
        public let unit: String
        public let zoneOffset: String?
        public let source: Source?

        public init(
            id: String,
            type: String,
            startDate: Date,
            endDate: Date,
            value: Double,
            unit: String,
            zoneOffset: String? = nil,
            source: Source? = nil
        ) {
            self.id = id
            self.type = type
            self.startDate = startDate
            self.endDate = endDate
            self.value = value
            self.unit = unit
            self.zoneOffset = zoneOffset
            self.source = source
        }
    }

    public struct Sleep: Codable, Equatable {
        public let id: String
        public let stage: String
        public let startDate: Date
        public let endDate: Date
        public let zoneOffset: String?
        public let source: Source?

        public init(
            id: String,
            stage: String,
            startDate: Date,
            endDate: Date,
            zoneOffset: String? = nil,
            source: Source? = nil
        ) {
            self.id = id
            self.stage = stage
            self.startDate = startDate
            self.endDate = endDate
            self.zoneOffset = zoneOffset
            self.source = source
        }
    }

    public struct Workout: Codable, Equatable {
        public let id: String
        public let type: String
        public let startDate: Date
        public let endDate: Date
        public let values: [Statistic]
        public let zoneOffset: String?
        public let source: Source?

        public init(
            id: String,
            type: String,
            startDate: Date,
            endDate: Date,
            values: [Statistic],
            zoneOffset: String? = nil,
            source: Source? = nil
        ) {
            self.id = id
            self.type = type
            self.startDate = startDate
            self.endDate = endDate
            self.values = values
            self.zoneOffset = zoneOffset
            self.source = source
        }
    }

    public struct Statistic: Codable, Equatable {
        public let type: String
        public let unit: String
        public let value: Double
    }

    public let schema: String
    public let sdkVersion: String
    public let syncTimestamp: Date
    public let data: DataSet

    public init(syncTimestamp: Date = Date(), data: DataSet) {
        schema = "healthmes.healthkit.v1"
        sdkVersion = "healthmes-ios/1"
        self.syncTimestamp = syncTimestamp
        self.data = data
    }
}

public enum HealthKitWireFormat {
    public static func percentage(fromFraction value: Double) -> Double {
        value * 100
    }

    public static func zoneOffset(for date: Date, timeZone: TimeZone = .current) -> String {
        let seconds = timeZone.secondsFromGMT(for: date)
        let sign = seconds < 0 ? "-" : "+"
        let absolute = abs(seconds)
        return String(format: "%@%02d:%02d", sign, absolute / 3_600, absolute % 3_600 / 60)
    }
}

public struct HealthKitIngestAck: Codable, Equatable {
    public let rawID: String
    public let durable: Bool
    public let sha256: String
    public let sizeBytes: Int
    public let parseStatus: String
    public let forwardStatus: String
    public let recordsForwarded: Int
    public let sleepForwarded: Int
    public let workoutsForwarded: Int
    public let deletionsReceived: Int
    public let statusPersistenceUncertain: Bool

    enum CodingKeys: String, CodingKey {
        case rawID = "raw_id"
        case durable
        case sha256
        case sizeBytes = "size_bytes"
        case parseStatus = "parse_status"
        case forwardStatus = "forward_status"
        case recordsForwarded = "records_forwarded"
        case sleepForwarded = "sleep_forwarded"
        case workoutsForwarded = "workouts_forwarded"
        case deletionsReceived = "deletions_received"
        case statusPersistenceUncertain = "status_persistence_uncertain"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        rawID = try container.decode(String.self, forKey: .rawID)
        durable = try container.decode(Bool.self, forKey: .durable)
        sha256 = try container.decode(String.self, forKey: .sha256)
        sizeBytes = try container.decode(Int.self, forKey: .sizeBytes)
        parseStatus = try container.decode(String.self, forKey: .parseStatus)
        forwardStatus = try container.decode(String.self, forKey: .forwardStatus)
        recordsForwarded = try container.decode(
            Int.self,
            forKey: .recordsForwarded
        )
        sleepForwarded = try container.decode(
            Int.self,
            forKey: .sleepForwarded
        )
        workoutsForwarded = try container.decode(
            Int.self,
            forKey: .workoutsForwarded
        )
        deletionsReceived = try container.decode(
            Int.self,
            forKey: .deletionsReceived
        )
        // Older Main builds did not return this field. Missing means that the
        // server explicitly confirmed the status write.
        statusPersistenceUncertain = try container.decodeIfPresent(
            Bool.self,
            forKey: .statusPersistenceUncertain
        ) ?? false
    }

    public func validate(exactBody: Data) throws {
        guard durable else {
            throw HealthKitIngestAckValidationError.notDurable
        }
        guard sizeBytes == exactBody.count else {
            throw HealthKitIngestAckValidationError.sizeMismatch(
                expected: exactBody.count,
                received: sizeBytes
            )
        }
        let expectedHash = SHA256.hash(data: exactBody)
            .map { String(format: "%02x", $0) }
            .joined()
        guard sha256.lowercased() == expectedHash else {
            throw HealthKitIngestAckValidationError.hashMismatch
        }
        guard parseStatus == "parsed" else {
            throw HealthKitIngestAckValidationError.unsupportedParseStatus(
                parseStatus
            )
        }
        guard !statusPersistenceUncertain else {
            throw HealthKitIngestAckValidationError.statusPersistenceUncertain
        }
        switch forwardStatus {
        case "queued", "nothing_mapped":
            return
        case "deletions_recorded":
            // Older servers could persist a tombstone without removing the
            // canonical derivative. Never advance a deletion anchor on that
            // acknowledgement; the current server uses HTTP 503 instead.
            throw HealthKitIngestAckValidationError.deletionForwardingPending
        case "forward_failed", "skipped_no_user":
            throw HealthKitIngestAckValidationError.forwardingPending(
                status: forwardStatus
            )
        default:
            throw HealthKitIngestAckValidationError.unsupportedForwardStatus(
                forwardStatus
            )
        }
    }
}

public enum HealthKitIngestAckValidationError:
    Error,
    Equatable,
    LocalizedError
{
    case notDurable
    case sizeMismatch(expected: Int, received: Int)
    case hashMismatch
    case unsupportedParseStatus(String)
    case statusPersistenceUncertain
    case deletionForwardingPending
    case forwardingPending(status: String)
    case unsupportedForwardStatus(String)

    public var errorDescription: String? {
        switch self {
        case .notDurable:
            return "The personal server did not confirm durable HealthKit storage."
        case .sizeMismatch:
            return "The HealthKit acknowledgement size did not match the uploaded bytes."
        case .hashMismatch:
            return "The HealthKit acknowledgement hash did not match the uploaded bytes."
        case .unsupportedParseStatus:
            return "The personal server did not parse the first-party HealthKit batch."
        case .statusPersistenceUncertain:
            return "The personal server could not confirm the HealthKit status update."
        case .deletionForwardingPending:
            return "The personal server has not removed HealthKit deletions from its canonical data."
        case .forwardingPending:
            return "The personal server stored the HealthKit batch but has not finished forwarding it."
        case .unsupportedForwardStatus:
            return "The personal server returned an unsupported HealthKit forwarding status."
        }
    }
}

public enum HealthKitUploadFailureDisposition: Equatable, Sendable {
    case retryable
    case terminal(reason: String)

    public static func classify(_ error: Error) -> Self {
        if let error = error as? HealthMesAPIError {
            switch error {
            case .transport:
                return .retryable
            case .httpStatus(let status):
                return isRetryable(status: status)
                    ? .retryable
                    : .terminal(reason: "HTTP \(status)")
            case .server(let status, let code, _, _):
                if status == 409, code == "healthkit_ingest_in_progress" {
                    return .retryable
                }
                return isRetryable(status: status)
                    ? .retryable
                    : .terminal(reason: "HTTP \(status) \(code)")
            case .unauthorized(let status):
                return .terminal(reason: "HTTP \(status) unauthorized")
            case .decoding:
                return .terminal(
                    reason: "The HealthKit acknowledgement was invalid."
                )
            case .notPaired:
                return .terminal(reason: "HealthMes is not paired.")
            }
        }
        if let error = error as? HealthKitIngestAckValidationError {
            switch error {
            case .statusPersistenceUncertain,
                .forwardingPending,
                .deletionForwardingPending:
                return .retryable
            default:
                break
            }
            return .terminal(
                reason: "The HealthKit acknowledgement contract failed."
            )
        }
        if error is HealthKitUploadRequestError {
            return .terminal(
                reason: "The HealthKit acknowledgement contract failed."
            )
        }
        return .retryable
    }

    public static func shouldQuarantineAfterAutomaticRetries(
        _: Error,
        failedAttempts _: Int,
        isManualRetry _: Bool = false,
        retryPolicy _: HealthKitSyncRetryPolicy = .default
    ) -> Bool {
        false
    }

    public static func countsTowardAutomaticQuarantine(
        _: Error,
        isManualRetry _: Bool = false
    ) -> Bool {
        false
    }

    /// Raw durability alone is not canonical replay durability. Until the
    /// server owns a transactional replay worker, every unsuccessful upload
    /// keeps its exact body, idempotency key, and candidate anchor fail-closed.
    public static func permitsAnchorAdvance(
        _: Error,
        isManualRetry _: Bool = false
    ) -> Bool {
        false
    }

    private static func isRetryable(status: Int) -> Bool {
        status <= 0
            || status == 408
            || status == 425
            || status == 429
            || (500...599).contains(status)
    }
}
