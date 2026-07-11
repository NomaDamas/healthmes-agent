package com.healthmes.companion.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkRequest
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.healthmes.api.HealthmesApi
import com.healthmes.api.Proposal
import com.healthmes.api.ProposalsPage
import com.healthmes.briefing.PairingPrefs
import com.healthmes.companion.R
import com.healthmes.companion.notify.ActionResultNotifier
import com.healthmes.companion.work.ProposalActionLogic.Outcome
import com.healthmes.companion.work.ProposalActionLogic.Target
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONException

/**
 * One-shot WorkManager job behind the §8.5 notification buttons: resolves
 * the pending schedule proposal (unless an explicit id was passed), calls
 * `POST /v1/schedule/proposals/{id}/accept|decline` with the paired bearer
 * client, and posts a small result notification. Decision logic lives in
 * [ProposalActionLogic] (JVM-tested); this is the Android shell.
 */
class ProposalActionWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val context = applicationContext
        val action = inputData.getString(KEY_ACTION) ?: return@withContext Result.failure()
        val accept = action == ACTION_ACCEPT

        val prefs = PairingPrefs(context)
        val serverUrl = prefs.serverUrl
        if (serverUrl.isNullOrBlank()) {
            ActionResultNotifier.notify(
                context, context.getString(R.string.status_not_paired), openProposals = false
            )
            return@withContext Result.failure()
        }
        val api = HealthmesApi(serverUrl, prefs.token)

        val explicitId = inputData.getString(KEY_PROPOSAL_ID)
        val targetId: String = if (explicitId != null) {
            explicitId
        } else {
            when (val target = resolveTarget(api)) {
                is Target.Single -> target.proposal.id
                is Target.NonePending -> return@withContext finish(
                    context, Outcome.NonePending
                )
                is Target.Ambiguous -> return@withContext finish(
                    context, Outcome.Ambiguous(target.pendingCount)
                )
                null -> return@withContext retryOrFail(
                    context, Outcome.Retry("could not list pending proposals")
                )
            }
        }

        val response = api.post(Proposal.actionPath(targetId, accept))
        when (val outcome = ProposalActionLogic.classifyActionResponse(response)) {
            is Outcome.Retry -> retryOrFail(context, outcome)
            else -> finish(context, outcome)
        }
    }

    /** Null on transport/parse failure (caller retries). */
    private fun resolveTarget(api: HealthmesApi): Target? {
        val response = api.get(ProposalActionLogic.RESOLVE_PATH)
        if (response !is HealthmesApi.Response.Http || !response.isSuccess) return null
        return try {
            ProposalActionLogic.chooseTarget(ProposalsPage.parse(response.body))
        } catch (_: JSONException) {
            null
        }
    }

    private fun finish(context: Context, outcome: Outcome): Result {
        val (message, openProposals) = describe(context, outcome)
        ActionResultNotifier.notify(context, message, openProposals)
        return Result.success()
    }

    private fun retryOrFail(context: Context, outcome: Outcome.Retry): Result =
        if (runAttemptCount < MAX_RETRIES) {
            Result.retry()
        } else {
            ActionResultNotifier.notify(
                context,
                context.getString(R.string.action_result_failed, outcome.reason),
                openProposals = true,
            )
            Result.failure()
        }

    private fun describe(context: Context, outcome: Outcome): Pair<String, Boolean> =
        when (outcome) {
            is Outcome.Done ->
                if (outcome.status == "declined") {
                    context.getString(R.string.action_result_declined) to false
                } else {
                    context.getString(R.string.action_result_applied) to false
                }

            is Outcome.AlreadyResolved -> context.getString(
                R.string.action_result_already, outcome.currentStatus ?: "?"
            ) to true

            is Outcome.Gone -> context.getString(R.string.action_result_gone) to true
            is Outcome.NonePending -> context.getString(R.string.action_result_none) to true
            is Outcome.Ambiguous -> context.getString(
                R.string.action_result_ambiguous, outcome.pendingCount
            ) to true

            is Outcome.Retry ->
                context.getString(R.string.action_result_retrying) to false

            is Outcome.Failed -> context.getString(
                R.string.action_result_failed, outcome.reason
            ) to true
        }

    companion object {
        const val KEY_ACTION = "action"
        const val KEY_PROPOSAL_ID = "proposal_id"
        const val ACTION_ACCEPT = "accept"
        const val ACTION_DECLINE = "decline"
        private const val MAX_RETRIES = 3
        private const val WORK_NAME = "healthmes-proposal-action"

        /** Enqueue one action call (notification button tap). */
        fun enqueue(context: Context, action: String, proposalId: String?) {
            val request = OneTimeWorkRequestBuilder<ProposalActionWorker>()
                .setInputData(
                    workDataOf(KEY_ACTION to action, KEY_PROPOSAL_ID to proposalId)
                )
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                )
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    WorkRequest.MIN_BACKOFF_MILLIS,
                    TimeUnit.MILLISECONDS,
                )
                .build()
            // APPEND keeps a rapid apply-then-decline double tap ordered
            // (the second call answers 409 → "already resolved").
            WorkManager.getInstance(context).enqueueUniqueWork(
                WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request,
            )
        }
    }
}
