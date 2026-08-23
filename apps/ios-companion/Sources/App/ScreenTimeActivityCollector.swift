import Foundation
import Security

#if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
import Combine
import DeviceActivity
import FamilyControls
import ManagedSettings
import SwiftUI
#endif

enum ScreenTimeAuthorizationChangeObserverFactory {
    @MainActor
    static func make() -> any ScreenTimeAuthorizationChangeObserving {
        #if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
        if #available(iOS 26.4, *) {
            return IOS264ScreenTimeAuthorizationChangeObserver()
        }
        #endif
        return UnavailableScreenTimeAuthorizationChangeObserver()
    }
}

@MainActor
private final class UnavailableScreenTimeAuthorizationChangeObserver:
    ScreenTimeAuthorizationChangeObserving
{
    func start(
        onChange _: @escaping @MainActor @Sendable () async -> Void
    ) {}
}

enum ScreenTimeActivityCollectorFactory {
    static func make(
        pseudonymKeyData: Data?
    ) -> any ScreenTimeActivityCollecting {
        guard let pseudonymKeyData else {
            return UnavailableScreenTimeActivityCollector(
                reason: "ios_screen_time_pseudonym_key_unavailable"
            )
        }
        #if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
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
        #elseif HEALTHMES_SCREENTIME_OPT_IN_REQUESTED
        return UnavailableScreenTimeActivityCollector(
            reason: "ios_screen_time_export_sdk_unavailable"
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
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        unavailable()
    }

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
    case keychainDeleteFailed
}

enum ScreenTimeAuthorizationFailurePolicy {
    static func reportableResult(
        current: ScreenTimeCollectorResult
    ) -> ScreenTimeCollectorResult {
        switch current.permissionStatus {
        case .denied, .restricted, .revoked, .unavailable:
            return current
        case .granted, .unknown:
            return ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .unavailable,
                reason: "ios_screen_time_authorization_failed",
                samples: []
            )
        }
    }
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

