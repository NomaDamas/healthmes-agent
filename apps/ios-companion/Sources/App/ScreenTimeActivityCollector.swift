import Foundation
import Security

#if HEALTHMES_IOS_26_4_SCREENTIME_EXPORT
import DeviceActivity
import FamilyControls
#endif

enum ScreenTimeActivityCollectorFactory {
    static func make(
        pseudonymKeyData: Data?
    ) -> any ScreenTimeActivityCollecting {
        guard let pseudonymKeyData else {
            return UnavailableScreenTimeActivityCollector(
                reason: "ios_screen_time_pseudonym_key_unavailable"
            )
        }
        #if HEALTHMES_IOS_26_4_SCREENTIME_EXPORT
        if #available(iOS 26.4, *) {
            return IOS264ScreenTimeActivityCollector(
                pseudonymizer: ScreenTimeAppPseudonymizer(
                    keyData: pseudonymKeyData
                )
            )
        }
        return UnavailableScreenTimeActivityCollector(
            reason: "ios_screen_time_export_requires_ios_26_4"
        )
        #else
        return UnavailableScreenTimeActivityCollector(
            reason: "ios_screen_time_normal_build_unavailable"
        )
        #endif
    }
}

struct UnavailableScreenTimeActivityCollector: ScreenTimeActivityCollecting {
    let reason: String

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        unavailable()
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        unavailable()
    }

    private func unavailable() -> ScreenTimeCollectorResult {
        ScreenTimeCollectorResult(
            capability: .unavailable,
            permissionStatus: .unavailable,
            reason: reason,
            samples: []
        )
    }
}

enum ScreenTimePseudonymKeyError: Error {
    case randomGenerationFailed
    case keychainWriteFailed
}

struct ScreenTimePseudonymKeyStore {
    private let service = "com.healthmes.companion.screen-time"
    private let account = "app-pseudonym-key-v1"

    func loadOrCreate() throws -> Data {
        if let existing = read(accessGroup: AppGroup.keychainIdentifier)
            ?? read(accessGroup: nil)
        {
            return existing
        }

        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw ScreenTimePseudonymKeyError.randomGenerationFailed
        }
        let data = Data(bytes)
        if let stored = add(
            data,
            accessGroup: AppGroup.keychainIdentifier
        ) {
            return stored
        }
        if let stored = add(data, accessGroup: nil) {
            return stored
        }
        throw ScreenTimePseudonymKeyError.keychainWriteFailed
    }

    private func read(accessGroup: String?) -> Data? {
        var query = baseQuery(accessGroup: accessGroup)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard
            SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data,
            data.count >= 32
        else {
            return nil
        }
        return data
    }

    private func add(_ data: Data, accessGroup: String?) -> Data? {
        var attributes = baseQuery(accessGroup: accessGroup)
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] =
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem {
            return read(accessGroup: accessGroup)
        }
        return status == errSecSuccess ? data : nil
    }

    private func baseQuery(accessGroup: String?) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}

#if HEALTHMES_IOS_26_4_SCREENTIME_EXPORT
@available(iOS 26.4, *)
struct IOS264ScreenTimeActivityCollector: ScreenTimeActivityCollecting {
    private struct UsageKey: Hashable {
        let bucketStart: Date
        let opaqueAppToken: String
        let category: String
    }

    private let pseudonymizer: ScreenTimeAppPseudonymizer
    var pseudonymKeyID: String? { pseudonymizer.keyID }

