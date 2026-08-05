import XCTest

// The §8.5 grammar → notification-content mapping must be deterministic and
// line-faithful: observation = title, evidence + proposal = body lines,
// buttons only when a real proposal is attached, decision link in userInfo.

final class NotificationContentTests: XCTestCase {
    private let alertID = UUID(uuidString: "5b6a1c2d-93e4-4f58-a1c0-5b8e2f7d9a41")!
    private let proposalID = UUID(uuidString: "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10")!

    private var fullAlert: AlertItem {
        AlertItem(
            id: alertID,
            ruleId: "deep_sleep_drop",
            firedAt: Date(timeIntervalSince1970: 1_783_605_000),
            summary: "Recovery 38 today.",
            proposal: "Move the 14:00 block to tomorrow.",
            evidence: ["hrv_delta_pct": .number(-18), "baseline_days": .number(14)],
            decisionUrl: "http://192.168.1.20:8100/decisions/x?token=hm-ro-abc",
            proposalId: nil
        )
    }

    func testFullGrammarMapping() {
        let content = AlertNotificationContent.from(
            alert: fullAlert, pendingProposalID: proposalID
        )

        // Legacy alerts are compacted defensively for the smallest watch.
        XCTAssertEqual(content.title, "Recovery 38 today.")
        XCTAssertLessThanOrEqual(content.body.count, 32)
        XCTAssertFalse(content.body.contains("\n"))
        XCTAssertTrue(content.body.hasPrefix("baseline_days 14"))
        // Buttons only exist because a real pending proposal is attached.
        XCTAssertEqual(content.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(content.threadID, "deep_sleep_drop")
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoAlertID],
            alertID.uuidString.lowercased()
        )
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoProposalID],
            proposalID.uuidString.lowercased()
        )
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoDecisionURL],
            "http://192.168.1.20:8100/decisions/x?token=hm-ro-abc"
        )
    }

    func testNoProposalMeansInfoCategoryAndDroppedLines() {
        let bare = AlertItem(
            id: alertID,
            ruleId: "schedule_overload",
            firedAt: Date(),
            summary: "schedule_overload",
            proposal: nil,
            evidence: nil,
            decisionUrl: nil,
            proposalId: nil
        )
        let content = AlertNotificationContent.from(alert: bare, pendingProposalID: nil)

        XCTAssertEqual(content.title, "schedule_overload")
        // Lines the payload does not carry are DROPPED, never invented
        // (WATCH-NOTIFICATIONS.ko.md §1.1).
        XCTAssertEqual(content.body, "")
        XCTAssertEqual(content.categoryID, AlertNotificationContent.infoCategoryID)
        XCTAssertNil(content.userInfo[AlertNotificationContent.userInfoProposalID])
        XCTAssertNil(content.userInfo[AlertNotificationContent.userInfoDecisionURL])
    }

    func testDecisionCardUsesCompactActionableBannerAndExtensionPayload() {
        let after = Date(timeIntervalSince1970: 1_783_674_600)
        let endsAt = after.addingTimeInterval(5_400)
        let expiresAt = Date(timeIntervalSince1970: 1_783_610_400)
        let card = DecisionCard(
            decisionId: alertID,
            proposalId: proposalID,
            kind: "schedule_change",
            severity: "coaching",
            title: "Deep Work",
            observationShort: "Sleep debt is high",
            evidenceShort: "HRV is 18% below baseline",
            proposedAction: "Move deep work to tomorrow at 09:30?",
            before: nil,
            after: after,
            endsAt: endsAt,
            expiresAt: expiresAt,
            decisionUrl: nil
        )
        let alert = AlertItem(
            id: alertID,
            ruleId: "low_recovery_heavy_afternoon",
            firedAt: Date(),
            summary: "Fallback summary",
            proposal: "Fallback proposal",
            evidence: nil,
            decisionUrl: nil,
            proposalId: proposalID,
            decisionCard: card
        )

        let content = AlertNotificationContent.from(alert: alert)

        XCTAssertEqual(content.title, "Move Deep Work?")
        XCTAssertEqual(content.subtitle, "Sleep debt is high")
        XCTAssertTrue(content.body.hasPrefix("→ "))
        XCTAssertFalse(content.body.contains("\n"))
        XCTAssertEqual(content.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoDecisionObservation],
            "Sleep debt is high"
        )
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoDecisionAction],
            "Move deep work to tomorrow at 09:30?"
        )
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoDecisionEvidence],
            "HRV is 18% below baseline"
        )
        XCTAssertNotNil(content.userInfo[AlertNotificationContent.userInfoDecisionAfter])
        XCTAssertNotNil(content.userInfo[AlertNotificationContent.userInfoDecisionEndsAt])
        XCTAssertNotNil(content.userInfo[AlertNotificationContent.userInfoDecisionExpiresAt])
    }

    func testEvidenceLineIsSortedAndTypeStable() {
        XCTAssertNil(AlertNotificationContent.evidenceLine(nil))
        XCTAssertNil(AlertNotificationContent.evidenceLine([:]))
        let line = AlertNotificationContent.evidenceLine([
            "z_last": .string("low"),
            "a_first": .number(2.5),
            "flag": .bool(true),
            "count": .number(3),
        ])
        // Keys sorted alphabetically; whole numbers drop the ".0".
        XCTAssertEqual(line, "a_first 2.5 · count 3 · flag true · z_last low")
    }

    func testFromDecodedFixtureAlert() throws {
        // End-to-end: fixture bytes → decoded AlertItem → grammar content.
        let url = try XCTUnwrap(
            Bundle(for: NotificationContentTests.self)
                .url(forResource: "alerts", withExtension: "json")
        )
        let page = try GlanceJSON.decoder().decode(
            AlertsPage.self, from: Data(contentsOf: url)
        )
        let content = AlertNotificationContent.from(alert: page.data[0])
        XCTAssertEqual(content.title, "Recovery 38 today.")
        XCTAssertLessThanOrEqual(content.body.count, 32)
        XCTAssertFalse(content.body.contains("\n"))
        XCTAssertEqual(content.categoryID, AlertNotificationContent.actionableCategoryID)
        XCTAssertEqual(
            content.userInfo[AlertNotificationContent.userInfoProposalID],
            "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10"
        )
    }
}

