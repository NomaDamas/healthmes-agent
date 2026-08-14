import CryptoKit
import Foundation

// UI-neutral contracts for the iPhone Screen Time activity input.
//
// The device uploads only hourly durations, controlled category names, and
// keyed pseudonyms. Bundle identifiers, display names, pickups, notifications,
// screenshots, URLs, and content never cross this boundary.

public enum ScreenTimeActivityCapability: String, Codable {
    case aggregate
    case unavailable
}

public enum ScreenTimeActivityPermissionStatus: String, Codable {
    case granted
    case denied
    case restricted
    case revoked
    case unavailable
    case unknown
}

enum ScreenTimeDeviceIdentity {
    static func fromPseudonymKey(_ keyData: Data) -> String {
        var material = Data("healthmes-screen-time-device-v1\u{0}".utf8)
        material.append(keyData)
        let digest = SHA256.hash(data: material)
        let fingerprint = digest.prefix(20)
            .map { String(format: "%02x", $0) }
            .joined()
        return "ios-collector-v1-\(fingerprint)"
    }
}

struct ScreenTimeFallbackDeviceIdentityStore {
    private static let defaultsKey =
        "healthmes.screen-time.fallback-device-id.v1"
    private static let lock = NSLock()

    private let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.userDefaults) {
        self.defaults = defaults
    }

    func current() -> String {
        Self.lock.lock()
        defer { Self.lock.unlock() }
        if let existing = defaults.string(
            forKey: Self.defaultsKey
        ), !existing.isEmpty {
            return existing
        }
        let generated =
            "ios-collector-unavailable-v1-\(UUID().uuidString.lowercased())"
        defaults.set(generated, forKey: Self.defaultsKey)
        return generated
    }
}

struct ScreenTimeActivityIdentityResolution: Equatable {
    let deviceID: String
    let pseudonymKeyData: Data?
}

enum ScreenTimeActivityIdentityResolver {
    static func resolve(
        explicitDeviceID: String?,
        pseudonymKeyLoader: () throws -> Data,
        fallbackIdentityStore: ScreenTimeFallbackDeviceIdentityStore
    ) -> ScreenTimeActivityIdentityResolution {
        // The key is needed by the collector even when the caller supplies
        // an explicit device ID, so load it exactly once at this boundary.
        let pseudonymKeyData = try? pseudonymKeyLoader()
        let explicitDeviceID = explicitDeviceID?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedDeviceID: String
        if let explicitDeviceID, !explicitDeviceID.isEmpty {
            resolvedDeviceID = explicitDeviceID
        } else if let pseudonymKeyData {
            resolvedDeviceID = ScreenTimeDeviceIdentity.fromPseudonymKey(
                pseudonymKeyData
            )
        } else {
            resolvedDeviceID = fallbackIdentityStore.current()
        }
        return ScreenTimeActivityIdentityResolution(
            deviceID: resolvedDeviceID,
            pseudonymKeyData: pseudonymKeyData
        )
    }
}

public struct ScreenTimeActivitySample: Codable, Equatable {
    public let sourceRecordID: String
    public let bucketStart: Date
    public let foregroundSeconds: Int
    public let category: String?
    public let opaqueAppToken: String?
    public let coverageSeconds: Int?
    public let coverageOnly: Bool

    public init(
        sourceRecordID: String,
        bucketStart: Date,
        foregroundSeconds: Int,
        category: String?,
        opaqueAppToken: String?,
        coverageSeconds: Int?,
        coverageOnly: Bool = false
    ) {
        self.sourceRecordID = sourceRecordID
        self.bucketStart = bucketStart
        self.foregroundSeconds = foregroundSeconds
        self.category = category
        self.opaqueAppToken = opaqueAppToken
        self.coverageSeconds = coverageSeconds
        self.coverageOnly = coverageOnly
    }

    enum CodingKeys: String, CodingKey {
        case sourceRecordID = "source_record_id"
        case bucketStart = "bucket_start"
        case foregroundSeconds = "foreground_seconds"
        case category
        case opaqueAppToken = "opaque_app_token"
        case coverageSeconds = "coverage_seconds"
        case coverageOnly = "coverage_only"
    }
}

