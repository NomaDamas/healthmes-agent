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
        XCTAssertEqual(
            page.data[0].proposalId,
            UUID(uuidString: "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10")
        )
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
        XCTAssertEqual(report.schedule.displayBreakdown.syncPending, 4)
        XCTAssertEqual(report.schedule.displayBreakdown.applied, 1)
    }

    func testNotificationGrammarMappingOnMacOS() throws {
        // Legacy alerts with a real proposal still lead with the concrete
        // action before exposing Yes/No.
        let page = try GlanceJSON.decoder().decode(AlertsPage.self, from: fixtureData("alerts"))
        let alert = page.data[0]

        let plain = AlertNotificationContent.from(alert: alert)
        XCTAssertEqual(plain.title, "Move the 14:00 block to tomorrow?")
        XCTAssertEqual(plain.subtitle, "Recovery 38 today.")
        XCTAssertEqual(plain.body, "baseline_days 14 · hrv_delta_pc…")
        XCTAssertEqual(plain.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(plain.threadID, "deep_sleep_drop")
        XCTAssertNotNil(plain.userInfo[AlertNotificationContent.userInfoDecisionURL])
        XCTAssertEqual(
            plain.userInfo[AlertNotificationContent.userInfoProposalID],
            "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10"
        )

        let proposalID = UUID()
        let actionable = AlertNotificationContent.from(alert: alert, pendingProposalID: proposalID)
        XCTAssertEqual(actionable.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(
            actionable.userInfo[AlertNotificationContent.userInfoProposalID],
            proposalID.uuidString.lowercased()
        )
    }

    func testProposalWithoutExactActionFailsClosedOnMacOS() {
        let proposalID = UUID()
        let alert = AlertItem(
            id: UUID(),
            ruleId: "missing-action",
            firedAt: Date(),
            summary: "Recovery changed.",
            proposal: nil,
            evidence: nil,
            decisionUrl: nil,
            proposalId: proposalID
        )

        XCTAssertNil(ProposalActionPresentation.exactPrompt(alert: alert))
        XCTAssertEqual(
            AlertNotificationContent.from(alert: alert).categoryID,
            AlertNotificationContent.infoCategoryID
        )
    }

    func testGlanceSnapshotCacheRoundTripsOnMacOS() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-mac-tests-\(UUID().uuidString)")
        let cache = GlanceSnapshotCache(fileURL: directory.appendingPathComponent("snapshot.json"))
        defer { try? FileManager.default.removeItem(at: directory) }

        let payloadData = try fixtureData("glance")
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "owner-token"
        )
        cache.store(
            CachedGlance(
                pairingFingerprint: pairing.cacheFingerprint,
                pairingGeneration: 1,
                etag: "\"abc123\"",
                fetchedAt: Date(timeIntervalSince1970: 1_780_000_000),
                maxAgeSeconds: 300,
                payloadData: payloadData
            )
        )
        let loaded = try XCTUnwrap(cache.load())
        XCTAssertEqual(loaded.etag, "\"abc123\"")
        XCTAssertEqual(loaded.maxAgeSeconds, 300)
        let identity = PairingCacheIdentity(
            fingerprint: pairing.cacheFingerprint,
            generation: 1
        )
        XCTAssertEqual(cache.decodedPayload(for: identity)?.energy.score, 58)
        let other = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "different-owner"
        )
        XCTAssertNil(
            cache.decodedPayload(
                for: PairingCacheIdentity(
                    fingerprint: other.cacheFingerprint,
                    generation: 1
                )
            )
        )
    }

    func testGlanceSnapshotCacheRejectsOlderSameAccountWrite() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-cache-order-\(UUID().uuidString)")
        let cache = GlanceSnapshotCache(fileURL: directory.appendingPathComponent("snapshot.json"))
        defer { try? FileManager.default.removeItem(at: directory) }
        let payloadData = try fixtureData("glance")
        let fingerprint = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "owner-token"
        ).cacheFingerprint
        let newer = CachedGlance(
            pairingFingerprint: fingerprint,
            pairingGeneration: 4,
            etag: "\"newer\"",
            fetchedAt: Date(timeIntervalSince1970: 1_780_000_200),
            maxAgeSeconds: 300,
            payloadData: payloadData
        )
        let older = CachedGlance(
            pairingFingerprint: fingerprint,
            pairingGeneration: 4,
            etag: "\"older\"",
            fetchedAt: Date(timeIntervalSince1970: 1_780_000_100),
            maxAgeSeconds: 300,
            payloadData: payloadData
        )

        XCTAssertTrue(cache.store(newer))
        XCTAssertFalse(cache.store(older))
        XCTAssertEqual(cache.load()?.etag, "\"newer\"")
    }
}
