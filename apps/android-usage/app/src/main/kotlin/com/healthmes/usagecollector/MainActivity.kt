package com.healthmes.usagecollector

import android.os.Bundle
import android.widget.Button
import android.widget.CompoundButton
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.work.WorkManager
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import com.healthmes.usagecollector.work.UploadScheduling
import java.net.URI

/**
 * The whole UI (docs/PLAN.md §7: "pairing + toggle, one screen"):
 * server URL + optional token (encrypted prefs), usage-access onboarding with
 * a deep link into system settings, the collection toggle, and a manual
 * "upload now" for verifying the pairing.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var prefs: CollectorPrefs
    private lateinit var serverUrlLayout: TextInputLayout
    private lateinit var serverUrlInput: TextInputEditText
    private lateinit var tokenInput: TextInputEditText
    private lateinit var permissionStatusText: TextView
    private lateinit var grantButton: Button
    private lateinit var collectSwitch: MaterialSwitch
    private lateinit var statusText: TextView

    private val switchListener = CompoundButton.OnCheckedChangeListener { _, isChecked ->
        onToggleCollection(isChecked)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = CollectorPrefs(this)
        serverUrlLayout = findViewById(R.id.server_url_layout)
        serverUrlInput = findViewById(R.id.server_url_input)
        tokenInput = findViewById(R.id.token_input)
        permissionStatusText = findViewById(R.id.permission_status_text)
        grantButton = findViewById(R.id.grant_button)
        collectSwitch = findViewById(R.id.collect_switch)
        statusText = findViewById(R.id.status_text)

        serverUrlInput.setText(prefs.serverUrl.orEmpty())
        tokenInput.setText(prefs.token.orEmpty())
        findViewById<TextView>(R.id.device_id_text).text =
            getString(R.string.device_id_label, prefs.deviceId)

        findViewById<Button>(R.id.save_button).setOnClickListener { savePairing() }
        grantButton.setOnClickListener {
            if (!UsageAccess.openSettings(this)) toast(R.string.toast_no_usage_settings)
        }
        findViewById<Button>(R.id.upload_now_button).setOnClickListener { uploadNow() }

        // Refresh the status line whenever any upload work changes state.
        val workManager = WorkManager.getInstance(this)
        workManager.getWorkInfosForUniqueWorkLiveData(UploadScheduling.PERIODIC_WORK_NAME)
            .observe(this) { refreshStatus() }
        workManager.getWorkInfosForUniqueWorkLiveData(UploadScheduling.ONE_SHOT_WORK_NAME)
            .observe(this) { refreshStatus() }
    }

    override fun onResume() {
        super.onResume()
        // Also refreshes after the round trip to the usage-access settings.
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
        prefs.serverUrl = url
        prefs.token = tokenInput.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        toast(R.string.toast_pairing_saved)
    }

    private fun onToggleCollection(enabled: Boolean) {
        if (enabled) {
            if (prefs.serverUrl.isNullOrBlank()) {
                toast(R.string.toast_pair_first)
                setSwitchSilently(false)
                return
            }
            if (!UsageAccess.isGranted(this)) {
                toast(R.string.toast_grant_first)
                UsageAccess.openSettings(this)
                setSwitchSilently(false)
                return
            }
            prefs.collectionEnabled = true
            UploadScheduling.enable(this)
            UploadScheduling.uploadNow(this)
        } else {
            prefs.collectionEnabled = false
            UploadScheduling.disable(this)
        }
        refreshStatus()
    }

    private fun uploadNow() {
        if (prefs.serverUrl.isNullOrBlank()) {
            toast(R.string.toast_pair_first)
            return
        }
        if (!UsageAccess.isGranted(this)) {
            toast(R.string.toast_grant_first)
            UsageAccess.openSettings(this)
            return
        }
        UploadScheduling.uploadNow(this)
        toast(R.string.toast_upload_scheduled)
    }

    private fun refreshStatus() {
        val granted = UsageAccess.isGranted(this)
        permissionStatusText.text =
            getString(if (granted) R.string.permission_granted else R.string.permission_missing)
        grantButton.isEnabled = !granted
        setSwitchSilently(prefs.collectionEnabled)
        statusText.text = prefs.lastResult ?: getString(R.string.no_uploads_yet)
    }

    private fun setSwitchSilently(checked: Boolean) {
        collectSwitch.setOnCheckedChangeListener(null)
        collectSwitch.isChecked = checked
        collectSwitch.setOnCheckedChangeListener(switchListener)
    }

    private fun toast(resId: Int) {
        Toast.makeText(this, resId, Toast.LENGTH_SHORT).show()
    }
}