public struct ScreenTimeActivityReport: Codable, Equatable {
    public let deviceID: String
    public let timezone: String
    public let capability: ScreenTimeActivityCapability
    public let permissionStatus: ScreenTimeActivityPermissionStatus
    public let pseudonymKeyID: String?
    public let reason: String?
    public let collectedAt: Date
    public let collectionRevision: Int?
    public let collectionGeneration: Int?
    public let resetSnapshotFence: Bool
    public let snapshotSequence: Int?
    public let snapshotStart: Date?
    public let snapshotEnd: Date?
    public let authoritativeBucketStarts: [Date]
    public let samples: [ScreenTimeActivitySample]

    private init(
        deviceID: String,
        timezone: String,
        capability: ScreenTimeActivityCapability,
        permissionStatus: ScreenTimeActivityPermissionStatus,
        pseudonymKeyID: String?,
        reason: String?,
        collectedAt: Date,
        collectionRevision: Int?,
        collectionGeneration: Int?,
        resetSnapshotFence: Bool,
        snapshotSequence: Int?,
        snapshotStart: Date?,
        snapshotEnd: Date?,
        authoritativeBucketStarts: [Date],
        samples: [ScreenTimeActivitySample]
    ) {
        self.deviceID = deviceID
        self.timezone = timezone
        self.capability = capability
        self.permissionStatus = permissionStatus
        self.pseudonymKeyID = pseudonymKeyID
        self.reason = reason
        self.collectedAt = collectedAt
        self.collectionRevision = collectionRevision
        self.collectionGeneration = collectionGeneration
        self.resetSnapshotFence = resetSnapshotFence
        self.snapshotSequence = snapshotSequence
        self.snapshotStart = snapshotStart
        self.snapshotEnd = snapshotEnd
        self.authoritativeBucketStarts =
            authoritativeBucketStarts.sorted()
        self.samples = samples
    }

    public static func aggregate(
        deviceID: String,
        timezone: String,
        pseudonymKeyID: String,
        collectedAt: Date,
        collectionRevision: Int,
        collectionGeneration: Int,
        resetSnapshotFence: Bool = false,
        snapshotSequence: Int,
        snapshotStart: Date,
        snapshotEnd: Date,
        authoritativeBucketStarts: Set<Date>,
        samples: [ScreenTimeActivitySample]
    ) -> ScreenTimeActivityReport {
        ScreenTimeActivityReport(
            deviceID: deviceID,
            timezone: timezone,
            capability: .aggregate,
            permissionStatus: .granted,
            pseudonymKeyID: pseudonymKeyID,
            reason: nil,
            collectedAt: collectedAt,
            collectionRevision: collectionRevision,
            collectionGeneration: collectionGeneration,
            resetSnapshotFence: resetSnapshotFence,
            snapshotSequence: snapshotSequence,
            snapshotStart: snapshotStart,
            snapshotEnd: snapshotEnd,
            authoritativeBucketStarts: Array(
                authoritativeBucketStarts
            ),
            samples: samples
        )
    }

    public static func unavailable(
        deviceID: String,
        timezone: String,
        permissionStatus: ScreenTimeActivityPermissionStatus,
        reason: String,
        collectedAt: Date,
        collectionRevision: Int? = nil,
        collectionGeneration: Int? = nil
    ) -> ScreenTimeActivityReport {
        ScreenTimeActivityReport(
            deviceID: deviceID,
            timezone: timezone,
            capability: .unavailable,
            permissionStatus: permissionStatus,
            pseudonymKeyID: nil,
            reason: reason,
            collectedAt: collectedAt,
            collectionRevision: collectionRevision,
            collectionGeneration: collectionGeneration,
            resetSnapshotFence: false,
            snapshotSequence: nil,
            snapshotStart: nil,
            snapshotEnd: nil,
            authoritativeBucketStarts: [],
            samples: []
        )
    }

