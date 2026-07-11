import XCTest

final class StatusItemTextTests: XCTestCase {
    private func payload(score: Int?, alerts: Int) -> GlancePayload {
        GlancePayload(
            generatedAt: Date(),
            timezone: "UTC",
            energy: GlanceEnergy(
                score: score,
                confidence: .high,
                curve24h: (0..<24).map { GlanceCurvePoint(hour: $0, score: nil) }
            ),
            nextBlocks: [],
            alerts: GlanceAlerts(unresolvedCount: alerts, top: nil),
            latestDecision: nil
        )
    }

    func testNotPairedShowsPlaceholder() {
        XCTAssertEqual(StatusItemText.title(payload: nil, stale: false, isPaired: false), "--")
        // Paired but nothing fetched yet is the same honest placeholder.
        XCTAssertEqual(StatusItemText.title(payload: nil, stale: false, isPaired: true), "--")
    }

    func testScoreRendersPlain() {
        XCTAssertEqual(
            StatusItemText.title(payload: payload(score: 58, alerts: 0), stale: false, isPaired: true),
            "58"
        )
    }

    func testNullScoreStaysHonest() {
        XCTAssertEqual(
            StatusItemText.title(payload: payload(score: nil, alerts: 0), stale: false, isPaired: true),
            "--"
        )
    }

    func testAlertsAddTheDot() {
        XCTAssertEqual(
            StatusItemText.title(payload: payload(score: 58, alerts: 2), stale: false, isPaired: true),
            "58•"
        )
    }

    func testStaleWrapsInParentheses() {
        XCTAssertEqual(
            StatusItemText.title(payload: payload(score: 58, alerts: 2), stale: true, isPaired: true),
            "(58•)"
        )
    }
}
