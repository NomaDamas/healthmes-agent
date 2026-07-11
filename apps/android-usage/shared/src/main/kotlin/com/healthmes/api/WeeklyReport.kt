package com.healthmes.api

import org.json.JSONException
import org.json.JSONObject

/**
 * Parsed body of `GET /reports/weekly.json` (healthmes/api/reports.py
 * `WeeklyReportOut`) — the week-at-a-glance digest the app renders natively.
 * JSON/HTML parity holds server-side by construction, so this model is the
 * same data the `/reports/weekly` page shows.
 *
 * Honesty rules carry over: `null` scores mean "no persisted data that day",
 * item lists are display-capped while `count` fields cover the whole week.
 */
data class WeeklyReport(
    val generatedAtIso: String,
    val timezone: String,
    /** ISO dates (yyyy-MM-dd) of the 7-local-day window, inclusive. */
    val weekStart: String,
    val weekEnd: String,
    /** Browser-tappable HTML twin (viewer token embedded). */
    val reportUrl: String,
    val energy: Energy,
    val insights: Insights,
    val schedule: Schedule,
    val alerts: AlertDigest,
    val decisions: Decisions,
) {

    data class Energy(
        val days: List<EnergyDay>,
        val overallAvg: Int?,
        val samples: Int,
    )

    data class EnergyDay(
        val date: String,
        val avgScore: Int?,
        val minScore: Int?,
        val maxScore: Int?,
        val samples: Int,
    )

    data class Insights(val count: Int, val items: List<InsightItem>)

    data class InsightItem(
        val id: String,
        val period: String,
        val kind: String,
        val statement: String,
        val confidence: Double?,
        /** "high" | "medium" | "low" | "none" — server placeholder ladder. */
        val confidenceLevel: String,
        val createdAtIso: String,
    )

    data class Schedule(
        val proposed: Int,
        val accepted: Int,
        val pushed: Int,
        val declined: Int,
        val decided: Int,
        val acceptancePct: Int?,
    )

    data class AlertDigest(
        val fired: Int,
        val delivered: Int,
        val dailyBudget: Int,
        val weeklyBudget: Int,
        val byRule: List<RuleCount>,
    )

    data class RuleCount(val ruleId: String, val fired: Int, val delivered: Int)

    data class Decisions(
        val count: Int,
        val kindCounts: Map<String, Int>,
        val items: List<DecisionItem>,
    )

    data class DecisionItem(
        val id: String,
        /** healthmes.store.enums.DecisionKind value, e.g. "alert". */
        val kind: String,
        val summary: String,
        val createdAtIso: String,
        val url: String,
    )

    companion object {
        const val ENDPOINT_PATH = "/reports/weekly.json"

        /** Unknown keys ignored (additive evolution); missing required → throw. */
        @Throws(JSONException::class)
        fun parse(json: String): WeeklyReport {
            val root = JSONObject(json)

            val energyObj = root.getJSONObject("energy")
            val daysArr = energyObj.getJSONArray("days")
            val days = buildList {
                for (i in 0 until daysArr.length()) {
                    val day = daysArr.getJSONObject(i)
                    add(
                        EnergyDay(
                            date = day.getString("date"),
                            avgScore = day.intOrNull("avg_score"),
                            minScore = day.intOrNull("min_score"),
                            maxScore = day.intOrNull("max_score"),
                            samples = day.getInt("samples"),
                        )
                    )
                }
            }
            val energy = Energy(
                days = days,
                overallAvg = energyObj.intOrNull("overall_avg"),
                samples = energyObj.getInt("samples"),
            )

            val insightsObj = root.getJSONObject("insights")
            val insightArr = insightsObj.getJSONArray("items")
            val insightItems = buildList {
                for (i in 0 until insightArr.length()) {
                    val item = insightArr.getJSONObject(i)
                    add(
                        InsightItem(
                            id = item.getString("id"),
                            period = item.getString("period"),
                            kind = item.getString("kind"),
                            statement = item.getString("statement"),
                            confidence = if (item.isNull("confidence")) null else item.getDouble("confidence"),
                            confidenceLevel = item.getString("confidence_level"),
                            createdAtIso = item.getString("created_at"),
                        )
                    )
                }
            }
            val insights = Insights(count = insightsObj.getInt("count"), items = insightItems)

            val scheduleObj = root.getJSONObject("schedule")
            val schedule = Schedule(
                proposed = scheduleObj.getInt("proposed"),
                accepted = scheduleObj.getInt("accepted"),
                pushed = scheduleObj.getInt("pushed"),
                declined = scheduleObj.getInt("declined"),
                decided = scheduleObj.getInt("decided"),
                acceptancePct = scheduleObj.intOrNull("acceptance_pct"),
            )

            val alertsObj = root.getJSONObject("alerts")
            val ruleArr = alertsObj.getJSONArray("by_rule")
            val byRule = buildList {
                for (i in 0 until ruleArr.length()) {
                    val rule = ruleArr.getJSONObject(i)
                    add(
                        RuleCount(
                            ruleId = rule.getString("rule_id"),
                            fired = rule.getInt("fired"),
                            delivered = rule.getInt("delivered"),
                        )
                    )
                }
            }
            val alerts = AlertDigest(
                fired = alertsObj.getInt("fired"),
                delivered = alertsObj.getInt("delivered"),
                dailyBudget = alertsObj.getInt("daily_budget"),
                weeklyBudget = alertsObj.getInt("weekly_budget"),
                byRule = byRule,
            )

            val decisionsObj = root.getJSONObject("decisions")
            val kindCountsObj = decisionsObj.getJSONObject("kind_counts")
            val kindCounts = buildMap {
                kindCountsObj.keys().forEach { key -> put(key, kindCountsObj.getInt(key)) }
            }
            val decisionArr = decisionsObj.getJSONArray("items")
            val decisionItems = buildList {
                for (i in 0 until decisionArr.length()) {
                    val item = decisionArr.getJSONObject(i)
                    add(
                        DecisionItem(
                            id = item.getString("id"),
                            kind = item.getString("kind"),
                            summary = item.getString("summary"),
                            createdAtIso = item.getString("created_at"),
                            url = item.getString("url"),
                        )
                    )
                }
            }
            val decisions = Decisions(
                count = decisionsObj.getInt("count"),
                kindCounts = kindCounts,
                items = decisionItems,
            )

            return WeeklyReport(
                generatedAtIso = root.getString("generated_at"),
                timezone = root.getString("timezone"),
                weekStart = root.getString("week_start"),
                weekEnd = root.getString("week_end"),
                reportUrl = root.getString("report_url"),
                energy = energy,
                insights = insights,
                schedule = schedule,
                alerts = alerts,
                decisions = decisions,
            )
        }
    }
}
