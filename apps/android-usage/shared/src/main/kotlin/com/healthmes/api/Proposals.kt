package com.healthmes.api

import org.json.JSONException
import org.json.JSONObject

/**
 * Parsed page of `GET /v1/schedule/proposals` (healthmes/api/schedule.py) and
 * the accept/decline action bodies. The propose-then-confirm gate
 * (docs/PLAN.md §2/§6): accepting marks `accepted`; the calendar sync layer
 * later advances it to `pushed`.
 */
data class ProposalsPage(val proposals: List<Proposal>, val pagination: PageMeta) {

    companion object {
        const val ENDPOINT_PATH = "/v1/schedule/proposals"

        @Throws(JSONException::class)
        fun parse(json: String): ProposalsPage {
            val root = JSONObject(json)
            val dataArr = root.getJSONArray("data")
            val proposals = buildList {
                for (i in 0 until dataArr.length()) {
                    add(Proposal.parse(dataArr.getJSONObject(i)))
                }
            }
            return ProposalsPage(proposals, PageMeta.parse(root.getJSONObject("pagination")))
        }
    }
}

data class Proposal(
    val id: String,
    val taskId: String,
    /**
     * ISO-8601 UTC instants. On the default sqlite deployment these are
     * NAIVE (`2026-07-11T14:51:20.497821`, no `Z`/offset — UTC by store
     * contract); Postgres emits an explicit offset. Parse with
     * [com.healthmes.briefing.BriefingDisplayState.parseIsoInstant], which
     * accepts all three shapes.
     */
    val proposedStartIso: String,
    val proposedEndIso: String,
    /** "proposed" | "accepted" | "pushed" | "declined". */
    val status: String,
    val decisionRecordId: String?,
) {
    val isPending: Boolean get() = status == STATUS_PROPOSED

    companion object {
        const val STATUS_PROPOSED = "proposed"

        @Throws(JSONException::class)
        fun parse(obj: JSONObject): Proposal = Proposal(
            id = obj.getString("id"),
            taskId = obj.getString("task_id"),
            proposedStartIso = obj.getString("proposed_start"),
            proposedEndIso = obj.getString("proposed_end"),
            status = obj.getString("status"),
            decisionRecordId = obj.stringOrNull("decision_record_id"),
        )

        /** Accept/decline action path (POST, empty body). */
        fun actionPath(proposalId: String, accept: Boolean): String =
            "${ProposalsPage.ENDPOINT_PATH}/$proposalId/${if (accept) "accept" else "decline"}"
    }
}
