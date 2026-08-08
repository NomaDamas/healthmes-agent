import XCTest

// End-to-end UI tests for the issue-#10 daily loop, driven against a REAL
// paired healthmes instance (see README "Live smoke test"): the fixed wellness
// canvas renders live data, deeper views stay behind Explore, and the
// explicit apply button drives the real accept endpoint.
//
// These tests SKIP (never fail) when the app is not paired or the instance
// is unreachable — plain `xcodebuild test` in CI has no live server. To run
// them for real: serve healthmes, pre-seed the pairing app-group default
// (or pair by hand once), then
//   xcodebuild test … -only-testing:HealthMesCompanionUITests
final class CompanionUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    private func launchPairedApp() throws -> XCUIApplication {
        let app = XCUIApplication()
        app.launch()
        // Paired + reachable == the energy card appears with live data.
        guard app.staticTexts["인지 에너지"].waitForExistence(timeout: 15) else {
            throw XCTSkip(
                "No paired live instance — serve healthmes and pair first (README)."
            )
        }
        return app
    }

    private func tapSettingsLink(_ label: String, in app: XCUIApplication) throws {
        let element = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", label))
            .firstMatch
        for _ in 0..<4 where !element.exists {
            app.swipeUp()
        }
        guard element.waitForExistence(timeout: 3) else {
            XCTFail("Settings link '\(label)' was not reachable.")
            return
        }
        element.tap()
    }

    /// Acceptance sketch #1: briefing home shows live data; drill into the
    /// weekly report and capture surfaces.
    func testDailyLoopSurfacesRenderAgainstLiveInstance() throws {
        let app = try launchPairedApp()

        XCTAssertTrue(app.staticTexts["몸 → 오늘 계획"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["인지 에너지"].exists)
        XCTAssertTrue(app.textFields.firstMatch.exists)
        XCTAssertFalse(app.buttons["조율"].exists)
        XCTAssertFalse(app.buttons["변화"].exists)

        app.buttons["전체 보기"].tap()
        app.buttons["일정과 목표"].tap()
        XCTAssertTrue(app.staticTexts["일정과 목표"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["보호할 목표와 할 일"].exists)
        XCTAssertTrue(app.staticTexts["일정 영향"].exists)

        app.buttons["전체 보기"].tap()
        app.buttons["결정 결과"].tap()
        XCTAssertTrue(app.staticTexts["결정 결과"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["OUTCOME LOOP"].exists)

        app.buttons["전체 보기"].tap()
        app.buttons["설정"].tap()
        try tapSettingsLink("Weekly report", in: app)
        XCTAssertTrue(app.staticTexts["Energy trend"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["Schedule adherence"].exists)
        XCTAssertTrue(app.staticTexts["Alert digest"].exists)
    }

    /// Acceptance sketch #2/#5: Apply on a pending proposal calls the real
    /// accept endpoint; the row resolves and the confirmation banner shows.
    /// Needs a seeded `proposed` proposal (the smoke script creates one).
    func testApplyProposalRoundTrip() throws {
        let app = try launchPairedApp()

        let apply = app.buttons["변경 승인"].firstMatch
        guard apply.waitForExistence(timeout: 10) else {
            throw XCTSkip("No pending proposal seeded — nothing to apply.")
        }
        apply.tap()
        XCTAssertTrue(
            app.staticTexts["Approved · calendar sync pending"].waitForExistence(timeout: 15),
            "accept endpoint round-trip should confirm in the banner"
        )
    }

    /// Nutrition capture requires analysis followed by an explicit owner outcome.
    func testFoodCaptureRoundTrip() throws {
        let app = try launchPairedApp()

        app.buttons["전체 보기"].tap()
        app.buttons["설정"].tap()
        try tapSettingsLink("Capture", in: app)
        let field = app.textFields["Description"]
        guard field.waitForExistence(timeout: 10) else {
            throw XCTSkip("Capture form not reachable.")
        }
        field.tap()
        field.typeText("UITest kimbap roll")
        app.buttons["Analyze for review"].tap()
        let consumed = app.buttons["Consumed"]
        guard consumed.waitForExistence(timeout: 30) else {
            throw XCTSkip("Nutrition provider is not configured for live UI QA.")
        }
        consumed.tap()
        XCTAssertTrue(
            app.staticTexts["Recorded as consumed."].waitForExistence(timeout: 15),
            "explicit consumed outcome should round-trip against the live instance"
        )
    }

    /// Visual contract for #91: the ordinary compact banner arrives first,
    /// then expanding it reveals the glass decision remote without opening
    /// the HealthMes app.
    func testExpandedDecisionNotificationShowsNoAndYes() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-healthmes-notification-demo"]

        addUIInterruptionMonitor(withDescription: "Notification permission") { alert in
            let allow = alert.buttons["허용"].exists ? alert.buttons["허용"] : alert.buttons["Allow"]
            guard allow.exists else { return false }
            allow.tap()
            return true
        }

        app.launch()
        app.tap()
        sleep(1)
        XCUIDevice.shared.press(.home)

        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let notificationTitle = springboard.staticTexts["Move Deep Work?"]
        XCTAssertTrue(
            notificationTitle.waitForExistence(timeout: 8),
            "The deterministic HealthMes decision notification should appear."
        )
        XCTAssertTrue(
            springboard.staticTexts["Low recovery · sleep debt"].exists,
            "The compact notification must explain the health reason immediately."
        )
        XCTAssertFalse(
            springboard.buttons["healthmes-decision-yes"].exists,
            "iOS owns the compact banner and may hide category actions until expansion."
        )
        let compactScreenshot = XCTAttachment(screenshot: springboard.screenshot())
        compactScreenshot.name = "HealthMes ordinary compact banner"
        compactScreenshot.lifetime = .keepAlways
        add(compactScreenshot)

        notificationTitle.press(forDuration: 1.5)

        XCTAssertTrue(
            springboard.staticTexts["HEALTHMES · DECISION"].waitForExistence(timeout: 5),
            "The HealthMes content extension must render instead of a blank card."
        )
        XCTAssertTrue(
            springboard.staticTexts["healthmes-decision-details"].exists,
            "The expanded notification must expose a scrollable decision detail region."
        )

        let no = springboard.buttons["healthmes-decision-no"]
        let yes = springboard.buttons["healthmes-decision-yes"]
        XCTAssertTrue(no.waitForExistence(timeout: 5), "Expanded notification must show No.")
        XCTAssertTrue(yes.waitForExistence(timeout: 5), "Expanded notification must show Yes.")

        let screenshot = XCTAttachment(screenshot: springboard.screenshot())
        screenshot.name = "HealthMes expanded decision notification"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
