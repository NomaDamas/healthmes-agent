import XCTest

// End-to-end UI tests for the issue-#10 daily loop, driven against a REAL
// paired healthmes instance (see README "Live smoke test"): briefing home
// renders live glance/alerts data, tab navigation works, and the §8.5
// Apply button drives the real accept endpoint.
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

    /// Acceptance sketch #1: briefing home shows live data; drill into the
    /// weekly report and capture surfaces.
    func testDailyLoopSurfacesRenderAgainstLiveInstance() throws {
        let app = try launchPairedApp()

        // Home: alert list carries the §8.5 grammar lines from /v1/alerts.
        XCTAssertTrue(app.staticTexts["Alerts · last 24h"].waitForExistence(timeout: 10))

        // Report tab: native weekly.json rendering.
        app.tabBars.buttons["Report"].tap()
        XCTAssertTrue(app.staticTexts["Energy trend"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["Schedule adherence"].exists)
        XCTAssertTrue(app.staticTexts["Alert digest"].exists)

        // Capture tab: the three capture targets and description field.
        app.tabBars.buttons["Capture"].tap()
        XCTAssertTrue(app.buttons["Food"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Medication"].exists)
        XCTAssertTrue(app.buttons["Symptom"].exists)

        // Settings tab: pairing entry + the delivery-honesty copy.
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.staticTexts["Native alerts"].waitForExistence(timeout: 10))
    }

    /// Acceptance sketch #2/#5: ✅ Apply on a pending proposal calls the real
    /// accept endpoint; the row resolves and the confirmation banner shows.
    /// Needs a seeded `proposed` proposal (the smoke script creates one).
    func testApplyProposalRoundTrip() throws {
        let app = try launchPairedApp()

        let apply = app.buttons["Apply"].firstMatch
        guard apply.waitForExistence(timeout: 10) else {
            throw XCTSkip("No pending proposal seeded — nothing to apply.")
        }
        apply.tap()
        XCTAssertTrue(
            app.staticTexts["Proposal applied."].waitForExistence(timeout: 15),
            "accept endpoint round-trip should confirm in the banner"
        )
    }

    /// Capture round-trip without media: type a description, save, expect
    /// the success row (POST /v1/food-logs against the live instance).
    func testFoodCaptureRoundTrip() throws {
        let app = try launchPairedApp()

        app.tabBars.buttons["Capture"].tap()
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
}