    public func resettingSnapshotFence() -> ScreenTimeActivityReport {
        ScreenTimeActivityReport(
            deviceID: deviceID,
            timezone: timezone,
            capability: capability,
            permissionStatus: permissionStatus,
            pseudonymKeyID: pseudonymKeyID,
            reason: reason,
            collectedAt: collectedAt,
            collectionRevision: collectionRevision,
            collectionGeneration: collectionGeneration,
            resetSnapshotFence: true,
            snapshotSequence: snapshotSequence,
            snapshotStart: snapshotStart,
            snapshotEnd: snapshotEnd,
            authoritativeBucketStarts: authoritativeBucketStarts,
            samples: samples
        )
    }

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case timezone
        case capability
        case permissionStatus = "permission_status"
        case pseudonymKeyID = "pseudonym_key_id"
        case reason
        case collectedAt = "collected_at"
        case collectionRevision = "collection_revision"
        case collectionGeneration = "collection_generation"
        case resetSnapshotFence = "reset_snapshot_fence"
        case snapshotSequence = "snapshot_sequence"
        case snapshotStart = "snapshot_start"
        case snapshotEnd = "snapshot_end"
        case authoritativeBucketStarts = "authoritative_bucket_starts"
        case samples
    }
}

public struct ScreenTimeCollectionState: Codable, Equatable {
    public let deviceID: String
    public let enabled: Bool
    public let excludedApps: [String]
    public let pausedUntil: Date?
    public let effectiveCollecting: Bool
    public let blockedReason: String?
    public let configRevision: Int
    public let rawRetentionCutoff: Date?

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case enabled
        case excludedApps = "excluded_apps"
        case pausedUntil = "paused_until"
        case effectiveCollecting = "effective_collecting"
        case blockedReason = "blocked_reason"
        case configRevision = "config_revision"
        case rawRetentionCutoff = "raw_retention_cutoff"
    }
}

enum ScreenTimeSyncError: Error, Equatable {
    case invalidCompletedHourWindow
}

enum ScreenTimePseudonymBoundaryError: Error, Equatable {
    case pseudonymKeyUnavailable
    case pseudonymKeyChanged
    case invalidExcludedAppToken
}

struct ScreenTimePseudonymBoundary: Equatable {
    let requiresExclusionReapproval: Bool
    let reason: String?

    static let accepted = ScreenTimePseudonymBoundary(
        requiresExclusionReapproval: false,
        reason: nil
    )
}

struct ScreenTimeCollectionWindow: Equatable {
    let start: Date
    let end: Date
    let timezone: TimeZone
}

enum ScreenTimeCoveragePlanner {
    static func confirmedZeroHourStarts(
        in window: ScreenTimeCollectionWindow,
        confirmedZeroBuckets: Set<Date>
    ) -> [Date] {
        confirmedZeroBuckets
            .filter { $0 >= window.start && $0 < window.end }
            .sorted()
    }
}

struct ScreenTimeAccumulatedUsage: Equatable {
    let bucketStart: Date
    let opaqueAppToken: String
    let category: String
    let foregroundSeconds: Int
}

enum ScreenTimeSamplePlanner {
    static func samples(
        usage: [ScreenTimeAccumulatedUsage],
        confirmedZeroBuckets: Set<Date>,
        privacyTaintedBuckets: Set<Date>,
        window: ScreenTimeCollectionWindow,
        pseudonymizer: ScreenTimeAppPseudonymizer
    ) -> [ScreenTimeActivitySample] {
        let activitySamples = usage.map { value in
            ScreenTimeActivitySample(
                sourceRecordID: pseudonymizer.sourceRecordID(
                    opaqueAppToken: value.opaqueAppToken,
                    bucketStart: value.bucketStart
                ),
                bucketStart: value.bucketStart,
                foregroundSeconds: min(
                    3_600,
                    max(0, value.foregroundSeconds)
                ),
                category: value.category,
                opaqueAppToken: value.opaqueAppToken,
                coverageSeconds: privacyTaintedBuckets.contains(
                    value.bucketStart
                )
                    ? nil
                    : 3_600
            )
        }
        let coverageSamples = ScreenTimeCoveragePlanner.confirmedZeroHourStarts(
            in: window,
            confirmedZeroBuckets: confirmedZeroBuckets
        )
            .map { bucketStart in
                ScreenTimeActivitySample(
                    sourceRecordID: pseudonymizer.coverageRecordID(
                        bucketStart: bucketStart
                    ),
                    bucketStart: bucketStart,
                    foregroundSeconds: 0,
                    category: nil,
                    opaqueAppToken: nil,
                    coverageSeconds: 3_600,
                    coverageOnly: true
                )
            }
        return (activitySamples + coverageSamples)
            .sorted {
                if $0.bucketStart != $1.bucketStart {
                    return $0.bucketStart < $1.bucketStart
                }
                return ($0.opaqueAppToken ?? "")
                    < ($1.opaqueAppToken ?? "")
            }
    }
}

