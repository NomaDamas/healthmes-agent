package com.healthmes.companion.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SuggestionChip
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
import com.healthmes.api.HealthmesApi
import com.healthmes.api.WeeklyReport
import com.healthmes.companion.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Native rendering of `GET /reports/weekly.json` (issue #10) — the same
 * numbers as the `/reports/weekly` HTML page (parity holds server-side by
 * construction). "Open web version" hands the tokenized `report_url` to the
 * decision-viewer opener. Visual composition is PLACEHOLDER
 * (docs/design/WATCH-NOTIFICATIONS.ko.md); slots and numbers are real.
 */
@Composable
fun ReportScreen(
    services: AppServices,
    onOpenUrl: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var report by remember { mutableStateOf<WeeklyReport?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }
    var reloadKey by remember { mutableIntStateOf(0) }

    LaunchedEffect(reloadKey) {
        loading = true
        error = null
        val result = withContext(Dispatchers.IO) { loadReport(services.api()) }
        result.fold(
            onSuccess = { report = it },
            onFailure = { error = it.message },
        )
        loading = false
    }

    val current = report
    when {
        loading && current == null -> Loading(modifier)

        current == null -> Column(
            modifier = modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                stringResource(R.string.report_error, error ?: "?"),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodyMedium,
            )
            Button(onClick = { reloadKey++ }) {
                Text(stringResource(R.string.report_retry))
            }
        }

        else -> ReportBody(current, onOpenUrl, modifier)
    }
}

private fun loadReport(api: HealthmesApi?): Result<WeeklyReport> {
    if (api == null) return Result.failure(IllegalStateException("not paired"))
    return when (val response = api.get(WeeklyReport.ENDPOINT_PATH)) {
        is HealthmesApi.Response.NetworkError -> Result.failure(Exception(response.reason))
        is HealthmesApi.Response.Http ->
            if (response.isSuccess) {
                runCatching { WeeklyReport.parse(response.body) }
            } else {
                Result.failure(Exception("HTTP ${response.code}"))
            }
    }
}

@Composable
private fun Loading(modifier: Modifier) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        CircularProgressIndicator()
        Text(stringResource(R.string.report_loading))
    }
}

@Composable
private fun ReportBody(
    report: WeeklyReport,
    onOpenUrl: (String) -> Unit,
    modifier: Modifier,
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        stringResource(R.string.report_title),
                        style = MaterialTheme.typography.titleLarge,
                        modifier = Modifier.semantics { heading() },
                    )
                    Text(
                        stringResource(R.string.report_week, report.weekStart, report.weekEnd),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                TextButton(onClick = { onOpenUrl(report.reportUrl) }) {
                    Text(stringResource(R.string.report_open_html))
                }
            }
        }

        item { EnergySection(report.energy) }
        item { ScheduleSection(report.schedule) }
        item { AlertsSection(report.alerts) }

        item {
            Text(
                stringResource(R.string.report_insights_section, report.insights.count),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
        }
        if (report.insights.items.isEmpty()) {
            item { MutedText(stringResource(R.string.report_no_insights)) }
        } else {
            items(report.insights.items, key = { it.id }) { insight ->
                InsightCard(insight)
            }
        }

        item {
            Text(
                stringResource(R.string.report_decisions_section, report.decisions.count),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
        }
        if (report.decisions.items.isEmpty()) {
            item { MutedText(stringResource(R.string.report_no_decisions)) }
        } else {
            items(report.decisions.items, key = { it.id }) { decision ->
                DecisionCard(decision, onOpenUrl)
            }
        }
    }
}

@Composable
private fun MutedText(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

@Composable
private fun EnergySection(energy: WeeklyReport.Energy) {
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text(
                stringResource(R.string.report_energy_section),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            if (energy.samples == 0) {
                MutedText(stringResource(R.string.report_energy_none))
            } else {
                Text(
                    stringResource(
                        R.string.report_energy_overall,
                        energy.overallAvg?.toString() ?: "—",
                        energy.samples,
                    ),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
            energy.days.forEach { day -> EnergyDayRow(day) }
        }
    }
}

@Composable
private fun EnergyDayRow(day: WeeklyReport.EnergyDay) {
    // yyyy-MM-dd → MM-dd for the row label; the a11y line keeps the full date.
    val shortDate = day.date.drop(5)
    val a11y = if (day.avgScore != null) {
        stringResource(
            R.string.report_day_a11y,
            day.date,
            day.avgScore.toString(),
            day.minScore?.toString() ?: "—",
            day.maxScore?.toString() ?: "—",
            day.samples,
        )
    } else {
        stringResource(R.string.report_day_a11y_empty, day.date)
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .semantics { contentDescription = a11y },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            shortDate,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.width(48.dp),
        )
        // PLACEHOLDER bar rendering (expert-pending): width ∝ avg score.
        Box(
            modifier = Modifier
                .weight(1f)
                .height(10.dp)
                .background(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(5.dp),
                ),
        ) {
            day.avgScore?.let { avg ->
                Box(
                    modifier = Modifier
                        .fillMaxWidth(avg.coerceIn(0, 100) / 100f)
                        .height(10.dp)
                        .background(
                            color = MaterialTheme.colorScheme.primary,
                            shape = RoundedCornerShape(5.dp),
                        ),
                )
            }
        }
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            day.avgScore?.toString() ?: "—",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.width(28.dp),
        )
    }
}

@Composable
private fun ScheduleSection(schedule: WeeklyReport.Schedule) {
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(
                stringResource(R.string.report_schedule_section),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            Text(
                stringResource(
                    R.string.report_schedule_line,
                    schedule.accepted,
                    schedule.pushed,
                    schedule.declined,
                    schedule.proposed,
                ),
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                schedule.acceptancePct?.let {
                    stringResource(R.string.report_schedule_acceptance, it)
                } ?: stringResource(R.string.report_schedule_acceptance_none),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun AlertsSection(alerts: WeeklyReport.AlertDigest) {
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(
                stringResource(R.string.report_alerts_section),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            Text(
                stringResource(
                    R.string.report_alerts_line,
                    alerts.fired,
                    alerts.delivered,
                    alerts.dailyBudget,
                ),
                style = MaterialTheme.typography.bodyMedium,
            )
            alerts.byRule.forEach { rule ->
                Text(
                    "${rule.ruleId}: ${rule.delivered}/${rule.fired}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun InsightCard(insight: WeeklyReport.InsightItem) {
    val levelLabel = when (insight.confidenceLevel) {
        "high" -> stringResource(R.string.confidence_high)
        "medium" -> stringResource(R.string.confidence_medium)
        "low" -> stringResource(R.string.confidence_low)
        else -> stringResource(R.string.confidence_none)
    }
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(insight.statement, style = MaterialTheme.typography.bodyMedium)
            Row(verticalAlignment = Alignment.CenterVertically) {
                SuggestionChip(
                    onClick = {},
                    enabled = false,
                    label = { Text(levelLabel, style = MaterialTheme.typography.labelSmall) },
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    "${insight.kind} · ${insight.period}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun DecisionCard(decision: WeeklyReport.DecisionItem, onOpenUrl: (String) -> Unit) {
    Card {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(decision.summary, style = MaterialTheme.typography.bodyMedium)
                Text(
                    decision.kind,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            TextButton(onClick = { onOpenUrl(decision.url) }) {
                Text(stringResource(R.string.home_alert_why))
            }
        }
    }
}
