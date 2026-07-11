import XCTest

/// The issue-#11 reuse proof, executed natively on macOS: the exact
/// contract fixtures the iOS suite pins (apps/ios-companion/Tests/Fixtures —
/// referenced, not copied: one fixture set across platforms, and the same
/// files are validated server-side by tests/api/test_glance_fixtures.py)
/// must decode through the shared Sources/Shared layer compiled for macOS.
final class SharedContractMacTests: XCTestCase {
    private func fixtureData(_ name: String) throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: SharedContractMacTests.self).url(forResource: name, withExtension: "json"),
            "fixture \(name).json missing from the test bundle"
        )
        return try Data(contentsOf: url)
    }

    func testGlanceFixtureDecodesOnMacOS() throws {
        let payload = try GlanceJSON.decodePayload(fixtureData("glance"))
        XCTAssertEqual(payload.energy.score, 58)
        XCTAssertEqual(payload.energy.confidence, .high)
        XCTAssertEqual(payload.energy.curve24h.count, 24)
        XCTAssertEqual(payload.nextBlocks.count, 3)
        XCTAssertEqual(payload.alerts.unresolvedCount, 2)
        XCTAssertEqual(payload.alerts.top?.ruleId, "stress_spike_vs_baseline")
        XCTAssertNotNil(payload.latestDecision)
    }

    func testAlertsFixtureDecodesOnMacOS() throws {
        let page = try GlanceJSON.decoder().decode(AlertsPage.self, from: fixtureData("alerts"))
        XCTAssertEqual(page.data.count, 2)
        XCTAssertEqual(page.pagination.totalCount, 2)
        XCTAssertEqual(page.data[0].ruleId, "deep_sleep_drop")
        // Legacy payload-less rows fall back to rule_id as the summary.
        XCTAssertEqual(page.data[1].summary, "schedule_overload")
        XCTAssertNil(page.data[1].evidence)
    }

    func testWeeklyReportFixtureDecodesOnMacOS() throws {
        let report = try GlanceJSON.decoder().decode(
            WeeklyReport.self, from: fixtureData("weekly_report")
        )
        XCTAssertEqual(report.weekStart, "2026-07-03")
        XCTAssertEqual(report.energy.days.count, 7)
        // Honest missing day stays null.
        XCTAssertNil(report.energy.days[2].avgScore)
    }

    func testNotificationGrammarMappingOnMacOS() throws {
        // §8.5 line order out of a real alert item: observation → title,
        // evidence line then proposal line → body; actionable category only
        // when a pending proposal id is attached.
        let page = try GlanceJSON.decoder().decode(AlertsPage.self, from: fixtureData("alerts"))
        let alert = page.data[0]

        let plain = AlertNotificationContent.from(alert: alert)
        XCTAssertEqual(plain.title, "Recovery 38 today.")
        XCTAssertEqual(
            plain.body,
            "baseline_days 14 · hrv_delta_pct -18\nMove the 14:00 block to tomorrow."
        )
        XCTAssertEqual(plain.categoryID, AlertNotificationContent.infoCategoryID)
        XCTAssertEqual(plain.threadID, "deep_sleep_drop")
        XCTAssertNotNil(plain.userInfo[AlertNotificationContent.userInfoDecisionURL])

        let proposalID = UUID()
        let actionable = AlertNotificationContent.from(alert: alert, pendingProposalID: proposalID)
        XCTAssertEqual(actionable.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(
            actionable.userInfo[AlertNotificationContent.userInfoProposalID],
            proposalID.uuidString.lowercased()
        )
    }

    func testGlanceSnapshotCacheRoundTripsOnMacOS() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-mac-tests-\(UUID().uuidString)")
        let cache = GlanceSnapshotCache(fileURL: directory.appendingPathComponent("snapshot.json"))
        defer { try? FileManager.default.removeItem(at: directory) }

        let payloadData = try fixtureData("glance")
        cache.store(
            CachedGlance(
                etag: "\"abc123\"",
                fetchedAt: Date(timeIntervalSince1970: 1_780_000_000),
                maxAgeSeconds: 300,
                payloadData: payloadData
            )
        )
        let loaded = try XCTUnwrap(cache.load())
        XCTAssertEqual(loaded.etag, "\"abc123\"")
        XCTAssertEqual(loaded.maxAgeSeconds, 300)
        XCTAssertEqual(cache.decodedPayload()?.energy.score, 58)
    }
}
