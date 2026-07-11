import XCTest

/// The issue-#11 privacy toggle as a data rule: hiding health numbers must
/// remove them entirely from the render model (nothing blurred, nothing
/// leaked), while keeping schedule facts and the honest empty states.
final class SaverBriefingTests: XCTestCase {
    private var fixturePayload: GlancePayload!
    /// 2026-07-09T14:30:00Z — half past the fixture's latest curve hour.
    private let now = Date(timeIntervalSince1970: 1_783_607_400)

    override func setUpWithError() throws {
        let url = try XCTUnwrap(
            Bundle(for: SaverBriefingTests.self).url(forResource: "glance", withExtension: "json")
        )
        fixturePayload = try GlanceJSON.decodePayload(Data(contentsOf: url))
    }

    func testNotPairedState() {
        let briefing = SaverBriefing.make(
            payload: nil, fetchedAt: nil, isPaired: false, hideNumbers: false, now: now
        )
        XCTAssertEqual(briefing.state, .notPaired)
    }

    func testPairedWithoutCacheIsNoData() {
        let briefing = SaverBriefing.make(
            payload: nil, fetchedAt: nil, isPaired: true, hideNumbers: false, now: now
        )
        XCTAssertEqual(briefing.state, .noData)
    }

    func testFullBriefingCarriesEverySlot() throws {
        let briefing = SaverBriefing.make(
            payload: fixturePayload,
            fetchedAt: now.addingTimeInterval(-180),
            isPaired: true,
            hideNumbers: false,
            now: now
        )
        guard case .briefing(let content) = briefing.state else {
            return XCTFail("expected briefing state")
        }
        XCTAssertEqual(content.scoreText, "58")
        XCTAssertEqual(content.confidenceRaw, "high")
        XCTAssertEqual(content.curve?.count, 24)
        // Fixture timezone is UTC; 14:30Z → hour 14.
        XCTAssertEqual(content.currentHour, 14)
        XCTAssertTrue(content.hasNextBlock)
        XCTAssertEqual(content.nextBlockTimeText, "14:00")
        XCTAssertEqual(content.nextBlockTitle, "Deep work block")
        XCTAssertEqual(content.nextBlockDemandRaw, "high")
        XCTAssertEqual(content.alertCount, 2)
        XCTAssertEqual(content.topAlertSummary, "Stress 82 vs baseline 55")
        XCTAssertEqual(content.updatedMinutesAgo, 3)
        XCTAssertFalse(content.numbersHidden)
    }

    func testPrivacyToggleRemovesEveryHealthValue() throws {
        let briefing = SaverBriefing.make(
            payload: fixturePayload,
            fetchedAt: now.addingTimeInterval(-180),
            isPaired: true,
            hideNumbers: true,
            now: now
        )
        guard case .briefing(let content) = briefing.state else {
            return XCTFail("expected briefing state")
        }
        // Health-derived values: absent, not obscured.
        XCTAssertNil(content.scoreText)
        XCTAssertNil(content.confidenceRaw)
        XCTAssertNil(content.curve)
        XCTAssertNil(content.currentHour)
        XCTAssertNil(content.nextBlockDemandRaw)
        XCTAssertNil(content.alertCount)
        XCTAssertNil(content.topAlertSummary)
        XCTAssertTrue(content.numbersHidden)
        // Schedule facts stay useful in a shared space.
        XCTAssertEqual(content.nextBlockTimeText, "14:00")
        XCTAssertEqual(content.nextBlockTitle, "Deep work block")
        // Freshness honesty is not a health number.
        XCTAssertEqual(content.updatedMinutesAgo, 3)
    }

    func testSaverDataSourceReadsTheSharedCache() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-saver-tests-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = GlanceSnapshotCache(fileURL: directory.appendingPathComponent("snapshot.json"))

        let suiteName = "com.healthmes.mac.tests.saver-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let source = SaverDataSource(cache: cache, defaults: defaults)

        // Nothing paired, nothing cached.
        XCTAssertEqual(source.briefing(hideNumbers: false, now: now).state, .notPaired)

        // Paired (base URL default present — the saver never reads the
        // keychain half) but cache still cold.
        defaults.set("http://127.0.0.1:8100", forKey: SaverDataSource.pairedBaseURLDefaultsKey)
        XCTAssertEqual(source.briefing(hideNumbers: false, now: now).state, .noData)

        // Warm cache → full briefing.
        let url = try XCTUnwrap(
            Bundle(for: SaverBriefingTests.self).url(forResource: "glance", withExtension: "json")
        )
        cache.store(
            CachedGlance(
                etag: nil,
                fetchedAt: now.addingTimeInterval(-60),
                maxAgeSeconds: 300,
                payloadData: try Data(contentsOf: url)
            )
        )
        guard case .briefing(let content) = source.briefing(hideNumbers: false, now: now).state
        else {
            return XCTFail("expected briefing state")
        }
        XCTAssertEqual(content.scoreText, "58")
        XCTAssertEqual(content.updatedMinutesAgo, 1)
    }
}
