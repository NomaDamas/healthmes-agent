package com.healthmes.companion

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.work.WorkManager
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.BriefingRepository
import com.healthmes.companion.work.RefreshScheduling
import java.net.URI
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Minimal pairing screen — same base-URL + bearer-token pattern as the
 * collector app's MainActivity (pattern shared via :shared PairingPrefs, the
 * collector module itself is not imported). Saving schedules the 15-minute
 * briefing refresh; the widget and notifications feed off its cache.
 */
class PairingActivity : AppCompatActivity() {

    private lateinit var repository: BriefingRepository
    private lateinit var serverUrlLayout: TextInputLayout
    private lateinit var serverUrlInput: TextInputEditText
    private lateinit var tokenInput: TextInputEditText
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_pairing)

        repository = BriefingRepository(this)
        serverUrlLayout = findViewById(R.id.server_url_layout)
        serverUrlInput = findViewById(R.id.server_url_input)
        tokenInput = findViewById(R.id.token_input)
        statusText = findViewById(R.id.status_text)

        serverUrlInput.setText(repository.prefs.serverUrl.orEmpty())
        tokenInput.setText(repository.prefs.token.orEmpty())

        findViewById<Button>(R.id.save_button).setOnClickListener { savePairing() }
        findViewById<Button>(R.id.refresh_now_button).setOnClickListener { refreshNow() }
        findViewById<Button>(R.id.clear_button).setOnClickListener { clearPairing() }

        // Refresh the status line whenever refresh work changes state.
        val workManager = WorkManager.getInstance(this)
        workManager.getWorkInfosForUniqueWorkLiveData(RefreshScheduling.PERIODIC_WORK_NAME)
            .observe(this) { refreshStatus() }
        workManager.getWorkInfosForUniqueWorkLiveData(RefreshScheduling.ONE_SHOT_WORK_NAME)
            .observe(this) { refreshStatus() }

        requestNotificationPermissionIfNeeded()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun savePairing() {
        val url = serverUrlInput.text?.toString()?.trim().orEmpty().trimEnd('/')
        val parsed = runCatching { URI(url) }.getOrNull()
        val scheme = parsed?.scheme?.lowercase()
        val host = parsed?.host
        if (url.isEmpty() || (scheme != "http" && scheme != "https") || host.isNullOrBlank()) {
            serverUrlLayout.error = getString(R.string.error_invalid_url)
            return
        }
        serverUrlLayout.error = null
        repository.prefs.serverUrl = url
        repository.prefs.token = tokenInput.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        RefreshScheduling.schedule(this)
        RefreshScheduling.refreshNow(this)
        toast(R.string.toast_pairing_saved)
    }

    private fun refreshNow() {
        if (!repository.prefs.isPaired) {
            toast(R.string.toast_pair_first)
            return
        }
        RefreshScheduling.refreshNow(this)
        toast(R.string.toast_refresh_scheduled)
    }

    private fun clearPairing() {
        RefreshScheduling.cancel(this)
        repository.prefs.clear()
        serverUrlInput.setText("")
        tokenInput.setText("")
        refreshStatus()
        toast(R.string.toast_pairing_cleared)
    }

    private fun refreshStatus() {
        val prefs = repository.prefs
        if (!prefs.isPaired) {
            statusText.text = getString(R.string.status_not_paired)
            return
        }
        val lines = mutableListOf(prefs.lastResult ?: getString(R.string.no_refresh_yet))
        repository.cached()?.let { briefing ->
            val state = BriefingDisplayState.from(briefing)
            lines += "Energy ${state.scoreText} (confidence ${state.confidence})"
            state.nextBlockLine?.let { lines += "Next: $it" }
            lines += "Unresolved alerts: ${state.alertCount}"
            lines += "Generated: ${
                TIME_FORMAT.withZone(ZoneId.systemDefault())
                    .format(Instant.ofEpochMilli(state.generatedAtMs))
            }"
        }
        statusText.text = lines.joinToString("\n")
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun toast(resId: Int) {
        Toast.makeText(this, resId, Toast.LENGTH_SHORT).show()
    }

    private companion object {
        val TIME_FORMAT: DateTimeFormatter = DateTimeFormatter.ofPattern("MMM d HH:mm")
    }
}
