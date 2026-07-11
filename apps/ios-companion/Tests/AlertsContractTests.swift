import XCTest

// Contract tests for `GET /v1/alerts` decoding (Tests/Fixtures/alerts.json —
// validated against the server's Page[AlertOut] pydantic model; see README).

final class AlertsContractTests: XCTestCase {
    private func fixtureData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: AlertsContractTests.self)
                .url(forResource: "alerts", withExtension: "json"),
            "alerts.json fixture missing from the test bundle resources"
        )
        return try Data(contentsOf: url)
    }

    func testDecodesFixtureExactly() throws {
        let page = try GlanceJSON.decoder().decode(AlertsPage.self, from: fixtureData())

        XCTAssertEqual(page.pagination.totalCount, 2)
        XCTAssertEqual(page.pagination.limit, 50)
        XCTAssertEqual(page.pagination.offset, 0)
        XCTAssertFalse(page.pagination.hasMore)
        XCTAssertEqual(page.data.count, 2)

        // Newest first, full §8.5 grammar payload.
        let top = page.data[0]
        XCTAssertEqual(top.id, UUID(uuidString: "5b6a1c2d-93e4-4f58-a1c0-5b8e2f7d9a41"))
        XCTAssertEqual(top.ruleId, "deep_sleep_drop")
        XCTAssertEqual(top.firedAt.timeIntervalSince1970, 1_783_605_000, accuracy: 0.001)
        XCTAssertEqual(top.summary, "Recovery 38 today.")
        XCTAssertEqual(top.proposal, "Move the 14:00 block to tomorrow.")
        XCTAssertEqual(
            top.evidence,
            ["hrv_delta_pct": .number(-18), "baseline_days": .number(14)]
        )
        XCTAssertEqual(
            top.decisionUrl,
            "http://192.168.1.20:8100/decisions/00000000-0000-0000-0000-00000000e002"
                + "?token=hm-ro-3q2b8d1f7c6e5a4"
        )

        // Legacy payload-less row: summary falls back to rule_id server-side,
        // proposal/evidence/decision_url are honest nulls.
        let legacy = page.data[1]
        XCTAssertEqual(legacy.summary, legacy.ruleId)
        XCTAssertNil(legacy.proposal)
        XCTAssertNil(legacy.evidence)
        XCTAssertNil(legacy.decisionUrl)
    }

    func testDecodesEmptyPage() throws {
        let json = """
            {
              "data": [],
              "pagination": {"total_count": 0, "limit": 50, "offset": 0, "has_more": false}
            }
            """
        let page = try GlanceJSON.decoder().decode(AlertsPage.self, from: Data(json.utf8))
        XCTAssertTrue(page.data.isEmpty)
        XCTAssertEqual(page.pagination.totalCount, 0)
    }
}

final class SeenAlertsStoreTests: XCTestCase {
    private func makeStore() -> (SeenAlertsStore, UserDefaults) {
        let suite = "seen-alerts-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return (SeenAlertsStore(defaults: defaults), defaults)
    }

    private func alert(_ id: UUID) -> AlertItem {
        AlertItem(
            id: id,
            ruleId: "rule",
            firedAt: Date(),
            summary: "s",
            proposal: nil,
            evidence: nil,
            decisionUrl: nil
        )
    }

    func testUnseenThenMarkSeenIsExactlyOnce() {
        let (store, _) = makeStore()
        let first = alert(UUID())
        let second = alert(UUID())

        XCTAssertEqual(store.unseen(from: [first, second]).map(\.id), [first.id, second.id])
        store.markSeen([first])
        XCTAssertEqual(store.unseen(from: [first, second]).map(\.id), [second.id])
        store.markSeen([second])
        XCTAssertTrue(store.unseen(from: [first, second]).isEmpty)
    }

    func testPrimingSwallowsExistingHistory() {
        let (store, _) = makeStore()
        let existing = [alert(UUID()), alert(UUID())]
        store.primeWithoutNotifying(existing)
        XCTAssertTrue(store.unseen(from: existing).isEmpty)
    }

    func testCapKeepsNewestIDs() {
        let (store, _) = makeStore()
        let overflow = (0..<(SeenAlertsStore.capacity + 10)).map { _ in alert(UUID()) }
        store.markSeen(overflow)
        XCTAssertEqual(store.seenIDs().count, SeenAlertsStore.capacity)
        // The most recently marked alerts must survive the trim.
        XCTAssertTrue(store.unseen(from: Array(overflow.suffix(5))).isEmpty)
    }
}
