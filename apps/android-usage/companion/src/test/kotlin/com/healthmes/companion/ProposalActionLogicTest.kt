package com.healthmes.companion

import com.healthmes.api.HealthmesApi
import com.healthmes.api.ProposalsPage
import com.healthmes.companion.work.ProposalActionLogic
import com.healthmes.companion.work.ProposalActionLogic.Outcome
import com.healthmes.companion.work.ProposalActionLogic.Target
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The notification-button decision logic: which proposal a tap acts on, and
 * how the accept/decline HTTP responses map to user-facing outcomes
 * (409 invalid_transition → "already resolved", per the contract audit).
 */
class ProposalActionLogicTest {

    private fun page(totalCount: Int, ids: List<String>): ProposalsPage {
        // Datetimes are deliberately sqlite-NAIVE (no Z/offset): that is what
        // the default deployment's GET /v1/schedule/proposals really emits
        // (UTC by store contract). Z-suffixed fixtures previously masked a
        // parse crash in the proposals screen.
        val data = ids.joinToString(",") { id ->
            """
            {"id": "$id", "task_id": "11111111-2222-3333-4444-555555555555",
             "proposed_start": "2026-07-09T05:00:00.497821", "proposed_end": "2026-07-09T06:00:00.497821",
             "status": "proposed", "decision_record_id": null}
            """.trimIndent()
        }
        return ProposalsPage.parse(
            """
            {"data": [$data],
             "pagination": {"total_count": $totalCount, "limit": 2, "offset": 0,
                            "has_more": ${totalCount > ids.size}}}
            """.trimIndent()
        )
    }

    @Test
    fun `zero pending proposals means nothing to act on`() {
        assertEquals(Target.NonePending, ProposalActionLogic.chooseTarget(page(0, emptyList())))
    }

    @Test
    fun `exactly one pending proposal is the unambiguous target`() {
        val target = ProposalActionLogic.chooseTarget(page(1, listOf("aaa")))

        assertTrue(target is Target.Single)
        assertEquals("aaa", (target as Target.Single).proposal.id)
    }

    @Test
    fun `two or more pending proposals refuse to guess`() {
        assertEquals(
            Target.Ambiguous(2),
            ProposalActionLogic.chooseTarget(page(2, listOf("aaa", "bbb"))),
        )
        // total_count larger than the fetched window still counts as ambiguous.
        assertEquals(
            Target.Ambiguous(5),
            ProposalActionLogic.chooseTarget(page(5, listOf("aaa", "bbb"))),
        )
    }

    @Test
    fun `2xx with the proposal body reports the reached status`() {
        val body = """
            {"id": "aaa", "task_id": "t", "proposed_start": "2026-07-09T05:00:00Z",
             "proposed_end": "2026-07-09T06:00:00Z", "status": "accepted",
             "decision_record_id": null}
        """.trimIndent()

        val outcome = ProposalActionLogic.classifyActionResponse(
            HealthmesApi.Response.Http(200, body)
        )

        assertEquals(Outcome.Done("accepted"), outcome)
    }

    @Test
    fun `409 invalid_transition renders already resolved with the current status`() {
        val body = """
            {"error": {"code": "invalid_transition",
                       "message": "schedule_proposal cannot go accepted -> declined",
                       "detail": {"current": "accepted", "requested": "declined"}}}
        """.trimIndent()

        val outcome = ProposalActionLogic.classifyActionResponse(
            HealthmesApi.Response.Http(409, body)
        )

        assertEquals(Outcome.AlreadyResolved("accepted"), outcome)
    }

    @Test
    fun `404 means the proposal is gone`() {
        val body = """
            {"error": {"code": "not_found", "message": "schedule_proposal x not found",
                       "detail": null}}
        """.trimIndent()

        assertEquals(
            Outcome.Gone,
            ProposalActionLogic.classifyActionResponse(HealthmesApi.Response.Http(404, body)),
        )
    }

    @Test
    fun `5xx and transport failures retry, other 4xx fail permanently`() {
        assertEquals(
            Outcome.Retry("HTTP 503"),
            ProposalActionLogic.classifyActionResponse(HealthmesApi.Response.Http(503, "")),
        )
        assertEquals(
            Outcome.Retry("connect timed out"),
            ProposalActionLogic.classifyActionResponse(
                HealthmesApi.Response.NetworkError("connect timed out")
            ),
        )
        val unauthorized = """
            {"error": {"code": "unauthorized", "message": "invalid token", "detail": null}}
        """.trimIndent()
        assertEquals(
            Outcome.Failed("invalid token"),
            ProposalActionLogic.classifyActionResponse(
                HealthmesApi.Response.Http(401, unauthorized)
            ),
        )
        // Non-envelope error bodies still classify.
        assertEquals(
            Outcome.Failed("HTTP 400"),
            ProposalActionLogic.classifyActionResponse(HealthmesApi.Response.Http(400, "nope")),
        )
    }
}
