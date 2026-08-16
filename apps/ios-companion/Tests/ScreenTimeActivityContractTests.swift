import XCTest

private enum ScreenTimeCollectorTestError: Error {
    case unexpectedCollection
    case pseudonymKeyUnavailable
}

private func iosKeyID(_ fingerprint: String = "1") -> String {
    "ios-key-" + String(repeating: fingerprint, count: 40)
}

private func iosAppToken(
    keyFingerprint: String = "1",
    appDigest: String = "a"
) -> String {
    "ios-app-v2-"
        + String(repeating: keyFingerprint, count: 40)
        + "-"
        + String(repeating: appDigest, count: 40)
}

private struct FixedScreenTimeCollector: ScreenTimeActivityCollecting {
    let result: ScreenTimeCollectorResult
    let pseudonymKeyID: String?

    init(
        result: ScreenTimeCollectorResult,
        pseudonymKeyID: String? = nil
    ) {
        self.result = result
        self.pseudonymKeyID = pseudonymKeyID
    }

    @MainActor
    func currentAuthorizationStatus() async -> ScreenTimeCollectorResult {
        result
    }

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        result
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        result
    }
}

private struct RejectingScreenTimeCollector: ScreenTimeActivityCollecting {
    let pseudonymKeyID: String?

    init(pseudonymKeyID: String? = nil) {
        self.pseudonymKeyID = pseudonymKeyID
    }

    @MainActor
    func currentAuthorizationStatus() async -> ScreenTimeCollectorResult {
        ScreenTimeCollectorResult(
            capability: .aggregate,
            permissionStatus: .granted,
            reason: nil,
            samples: []
        )
    }

    @MainActor
    func requestAuthorization() async throws -> ScreenTimeCollectorResult {
        throw ScreenTimeCollectorTestError.unexpectedCollection
    }

    func collect(
        window _: ScreenTimeCollectionWindow,
        excludedAppTokens _: Set<String>
    ) async throws -> ScreenTimeCollectorResult {
        throw ScreenTimeCollectorTestError.unexpectedCollection
    }
}

private actor RecordingScreenTimeTransport: ScreenTimeActivityTransport {
    private let state: ScreenTimeCollectionState
    private let result: ScreenTimeActivityBatchResult
    private let failFirstUploadWithFence: Bool
    private var reports: [ScreenTimeActivityReport] = []

    init(
        state: ScreenTimeCollectionState,
        failFirstUploadWithFence: Bool = false
    ) {
        self.state = state
        self.failFirstUploadWithFence = failFirstUploadWithFence
        result = ScreenTimeActivityBatchResult(
            accepted: 1,
            created: 1,
            updated: 0,
            duplicates: 0,
            excluded: 0,
            tombstoned: 0,
            affectedDates: ["2026-08-14"]
        )
    }

    func collectionState(
        pairing _: Pairing,
        deviceID _: String
    ) async throws -> ScreenTimeCollectionState {
        state
    }

    func upload(
        pairing _: Pairing,
        report: ScreenTimeActivityReport
    ) async throws -> ScreenTimeActivityBatchResult {
        reports.append(report)
        if failFirstUploadWithFence, reports.count == 1 {
            throw HealthMesAPIError.server(
                statusCode: 409,
                code: "activity_snapshot_fence_reset_required",
                message: "reset required",
                detail: nil
            )
        }
        return result
    }

    func capturedReports() -> [ScreenTimeActivityReport] {
        reports
    }
}

