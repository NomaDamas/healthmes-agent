package com.healthmes.wear

import android.app.Activity
import android.content.ComponentName
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.wear.tiles.TileService
import androidx.wear.watchface.complications.datasource.ComplicationDataSourceUpdateRequester
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.BriefingRepository
import com.healthmes.wear.complication.EnergyComplicationService
import com.healthmes.wear.tile.BriefingTileService
import java.net.URI

/**
 * Minimal on-watch pairing screen (base URL + bearer token into encrypted
 * prefs — the same pattern as the phone apps). Standalone by design: the
 * watch talks to the user's HealthMes instance directly. Typing a URL on a
 * watch is tolerable exactly once; nicer pairing (QR/phone data layer) can
 * ride the final UX pass (docs/design/WATCH-NOTIFICATIONS.ko.md).
 *
 * This activity is also the tap target of the tile and the complication
 * (issue #7: tapping a glance surface opens the briefing): its status
 * readout (energy/next block/alerts) is the placeholder on-watch briefing
 * view until the domain expert designs a dedicated one.
 */
class WearPairingActivity : Activity() {

    private lateinit var repository: BriefingRepository
    private lateinit var serverUrlInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_pairing)

        repository = BriefingRepository(this)
        serverUrlInput = findViewById(R.id.server_url_input)
        tokenInput = findViewById(R.id.token_input)
        statusText = findViewById(R.id.status_text)

        serverUrlInput.setText(repository.prefs.serverUrl.orEmpty())
        tokenInput.setText(repository.prefs.token.orEmpty())

        findViewById<Button>(R.id.save_button).setOnClickListener { savePairing() }
        findViewById<Button>(R.id.fetch_button).setOnClickListener { fetchNow() }

        refreshStatus()
    }

    private fun savePairing() {
        val url = serverUrlInput.text?.toString()?.trim().orEmpty().trimEnd('/')
        val parsed = runCatching { URI(url) }.getOrNull()
        val scheme = parsed?.scheme?.lowercase()
        val host = parsed?.host
        if (url.isEmpty() || (scheme != "http" && scheme != "https") || host.isNullOrBlank()) {
            statusText.text = getString(R.string.error_invalid_url)
            return
        }
        repository.prefs.serverUrl = url
        repository.prefs.token = tokenInput.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        statusText.text = getString(R.string.status_saved)
    }

    private fun fetchNow() {
        if (!repository.prefs.isPaired) {
            statusText.text = getString(R.string.status_not_paired)
            return
        }
        statusText.text = getString(R.string.status_fetching)
        // One short-lived background thread per manual tap — nothing
        // long-lived; scheduled refresh on watch is the tile/complication
        // update cadence itself.
        Thread {
            val outcome = repository.refresh()
            val message = when (outcome) {
                is BriefingRepository.RefreshOutcome.Updated -> statusLine()
                is BriefingRepository.RefreshOutcome.Unchanged -> "Up to date. ${statusLine()}"
                is BriefingRepository.RefreshOutcome.Failed -> "Failed: ${outcome.reason}"
                is BriefingRepository.RefreshOutcome.NotPaired ->
                    getString(R.string.status_not_paired)
            }
            runOnUiThread {
                statusText.text = message
                requestSurfaceUpdates()
            }
        }.start()
    }

    private fun refreshStatus() {
        statusText.text =
            if (!repository.prefs.isPaired) getString(R.string.status_not_paired) else statusLine()
    }

    private fun statusLine(): String {
        val briefing = repository.cached() ?: return "Paired; no data yet."
        val state = BriefingDisplayState.from(briefing)
        return buildString {
            append("Energy ${state.scoreText} (${state.confidence})")
            state.nextBlockLine?.let { append("\nNext: $it") }
            append("\nAlerts: ${state.alertCount}")
        }
    }

    private fun requestSurfaceUpdates() {
        TileService.getUpdater(this).requestUpdate(BriefingTileService::class.java)
        ComplicationDataSourceUpdateRequester
            .create(this, ComponentName(this, EnergyComplicationService::class.java))
            .requestUpdateAll()
    }
}