    func delete() throws {
        var unexpectedFailure = false
        for accessGroup in [
            AppGroup.keychainIdentifier,
            nil,
        ] {
            let status = SecItemDelete(
                baseQuery(accessGroup: accessGroup) as CFDictionary
            )
            if status != errSecSuccess,
                status != errSecItemNotFound,
                status != errSecMissingEntitlement
            {
                unexpectedFailure = true
            }
        }
        if unexpectedFailure {
            throw ScreenTimePseudonymKeyError.keychainDeleteFailed
        }
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

#if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
@available(iOS 26.4, *)
@MainActor
private final class IOS264ScreenTimeAuthorizationChangeObserver:
    ScreenTimeAuthorizationChangeObserving
{
    private var cancellable: AnyCancellable?

    func start(
        onChange: @escaping @MainActor @Sendable () async -> Void
    ) {
        guard cancellable == nil else { return }
        cancellable = AuthorizationCenter.shared
            .$authorizationStatus
            .dropFirst()
            .sink { _ in
                Task { @MainActor in
                    await onChange()
                }
            }
    }
}

@available(iOS 26.4, *)
struct IOS264ScreenTimeActivityCollector: ScreenTimeActivityCollecting {
    private struct UsageKey: Hashable {
        let bucketStart: Date
        let opaqueAppToken: String
        let category: String
    }

    private struct CategoryMetadata {
        let localizedDisplayName: String?
        let opaqueToken: String
    }

    private let pseudonymizer: ScreenTimeAppPseudonymizer
    var pseudonymKeyID: String? { pseudonymizer.keyID }

    init(pseudonymizer: ScreenTimeAppPseudonymizer) {
        self.pseudonymizer = pseudonymizer
    }

    @MainActor
    func currentAuthorizationStatus() async
        -> ScreenTimeCollectorResult
    {
        statusResult()
    }

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        do {
            try await AuthorizationCenter.shared.requestAuthorization(
                for: .individual
            )
            return statusResult()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            return ScreenTimeAuthorizationFailurePolicy
                .reportableResult(current: statusResult())
        }
    }

    func collect(
        window: ScreenTimeCollectionWindow,
        excludedAppTokens: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        let status = await statusResult()
        guard status.permissionStatus == .granted else {
            return status
        }
        do {
            return try await collectAuthorized(
                window: window,
                excludedAppTokens: excludedAppTokens
            )
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as DeviceActivityData.Error {
            return try ScreenTimeActivityCollectionFailurePolicy.result(
                for: collectionFailure(for: error)
            )
        } catch {
            let current = await statusResult()
            if let authorization =
                ScreenTimeActivityCollectionFailurePolicy
                    .authorizationResultAfterUnexpectedFailure(
                        current: current
                    )
            {
                return authorization
            }
            throw error
        }
    }

    private func collectAuthorized(
        window: ScreenTimeCollectionWindow,
        excludedAppTokens: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        // Omitting users and devices scopes the export to the current person
        // and this iPhone. Using `.all` would mix Share Across Devices data
        // into this installation's device ID.
        let filter = DeviceActivityFilter(
            segment: .hourly(
                during: DateInterval(start: window.start, end: window.end)
            )
        )
        let activityData = FamilyActivityData.shared
        let installedApplications =
            try await activityData.installedApplications
        let activityCategories =
            try await activityData.activityCategories
        let applicationBundleIDs =
            Dictionary(
                uniqueKeysWithValues:
                    installedApplications.compactMap {
                        application
                            -> (ApplicationToken, String)? in
                        guard
                            let token = application.token,
                            let bundleIdentifier =
                                application.bundleIdentifier
                        else {
                            return nil
                        }
                        return (token, bundleIdentifier)
                    }
            )
        let categoryMetadata =
            Dictionary(
                uniqueKeysWithValues:
                    activityCategories.compactMap {
                        category
                            -> (ActivityCategoryToken, CategoryMetadata)? in
                        guard
                            let token = category.token,
                            let encodedToken =
                                try? JSONEncoder().encode(token)
                        else {
                            return nil
                        }
                        return (
                            token,
                            CategoryMetadata(
                                localizedDisplayName:
                                    category.localizedDisplayName,
                                opaqueToken:
                                    pseudonymizer.categoryToken(
                                        encodedToken: encodedToken
                                    )
                            )
                        )
                    }
            )
        var usage: [UsageKey: Int] = [:]
        var observedBucketStarts = Set<Date>()
        var confirmedZeroBuckets = Set<Date>()
        var bucketObservations:
            [Date: ScreenTimeActivityBucketObservation] = [:]
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
                observedBucketStarts.insert(bucketStart)
                let segmentSeconds = max(
                    0,
                    Int(
                        segment.totalActivityDuration
                            .rounded(.toNearestOrAwayFromZero)
                    )
                )
                var knownApplicationSeconds = 0
                var representedAppSeconds = 0
                var privacyFilteredSeconds = 0
                var unresolvedApplicationSeconds = 0
                var websiteActivitySeconds = 0
                for try await categoryActivity in segment.categories {
                    let category: String
                    if
                        let categoryToken =
                            categoryActivity.category.token,
                        let metadata =
                            categoryMetadata[categoryToken]
                    {
                        category = ScreenTimeCategoryNormalizer
                            .normalize(
                                metadata.localizedDisplayName,
                                opaqueFallback: metadata.opaqueToken
                            )
                    } else if
                        let categoryToken =
                            categoryActivity.category.token,
                        let encodedToken =
                            try? JSONEncoder().encode(categoryToken)
                    {
                        category = ScreenTimeCategoryNormalizer
                            .normalize(
                                nil,
                                opaqueFallback:
                                    pseudonymizer.categoryToken(
                                        encodedToken: encodedToken
                                    )
                            )
                    } else {
                        category = "other"
                    }
                    for try await appActivity in categoryActivity.applications {
                        let seconds = max(
                            0,
                            Int(
                                appActivity.totalActivityDuration
                                    .rounded(.toNearestOrAwayFromZero)
                            )
                        )
                        guard seconds > 0 else { continue }
                        knownApplicationSeconds += seconds
                        guard
                            let applicationToken =
                                appActivity.application.token,
                            let bundleIdentifier =
                                applicationBundleIDs[applicationToken]
                        else {
                            unresolvedApplicationSeconds += seconds
                            continue
                        }
                        let token = pseudonymizer.appToken(
                            bundleIdentifier: bundleIdentifier
                        )
                        guard !excludedAppTokens.contains(token) else {
                            privacyFilteredSeconds += seconds
                            continue
                        }
                        representedAppSeconds += seconds
                        let key = UsageKey(
                            bucketStart: bucketStart,
                            opaqueAppToken: token,
                            category: category
                        )
                        usage[key, default: 0] += seconds
                    }
                    for try await websiteActivity
                        in categoryActivity.webDomains
                    {
                        websiteActivitySeconds += max(
                            0,
                            Int(
                                websiteActivity.totalActivityDuration
                                    .rounded(.toNearestOrAwayFromZero)
                            )
                        )
                    }
                }

                let activityNotRepresentedByApplications = max(
                    0,
                    segmentSeconds
                        - min(segmentSeconds, knownApplicationSeconds)
                )
                let websiteOnlySeconds = min(
                    activityNotRepresentedByApplications,
                    websiteActivitySeconds
                )
                let otherwiseUnknownSeconds = max(
                    0,
                    activityNotRepresentedByApplications
                        - websiteOnlySeconds
                )
                let observation = ScreenTimeActivityBucketObservation(
                    bucketStart: bucketStart,
                    observedActivitySeconds: segmentSeconds,
                    representedAppSeconds: representedAppSeconds,
                    privacyFilteredSeconds: privacyFilteredSeconds,
                    websiteActivitySeconds: websiteOnlySeconds,
                    unknownActivitySeconds:
                        unresolvedApplicationSeconds
                        + otherwiseUnknownSeconds
                )
                if let existing = bucketObservations[bucketStart] {
                    bucketObservations[bucketStart] =
                        existing.merging(observation)
                } else {
                    bucketObservations[bucketStart] = observation
                }
                if
                    segmentSeconds == 0,
                    representedAppSeconds == 0,
                    privacyFilteredSeconds == 0,
                    websiteOnlySeconds == 0,
                    unresolvedApplicationSeconds == 0,
                    otherwiseUnknownSeconds == 0
                {
                    confirmedZeroBuckets.insert(bucketStart)
                }
            }
        }

        let accumulatedUsage = usage.compactMap { key, seconds in
            return ScreenTimeAccumulatedUsage(
                bucketStart: key.bucketStart,
                opaqueAppToken: key.opaqueAppToken,
                category: key.category,
                foregroundSeconds: seconds
            )
        }
        let completeSamples = ScreenTimeSamplePlanner.samples(
            usage: accumulatedUsage,
            confirmedZeroBuckets: confirmedZeroBuckets,
            bucketObservations: bucketObservations,
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
            authoritativeBucketStarts: observedBucketStarts
        )
    }

    private func collectionFailure(
        for error: DeviceActivityData.Error
    ) -> ScreenTimeActivityCollectionFailure {
        switch error {
        case .unauthorized:
            return .unauthorized
        case .unavailable:
            return .unavailable
        case .missingData:
            return .transient
        @unknown default:
            return .transient
        }
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