final class ScreenTimeActivityContractTests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "http://192.168.1.20:8100")!,
        token: "secret-token"
    )

    private func date(_ value: String) -> Date {
        ISO8601DateFormatter().date(from: value)!
    }

    private func collectionState(
        enabled: Bool = true,
        excludedApps: [String] = [],
        pausedUntil: Date? = nil,
        effectiveCollecting: Bool = true,
        blockedReason: String? = nil,
        rawRetentionCutoff: Date? = nil
    ) -> ScreenTimeCollectionState {
        ScreenTimeCollectionState(
            deviceID: "ios-test-device",
            enabled: enabled,
            excludedApps: excludedApps,
            pausedUntil: pausedUntil,
            effectiveCollecting: effectiveCollecting,
            blockedReason: blockedReason,
            configRevision: 3,
            rawRetentionCutoff: rawRetentionCutoff
        )
    }

    private func isolatedStateStore()
        -> (ScreenTimeSyncStateStore, UserDefaults, String)
    {
        let suite = "screen-time-service-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return (
            ScreenTimeSyncStateStore(defaults: defaults),
            defaults,
            suite
        )
    }

    func testAggregateReportUsesServerContractWithoutFakeLaunchCount() throws {
        let bucket = Date(timeIntervalSince1970: 1_786_612_500)
        let appToken = iosAppToken()
        let report = ScreenTimeActivityReport.aggregate(
            deviceID: "ios-test-device",
            timezone: "Asia/Kathmandu",
            pseudonymKeyID: iosKeyID(),
            collectedAt: bucket.addingTimeInterval(7_200),
            collectionRevision: 3,
            collectionGeneration: 1_786_612_500_000,
            snapshotSequence: 9,
            snapshotStart: bucket,
            snapshotEnd: bucket.addingTimeInterval(3_600),
            authoritativeBucketStarts: [bucket],
            samples: [
                ScreenTimeActivitySample(
                    sourceRecordID: "ios-hour-token",
                    bucketStart: bucket,
                    foregroundSeconds: 900,
                    category: "productivity",
                    opaqueAppToken: appToken,
                    coverageSeconds: 3_600
                )
            ]
        )

        let request = try ScreenTimeActivityHTTP.reportRequest(
            pairing: pairing,
            report: report
        )

        XCTAssertEqual(
            request.url?.absoluteString,
            "http://192.168.1.20:8100/v1/activity/ios/report"
        )
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"),
            "Bearer secret-token"
        )
        let body = try XCTUnwrap(request.httpBody)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        XCTAssertEqual(object["device_id"] as? String, "ios-test-device")
        XCTAssertEqual(object["permission_status"] as? String, "granted")
        XCTAssertEqual(object["snapshot_sequence"] as? Int, 9)
        let samples = try XCTUnwrap(object["samples"] as? [[String: Any]])
        XCTAssertEqual(samples.count, 1)
        XCTAssertEqual(samples[0]["foreground_seconds"] as? Int, 900)
        XCTAssertEqual(
            samples[0]["opaque_app_token"] as? String,
            appToken
        )
        XCTAssertNil(samples[0]["launches"])
    }

    func testUnavailableReportCannotCarryFakeZeroSamples() throws {
        let report = ScreenTimeActivityReport.unavailable(
            deviceID: "ios-unavailable",
            timezone: "UTC",
            permissionStatus: .unavailable,
            reason: "ios_screen_time_export_requires_ios_26_4",
            collectedAt: Date(timeIntervalSince1970: 1_786_612_500),
            collectionRevision: 4,
            collectionGeneration: 1_786_612_500_000
        )

        let data = try ScreenTimeActivityHTTP.encoder().encode(report)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )

        XCTAssertEqual(object["capability"] as? String, "unavailable")
        XCTAssertEqual((object["samples"] as? [Any])?.count, 0)
        XCTAssertNil(object["snapshot_start"])
        XCTAssertNil(object["snapshot_end"])
        XCTAssertNil(object["snapshot_sequence"])
        XCTAssertEqual(object["collection_revision"] as? Int, 4)
        XCTAssertEqual(
            object["collection_generation"] as? Int,
            1_786_612_500_000
        )
    }

    func testCoverageOnlySampleEncodesObservedZeroWithoutIdentity() throws {
        let bucket = Date(timeIntervalSince1970: 1_786_612_400)
        let sample = ScreenTimeActivitySample(
            sourceRecordID: "ios-coverage-hour",
            bucketStart: bucket,
            foregroundSeconds: 0,
            category: nil,
            opaqueAppToken: nil,
            coverageSeconds: 3_600,
            coverageOnly: true,
            coverageStatus: .complete,
            observedActivitySeconds: 0,
            representedAppSeconds: 0,
            privacyFilteredSeconds: 0,
            websiteActivitySeconds: 0,
            unknownActivitySeconds: 0
        )

        let data = try ScreenTimeActivityHTTP.encoder().encode(sample)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )

        XCTAssertEqual(object["coverage_only"] as? Bool, true)
        XCTAssertEqual(object["foreground_seconds"] as? Int, 0)
        XCTAssertEqual(object["coverage_seconds"] as? Int, 3_600)
        XCTAssertEqual(object["coverage_status"] as? String, "complete")
        XCTAssertEqual(object["observed_activity_seconds"] as? Int, 0)
        XCTAssertNil(object["category"])
        XCTAssertNil(object["opaque_app_token"])
    }

    func testCoveragePlannerDoesNotTurnPrivateActivityIntoZeroUsage() {
        let timezone = TimeZone(secondsFromGMT: 0)!
        let first = date("2026-08-14T09:00:00Z")
        let last = date("2026-08-14T11:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: first,
            end: date("2026-08-14T12:00:00Z"),
            timezone: timezone
        )

        let zeroUsageHours =
            ScreenTimeCoveragePlanner.confirmedZeroHourStarts(
            in: window,
            confirmedZeroBuckets: [first, last]
        )

        XCTAssertEqual(zeroUsageHours, [first, last])
    }

    func testSamplePlannerKeepsAllowedAppsAndMarksPrivateCoverage() {
        let key = Data(repeating: 0xAB, count: 32)
        let pseudonymizer = ScreenTimeAppPseudonymizer(keyData: key)
        let bucket = date("2026-08-14T09:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: bucket,
            end: bucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let token = pseudonymizer.appToken(
            bundleIdentifier: "com.example.allowed"
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [
                ScreenTimeAccumulatedUsage(
                    bucketStart: bucket,
                    opaqueAppToken: token,
                    category: "productivity",
                    foregroundSeconds: 900
                )
            ],
            confirmedZeroBuckets: [],
            bucketObservations: [
                bucket: ScreenTimeActivityBucketObservation(
                    bucketStart: bucket,
                    observedActivitySeconds: 1_500,
                    representedAppSeconds: 900,
                    privacyFilteredSeconds: 600,
                    websiteActivitySeconds: 0,
                    unknownActivitySeconds: 0
                )
            ],
            window: window,
            pseudonymizer: pseudonymizer
        )

        XCTAssertEqual(samples.count, 2)
        let marker = samples[0]
        let allowed = samples[1]
        XCTAssertTrue(marker.coverageOnly)
        XCTAssertEqual(marker.coverageStatus, .privacyFiltered)
        XCTAssertEqual(marker.observedActivitySeconds, 1_500)
        XCTAssertEqual(marker.representedAppSeconds, 900)
        XCTAssertEqual(marker.privacyFilteredSeconds, 600)
        XCTAssertEqual(allowed.foregroundSeconds, 900)
        XCTAssertNil(allowed.coverageSeconds)
        XCTAssertFalse(allowed.coverageOnly)
    }

    func testSamplePlannerMarksPrivateOnlyHourWithoutFalseZero() {
        let pseudonymizer = ScreenTimeAppPseudonymizer(
            keyData: Data(repeating: 0xAB, count: 32)
        )
        let bucket = date("2026-08-14T09:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: bucket,
            end: bucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [],
            confirmedZeroBuckets: [],
            bucketObservations: [
                bucket: ScreenTimeActivityBucketObservation(
                    bucketStart: bucket,
                    observedActivitySeconds: 600,
                    representedAppSeconds: 0,
                    privacyFilteredSeconds: 600,
                    websiteActivitySeconds: 0,
                    unknownActivitySeconds: 0
                )
            ],
            window: window,
            pseudonymizer: pseudonymizer
        )

        XCTAssertEqual(samples.count, 1)
        XCTAssertTrue(samples[0].coverageOnly)
        XCTAssertEqual(samples[0].coverageStatus, .privacyFiltered)
        XCTAssertEqual(samples[0].observedActivitySeconds, 600)
        XCTAssertEqual(samples[0].foregroundSeconds, 0)
        XCTAssertNil(samples[0].coverageSeconds)
    }

    func testSamplePlannerMarksWebsiteOnlyAndUnknownHours() {
        let pseudonymizer = ScreenTimeAppPseudonymizer(
            keyData: Data(repeating: 0xAB, count: 32)
        )
        let websiteBucket = date("2026-08-14T09:00:00Z")
        let unknownBucket = date("2026-08-14T10:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: websiteBucket,
            end: unknownBucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [],
            confirmedZeroBuckets: [],
            bucketObservations: [
                websiteBucket: ScreenTimeActivityBucketObservation(
                    bucketStart: websiteBucket,
                    observedActivitySeconds: 1_200,
                    representedAppSeconds: 0,
                    privacyFilteredSeconds: 0,
                    websiteActivitySeconds: 1_200,
                    unknownActivitySeconds: 0
                ),
                unknownBucket: ScreenTimeActivityBucketObservation(
                    bucketStart: unknownBucket,
                    observedActivitySeconds: 450,
                    representedAppSeconds: 0,
                    privacyFilteredSeconds: 0,
                    websiteActivitySeconds: 0,
                    unknownActivitySeconds: 450
                ),
            ],
            window: window,
            pseudonymizer: pseudonymizer
        )

        XCTAssertEqual(samples.map(\.coverageStatus), [
            .websiteActivity,
            .unknownActivity,
        ])
        XCTAssertEqual(samples[0].websiteActivitySeconds, 1_200)
        XCTAssertEqual(samples[1].unknownActivitySeconds, 450)
    }

    func testBucketObservationMarksMixedAndPartitionsObservedTime() {
        let bucket = date("2026-08-14T09:00:00Z")
        let observation = ScreenTimeActivityBucketObservation(
            bucketStart: bucket,
            observedActivitySeconds: 1_200,
            representedAppSeconds: 600,
            privacyFilteredSeconds: 300,
            websiteActivitySeconds: 200,
            unknownActivitySeconds: 0
        )

        XCTAssertEqual(observation.coverageStatus, .mixedPartial)
        XCTAssertEqual(
            observation.representedAppSeconds
                + observation.privacyFilteredSeconds
                + observation.websiteActivitySeconds
                + observation.unknownActivitySeconds,
            observation.observedActivitySeconds
        )
        XCTAssertEqual(observation.unknownActivitySeconds, 100)
    }

    func testBucketObservationPreservesRoundedPrivateCoverage() {
        let observation = ScreenTimeActivityBucketObservation(
            bucketStart: date("2026-08-14T09:00:00Z"),
            observedActivitySeconds: 1,
            representedAppSeconds: 1,
            privacyFilteredSeconds: 1,
            websiteActivitySeconds: 0,
            unknownActivitySeconds: 0
        )

        XCTAssertEqual(observation.observedActivitySeconds, 2)
        XCTAssertEqual(observation.representedAppSeconds, 1)
        XCTAssertEqual(observation.privacyFilteredSeconds, 1)
        XCTAssertEqual(observation.coverageStatus, .privacyFiltered)
    }

    func testSamplePlannerNormalizesRoundedHourWithoutDroppingApps()
        throws
    {
        let pseudonymizer = ScreenTimeAppPseudonymizer(
            keyData: Data(repeating: 0xAB, count: 32)
        )
        let bucket = date("2026-08-14T09:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: bucket,
            end: bucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let observation = ScreenTimeActivityBucketObservation(
            bucketStart: bucket,
            observedActivitySeconds: 3_600,
            representedAppSeconds: 3_601,
            privacyFilteredSeconds: 1,
            websiteActivitySeconds: 0,
            unknownActivitySeconds: 0
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [
                ScreenTimeAccumulatedUsage(
                    bucketStart: bucket,
                    opaqueAppToken: "allowed-app-a",
                    category: "productivity",
                    foregroundSeconds: 1_801
                ),
                ScreenTimeAccumulatedUsage(
                    bucketStart: bucket,
                    opaqueAppToken: "allowed-app-b",
                    category: "productivity",
                    foregroundSeconds: 1_800
                ),
            ],
            confirmedZeroBuckets: [],
            bucketObservations: [bucket: observation],
            window: window,
            pseudonymizer: pseudonymizer
        )

        let apps = samples.filter { !$0.coverageOnly }
        let marker = try XCTUnwrap(
            samples.first(where: \.coverageOnly)
        )

        XCTAssertEqual(apps.count, 2)
        XCTAssertTrue(apps.allSatisfy { $0.foregroundSeconds > 0 })
        XCTAssertEqual(
            apps.reduce(0) { $0 + $1.foregroundSeconds },
            3_599
        )
        XCTAssertEqual(marker.observedActivitySeconds, 3_600)
        XCTAssertEqual(marker.representedAppSeconds, 3_599)
        XCTAssertEqual(marker.privacyFilteredSeconds, 1)
    }

    func testSamplePlannerDoesNotEmitFalseZeroAfterMergedActivity() {
        let pseudonymizer = ScreenTimeAppPseudonymizer(
            keyData: Data(repeating: 0xAB, count: 32)
        )
        let bucket = date("2026-08-14T09:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: bucket,
            end: bucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let observation = ScreenTimeActivityBucketObservation(
            bucketStart: bucket,
            observedActivitySeconds: 900,
            representedAppSeconds: 900,
            privacyFilteredSeconds: 0,
            websiteActivitySeconds: 0,
            unknownActivitySeconds: 0
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [
                ScreenTimeAccumulatedUsage(
                    bucketStart: bucket,
                    opaqueAppToken: "allowed-app",
                    category: "productivity",
                    foregroundSeconds: 900
                )
            ],
            confirmedZeroBuckets: [bucket],
            bucketObservations: [bucket: observation],
            window: window,
            pseudonymizer: pseudonymizer
        )

        XCTAssertEqual(samples.count, 1)
        XCTAssertFalse(samples[0].coverageOnly)
        XCTAssertEqual(samples[0].foregroundSeconds, 900)
        XCTAssertEqual(samples[0].coverageSeconds, 3_600)
    }

    func testSamplePlannerEmitsCoverageOnlyForTrulyEmptyHour() {
        let pseudonymizer = ScreenTimeAppPseudonymizer(
            keyData: Data(repeating: 0xAB, count: 32)
        )
        let bucket = date("2026-08-14T09:00:00Z")
        let window = ScreenTimeCollectionWindow(
            start: bucket,
            end: bucket.addingTimeInterval(3_600),
            timezone: TimeZone(secondsFromGMT: 0)!
        )

        let samples = ScreenTimeSamplePlanner.samples(
            usage: [],
            confirmedZeroBuckets: [bucket],
            window: window,
            pseudonymizer: pseudonymizer
        )

        XCTAssertEqual(samples.count, 1)
        XCTAssertTrue(samples[0].coverageOnly)
        XCTAssertEqual(samples[0].coverageSeconds, 3_600)
    }

    func testPseudonymsAreStableKeyedAndDoNotRevealBundleIdentifier() {
        let key = Data(repeating: 0xAB, count: 32)
        let otherKey = Data(repeating: 0xCD, count: 32)
        let pseudonymizer = ScreenTimeAppPseudonymizer(keyData: key)
        let other = ScreenTimeAppPseudonymizer(keyData: otherKey)

        let first = pseudonymizer.appToken(
            bundleIdentifier: "com.example.PrivateApp"
        )
        let repeated = pseudonymizer.appToken(
            bundleIdentifier: " COM.EXAMPLE.PRIVATEAPP "
        )
        let differentKey = other.appToken(
            bundleIdentifier: "com.example.PrivateApp"
        )

        XCTAssertEqual(first, repeated)
        XCTAssertNotEqual(first, differentKey)
        XCTAssertEqual(
            pseudonymizer.keyID,
            ScreenTimeAppPseudonymizer(keyData: key).keyID
        )
        XCTAssertNotEqual(pseudonymizer.keyID, other.keyID)
        XCTAssertFalse(first.contains("example"))
        XCTAssertEqual(first.count, 92)
        XCTAssertTrue(
            first.hasPrefix(
                "ios-app-v2-"
                    + pseudonymizer.keyID.dropFirst("ios-key-".count)
                    + "-"
            )
        )

        let bucket = Date(timeIntervalSince1970: 1_786_612_500)
        XCTAssertEqual(
            pseudonymizer.sourceRecordID(
                opaqueAppToken: first,
                bucketStart: bucket
            ),
            pseudonymizer.sourceRecordID(
                opaqueAppToken: first,
                bucketStart: bucket
            )
        )
    }

    func testCollectionRequestScopesTheDevicePath() {
        let request = ScreenTimeActivityHTTP.collectionRequest(
            pairing: pairing,
            deviceID: "ios device/one"
        )

        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(
            request.url?.absoluteString,
            "http://192.168.1.20:8100/v1/activity/devices/"
                + "ios%20device%2Fone/collection?platform=ios"
        )
    }

    func testScreenTimeDeviceIdentityIsStableAndKeyScoped() {
        let key = Data(repeating: 0x11, count: 32)
        let otherKey = Data(repeating: 0x22, count: 32)

        let first = ScreenTimeDeviceIdentity.fromPseudonymKey(key)
        let repeated = ScreenTimeDeviceIdentity.fromPseudonymKey(key)
        let changed = ScreenTimeDeviceIdentity.fromPseudonymKey(otherKey)

        XCTAssertEqual(first, repeated)
        XCTAssertNotEqual(first, changed)
        XCTAssertTrue(first.hasPrefix("ios-collector-v1-"))
        XCTAssertEqual(first.count, 57)
    }

    func testUnavailableDeviceIdentityDoesNotPersistLocalIdentifier() {
        let suite = "screen-time-unavailable-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }

        let first = ScreenTimeActivityIdentityResolver
            .unavailableDeviceID
        let repeated = ScreenTimeActivityIdentityResolver
            .unavailableDeviceID

        XCTAssertEqual(first, repeated)
        XCTAssertEqual(first, "ios-collector-unavailable-v1")
        XCTAssertNil(
            defaults.string(
                forKey: "healthmes.screen-time.fallback-device-id.v1"
            )
        )
    }

    func testIdentityResolverLoadsPseudonymKeyExactlyOnce() {
        let suite = "screen-time-identity-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }
        let key = Data(repeating: 0x11, count: 32)
        var loadCount = 0

        let identity = ScreenTimeActivityIdentityResolver.resolve(
            explicitDeviceID: nil,
            pseudonymKeyLoader: {
                loadCount += 1
                return key
            }
        )

        XCTAssertEqual(loadCount, 1)
        XCTAssertEqual(identity.pseudonymKeyData, key)
        XCTAssertEqual(
            identity.deviceID,
            ScreenTimeDeviceIdentity.fromPseudonymKey(key)
        )
    }

    func testIdentityResolverUsesRememberedDeviceOnlyWhenKeyLoadFails() {
        let key = Data(repeating: 0x11, count: 32)
        let derived = ScreenTimeActivityIdentityResolver.resolve(
            explicitDeviceID: nil,
            pseudonymKeyLoader: { key },
            rememberedDeviceID: "remembered-device"
        )
        let recovered = ScreenTimeActivityIdentityResolver.resolve(
            explicitDeviceID: nil,
            pseudonymKeyLoader: {
                throw ScreenTimeCollectorTestError
                    .pseudonymKeyUnavailable
            },
            rememberedDeviceID: " remembered-device "
        )

        XCTAssertEqual(
            derived.deviceID,
            ScreenTimeDeviceIdentity.fromPseudonymKey(key)
        )
        XCTAssertEqual(recovered.deviceID, "remembered-device")
        XCTAssertNil(recovered.pseudonymKeyData)
    }

    func testCollectionStateDecodesForFutureSettingsUI() throws {
        let appToken = iosAppToken()
        let json = """
            {
              "device_id": "ios-test-device",
              "platform": "ios",
              "enabled": true,
              "excluded_apps": [
                "\(appToken)"
              ],
              "paused_until": null,
              "effective_collecting": true,
              "blocked_reason": null,
              "permission_status": "granted",
              "capability": "aggregate",
              "status_reason": null,
              "status_observed_at": null,
              "collection_generation": 123,
              "last_collected_at": null,
              "last_uploaded_at": null,
              "queue_oldest_at": null,
              "queue_age_seconds": null,
              "queue_depth": 0,
              "coverage": null,
              "config_revision": 4,
              "raw_retention_cutoff": "2026-08-13T00:00:00Z",
              "cursors": {}
            }
            """

        let state = try GlanceJSON.decoder().decode(
            ScreenTimeCollectionState.self,
            from: Data(json.utf8)
        )

        XCTAssertEqual(state.deviceID, "ios-test-device")
        XCTAssertEqual(
            state.excludedApps,
            [appToken]
        )
        XCTAssertTrue(state.effectiveCollecting)
        XCTAssertEqual(state.configRevision, 4)
        XCTAssertEqual(
            state.rawRetentionCutoff,
            date("2026-08-13T00:00:00Z")
        )
    }

    func testCategoryNormalizationSupportsKoreanAndOpaqueFallback() {
        let opaque = "ios-category-" + String(repeating: "a", count: 40)
        XCTAssertEqual(
            ScreenTimeCategoryNormalizer.normalize("게임"),
            "game"
        )
        XCTAssertEqual(
            ScreenTimeCategoryNormalizer.normalize("정보 및 독서"),
            "research"
        )
        XCTAssertEqual(
            ScreenTimeCategoryNormalizer.normalize(
                "알 수 없는 분류",
                opaqueFallback: opaque
            ),
            opaque
        )
        XCTAssertEqual(
            ScreenTimeCategoryNormalizer.normalize(
                nil,
                opaqueFallback: opaque
            ),
            opaque
        )
    }

    func testRetentionCutoffLeavesOneHourForUploadDelay() throws {
        let timezone = TimeZone(secondsFromGMT: 0)!
        let now = date("2026-08-14T12:34:00Z")

        let exactBoundary = try ScreenTimeSyncPlanner.completedHourWindow(
            now: now,
            timezone: timezone,
            retentionCutoff: date("2026-08-13T12:00:00Z")
        )
        XCTAssertEqual(
            exactBoundary.start,
            date("2026-08-13T13:00:00Z")
        )
        XCTAssertEqual(exactBoundary.end, date("2026-08-14T12:00:00Z"))

        let partialHour = try ScreenTimeSyncPlanner.completedHourWindow(
            now: now,
            timezone: timezone,
            retentionCutoff: date("2026-08-13T12:00:01Z")
        )
        XCTAssertEqual(
            partialHour.start,
            date("2026-08-13T14:00:00Z")
        )
        XCTAssertEqual(partialHour.end, date("2026-08-14T12:00:00Z"))
    }

    func testFirstCollectionUsesOneCompletedHourAcrossSpringForward()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let timezone = try XCTUnwrap(
            TimeZone(identifier: "America/Los_Angeles")
        )
        let now = date("2026-03-08T10:30:00Z")

        let boundary = try await stateStore.0.proposedTimezoneBoundary(
            deviceID: "ios-spring-forward",
            timezone: timezone,
            now: now
        )
        let window = try ScreenTimeSyncPlanner.completedHourWindow(
            now: now,
            timezone: timezone,
            retentionCutoff: nil,
            earliestCollectionStart: boundary
        )

        XCTAssertEqual(boundary, date("2026-03-08T09:00:00Z"))
        XCTAssertEqual(window.start, boundary)
        XCTAssertEqual(window.end, date("2026-03-08T10:00:00Z"))
        XCTAssertEqual(window.end.timeIntervalSince(window.start), 3_600)
    }

    func testFirstCollectionUsesOneCompletedHourAcrossFallBack()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let timezone = try XCTUnwrap(
            TimeZone(identifier: "America/Los_Angeles")
        )
        let now = date("2026-11-01T10:30:00Z")

        let boundary = try await stateStore.0.proposedTimezoneBoundary(
            deviceID: "ios-fall-back",
            timezone: timezone,
            now: now
        )
        let window = try ScreenTimeSyncPlanner.completedHourWindow(
            now: now,
            timezone: timezone,
            retentionCutoff: nil,
            earliestCollectionStart: boundary
        )

        XCTAssertEqual(boundary, date("2026-11-01T09:00:00Z"))
        XCTAssertEqual(window.start, boundary)
        XCTAssertEqual(window.end, date("2026-11-01T10:00:00Z"))
        XCTAssertEqual(window.end.timeIntervalSince(window.start), 3_600)
    }

    func testCompletedWindowPreservesLordHoweHalfHourFallbackBoundary()
        throws
    {
        let timezone = try XCTUnwrap(
            TimeZone(identifier: "Australia/Lord_Howe")
        )
        let window = try ScreenTimeSyncPlanner.completedHourWindow(
            now: date("2026-04-04T15:45:00Z"),
            timezone: timezone,
            retentionCutoff: nil,
            earliestCollectionStart: date(
                "2026-04-04T14:30:00Z"
            )
        )

        XCTAssertEqual(
            window.start,
            date("2026-04-04T14:30:00Z")
        )
        XCTAssertEqual(
            window.end,
            date("2026-04-04T15:30:00Z")
        )
    }

    func testTimezoneChangeAdvancesGenerationAndResetsCollectionBoundary()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let now = date("2026-08-14T12:34:00Z")
        let utc = TimeZone(secondsFromGMT: 0)!
        let kathmandu = try XCTUnwrap(
            TimeZone(identifier: "Asia/Kathmandu")
        )

        let initialBoundary =
            try await stateStore.0.acceptTimezoneBoundary(
                deviceID: "ios-timezone-change",
                timezone: utc,
                now: now
            )
        let initialGeneration =
            await stateStore.0.collectionGeneration(
                deviceID: "ios-timezone-change",
                permissionStatus: .granted,
                now: now
            )
        let repeatedBoundary =
            try await stateStore.0.proposedTimezoneBoundary(
                deviceID: "ios-timezone-change",
                timezone: utc,
                now: now.addingTimeInterval(3_600)
            )
        let changedBoundary =
            try await stateStore.0.acceptTimezoneBoundary(
                deviceID: "ios-timezone-change",
                timezone: kathmandu,
                now: now
            )
        let changedGeneration =
            await stateStore.0.collectionGeneration(
                deviceID: "ios-timezone-change",
                permissionStatus: .granted,
                now: now
            )

        XCTAssertEqual(
            initialBoundary,
            date("2026-08-14T11:00:00Z")
        )
        XCTAssertEqual(repeatedBoundary, initialBoundary)
        XCTAssertEqual(
            changedBoundary,
            date("2026-08-14T11:15:00Z")
        )
        XCTAssertGreaterThan(changedGeneration, initialGeneration)
    }

    func testDeniedAuthorizationDoesNotPersistHistoricalCollectionBoundary()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let timezone = TimeZone(secondsFromGMT: 0)!
        let deviceID = "ios-denied-boundary"
        let deniedAt = date("2026-08-10T12:34:00Z")

        let deniedProposal =
            try await stateStore.0.proposedTimezoneBoundary(
                deviceID: deviceID,
                timezone: timezone,
                now: deniedAt
            )
        _ = await stateStore.0.collectionGeneration(
            deviceID: deviceID,
            permissionStatus: .denied,
            now: deniedAt
        )
        let grantedAt = date("2026-08-14T12:34:00Z")
        let grantedProposal =
            try await stateStore.0.proposedTimezoneBoundary(
                deviceID: deviceID,
                timezone: timezone,
                now: grantedAt
            )
        let accepted = try await stateStore.0.acceptTimezoneBoundary(
            deviceID: deviceID,
            timezone: timezone,
            now: grantedAt
        )
        _ = await stateStore.0.collectionGeneration(
            deviceID: deviceID,
            permissionStatus: .granted,
            now: grantedAt
        )

        XCTAssertEqual(
            deniedProposal,
            date("2026-08-10T11:00:00Z")
        )
        XCTAssertEqual(
            grantedProposal,
            date("2026-08-14T11:00:00Z")
        )
        XCTAssertEqual(accepted, grantedProposal)
    }

    func testStaleServerPermissionBlockDoesNotPreventLocalRecovery() {
        let staleBlockedState = collectionState(
            effectiveCollecting: false,
            blockedReason: "permission_denied"
        )

        XCTAssertNil(
            ScreenTimeSyncPlanner.skipReason(
                state: staleBlockedState,
                now: date("2026-08-14T12:00:00Z")
            )
        )
        XCTAssertEqual(
            ScreenTimeSyncPlanner.skipReason(
                state: collectionState(enabled: false),
                now: date("2026-08-14T12:00:00Z")
            ),
            "collection_disabled"
        )
    }

    func testPermissionTransitionAdvancesCollectionGeneration() async {
        let suite = "screen-time-generation-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = ScreenTimeSyncStateStore(defaults: defaults)
        let now = date("2026-08-14T12:00:00Z")

        let denied = await store.collectionGeneration(
            deviceID: "ios-generation-test",
            permissionStatus: .denied,
            now: now
        )
        let repeated = await store.collectionGeneration(
            deviceID: "ios-generation-test",
            permissionStatus: .denied,
            now: now
        )
        let granted = await store.collectionGeneration(
            deviceID: "ios-generation-test",
            permissionStatus: .granted,
            now: now
        )

        XCTAssertEqual(repeated, denied)
        XCTAssertGreaterThan(granted, denied)
    }

    func testPseudonymKeyAndExactExclusionSetRequireExplicitApproval()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let now = date("2026-08-14T12:00:00Z")
        let keyA = iosKeyID("1")
        let keyB = iosKeyID("2")
        let token = iosAppToken(keyFingerprint: "1", appDigest: "a")
        let otherToken = iosAppToken(
            keyFingerprint: "1",
            appDigest: "b"
        )

        let initial = await stateStore.0.preparePseudonymBoundary(
            deviceID: "ios-key-change",
            pseudonymKeyID: keyA,
            excludedAppTokens: [],
            now: now
        )
        let generationA = await stateStore.0.collectionGeneration(
            deviceID: "ios-key-change",
            permissionStatus: .granted,
            now: now
        )
        let sameKeyBeforeApproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyA,
                excludedAppTokens: [token],
                now: now
            )
        try await stateStore.0.approveExcludedApps(
            deviceID: "ios-key-change",
            pseudonymKeyID: keyA,
            excludedAppTokens: [token]
        )
        let sameKeyAfterApproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyA,
                excludedAppTokens: [token],
                now: now
            )
        let changedSetWithoutApproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyA,
                excludedAppTokens: [token, otherToken],
                now: now
            )
        let changedKeyWithExclusions =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyB,
                excludedAppTokens: [token],
                now: now
            )
        let generationB = await stateStore.0.collectionGeneration(
            deviceID: "ios-key-change",
            permissionStatus: .granted,
            now: now
        )
        _ = await stateStore.0.preparePseudonymBoundary(
            deviceID: "ios-key-change",
            pseudonymKeyID: keyB,
            excludedAppTokens: [
                iosAppToken(keyFingerprint: "2", appDigest: "a")
            ],
            now: now
        )
        let repeatedGeneration =
            await stateStore.0.collectionGeneration(
                deviceID: "ios-key-change",
                permissionStatus: .granted,
                now: now
            )
        let clearedForReapproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyB,
                excludedAppTokens: [],
                now: now
            )
        let reconfiguredBeforeApproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyB,
                excludedAppTokens: [
                    iosAppToken(keyFingerprint: "2", appDigest: "a")
                ],
                now: now
            )
        try await stateStore.0.approveExcludedApps(
            deviceID: "ios-key-change",
            pseudonymKeyID: keyB,
            excludedAppTokens: [
                iosAppToken(keyFingerprint: "2", appDigest: "a")
            ]
        )
        let reconfiguredAfterApproval =
            await stateStore.0.preparePseudonymBoundary(
                deviceID: "ios-key-change",
                pseudonymKeyID: keyB,
                excludedAppTokens: [
                    iosAppToken(keyFingerprint: "2", appDigest: "a")
                ],
                now: now
            )

        XCTAssertFalse(initial.requiresExclusionReapproval)
        XCTAssertTrue(sameKeyBeforeApproval.requiresExclusionReapproval)
        XCTAssertFalse(sameKeyAfterApproval.requiresExclusionReapproval)
        XCTAssertTrue(changedSetWithoutApproval.requiresExclusionReapproval)
        XCTAssertTrue(changedKeyWithExclusions.requiresExclusionReapproval)
        XCTAssertGreaterThan(generationB, generationA)
        XCTAssertEqual(repeatedGeneration, generationB)
        XCTAssertFalse(clearedForReapproval.requiresExclusionReapproval)
        XCTAssertTrue(reconfiguredBeforeApproval.requiresExclusionReapproval)
        XCTAssertFalse(reconfiguredAfterApproval.requiresExclusionReapproval)
    }

    func testPseudonymBoundaryRejectsStaleKeyAndInvalidTokenApproval()
        async
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let now = date("2026-08-14T12:00:00Z")
        let currentKeyID = iosKeyID("1")
        _ = await stateStore.0.preparePseudonymBoundary(
            deviceID: "ios-key-approval",
            pseudonymKeyID: currentKeyID,
            excludedAppTokens: [],
            now: now
        )

        do {
            try await stateStore.0.approveExcludedApps(
                deviceID: "ios-key-approval",
                pseudonymKeyID: iosKeyID("2"),
                excludedAppTokens: [
                    iosAppToken(keyFingerprint: "2")
                ]
            )
            XCTFail("expected stale pseudonym key rejection")
        } catch {
            XCTAssertEqual(
                error as? ScreenTimePseudonymBoundaryError,
                .pseudonymKeyChanged
            )
        }

        do {
            try await stateStore.0.approveExcludedApps(
                deviceID: "ios-key-approval",
                pseudonymKeyID: currentKeyID,
                excludedAppTokens: ["com.example.private"]
            )
            XCTFail("expected invalid token rejection")
        } catch {
            XCTAssertEqual(
                error as? ScreenTimePseudonymBoundaryError,
                .invalidExcludedAppToken
            )
        }

        do {
            try await stateStore.0.approveExcludedApps(
                deviceID: "ios-key-approval",
                pseudonymKeyID: currentKeyID,
                excludedAppTokens: [
                    iosAppToken(keyFingerprint: "2")
                ]
            )
            XCTFail("expected stale token namespace rejection")
        } catch {
            XCTAssertEqual(
                error as? ScreenTimePseudonymBoundaryError,
                .invalidExcludedAppToken
            )
        }
    }

    func testSyncServiceExplicitApprovalUnblocksExactExclusionSet()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let token = iosAppToken()
        let transport = RecordingScreenTimeTransport(
            state: collectionState(excludedApps: [token])
        )
        let collector = FixedScreenTimeCollector(
            result: ScreenTimeCollectorResult(
                capability: .aggregate,
                permissionStatus: .granted,
                reason: nil,
                samples: []
            ),
            pseudonymKeyID: iosKeyID()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-key-approval-service",
            collector: collector,
            transport: transport,
            stateStore: stateStore.0
        )

        let blocked = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        try await service.approveExcludedApps([token])
        let uploaded = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )

        XCTAssertEqual(
            blocked,
            .skipped(
                reason: "ios_screen_time_exclusions_require_"
                    + "reapproval_after_key_change"
            )
        )
        guard case .uploaded = uploaded else {
            return XCTFail("expected exact approved set to upload")
        }
    }

    func testSnapshotSequenceAllocationIsActorSerialized() async {
        let suite = "screen-time-sequence-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = ScreenTimeSyncStateStore(defaults: defaults)

        let sequences = await withTaskGroup(
            of: Int.self,
            returning: [Int].self
        ) { group in
            for _ in 0..<100 {
                group.addTask {
                    await store.allocateSnapshotSequence(
                        deviceID: "ios-sequence-test"
                    )
                }
            }
            var values: [Int] = []
            for await value in group {
                values.append(value)
            }
            return values
        }

        XCTAssertEqual(sequences.sorted(), Array(1...100))
    }

    func testSnapshotFenceRetryUsesMachineErrorCode() {
        let matching = HealthMesAPIError.server(
            statusCode: 409,
            code: "activity_snapshot_fence_reset_required",
            message: "localized human text may change",
            detail: nil
        )
        let oldMessageOnly = HealthMesAPIError.server(
            statusCode: 409,
            code: "activity_source_conflict",
            message: "collection generation changed",
            detail: nil
        )

        XCTAssertTrue(
            ScreenTimeSyncPlanner.shouldResetSnapshotFence(matching)
        )
        XCTAssertFalse(
            ScreenTimeSyncPlanner.shouldResetSnapshotFence(oldMessageOnly)
        )
    }

    func testSyncServiceSkipsDisabledCollectionBeforeCollector() async throws {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let transport = RecordingScreenTimeTransport(
            state: collectionState(enabled: false)
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-disabled",
            collector: RejectingScreenTimeCollector(),
            transport: transport,
            stateStore: stateStore.0
        )

        let outcome = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(outcome, .skipped(reason: "collection_disabled"))
        XCTAssertTrue(reports.isEmpty)
    }

    func testSyncServiceBlocksStaleExclusionsBeforeCollection() async throws {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let token = iosAppToken()
        let transport = RecordingScreenTimeTransport(
            state: collectionState(excludedApps: [token])
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-key-boundary",
            collector: RejectingScreenTimeCollector(
                pseudonymKeyID: iosKeyID()
            ),
            transport: transport,
            stateStore: stateStore.0
        )

        let outcome = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(
            outcome,
            .skipped(
                reason: "ios_screen_time_exclusions_require_"
                    + "reapproval_after_key_change"
            )
        )
        XCTAssertTrue(reports.isEmpty)
    }

    func testSyncServiceReportsUnavailableWithGeneration() async throws {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let transport = RecordingScreenTimeTransport(
            state: collectionState()
        )
        let collector = FixedScreenTimeCollector(
            result: ScreenTimeCollectorResult(
                capability: .unavailable,
                permissionStatus: .denied,
                reason: "ios_screen_time_permission_denied",
                samples: []
            )
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-unavailable",
            collector: collector,
            transport: transport,
            stateStore: stateStore.0
        )

        let outcome = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_permission_denied"
            )
        )
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].permissionStatus, .denied)
        XCTAssertEqual(reports[0].collectionRevision, 3)
        XCTAssertNotNil(reports[0].collectionGeneration)
    }

    func testIdentityResolverReportsUnavailableWhenKeychainFails()
        async throws
    {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        var loadCount = 0
        let identity = ScreenTimeActivityIdentityResolver.resolve(
            explicitDeviceID: nil,
            pseudonymKeyLoader: {
                loadCount += 1
                throw ScreenTimeCollectorTestError
                    .pseudonymKeyUnavailable
            }
        )
        let transport = RecordingScreenTimeTransport(
            state: collectionState()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: identity.deviceID,
            collector: FixedScreenTimeCollector(
                result: ScreenTimeCollectorResult(
                    capability: .unavailable,
                    permissionStatus: .unavailable,
                    reason:
                        "ios_screen_time_pseudonym_key_unavailable",
                    samples: []
                )
            ),
            transport: transport,
            stateStore: stateStore.0
        )

        let outcome = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(loadCount, 1)
        XCTAssertNil(identity.pseudonymKeyData)
        XCTAssertEqual(
            identity.deviceID,
            ScreenTimeActivityIdentityResolver.unavailableDeviceID
        )
        XCTAssertEqual(
            outcome,
            .unavailableReported(
                reason: "ios_screen_time_pseudonym_key_unavailable"
            )
        )
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(
            reports[0].deviceID,
            ScreenTimeActivityIdentityResolver.unavailableDeviceID
        )
        XCTAssertEqual(
            reports[0].reason,
            "ios_screen_time_pseudonym_key_unavailable"
        )
    }

    func testLiveFactoryDoesNotLoadIdentityBeforeOptIn() {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: stateStore.1
        )
        var loadCount = 0

        _ = ScreenTimeActivitySyncService.live(
            transport: RecordingScreenTimeTransport(
                state: collectionState()
            ),
            stateStore: stateStore.0,
            pseudonymKeyLoader: {
                loadCount += 1
                return Data(repeating: 0x11, count: 32)
            },
            authorizationIntentStore: intentStore
        )

        XCTAssertEqual(loadCount, 0)
    }

    func testUnsupportedBuildDoesNotLoadOrPersistIdentityAfterOptIn()
        throws
    {
        #if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
        throw XCTSkip(
            "SDK-capable builds create the pseudonym key after opt-in."
        )
        #else
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let intentStore = ScreenTimeAuthorizationIntentStore(
            defaults: stateStore.1
        )
        intentStore.setOptedIn(true)
        var loadCount = 0

        _ = ScreenTimeActivitySyncService.live(
            transport: RecordingScreenTimeTransport(
                state: collectionState()
            ),
            stateStore: stateStore.0,
            pseudonymKeyLoader: {
                loadCount += 1
                return Data(repeating: 0x11, count: 32)
            },
            authorizationIntentStore: intentStore
        )

        XCTAssertEqual(loadCount, 0)
        XCTAssertNil(
            stateStore.1.string(
                forKey: "healthmes.screen-time.fallback-device-id.v1"
            )
        )
        #endif
    }

    func testSyncServiceUploadsRetentionClippedAggregate() async throws {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let retentionCutoff = date("2026-08-13T12:00:00Z")
        let bucket = date("2026-08-13T13:00:00Z")
        let sample = ScreenTimeActivitySample(
            sourceRecordID: "ios-hour-retained",
            bucketStart: bucket,
            foregroundSeconds: 900,
            category: "productivity",
            opaqueAppToken: iosAppToken(),
            coverageSeconds: 3_600
        )
        let transport = RecordingScreenTimeTransport(
            state: collectionState(rawRetentionCutoff: retentionCutoff)
        )
        let collector = FixedScreenTimeCollector(
            result: ScreenTimeCollectorResult(
                capability: .aggregate,
                permissionStatus: .granted,
                reason: nil,
                samples: [sample]
            ),
            pseudonymKeyID: iosKeyID()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-aggregate",
            collector: collector,
            transport: transport,
            stateStore: stateStore.0
        )
        _ = try await stateStore.0.acceptTimezoneBoundary(
            deviceID: "ios-aggregate",
            timezone: TimeZone(secondsFromGMT: 0)!,
            now: date("2026-08-13T11:34:00Z")
        )
        _ = await stateStore.0.collectionGeneration(
            deviceID: "ios-aggregate",
            permissionStatus: .granted,
            now: date("2026-08-13T11:34:00Z")
        )

        let outcome = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        guard case .uploaded = outcome else {
            return XCTFail("expected an uploaded outcome")
        }
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports[0].snapshotStart, bucket)
        XCTAssertEqual(
            reports[0].snapshotEnd,
            date("2026-08-14T12:00:00Z")
        )
        XCTAssertEqual(reports[0].samples, [sample])
        XCTAssertFalse(reports[0].resetSnapshotFence)
    }

    func testSyncServiceRetriesSnapshotFenceExactlyOnce() async throws {
        let stateStore = isolatedStateStore()
        defer {
            stateStore.1.removePersistentDomain(forName: stateStore.2)
        }
        let transport = RecordingScreenTimeTransport(
            state: collectionState(),
            failFirstUploadWithFence: true
        )
        let collector = FixedScreenTimeCollector(
            result: ScreenTimeCollectorResult(
                capability: .aggregate,
                permissionStatus: .granted,
                reason: nil,
                samples: []
            ),
            pseudonymKeyID: iosKeyID()
        )
        let service = ScreenTimeActivitySyncService(
            deviceID: "ios-fence",
            collector: collector,
            transport: transport,
            stateStore: stateStore.0
        )

        _ = try await service.sync(
            pairing: pairing,
            now: date("2026-08-14T12:34:00Z"),
            timezone: TimeZone(secondsFromGMT: 0)!
        )
        let reports = await transport.capturedReports()

        XCTAssertEqual(reports.count, 2)
        XCTAssertFalse(reports[0].resetSnapshotFence)
        XCTAssertTrue(reports[1].resetSnapshotFence)
        XCTAssertEqual(
            reports[0].snapshotSequence,
            reports[1].snapshotSequence
        )
    }
}