    init(pseudonymizer: ScreenTimeAppPseudonymizer) {
        self.pseudonymizer = pseudonymizer
    }

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        try await AuthorizationCenter.shared.requestAuthorization(
            for: .individual
        )
        return statusResult()
    }

    func collect(
        window: ScreenTimeCollectionWindow,
        excludedAppTokens: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        let status = await statusResult()
        guard status.permissionStatus == .granted else {
            return status
        }

        let filter = DeviceActivityFilter(
            segment: .hourly(
                during: DateInterval(start: window.start, end: window.end)
            ),
            users: .all,
            devices: .init([.iPhone])
        )
        var usage: [UsageKey: Int] = [:]
        var authoritativeBucketStarts = Set<Date>()
        var confirmedZeroBuckets = Set<Date>()
        var privacyTaintedBuckets = Set<Date>()
        let results = DeviceActivityData.activityData(
            filteredBy: filter,
            using: .live
        )
        for try await activity in results {
            for try await segment in activity.activitySegments {
                let bucketStart = segment.dateInterval.start
                guard bucketStart >= window.start, bucketStart < window.end else {
                    continue
                }
                authoritativeBucketStarts.insert(bucketStart)
                let segmentSeconds = max(
                    0,
                    Int(
                        segment.totalActivityDuration
                            .rounded(.toNearestOrAwayFromZero)
                    )
                )
                if segmentSeconds == 0 {
                    confirmedZeroBuckets.insert(bucketStart)
                    continue
                }
                var attributedSeconds = 0
                for try await categoryActivity in segment.categories {
                    let opaqueCategoryToken = categoryActivity.category.token
                        .flatMap { token in
                            guard
                                let encoded = try? JSONEncoder().encode(token)
                            else {
                                return nil
                            }
                            return pseudonymizer.categoryToken(
                                encodedToken: encoded
                            )
                        }
                    let category = ScreenTimeCategoryNormalizer.normalize(
                        categoryActivity.category.localizedDisplayName,
                        opaqueFallback: opaqueCategoryToken
                    )
                    for try await appActivity in categoryActivity.applications {
                        let seconds = max(
                            0,
                            Int(
                                appActivity.totalActivityDuration
                                    .rounded(.toNearestOrAwayFromZero)
                            )
                        )
                        guard seconds > 0 else { continue }
                        guard
                            let bundleIdentifier =
                                appActivity.application.bundleIdentifier
                        else {
                            privacyTaintedBuckets.insert(bucketStart)
                            continue
                        }
                        let token = pseudonymizer.appToken(
                            bundleIdentifier: bundleIdentifier
                        )
                        guard !excludedAppTokens.contains(token) else {
                            privacyTaintedBuckets.insert(bucketStart)
                            continue
                        }
                        let key = UsageKey(
                            bucketStart: bucketStart,
                            opaqueAppToken: token,
                            category: category
                        )
                        usage[key, default: 0] += seconds
                        attributedSeconds += seconds
                    }
                }
                if attributedSeconds < segmentSeconds {
                    privacyTaintedBuckets.insert(bucketStart)
                }
            }
        }

        let accumulatedUsage = usage.map { key, seconds in
            ScreenTimeAccumulatedUsage(
                bucketStart: key.bucketStart,
                opaqueAppToken: key.opaqueAppToken,
                category: key.category,
                foregroundSeconds: seconds
            )
        }
        let completeSamples = ScreenTimeSamplePlanner.samples(
            usage: accumulatedUsage,
            confirmedZeroBuckets: confirmedZeroBuckets,
            privacyTaintedBuckets: privacyTaintedBuckets,
            window: window,
            pseudonymizer: pseudonymizer
        )

        guard completeSamples.count <= 5_000 else {
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .granted,
                reason: "ios_screen_time_snapshot_exceeds_upload_limit",
                samples: []
            )
        }
        return ScreenTimeCollectorResult(
            capability: .aggregate,
            permissionStatus: .granted,
            reason: nil,
            samples: completeSamples,
            authoritativeBucketStarts: authoritativeBucketStarts
        )
    }

    @MainActor
    private func statusResult() -> ScreenTimeCollectorResult {
        switch AuthorizationCenter.shared.authorizationStatus {
        case .approvedWithDataAccess:
            return ScreenTimeCollectorResult(
                capability: .aggregate,
                permissionStatus: .granted,
                reason: nil,
                samples: []
            )
        case .approved:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .restricted,
                reason: "ios_screen_time_data_access_not_approved",
                samples: []
            )
        case .denied:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .denied,
                reason: "ios_screen_time_permission_denied",
                samples: []
            )
        case .notDetermined:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .unknown,
                reason: "ios_screen_time_permission_not_determined",
                samples: []
            )
        @unknown default:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .unavailable,
                reason: "ios_screen_time_authorization_status_unknown",
                samples: []
            )
        }
    }
}
#endif
