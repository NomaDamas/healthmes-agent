import XCTest

// Contract tests for `GET /reports/weekly.json` decoding
// (Tests/Fixtures/weekly_report.json — validated against the server's
// WeeklyReportOut pydantic model; see README).

final class WeeklyReportContractTests: XCTestCase {
    private func fixtureData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: WeeklyReportContractTests.self)
                .url(forResource: "weekly_report", withExtension: "json"),
            "weekly_report.json fixture missing from the test bundle resources"
        )
        return try Data(contentsOf: url)
    }

    func testDecodesFixtureExactly() throws {
        let report = try GlanceJSON.decoder().decode(WeeklyReport.self, from: fixtureData())

        XCTAssertEqual(report.timezone, "UTC")
        XCTAssertEqual(report.weekStart, "2026-07-03")
        XCTAssertEqual(report.weekEnd, "2026-07-09")
        XCTAssertEqual(
            report.reportUrl,
            "http://192.168.1.20:8100/reports/weekly?token=hm-ro-3q2b8d1f7c6e5a4"
        )

        // Energy trend: 7 local days, one honestly-null day.
        XCTAssertEqual(report.energy.days.count, 7)
        XCTAssertEqual(report.energy.overallAvg, 58)
        XCTAssertEqual(report.energy.samples, 52)
        let missingDay = report.energy.days[2]
        XCTAssertEqual(missingDay.date, "2026-07-05")
        XCTAssertNil(missingDay.avgScore)
        XCTAssertEqual(missingDay.samples, 0)
        XCTAssertEqual(report.energy.days[0].avgScore, 61)
        XCTAssertEqual(report.energy.days[0].minScore, 48)
        XCTAssertEqual(report.energy.days[0].maxScore, 74)

        // Insights with the full confidence-badge ladder.
        XCTAssertEqual(report.insights.count, 3)
        XCTAssertEqual(
            report.insights.items.map(\.confidenceLevel),
            [.high, .medium, ReportConfidenceLevel.none]
        )
        XCTAssertEqual(report.insights.items[0].confidence ?? 0, 0.82, accuracy: 0.0001)
        XCTAssertNil(report.insights.items[2].confidence)
        XCTAssertEqual(report.insights.items[0].kind, "stress_by_hour")

        // Adherence.
        XCTAssertEqual(report.schedule.proposed, 1)
        XCTAssertEqual(report.schedule.accepted, 4)
        XCTAssertEqual(report.schedule.pushed, 1)
        XCTAssertEqual(report.schedule.declined, 1)
        XCTAssertEqual(report.schedule.decided, 6)
        XCTAssertEqual(report.schedule.acceptancePct, 83)

        // Alert digest.
        XCTAssertEqual(report.alerts.fired, 9)
        XCTAssertEqual(report.alerts.delivered, 6)
        XCTAssertEqual(report.alerts.dailyBudget, 8)
        XCTAssertEqual(report.alerts.weeklyBudget, 56)
        XCTAssertEqual(report.alerts.byRule.count, 3)
        XCTAssertEqual(report.alerts.byRule[0].ruleId, "stress_spike_vs_baseline")
        XCTAssertEqual(report.alerts.byRule[0].fired, 5)
        XCTAssertEqual(report.alerts.byRule[0].delivered, 4)

        // Decisions.
        XCTAssertEqual(report.decisions.count, 4)
        XCTAssertEqual(report.decisions.kindCounts, ["alert": 2, "schedule_change": 2])
        XCTAssertEqual(report.decisions.items.count, 2)
        XCTAssertEqual(report.decisions.items[0].kind, .alert)
        XCTAssertEqual(report.decisions.items[1].kind, .scheduleChange)
        XCTAssertTrue(report.decisions.items[0].url.contains("/decisions/"))
    }

    func testDecodesEmptyWeekShape() throws {
        // What a fresh instance returns: all sections present, zeroed/null.
        let json = """
            {
              "generated_at": "2026-07-09T14:23:00Z",
              "timezone": "UTC",
              "week_start": "2026-07-03",
              "week_end": "2026-07-09",
              "report_url": "http://127.0.0.1:8100/reports/weekly",
              "energy": {"days": [], "overall_avg": null, "samples": 0},
              "insights": {"count": 0, "items": []},
              "schedule": {
                "proposed": 0, "accepted": 0, "pushed": 0, "declined": 0,
                "decided": 0, "acceptance_pct": null
              },
              "alerts": {
                "fired": 0, "delivered": 0, "daily_budget": 8,
                "weekly_budget": 56, "by_rule": []
              },
              "decisions": {"count": 0, "kind_counts": {}, "items": []}
            }
            """
        let report = try GlanceJSON.decoder().decode(WeeklyReport.self, from: Data(json.utf8))
        XCTAssertNil(report.energy.overallAvg)
        XCTAssertNil(report.schedule.acceptancePct)
        XCTAssertTrue(report.decisions.kindCounts.isEmpty)
    }
}
