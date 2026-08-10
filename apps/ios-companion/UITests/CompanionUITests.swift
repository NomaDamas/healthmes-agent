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
        app.launchArguments += [
            "-healthmes-ui-test-base-url",
            "http://127.0.0.1:8201",
        ]
        app.launch()
        // Wait for the stable control-canvas contract. The initial body card
        // can be replaced immediately by a generated wellness scene.
        guard app.otherElements["healthmes-wellness-control"].waitForExistence(timeout: 15) else {
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

    /// The iPhone default stays within the four-area wellness contract and
    /// exposes food capture and bounded question input without old lens pages.
    func testDailyLoopSurfacesRenderAgainstLiveInstance() throws {
        let app = try launchPairedApp()

        guard app.otherElements["healthmes-generated-scene"].waitForExistence(timeout: 15) else {
            throw XCTSkip("No live wellness scene seeded for UI QA.")
        }
        XCTAssertTrue(app.buttons["식사 사진"].exists)
        XCTAssertTrue(app.buttons["지금"].exists)
        XCTAssertTrue(app.buttons["조율"].exists)
        XCTAssertTrue(app.buttons["변화"].exists)
        XCTAssertFalse(app.staticTexts["OUTCOME LOOP"].exists)
        XCTAssertFalse(app.staticTexts["RECENT EVIDENCE"].exists)
        XCTAssertFalse(app.buttons["전체 보기"].exists)
        XCTAssertTrue(
            app.textFields["healthmes-command-input"].waitForExistence(timeout: 3)
        )
    }

    /// The seeded proactive proposal exposes real controls, while a later
    /// uncorrelated command stays read-only instead of approving by inference.
    func testWellnessCommandRendersScheduleScene() throws {
        let app = try launchPairedApp()
        guard app.otherElements["healthmes-generated-scene"].waitForExistence(timeout: 15) else {
            throw XCTSkip("No proactive wellness proposal seeded for live UI QA.")
        }
        guard app.buttons["유지"].exists, app.buttons["변경 승인"].exists else {
            throw XCTSkip("No actionable health-backed proposal seeded for live UI QA.")
        }

        let command = app.textFields["healthmes-command-input"]
        XCTAssertTrue(command.waitForExistence(timeout: 3))

        command.tap()
        command.typeText("adjust today schedule")
        app.buttons["질문 보내기"].tap()

        XCTAssertTrue(
            app.otherElements["healthmes-generated-scene"].waitForExistence(timeout: 15)
        )
        XCTAssertFalse(app.buttons["유지"].exists)
        XCTAssertFalse(app.buttons["변경 승인"].exists)

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "HealthMes generative schedule scene"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    /// Acceptance sketch #2/#5: Apply on a pending proposal calls the real
    /// accept endpoint; the row resolves and the confirmation banner shows.
    /// Needs a seeded `proposed` proposal (the smoke script creates one).
    func testZZApplyProposalRoundTrip() throws {
        let app = try launchPairedApp()

        let generatedApply = app.buttons["적용"].firstMatch
        let legacyApply = app.buttons["변경 승인"].firstMatch
        let apply = generatedApply.waitForExistence(timeout: 10)
            ? generatedApply
            : legacyApply
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

        app.buttons["식사 사진"].tap()
        let field = app.textFields["healthmes-capture-description"]
        guard field.waitForExistence(timeout: 10) else {
            throw XCTSkip("Capture form not reachable.")
        }
        let cameraDismiss = app.buttons
            .matching(identifier: "DismissImagePickerButton")
            .matching(NSPredicate(format: "label == %@", "Close camera"))
            .firstMatch
        if cameraDismiss.waitForExistence(timeout: 3) {
            cameraDismiss.tap()
        }
        let form = app.descendants(matching: .any)["healthmes-capture-form"]
        XCTAssertTrue(
            form.waitForExistence(timeout: 3),
            "Capture form must expose a stable scroll container."
        )
        for _ in 0..<4 where !field.isHittable {
            form.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.72))
                .press(
                    forDuration: 0.05,
                    thenDragTo: form.coordinate(
                        withNormalizedOffset: CGVector(dx: 0.5, dy: 0.42)
                    )
                )
        }
        if !field.isHittable {
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "Food capture field not hittable"
            screenshot.lifetime = .keepAlways
            add(screenshot)

            let hierarchy = XCTAttachment(
                data: Data(app.debugDescription.utf8),
                uniformTypeIdentifier: "public.plain-text"
            )
            hierarchy.name = "Food capture accessibility hierarchy"
            hierarchy.lifetime = .keepAlways
            add(hierarchy)
        }
        XCTAssertTrue(
            field.isHittable,
            "Food description must remain reachable on the iPhone 13 mini layout."
        )
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
            notificationTitle.waitForExistence(timeout: 20),
            "The deterministic HealthMes decision notification should appear."
        )
        XCTAssertTrue(
            springboard.staticTexts["Low recovery · sleep debt"].waitForExistence(timeout: 5),
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

        notificationTitle.press(forDuration: 2)

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
