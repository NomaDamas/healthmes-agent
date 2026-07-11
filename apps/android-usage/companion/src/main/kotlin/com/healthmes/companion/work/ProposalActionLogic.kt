package com.healthmes.companion.work

import com.healthmes.api.ApiError
import com.healthmes.api.HealthmesApi
import com.healthmes.api.Proposal
import com.healthmes.api.ProposalsPage
import org.json.JSONException

/**
 * Pure decision logic behind the notification Apply / Keep-as-is buttons
 * (JVM unit-tested; [ProposalActionWorker] is the thin Android shell).
 *
 * Alerts carry no proposal id yet (trigger events and planner-created
 * proposals are unlinked server-side), so a button tap resolves its target
 * at execution time from `GET /v1/schedule/proposals?status=proposed` — and
 * acts ONLY when that is unambiguous. With zero or 2+ pending proposals the
 * worker refuses to guess (PLAN.md §11: a wrong assistant gets muted) and
 * routes the user into the app instead.
 */
object ProposalActionLogic {

    /** Query for the resolve step: 2 rows is enough to detect ambiguity. */
    const val RESOLVE_PATH = "${ProposalsPage.ENDPOINT_PATH}?status=proposed&limit=2&offset=0"

    sealed class Target {
        data class Single(val proposal: Proposal) : Target()
        data object NonePending : Target()
        data class Ambiguous(val pendingCount: Int) : Target()
    }

    /** Chooses the action target from the pending-proposals page. */
    fun chooseTarget(page: ProposalsPage): Target {
        // total_count covers pending proposals beyond the fetched window.
        val pending = page.pagination.totalCount
        return when {
            pending <= 0 -> Target.NonePending
            pending == 1 && page.proposals.isNotEmpty() -> Target.Single(page.proposals.first())
            else -> Target.Ambiguous(pending)
        }
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
