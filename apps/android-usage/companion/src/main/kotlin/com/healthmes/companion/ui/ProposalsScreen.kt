package com.healthmes.companion.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.healthmes.api.HealthmesApi
import com.healthmes.api.Proposal
import com.healthmes.api.ProposalsPage
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.companion.R
import com.healthmes.companion.work.ProposalActionLogic
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Schedule-proposal review (issue #10): the ✏️ Adjust notification button
 * deep-links here; accept/decline drive the real endpoints. A second tap on
 * an already-resolved proposal renders the server's 409 invalid_transition
 * as "already resolved" (per the contract audit) and reloads.
 */
@Composable
fun ProposalsScreen(services: AppServices, modifier: Modifier = Modifier) {
    var proposals by remember { mutableStateOf<List<Proposal>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }
    var pendingOnly by rememberSaveable { mutableStateOf(true) }
    var reloadKey by remember { mutableIntStateOf(0) }
    var actingOn by remember { mutableStateOf<String?>(null) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val context = androidx.compose.ui.platform.LocalContext.current

    LaunchedEffect(reloadKey, pendingOnly) {
        loading = true
        error = null
        val result = withContext(Dispatchers.IO) { loadProposals(services.api(), pendingOnly) }
        result.fold(
            onSuccess = { proposals = it },
            onFailure = { error = it.message },
        )
        loading = false
    }

    val act: (Proposal, Boolean) -> Unit = { proposal, accept ->
        actingOn = proposal.id
        scope.launch {
            val outcome = withContext(Dispatchers.IO) {
                services.api()?.let { api ->
                    ProposalActionLogic.classifyActionResponse(
                        api.post(Proposal.actionPath(proposal.id, accept))
                    )
                }
            }
            val text = when (outcome) {
                is ProposalActionLogic.Outcome.Done ->
                    if (outcome.status == "declined") {
                        context.getString(R.string.proposals_declined)
                    } else {
                        context.getString(R.string.proposals_accepted)
                    }

                is ProposalActionLogic.Outcome.AlreadyResolved -> context.getString(
                    R.string.proposals_already_resolved, outcome.currentStatus ?: "?"
                )

                is ProposalActionLogic.Outcome.Gone ->
                    context.getString(R.string.action_result_gone)

                is ProposalActionLogic.Outcome.Retry, null -> context.getString(
                    R.string.proposals_action_failed,
                    (outcome as? ProposalActionLogic.Outcome.Retry)?.reason ?: "not paired",
                )

                is ProposalActionLogic.Outcome.Failed ->
                    context.getString(R.string.proposals_action_failed, outcome.reason)

                else -> context.getString(R.string.proposals_action_failed, "?")
            }
            snackbar.showSnackbar(text)
            actingOn = null
            reloadKey++
        }
    }

    Column(modifier = modifier.fillMaxSize()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                stringResource(R.string.proposals_title),
                style = MaterialTheme.typography.titleLarge,
                modifier = Modifier
                    .weight(1f)
                    .semantics { heading() },
            )
            FilterChip(
                selected = pendingOnly,
                onClick = { pendingOnly = !pendingOnly },
                label = { Text(stringResource(R.string.proposals_pending_only)) },
            )
        }

        when {
            loading && proposals == null -> Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) { CircularProgressIndicator() }

            error != null -> Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    stringResource(R.string.proposals_error, error ?: "?"),
                    color = MaterialTheme.colorScheme.error,
                )
                OutlinedButton(onClick = { reloadKey++ }) {
                    Text(stringResource(R.string.report_retry))
                }
            }

            else -> LazyColumn(
                modifier = Modifier.weight(1f),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val list = proposals.orEmpty()
                if (list.isEmpty()) {
                    item {
                        Text(
                            stringResource(
                                if (pendingOnly) R.string.proposals_empty_pending
                                else R.string.proposals_empty
                            ),
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                items(list, key = { it.id }) { proposal ->
                    ProposalCard(
                        proposal = proposal,
                        busy = actingOn == proposal.id,
                        onAccept = { act(proposal, true) },
                        onDecline = { act(proposal, false) },
                    )
                }
            }
        }
        SnackbarHost(hostState = snackbar)
    }
}

private fun loadProposals(api: HealthmesApi?, pendingOnly: Boolean): Result<List<Proposal>> {
    if (api == null) return Result.failure(IllegalStateException("not paired"))
    val query = buildString {
        append("${ProposalsPage.ENDPOINT_PATH}?limit=50&offset=0")
        if (pendingOnly) append("&status=proposed")
    }
    return when (val response = api.get(query)) {
        is HealthmesApi.Response.NetworkError -> Result.failure(Exception(response.reason))
        is HealthmesApi.Response.Http ->
            if (response.isSuccess) {
                runCatching { ProposalsPage.parse(response.body).proposals }
            } else {
                Result.failure(Exception("HTTP ${response.code}"))
            }
    }
}

private val DAY_TIME: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d HH:mm")
private val TIME_ONLY: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")

@Composable
private fun ProposalCard(
    proposal: Proposal,
    busy: Boolean,
    onAccept: () -> Unit,
    onDecline: () -> Unit,
) {
    // parseIsoInstant accepts the wire formats the server actually emits
    // (aware `Z`/offset AND sqlite's naive-UTC datetimes); the runCatching is
    // a composition guard so an unforeseen malformed datetime degrades to the
    // raw string instead of crashing the whole app mid-render.
    val zone = ZoneId.systemDefault()
    val range = runCatching {
        val start = BriefingDisplayState.parseIsoInstant(proposal.proposedStartIso).atZone(zone)
        val end = BriefingDisplayState.parseIsoInstant(proposal.proposedEndIso).atZone(zone)
        "${DAY_TIME.format(start)}–${TIME_ONLY.format(end)}"
    }.getOrDefault("${proposal.proposedStartIso}–${proposal.proposedEndIso}")
    val a11y = stringResource(R.string.proposals_status_a11y, range, proposal.status)

    Card(modifier = Modifier.semantics { contentDescription = a11y }) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    range,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.weight(1f),
                )
                SuggestionChip(
                    onClick = {},
                    enabled = false,
                    label = { Text(proposal.status, style = MaterialTheme.typography.labelSmall) },
                )
            }
            if (proposal.isPending) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Spacer(modifier = Modifier.weight(1f))
                    TextButton(enabled = !busy, onClick = onDecline) {
                        Text(stringResource(R.string.proposals_decline))
                    }
                    Spacer(modifier = Modifier.width(8.dp))
                    OutlinedButton(enabled = !busy, onClick = onAccept) {
                        Text(stringResource(R.string.proposals_accept))
                    }
                }
            }
        }
    }
}