enum ScreenTimeSyncPlanner {
    static let authoritativeLookbackHours = 48
    static let retentionUploadSafetyMarginHours = 1

    static func skipReason(
        state: ScreenTimeCollectionState,
        now: Date
    ) -> String? {
        guard state.enabled else {
            return "collection_disabled"
        }
        if let pausedUntil = state.pausedUntil, pausedUntil > now {
            return "collection_paused"
        }
        // A denied/unavailable server snapshot may be stale. The device must
        // recollect so a later local authorization grant can recover it.
        return nil
    }

    static func completedHourWindow(
        now: Date,
        timezone: TimeZone,
        retentionCutoff: Date?,
        earliestCollectionStart: Date? = nil
    ) throws -> ScreenTimeCollectionWindow {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        guard
            let end = calendar.dateInterval(of: .hour, for: now)?.start,
            let lookbackStart = calendar.date(
                byAdding: .hour,
                value: -authoritativeLookbackHours,
                to: end
            ),
            lookbackStart < end
        else {
            throw ScreenTimeSyncError.invalidCompletedHourWindow
        }

        var start = lookbackStart
        if let retentionCutoff {
            guard
                let cutoffHour = calendar.dateInterval(
                    of: .hour,
                    for: retentionCutoff
                )
            else {
                throw ScreenTimeSyncError.invalidCompletedHourWindow
            }
            let retainedStart = (
                retentionCutoff <= cutoffHour.start
                    ? cutoffHour.start
                    : cutoffHour.end
            )
            guard
                let safeRetainedStart = calendar.date(
                    byAdding: .hour,
                    value: retentionUploadSafetyMarginHours,
                    to: retainedStart
                )
            else {
                throw ScreenTimeSyncError.invalidCompletedHourWindow
            }
            start = max(start, safeRetainedStart)
        }
        if let earliestCollectionStart {
            start = max(start, earliestCollectionStart)
        }
        guard start < end else {
            throw ScreenTimeSyncError.invalidCompletedHourWindow
        }
        return ScreenTimeCollectionWindow(
            start: start,
            end: end,
            timezone: timezone
        )
    }

    static func shouldResetSnapshotFence(
        _ error: HealthMesAPIError
    ) -> Bool {
        guard
            case .server(
                409,
                "activity_snapshot_fence_reset_required",
                _,
                _
            ) = error
        else {
            return false
        }
        return true
    }
}

