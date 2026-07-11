package com.healthmes.companion.ui

import android.text.format.DateUtils
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.healthmes.api.AlertItem
import com.healthmes.api.AlertsPage
import com.healthmes.api.HealthmesApi
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.R
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Briefing home (issue #10): energy score + 24h curve + next blocks from the
 * cached/refreshed glance payload, the 24h alert history from
 * `GET /v1/alerts`, and the latest-decision link. Alert cards carry the
 * §8.5 "why this?" deep link into the decision viewer.
 *
 * Visual composition is PLACEHOLDER (docs/design/WATCH-NOTIFICATIONS.ko.md);
 * the data plumbing and information slots are real.
 */
@Composable
fun HomeScreen(
    services: AppServices,
    onOpenDecision: (String) -> Unit,
    onGoToSettings: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var briefing by remember { mutableStateOf<GlanceBriefing?>(null) }
    var alerts by remember { mutableStateOf<List<AlertItem>?>(null) }
    var alertsError by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reloadKey by remember { mutableIntStateOf(0) }
    val paired = services.prefs.isPaired

    LaunchedEffect(reloadKey) {
        if (!services.prefs.isPaired) return@LaunchedEffect
        loading = true
        briefing = services.repository.cached()
        val (fresh, alertsResult) = withContext(Dispatchers.IO) {
            services.repository.refresh()
            Pair(services.repository.cached(), loadAlerts(services.api()))
        }
        briefing = fresh
        alertsResult.fold(
            onSuccess = { alerts = it; alertsError = null },
            onFailure = { alertsError = it.message },
        )
        loading = false
    }

    if (!paired) {
        NotPaired(onGoToSettings, modifier)
        return
    }

    val current = briefing
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            ScoreCard(
                briefing = current,
                loading = loading,
                onRefresh = { reloadKey++ },
            )
        }
        if (current != null) {
            item { CurveCard(current) }
            item { NextBlocksCard(current) }
        }
        item {
            AlertsHeader()
        }
        when {
            alertsError != null -> item {
                Text(
                    stringResource(R.string.home_alerts_error, alertsError ?: ""),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }

            alerts?.isEmpty() == true -> item {
                Text(
                    stringResource(R.string.home_no_alerts),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            else -> items(alerts.orEmpty(), key = { it.id }) { alert ->
                AlertCard(alert, onOpenDecision)
            }
        }
        current?.latestDecision?.let { decision ->
            item { LatestDecisionCard(decision.url, onOpenDecision) }
        }
    }
}

private fun loadAlerts(api: HealthmesApi?): Result<List<AlertItem>> {
    if (api == null) return Result.failure(IllegalStateException("not paired"))
    return when (val response = api.get("${AlertsPage.ENDPOINT_PATH}?limit=20&offset=0")) {
        is HealthmesApi.Response.NetworkError -> Result.failure(Exception(response.reason))
        is HealthmesApi.Response.Http ->
            if (response.isSuccess) {
                runCatching { AlertsPage.parse(response.body).alerts }
            } else {
                Result.failure(Exception("HTTP ${response.code}"))
            }
    }
}

@Composable
private fun NotPaired(onGoToSettings: () -> Unit, modifier: Modifier) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            stringResource(R.string.home_not_paired),
            style = MaterialTheme.typography.bodyLarge,
        )
        Button(onClick = onGoToSettings) {
            Text(stringResource(R.string.home_go_to_settings))
        }
    }
}

