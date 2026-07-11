package com.healthmes.companion

import com.healthmes.api.WeeklyReport
import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Pins the `GET /reports/weekly.json` contract (healthmes/api/reports.py
 * `WeeklyReportOut`) the native report screen renders.
 */
class WeeklyReportParserTest {

    private val report = WeeklyReport.parse(Fixtures.load("weekly_report.json"))

    @Test
    fun `parses the window and viewer link`() {
        assertEquals("2026-07-09T05:30:00Z", report.generatedAtIso)
        assertEquals("Asia/Seoul", report.timezone)
        assertEquals("2026-07-03", report.weekStart)
        assertEquals("2026-07-09", report.weekEnd)
        assertEquals(
            "http://192.168.1.20:8100/reports/weekly?token=viewer-abc123",
            report.reportUrl,
        )
    }

    @Test
    fun `energy days keep honest nulls`() {
        assertEquals(7, report.energy.days.size)
        assertEquals(65, report.energy.overallAvg)
        assertEquals(58, report.energy.samples)

        val empty = report.energy.days[2]
        assertEquals("2026-07-05", empty.date)
        assertNull(empty.avgScore)
        assertNull(empty.minScore)
        assertNull(empty.maxScore)
        assertEquals(0, empty.samples)

        val full = report.energy.days[0]
        assertEquals(61, full.avgScore)
        assertEquals(44, full.minScore)
        assertEquals(78, full.maxScore)
    }

    @Test
    fun `insights carry the badge ladder`() {
        assertEquals(2, report.insights.count)
        assertEquals("high", report.insights.items[0].confidenceLevel)
        assertEquals(0.82, report.insights.items[0].confidence!!, 1e-9)
        assertEquals("none", report.insights.items[1].confidenceLevel)
        assertNull(report.insights.items[1].confidence)
    }

    @Test
    fun `schedule adherence counts round-trip`() {
        with(report.schedule) {
            assertEquals(2, proposed)
            assertEquals(3, accepted)
            assertEquals(1, pushed)
            assertEquals(1, declined)
            assertEquals(5, decided)
            assertEquals(80, acceptancePct)
        }
    }

    @Test
    fun `alert digest includes per-rule counts and budgets`() {
        with(report.alerts) {
            assertEquals(9, fired)
            assertEquals(6, delivered)
            assertEquals(8, dailyBudget)
            assertEquals(56, weeklyBudget)
            assertEquals(2, byRule.size)
            assertEquals("stress_spike_vs_baseline", byRule[0].ruleId)
            assertEquals(4, byRule[0].fired)
            assertEquals(3, byRule[0].delivered)
        }
    }

    @Test
    fun `decisions expose kind counts and tappable urls`() {
        assertEquals(3, report.decisions.count)
        assertEquals(mapOf("alert" to 2, "schedule_change" to 1), report.decisions.kindCounts)
        val item = report.decisions.items.single()
        assertEquals("alert", item.kind)
        assertEquals(
            "http://192.168.1.20:8100/decisions/7c9e6679-7425-40de-944b-e07fc1f90ae7?token=viewer-abc123",
            item.url,
        )
    }

    @Test
    fun `null acceptance pct survives`() {
        val noDecisions = Fixtures.load("weekly_report.json")
            .replace("\"acceptance_pct\": 80", "\"acceptance_pct\": null")

        assertNull(WeeklyReport.parse(noDecisions).schedule.acceptancePct)
    }

    @Test(expected = JSONException::class)
    fun `missing required key throws`() {
        WeeklyReport.parse(
            Fixtures.load("weekly_report.json").replace("\"timezone\": \"Asia/Seoul\",", "")
        )
    }
}