final class FocusBlockSelectorTests: XCTestCase {
    private func block(startOffset: TimeInterval, endOffset: TimeInterval, title: String)
        -> GlanceBlock
    {
        let now = Date(timeIntervalSince1970: 1_783_606_980)
        // GlanceBlock has no public memberwise init in the contract file, so
        // build it through JSON like the server would send it.
        let formatter = ISO8601DateFormatter()
        let json = """
            {
              "start": "\(formatter.string(from: now.addingTimeInterval(startOffset)))",
              "end": "\(formatter.string(from: now.addingTimeInterval(endOffset)))",
              "title": "\(title)",
              "energy_demand": "high",
              "source": "calendar"
            }
            """
        return try! GlanceJSON.decoder().decode(GlanceBlock.self, from: Data(json.utf8))
    }

    private let now = Date(timeIntervalSince1970: 1_783_606_980)

    func testCurrentPicksTheOngoingBlock() {
        let ongoing = block(startOffset: -600, endOffset: 1800, title: "Deep work")
        let future = block(startOffset: 3600, endOffset: 7200, title: "Review")
        XCTAssertEqual(
            FocusBlockSelector.current(in: [ongoing, future], now: now)?.title, "Deep work"
        )
        XCTAssertEqual(
            FocusBlockSelector.upcoming(in: [ongoing, future], now: now)?.title, "Review"
        )
    }

    func testNoCurrentWhenAllFutureOrPast() {
        let past = block(startOffset: -7200, endOffset: -3600, title: "Done")
        let future = block(startOffset: 3600, endOffset: 7200, title: "Later")
        XCTAssertNil(FocusBlockSelector.current(in: [past, future], now: now))
        XCTAssertNil(FocusBlockSelector.current(in: [], now: now))
    }

    func testBlockEndIsExclusive() {
        let ending = block(startOffset: -3600, endOffset: 0, title: "Ending")
        XCTAssertNil(FocusBlockSelector.current(in: [ending], now: now))
    }

    func testProgressClamps() {
        let ongoing = block(startOffset: -600, endOffset: 600, title: "Half")
        XCTAssertEqual(FocusBlockSelector.progress(of: ongoing, now: now), 0.5, accuracy: 0.001)
        let past = block(startOffset: -1200, endOffset: -600, title: "Past")
        XCTAssertEqual(FocusBlockSelector.progress(of: past, now: now), 1.0)
    }
}
