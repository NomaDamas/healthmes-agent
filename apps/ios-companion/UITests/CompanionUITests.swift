import XCTest

// End-to-end UI tests for the issue-#10 daily loop, driven against a REAL
// paired healthmes instance (see README "Live smoke test"): briefing home
// renders live glance/alerts data, tab navigation works, and the §8.5
// Yes button drives the real accept endpoint.
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
        guard app.staticTexts["Cognitive energy"].waitForExistence(timeout: 15) else {
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

        XCTAssertTrue(app.staticTexts["NOW"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["NEXT"].exists)
        XCTAssertTrue(app.staticTexts["DECISION"].exists)

        app.buttons["Plan"].tap()
        XCTAssertTrue(app.staticTexts["THIS WEEK"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["SCHEDULE"].exists)
        XCTAssertTrue(app.staticTexts["OPEN TASKS"].exists)

        app.buttons["Decisions"].tap()
        XCTAssertTrue(app.staticTexts["HISTORY"].waitForExistence(timeout: 15))

        app.buttons["Profile"].tap()
        try tapSettingsLink("Weekly report", in: app)
        XCTAssertTrue(app.staticTexts["Energy trend"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["Schedule adherence"].exists)
        XCTAssertTrue(app.staticTexts["Alert digest"].exists)
    }

    /// Acceptance sketch #2/#5: Yes on a pending proposal calls the real
    /// accept endpoint; the row resolves and the confirmation banner shows.
    /// Needs a seeded `proposed` proposal (the smoke script creates one).
    func testApplyProposalRoundTrip() throws {
        let app = try launchPairedApp()

        let apply = app.buttons["Yes"].firstMatch
        guard apply.waitForExistence(timeout: 10) else {
            throw XCTSkip("No pending proposal seeded — nothing to apply.")
        }
        apply.tap()
        XCTAssertTrue(
            app.staticTexts["Approved · calendar sync pending"].waitForExistence(timeout: 15),
            "accept endpoint round-trip should confirm in the banner"
        )
    }

    /// Capture round-trip without media: type a description, save, expect
    /// the success row (POST /v1/food-logs against the live instance).
    func testFoodCaptureRoundTrip() throws {
        let app = try launchPairedApp()

        app.buttons["Profile"].tap()
        try tapSettingsLink("Capture", in: app)
        let field = app.textFields.firstMatch
        guard field.waitForExistence(timeout: 10) else {
            throw XCTSkip("Capture form not reachable.")
        }
        field.tap()
        field.typeText("UITest kimbap roll")
        app.buttons["Save to my instance"].tap()
        XCTAssertTrue(
            app.staticTexts["Food log saved."].waitForExistence(timeout: 15),
            "food-log POST should round-trip against the live instance"
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
