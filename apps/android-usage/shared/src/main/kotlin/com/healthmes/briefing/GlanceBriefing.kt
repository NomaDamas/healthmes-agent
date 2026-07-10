package com.healthmes.briefing

import org.json.JSONException
import org.json.JSONObject

/**
 * Parsed body of `GET /v1/briefing/glance` (healthmes/api/briefing.py).
 *
 * This mirrors the server contract verbatim; field-by-field semantics live in
 * the endpoint's docstring. Highlights the clients rely on:
 *
 * - `energy.score` is the latest persisted cognitive-energy window of the
 *   user-local day, `null` when nothing is persisted; `confidence` is a
 *   freshness ladder (`high`/`medium`/`low`), never absent.
 * - `energy.curve24h` has exactly 24 entries (local hours 0..23, ascending)
 *   with honest `null` scores for hours without a persisted window.
 * - `nextBlocks` has 0..3 entries, soonest first, merging mirrored calendar
 *   events with ACCEPTED schedule proposals.
 * - `alerts.unresolvedCount` counts alert-sent trigger events of the last
 *   24 h (placeholder resolution policy — see the server module docstring).
 * - Decision URLs are browser-tappable as-is (any required read-only viewer
 *   token is already embedded as a query parameter).
 *
 * Timestamps are kept as the raw ISO-8601 strings the server sent; use
 * [BriefingDisplayState] / [parseIsoInstant] for local-time rendering.
 */
data class GlanceBriefing(
    val generatedAtIso: String,
    val timezone: String,
    val energy: Energy,
    val nextBlocks: List<Block>,
    val alerts: Alerts,
    val latestDecision: Decision?,
) {

    data class Energy(
        val score: Int?,
        val confidence: String,
        val curve24h: List<CurvePoint>,
    )

    data class CurvePoint(val hour: Int, val score: Int?)

    data class Block(
        val startIso: String,
        val endIso: String,
        val title: String?,
        /** "low" | "med" | "high" | null — healthmes.store.enums.EnergyDemand. */
        val energyDemand: String?,
        /** "calendar" | "proposal". */
        val source: String,
    )

    data class Alerts(val unresolvedCount: Int, val top: TopAlert?)

    data class TopAlert(
        val ruleId: String,
        val summary: String,
        val decisionUrl: String?,
    )

    data class Decision(val id: String, val url: String)

    companion object {

        /**
         * Parses a glance payload. Unknown keys are ignored (additive server
         * evolution stays non-breaking); a missing/mistyped required key
         * throws [JSONException] — callers treat that as a failed fetch and
         * keep their cached copy.
         */
        @Throws(JSONException::class)
        fun parse(json: String): GlanceBriefing {
            val root = JSONObject(json)

            val energyObj = root.getJSONObject("energy")
            val curveArr = energyObj.getJSONArray("curve_24h")
            val curve = buildList {
                for (i in 0 until curveArr.length()) {
                    val point = curveArr.getJSONObject(i)
                    add(CurvePoint(point.getInt("hour"), point.optIntOrNull("score")))
                }
            }
            val energy = Energy(
                score = energyObj.optIntOrNull("score"),
                confidence = energyObj.getString("confidence"),
                curve24h = curve,
            )

            val blocksArr = root.getJSONArray("next_blocks")
            val blocks = buildList {
                for (i in 0 until blocksArr.length()) {
                    val block = blocksArr.getJSONObject(i)
                    add(
                        Block(
                            startIso = block.getString("start"),
                            endIso = block.getString("end"),
                            title = block.optStringOrNull("title"),
                            energyDemand = block.optStringOrNull("energy_demand"),
                            source = block.getString("source"),
                        )
                    )
                }
            }

            val alertsObj = root.getJSONObject("alerts")
            val topObj = alertsObj.optJSONObjectOrNull("top")
            val alerts = Alerts(
                unresolvedCount = alertsObj.getInt("unresolved_count"),
                top = topObj?.let {
                    TopAlert(
                        ruleId = it.getString("rule_id"),
                        summary = it.getString("summary"),
                        decisionUrl = it.optStringOrNull("decision_url"),
                    )
                },
            )

            val decisionObj = root.optJSONObjectOrNull("latest_decision")
            val latestDecision = decisionObj?.let {
                Decision(id = it.getString("id"), url = it.getString("url"))
            }

            return GlanceBriefing(
                generatedAtIso = root.getString("generated_at"),
                timezone = root.getString("timezone"),
                energy = energy,
                nextBlocks = blocks,
                alerts = alerts,
                latestDecision = latestDecision,
            )
        }

        private fun JSONObject.optIntOrNull(key: String): Int? =
            if (isNull(key)) null else getInt(key)

        private fun JSONObject.optStringOrNull(key: String): String? =
            if (isNull(key)) null else getString(key)

        private fun JSONObject.optJSONObjectOrNull(key: String): JSONObject? =
            if (isNull(key)) null else getJSONObject(key)
    }
}