actor ScreenTimeSyncStateStore {
    static let shared = ScreenTimeSyncStateStore()

    private let defaults: UserDefaults

    init(defaults: UserDefaults = AppGroup.userDefaults) {
        self.defaults = defaults
    }

    func preparePseudonymBoundary(
        deviceID: String,
        pseudonymKeyID: String?,
        excludedAppTokens: Set<String>,
        now: Date
    ) -> ScreenTimePseudonymBoundary {
        guard let pseudonymKeyID else {
            return .accepted
        }
        let keyIDKey = defaultsKey(
            "pseudonym-key-id",
            deviceID: deviceID
        )
        let approvedDigestKey = defaultsKey(
            "approved-exclusions-digest",
            deviceID: deviceID
        )
        let previousKeyID = defaults.string(forKey: keyIDKey)
        if let previousKeyID, previousKeyID != pseudonymKeyID {
            advanceCollectionGeneration(
                deviceID: deviceID,
                now: now
            )
            defaults.removeObject(forKey: approvedDigestKey)
        } else if previousKeyID == nil {
            ensureCollectionGeneration(
                deviceID: deviceID,
                now: now
            )
        }
        defaults.set(pseudonymKeyID, forKey: keyIDKey)

        if excludedAppTokens.isEmpty {
            defaults.removeObject(forKey: approvedDigestKey)
            return .accepted
        }
        guard
            let expectedDigest = exclusionApprovalDigest(
                pseudonymKeyID: pseudonymKeyID,
                excludedAppTokens: excludedAppTokens
            )
        else {
            return ScreenTimePseudonymBoundary(
                requiresExclusionReapproval: true,
                reason: "ios_screen_time_exclusions_invalid"
            )
        }
        guard
            defaults.string(forKey: approvedDigestKey) == expectedDigest
        else {
            return ScreenTimePseudonymBoundary(
                requiresExclusionReapproval: true,
                reason: "ios_screen_time_exclusions_require_"
                    + "reapproval_after_key_change"
            )
        }
        return .accepted
    }

    func approveExcludedApps(
        deviceID: String,
        pseudonymKeyID: String,
        excludedAppTokens: Set<String>
    ) throws {
        let keyIDKey = defaultsKey(
            "pseudonym-key-id",
            deviceID: deviceID
        )
        guard defaults.string(forKey: keyIDKey) == pseudonymKeyID else {
            throw ScreenTimePseudonymBoundaryError.pseudonymKeyChanged
        }
        let approvedDigestKey = defaultsKey(
            "approved-exclusions-digest",
            deviceID: deviceID
        )
        guard !excludedAppTokens.isEmpty else {
            defaults.removeObject(forKey: approvedDigestKey)
            return
        }
        guard
            let digest = exclusionApprovalDigest(
                pseudonymKeyID: pseudonymKeyID,
                excludedAppTokens: excludedAppTokens
            )
        else {
            throw ScreenTimePseudonymBoundaryError.invalidExcludedAppToken
        }
        defaults.set(digest, forKey: approvedDigestKey)
    }

    func proposedTimezoneBoundary(
        deviceID: String,
        timezone: TimeZone,
        now: Date
    ) throws -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        guard
            let completedEnd = calendar.dateInterval(
                of: .hour,
                for: now
            )?.start,
            let latestCompletedStart = calendar.date(
                byAdding: .hour,
                value: -1,
                to: completedEnd
            )
        else {
            throw ScreenTimeSyncError.invalidCompletedHourWindow
        }
        let timezoneKey = defaultsKey(
            "collection-timezone",
            deviceID: deviceID
        )
        let boundaryKey = defaultsKey(
            "timezone-boundary",
            deviceID: deviceID
        )
        let previousTimezone = defaults.string(forKey: timezoneKey)
        let previousPermission = defaults.string(
            forKey: defaultsKey(
                "permission-status",
                deviceID: deviceID
            )
        )
        if previousTimezone == timezone.identifier,
            previousPermission
                == ScreenTimeActivityPermissionStatus.granted.rawValue,
            let stored = defaults.object(forKey: boundaryKey) as? NSNumber
        {
            return Date(timeIntervalSince1970: stored.doubleValue)
        }
        return latestCompletedStart
    }

    func acceptTimezoneBoundary(
        deviceID: String,
        timezone: TimeZone,
        now: Date
    ) throws -> Date {
        let accepted = try proposedTimezoneBoundary(
            deviceID: deviceID,
            timezone: timezone,
            now: now
        )
        let timezoneKey = defaultsKey(
            "collection-timezone",
            deviceID: deviceID
        )
        let boundaryKey = defaultsKey(
            "timezone-boundary",
            deviceID: deviceID
        )
        let previousTimezone = defaults.string(forKey: timezoneKey)
        if previousTimezone != nil {
            if previousTimezone != timezone.identifier {
                advanceCollectionGeneration(
                    deviceID: deviceID,
                    now: now
                )
            }
        } else {
            ensureCollectionGeneration(
                deviceID: deviceID,
                now: now
            )
        }
        defaults.set(timezone.identifier, forKey: timezoneKey)
        defaults.set(
            accepted.timeIntervalSince1970,
            forKey: boundaryKey
        )
        return accepted
    }

    func collectionGeneration(
        deviceID: String,
        permissionStatus: ScreenTimeActivityPermissionStatus,
        now: Date
    ) -> Int {
        let key = defaultsKey("generation", deviceID: deviceID)
        let permissionKey = defaultsKey(
            "permission-status",
            deviceID: deviceID
        )
        let existing = defaults.object(forKey: key) as? NSNumber
        let previousPermission = defaults.string(forKey: permissionKey)
        let timestamp = max(
            1,
            Int((now.timeIntervalSince1970 * 1_000).rounded())
        )
        if let existing, existing.intValue > 0 {
            let current = existing.intValue
            if (
                previousPermission != nil
                    && previousPermission != permissionStatus.rawValue
            ) {
                let advanced = max(current + 1, timestamp)
                defaults.set(advanced, forKey: key)
                defaults.set(
                    permissionStatus.rawValue,
                    forKey: permissionKey
                )
                return advanced
            }
            defaults.set(permissionStatus.rawValue, forKey: permissionKey)
            return current
        }
        defaults.set(timestamp, forKey: key)
        defaults.set(permissionStatus.rawValue, forKey: permissionKey)
        return timestamp
    }

    func allocateSnapshotSequence(deviceID: String) -> Int {
        let key = defaultsKey("sequence", deviceID: deviceID)
        let previous = defaults.object(forKey: key) as? NSNumber
        let next = max(0, previous?.intValue ?? 0) + 1
        // Persist before network I/O. A timeout may have committed remotely;
        // skipping a sequence is safe, reusing it with changed data is not.
        defaults.set(next, forKey: key)
        return next
    }

    private func ensureCollectionGeneration(
        deviceID: String,
        now: Date
    ) {
        let key = defaultsKey("generation", deviceID: deviceID)
        guard defaults.object(forKey: key) == nil else {
            return
        }
        let timestamp = max(
            1,
            Int((now.timeIntervalSince1970 * 1_000).rounded())
        )
        defaults.set(timestamp, forKey: key)
    }

    private func advanceCollectionGeneration(
        deviceID: String,
        now: Date
    ) {
        let key = defaultsKey("generation", deviceID: deviceID)
        let current = (
            defaults.object(forKey: key) as? NSNumber
        )?.intValue ?? 0
        let timestamp = max(
            1,
            Int((now.timeIntervalSince1970 * 1_000).rounded())
        )
        defaults.set(max(current + 1, timestamp), forKey: key)
    }

    private func exclusionApprovalDigest(
        pseudonymKeyID: String,
        excludedAppTokens: Set<String>
    ) -> String? {
        let sortedTokens = excludedAppTokens.sorted()
        guard sortedTokens.allSatisfy({
            Self.isValidAppToken(
                $0,
                pseudonymKeyID: pseudonymKeyID
            )
        }) else {
            return nil
        }
        var material = Data(
            "healthmes-screen-time-exclusions-v1\u{0}".utf8
        )
        material.append(Data(pseudonymKeyID.utf8))
        for token in sortedTokens {
            material.append(0)
            material.append(Data(token.utf8))
        }
        let digest = SHA256.hash(data: material)
        return "sha256:" + digest
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func isValidAppToken(
        _ value: String,
        pseudonymKeyID: String
    ) -> Bool {
        let keyPrefix = "ios-key-"
        guard
            pseudonymKeyID.count == keyPrefix.count + 40,
            pseudonymKeyID.hasPrefix(keyPrefix)
        else {
            return false
        }
        let keyFingerprint = pseudonymKeyID.dropFirst(keyPrefix.count)
        guard keyFingerprint.allSatisfy(Self.isLowerHex) else {
            return false
        }
        let tokenPrefix = "ios-app-v2-\(keyFingerprint)-"
        guard
            value.count == tokenPrefix.count + 40,
            value.hasPrefix(tokenPrefix)
        else {
            return false
        }
        return value.dropFirst(tokenPrefix.count).allSatisfy(Self.isLowerHex)
    }

    private static func isLowerHex(_ character: Character) -> Bool {
        ("0"..."9").contains(character) || ("a"..."f").contains(character)
    }

    private func defaultsKey(_ kind: String, deviceID: String) -> String {
        let digest = SHA256.hash(data: Data(deviceID.utf8))
        let suffix = digest.prefix(12)
            .map { String(format: "%02x", $0) }
            .joined()
        return "healthmes.screen-time.\(kind).\(suffix)"
    }
}