@Composable
private fun ScoreCard(briefing: GlanceBriefing?, loading: Boolean, onRefresh: () -> Unit) {
    val display = briefing?.let { BriefingDisplayState.from(it) }
    val scoreText = display?.scoreText ?: BriefingDisplayState.NO_SCORE
    val confidence = display?.confidence ?: "low"
    val a11y = if (display?.score != null) {
        stringResource(R.string.score_a11y, scoreText, confidence)
    } else {
        stringResource(R.string.score_a11y_missing, confidence)
    }

    Card {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(
                modifier = Modifier
                    .weight(1f)
                    // One TalkBack stop reading the §1.4-style sentence.
                    .semantics { contentDescription = a11y },
            ) {
                Text(
                    scoreText,
                    style = MaterialTheme.typography.displayMedium,
                    color = MaterialTheme.colorScheme.primary,
                )
                Text(
                    stringResource(R.string.home_energy_label) + " · " +
                        stringResource(R.string.home_confidence, confidence),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                display?.generatedAtMs?.let { ms ->
                    Text(
                        stringResource(
                            R.string.home_generated_at,
                            DateUtils.getRelativeTimeSpanString(ms).toString(),
                        ),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (briefing == null) {
                    Text(
                        stringResource(R.string.home_no_data),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (loading) {
                CircularProgressIndicator(modifier = Modifier.padding(end = 8.dp))
            }
            IconButton(onClick = onRefresh, enabled = !loading) {
                Icon(
                    Icons.Filled.Refresh,
                    contentDescription = stringResource(R.string.home_refresh),
                )
            }
        }
    }
}

@Composable
private fun CurveCard(briefing: GlanceBriefing) {
    val zone = runCatching { ZoneId.of(briefing.timezone) }.getOrDefault(ZoneId.systemDefault())
    val currentHour = Instant.now().atZone(zone).hour
    val display = BriefingDisplayState.from(briefing)
    val a11y = stringResource(R.string.home_curve_a11y, display.scoreText, display.confidence)

    Card {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                stringResource(R.string.home_curve_title),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            Spacer(modifier = Modifier.width(8.dp))
            EnergyCurve(
                curve = briefing.energy.curve24h,
                currentHour = currentHour,
                modifier = Modifier
                    .padding(top = 8.dp)
                    .semantics { contentDescription = a11y },
            )
        }
    }
}

private val HOUR_MINUTE = DateTimeFormatter.ofPattern("HH:mm")

@Composable
private fun NextBlocksCard(briefing: GlanceBriefing) {
    val zone = runCatching { ZoneId.of(briefing.timezone) }.getOrDefault(ZoneId.systemDefault())
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                stringResource(R.string.home_next_blocks),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            if (briefing.nextBlocks.isEmpty()) {
                Text(
                    stringResource(R.string.home_no_blocks),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            briefing.nextBlocks.forEach { block ->
                val start = HOUR_MINUTE.format(
                    BriefingDisplayState.parseIsoInstant(block.startIso).atZone(zone)
                )
                val end = HOUR_MINUTE.format(
                    BriefingDisplayState.parseIsoInstant(block.endIso).atZone(zone)
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            "$start–$end  ${block.title ?: stringResource(R.string.focus_untitled)}",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        val details = buildList {
                            block.energyDemand?.let {
                                add(stringResource(R.string.home_block_demand, it))
                            }
                            if (block.source == "proposal") {
                                add(stringResource(R.string.home_block_source_proposal))
                            }
                        }
                        if (details.isNotEmpty()) {
                            Text(
                                details.joinToString(" · "),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun AlertsHeader() {
    Text(
        stringResource(R.string.home_alerts_title),
        style = MaterialTheme.typography.titleSmall,
        modifier = Modifier.semantics { heading() },
    )
}

@Composable
private fun AlertCard(alert: AlertItem, onOpenDecision: (String) -> Unit) {
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            // §8.5 grammar slots: observation / evidence / proposal / link.
            Text(alert.summary, style = MaterialTheme.typography.bodyLarge)
            alert.evidenceLine()?.let { line ->
                Text(
                    line,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            alert.proposal?.let { proposal ->
                Text(proposal, style = MaterialTheme.typography.bodyMedium)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                val firedMs = runCatching {
                    BriefingDisplayState.parseIsoInstant(alert.firedAtIso).toEpochMilli()
                }.getOrNull()
                AssistChip(
                    onClick = {},
                    enabled = false,
                    label = {
                        Text(
                            firedMs?.let {
                                DateUtils.getRelativeTimeSpanString(it).toString()
                            } ?: alert.ruleId,
                            style = MaterialTheme.typography.labelSmall,
                        )
                    },
                )
                Spacer(modifier = Modifier.weight(1f))
                alert.decisionUrl?.let { url ->
                    TextButton(onClick = { onOpenDecision(url) }) {
                        Text(stringResource(R.string.home_alert_why))
                    }
                }
            }
        }
    }
}

@Composable
private fun LatestDecisionCard(url: String, onOpenDecision: (String) -> Unit) {
    Card {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                stringResource(R.string.home_latest_decision),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { onOpenDecision(url) }) {
                Text(stringResource(R.string.home_open_decision))
            }
        }
    }
}
