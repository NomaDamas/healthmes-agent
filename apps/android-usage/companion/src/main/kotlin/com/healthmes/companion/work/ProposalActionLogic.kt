package com.healthmes.companion.work

import com.healthmes.api.ApiError
import com.healthmes.api.HealthmesApi
import com.healthmes.api.Proposal
import org.json.JSONException

/**
 * Pure decision logic behind the notification Apply / Keep-as-is buttons
 * (JVM unit-tested; [ProposalActionWorker] is the thin Android shell).
 *
 * Proposal alerts must carry an explicit server-correlated id and use the
 * direct detail endpoint. Generic alerts never infer a target from the global
 * pending-proposal list (PLAN.md §11: a wrong assistant gets muted).
 */
object ProposalActionLogic {

    fun resolvePath(explicitId: String?): String? =
        explicitId?.let(Proposal::detailPath)

    sealed class Target {
        data class Single(val proposal: Proposal) : Target()
        data object NonePending : Target()
        data class Ambiguous(val pendingCount: Int) : Target()
    }

    @Throws(JSONException::class)
    fun chooseTarget(body: String, explicitId: String?): Target =
        if (explicitId != null) {
            val proposal = Proposal.parse(org.json.JSONObject(body))
            if (proposal.id == explicitId && proposal.isPending) {
                Target.Single(proposal)
            } else {
                Target.NonePending
            }
        } else {
            Target.NonePending
        }

    sealed class Outcome {
        /** 2xx — the proposal reached [status] ("accepted" / "declined"). */
        data class Done(val status: String) : Outcome()

        /** 409 invalid_transition — someone already resolved it. */
        data class AlreadyResolved(val currentStatus: String?) : Outcome()

        /** 404 — the proposal no longer exists. */
        data object Gone : Outcome()

        data object NonePending : Outcome()
        data class Ambiguous(val pendingCount: Int) : Outcome()

        /** Transport failure or 5xx — worth a WorkManager retry. */
        data class Retry(val reason: String) : Outcome()

        /** Other 4xx — retrying won't help. */
        data class Failed(val reason: String) : Outcome()
    }

    /** Maps the accept/decline HTTP response to an outcome. */
    fun classifyActionResponse(response: HealthmesApi.Response): Outcome = when (response) {
        is HealthmesApi.Response.NetworkError -> Outcome.Retry(response.reason)
        is HealthmesApi.Response.Http -> when {
            response.isSuccess -> {
                val status = try {
                    Proposal.parse(org.json.JSONObject(response.body)).status
                } catch (_: JSONException) {
                    "" // 2xx with unexpected body: still done, status unknown
                }
                Outcome.Done(status)
            }

            response.code == 409 ->
                Outcome.AlreadyResolved(ApiError.parseOrNull(response.body)?.detailCurrent)

            response.code == 404 -> Outcome.Gone

            response.code in 500..599 ->
                Outcome.Retry("HTTP ${response.code}")

            else -> Outcome.Failed(
                ApiError.parseOrNull(response.body)?.message ?: "HTTP ${response.code}"
            )
        }
    }
}