public struct ScreenTimeActivityBatchResult: Codable, Equatable {
    public let accepted: Int
    public let created: Int
    public let updated: Int
    public let duplicates: Int
    public let excluded: Int
    public let tombstoned: Int
    public let affectedDates: [String]

    enum CodingKeys: String, CodingKey {
        case accepted
        case created
        case updated
        case duplicates
        case excluded
        case tombstoned
        case affectedDates = "affected_dates"
    }
}

public enum ScreenTimeActivityHTTP {
    public static func collectionRequest(
        pairing: Pairing,
        deviceID: String
    ) -> URLRequest {
        let url = appendingPathComponents(
            ["v1", "activity", "devices", deviceID, "collection"],
            to: pairing.baseURL
        )
        var components = URLComponents(
            url: url,
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "platform", value: "ios")
        ]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    public static func reportRequest(
        pairing: Pairing,
        report: ScreenTimeActivityReport
    ) throws -> URLRequest {
        let url = appendingPathComponents(
            ["v1", "activity", "ios", "report"],
            to: pairing.baseURL
        )
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.httpBody = try encoder().encode(report)
        return request
    }

    public static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(iso8601(date))
        }
        return encoder
    }

    private static func iso8601(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        return formatter.string(from: date)
    }

    private static func appendingPathComponents(
        _ pathComponents: [String],
        to baseURL: URL
    ) -> URL {
        var components = URLComponents(
            url: baseURL,
            resolvingAgainstBaseURL: false
        )!
        var path = components.percentEncodedPath
        if path.hasSuffix("/") {
            path.removeLast()
        }
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/")
        for component in pathComponents {
            let encoded = component.addingPercentEncoding(
                withAllowedCharacters: allowed
            )!
            path += "/\(encoded)"
        }
        components.percentEncodedPath = path
        return components.url!
    }
}

