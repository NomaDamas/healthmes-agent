package com.healthmes.companion

import com.healthmes.api.HealthmesApi
import com.healthmes.api.Proposal
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

    @Test
    fun `explicit notification target uses direct lookup beyond the list window`() {
        val explicitId = "target-51"
        val body = """
            {"id": "$explicitId", "task_id": "11111111-2222-3333-4444-555555555555",
             "proposed_start": "2026-07-11T05:00:00", "proposed_end": "2026-07-11T06:00:00",
             "status": "proposed", "decision_record_id": null,
             "accept_resolution_token": "accept-target",
             "decline_resolution_token": "decline-target"}
        """.trimIndent()

        assertEquals(
            "/v1/schedule/proposals/$explicitId",
            ProposalActionLogic.resolvePath(explicitId),
        )
        assertEquals(
            null,
            ProposalActionLogic.resolvePath(null),
        )
        val target = ProposalActionLogic.chooseTarget(body, explicitId)
        assertTrue(target is Target.Single)
        assertEquals(explicitId, (target as Target.Single).proposal.id)
    }

    @Test
    fun `generic alert never infers a proposal target`() {
        assertEquals(
            Target.NonePending,
            ProposalActionLogic.chooseTarget("""{"data": []}""", explicitId = null),
        )
    }

    @Test
    fun `proposed detail without resolution tokens is not actionable`() {
        val explicitId = "expired-target"
        val body = """
            {"id": "$explicitId", "task_id": "11111111-2222-3333-4444-555555555555",
             "proposed_start": "2026-07-11T05:00:00", "proposed_end": "2026-07-11T06:00:00",
             "status": "proposed", "decision_record_id": null,
             "accept_resolution_token": null, "decline_resolution_token": null}
        """.trimIndent()

        assertEquals(Target.NonePending, ProposalActionLogic.chooseTarget(body, explicitId))
    }

    @Test
    fun `2xx with the proposal body reports the reached status`() {
        val body = """
            {"id": "aaa", "task_id": "t", "proposed_start": "2026-07-09T05:00:00Z",
             "proposed_end": "2026-07-09T06:00:00Z", "status": "accepted",
             "decision_record_id": null, "accept_resolution_token": null,
             "decline_resolution_token": null}
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
