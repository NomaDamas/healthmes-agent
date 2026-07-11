package com.healthmes.companion.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.companion.R
import com.healthmes.companion.notify.FocusBlockNotifier
import com.healthmes.companion.work.RefreshScheduling
import java.net.URI
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Pairing + status (the issue #7 pairing activity as a Compose screen): base URL +
 * bearer token into the shared encrypted prefs, the 15-minute refresh
 * schedule, and an honest status readout. Local-first note included — the
 * paired URL is the only network destination of the whole app.
 */
@Composable
fun SettingsScreen(services: AppServices, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    var serverUrl by remember { mutableStateOf(services.prefs.serverUrl.orEmpty()) }
    var token by remember { mutableStateOf(services.prefs.token.orEmpty()) }
    var urlError by remember { mutableStateOf(false) }
    var statusVersion by remember { mutableIntStateOf(0) }

    val requestNotifications = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* result only affects whether alerts render; nothing to store */ }

    fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                context, Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            stringResource(R.string.settings_title),
            style = MaterialTheme.typography.titleLarge,
            modifier = Modifier.semantics { heading() },
        )

        OutlinedTextField(
            value = serverUrl,
            onValueChange = { serverUrl = it; urlError = false },
            label = { Text(stringResource(R.string.hint_server_url)) },
            isError = urlError,
            supportingText = if (urlError) {
                { Text(stringResource(R.string.error_invalid_url)) }
            } else {
                null
            },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        // The bearer token authorizes the whole /v1 + /mcp surface: mask it
        // like the iOS (SecureField) and Windows (UseSystemPasswordChar)
        // pairing forms — never clear text on screen/screenshots/screen
        // share, and no keyboard suggestion/autofill learning.
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text(stringResource(R.string.hint_token)) },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            modifier = Modifier.fillMaxWidth(),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                val trimmed = serverUrl.trim().trimEnd('/')
                val parsed = runCatching { URI(trimmed) }.getOrNull()
                val scheme = parsed?.scheme?.lowercase()
                if (trimmed.isEmpty() || (scheme != "http" && scheme != "https") ||
                    parsed?.host.isNullOrBlank()
                ) {
                    urlError = true
                    return@Button
                }
                services.prefs.serverUrl = trimmed
                services.prefs.token = token.trim().takeIf { it.isNotEmpty() }
                serverUrl = trimmed
                RefreshScheduling.schedule(context)
                RefreshScheduling.refreshNow(context)
                requestNotificationPermissionIfNeeded()
                statusVersion++
                Toast.makeText(context, R.string.toast_pairing_saved, Toast.LENGTH_SHORT).show()
            }) { Text(stringResource(R.string.save_pairing)) }

            OutlinedButton(onClick = {
                if (!services.prefs.isPaired) {
                    Toast.makeText(context, R.string.toast_pair_first, Toast.LENGTH_SHORT).show()
                } else {
                    RefreshScheduling.refreshNow(context)
                    statusVersion++
                    Toast.makeText(context, R.string.toast_refresh_scheduled, Toast.LENGTH_SHORT)
                        .show()
                }
            }) { Text(stringResource(R.string.refresh_now)) }
        }
        TextButton(onClick = {
            RefreshScheduling.cancel(context)
            services.prefs.clear()
            // No more polling — take the ongoing focus block down with it.
            FocusBlockNotifier.update(context, briefing = null)
            serverUrl = ""
            token = ""
            statusVersion++
            Toast.makeText(context, R.string.toast_pairing_cleared, Toast.LENGTH_SHORT).show()
        }) { Text(stringResource(R.string.clear_pairing)) }

        StatusCard(services, statusVersion)

        Text(
            stringResource(R.string.settings_privacy_note),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

private val TIME_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d HH:mm")

@Composable
private fun StatusCard(services: AppServices, statusVersion: Int) {
    // statusVersion forces re-read of prefs after save/refresh/clear taps.
    @Suppress("UNUSED_EXPRESSION") statusVersion
    val lines = buildList {
        if (!services.prefs.isPaired) {
            add(stringResource(R.string.status_not_paired))
        } else {
            add(services.prefs.lastResult ?: stringResource(R.string.no_refresh_yet))
            services.repository.cached()?.let { briefing ->
                val state = BriefingDisplayState.from(briefing)
                add(
                    stringResource(R.string.home_energy_label) + " ${state.scoreText} · " +
                        stringResource(R.string.home_confidence, state.confidence)
                )
                state.nextBlockLine?.let { add(it) }
                add(
                    stringResource(
                        R.string.home_generated_at,
                        TIME_FORMAT.withZone(ZoneId.systemDefault())
                            .format(Instant.ofEpochMilli(state.generatedAtMs)),
                    )
                )
            }
        }
    }
    Card {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(
                stringResource(R.string.settings_status),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            lines.forEach { line ->
                Text(line, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}