public struct ScreenTimeAppPseudonymizer {
    private let key: SymmetricKey
    public let keyID: String

    public init(keyData: Data) {
        key = SymmetricKey(data: keyData)
        var material = Data(
            "healthmes-screen-time-key-id-v1\u{0}".utf8
        )
        material.append(keyData)
        let digest = SHA256.hash(data: material)
        keyID = "ios-key-\(Self.hex(digest.prefix(20)))"
    }

    public func appToken(bundleIdentifier: String) -> String {
        let normalized = bundleIdentifier
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let digest = HMAC<SHA256>.authenticationCode(
            for: Data(normalized.utf8),
            using: key
        )
        let keyFingerprint = keyID.dropFirst("ios-key-".count)
        return "ios-app-v2-\(keyFingerprint)-"
            + Self.hex(digest.prefix(20))
    }

    public func sourceRecordID(
        opaqueAppToken: String,
        bucketStart: Date
    ) -> String {
        let milliseconds = Int64(
            (bucketStart.timeIntervalSince1970 * 1_000).rounded()
        )
        let digest = SHA256.hash(
            data: Data("\(opaqueAppToken)|\(milliseconds)".utf8)
        )
        return "ios-hour-\(Self.hex(digest.prefix(20)))"
    }

    public func coverageRecordID(bucketStart: Date) -> String {
        sourceRecordID(
            opaqueAppToken: "__healthmes_coverage__",
            bucketStart: bucketStart
        )
    }

    public func categoryToken(encodedToken: Data) -> String {
        let digest = HMAC<SHA256>.authenticationCode(
            for: encodedToken,
            using: key
        )
        return "ios-category-\(Self.hex(digest.prefix(20)))"
    }

    private static func hex<Bytes: Sequence>(_ bytes: Bytes) -> String
    where Bytes.Element == UInt8 {
        bytes.map { String(format: "%02x", $0) }.joined()
    }
}

public enum ScreenTimeCategoryNormalizer {
    private static let mappings: [String: String] = [
        "business": "productivity",
        "creativity": "productivity",
        "education": "education",
        "entertainment": "entertainment",
        "finance": "finance",
        "games": "game",
        "health & fitness": "fitness",
        "information & reading": "research",
        "news": "news",
        "productivity & finance": "productivity",
        "shopping & food": "shopping",
        "social": "social",
        "social networking": "social",
        "travel": "travel",
        "utilities": "utilities",
        "video": "video",
        "games / game": "game",
        "social / networking": "social",
        "other": "other",
        "게임": "game",
        "건강 및 피트니스": "fitness",
        "교육": "education",
        "금융": "finance",
        "기타": "other",
        "뉴스": "news",
        "비디오": "video",
        "비즈니스": "productivity",
        "생산성 및 금융": "productivity",
        "쇼핑 및 음식": "shopping",
        "소셜": "social",
        "소셜 네트워킹": "social",
        "엔터테인먼트": "entertainment",
        "여행": "travel",
        "유틸리티": "utilities",
        "정보 및 독서": "research",
        "창의성": "productivity",
    ]

    public static func normalize(
        _ displayName: String?,
        opaqueFallback: String? = nil
    ) -> String {
        guard let displayName else {
            return opaqueFallback ?? "other"
        }
        let key = displayName
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return mappings[key] ?? opaqueFallback ?? "other"
    }
}
