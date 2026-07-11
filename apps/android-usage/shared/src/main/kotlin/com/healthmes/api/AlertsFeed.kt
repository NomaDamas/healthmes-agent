package com.healthmes.api

import org.json.JSONException
import org.json.JSONObject

/**
 * Parsed page of `GET /v1/alerts` (healthmes/api/alerts.py) — recent *pushed*
 * trigger events, newest first, with the same "unresolved == recent"
 * placeholder semantics as the glance `alerts` block. Each item carries the
 * §8.5 grammar lines recorded at fire time: observation ([AlertItem.summary]),
 * evidence facts (client renders the line), proposal, and the "why this?"
 * decision-viewer deep link (derived viewer token already embedded — the
 * server resolves it with the exact glance top-alert heuristic, so
 * `data[0]` always agrees with the widget's top alert).
 */
data class AlertsPage(val alerts: List<AlertItem>, val pagination: PageMeta) {

    companion object {
        const val ENDPOINT_PATH = "/v1/alerts"

        /** Unknown keys ignored (additive evolution); missing required → throw. */
        @Throws(JSONException::class)
        fun parse(json: String): AlertsPage {
            val root = JSONObject(json)
            val dataArr = root.getJSONArray("data")
            val alerts = buildList {
                for (i in 0 until dataArr.length()) {
                    add(AlertItem.parse(dataArr.getJSONObject(i)))
                }
            }
            return AlertsPage(alerts, PageMeta.parse(root.getJSONObject("pagination")))
        }
    }
}

data class AlertItem(
    val id: String,
    val ruleId: String,
    /** ISO-8601 UTC instant (Z suffix). */
    val firedAtIso: String,
    /** §8.5 observation line (server falls back to rule_id on legacy rows). */
    val summary: String,
    /** §8.5 proposal line, or null. */
    val proposal: String?,
    /** §8.5 evidence facts as "key: value" pairs, or empty. */
    val evidence: Map<String, String>,
    /** Browser-tappable "why this?" viewer link, or null. */
    val decisionUrl: String?,
) {

    /** One compact evidence line ("hrv_delta_pct: -18 · window_h: 24"). */
    fun evidenceLine(): String? =
        evidence.entries
            .joinToString(" · ") { (key, value) -> "$key: $value" }
            .takeIf { it.isNotBlank() }

    companion object {
        @Throws(JSONException::class)
        internal fun parse(obj: JSONObject): AlertItem {
            val evidenceObj = if (obj.isNull("evidence")) null else obj.getJSONObject("evidence")
            val evidence = buildMap {
                evidenceObj?.keys()?.forEach { key ->
                    put(key, evidenceObj.get(key).toString())
                }
            }
            return AlertItem(
                id = obj.getString("id"),
                ruleId = obj.getString("rule_id"),
                firedAtIso = obj.getString("fired_at"),
                summary = obj.getString("summary"),
                proposal = obj.stringOrNull("proposal"),
                evidence = evidence,
                decisionUrl = obj.stringOrNull("decision_url"),
            )
        }
    }
}
