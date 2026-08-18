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
    }
}
