package com.healthmes.usagecollector

import android.Manifest
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.CompoundButton
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.work.WorkManager
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import com.healthmes.usagecollector.net.normalizedSecureServerUrl
import com.healthmes.usagecollector.work.UploadScheduling

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
    private val notificationPermissionLauncher =
        registerForActivityResult(
            ActivityResultContracts.RequestPermission(),
        ) { granted ->
            if (granted) {
                onToggleCollection(true)
            } else {
                setSwitchSilently(false)
                prefs.lastResult =
                    "Collection not started: notification permission is required " +
                        "for the visible privacy guard."
                refreshStatus()
            }
        }

    private val switchListener = CompoundButton.OnCheckedChangeListener { _, isChecked ->
        onToggleCollection(isChecked)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = CollectorPrefs(this)
        if (prefs.collectionEnabled) {
            startPrivacyGuardOrStop()
        }
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
        if (prefs.collectionEnabled) {
            startPrivacyGuardOrStop()
        }
        // Also refreshes after the round trip to the usage-access settings.
        refreshStatus()
    }

    private fun savePairing() {
        val url = normalizedSecureServerUrl(
            serverUrlInput.text?.toString().orEmpty(),
        )
        if (url == null) {
            serverUrlLayout.error = getString(R.string.error_invalid_url)
            return
        }
        serverUrlLayout.error = null
        val token = tokenInput.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        val update = UsageAccessGuardRegistry.withBoundaryFence {
            prefs.updatePairing(url, token)
        }
        when (update) {
            is PairingUpdateResult.Updated -> {
                UploadScheduling.disable(this)
                if (prefs.collectionEnabled && startPrivacyGuardOrStop()) {
                    UploadScheduling.enable(this)
                    UploadScheduling.uploadNow(this)
                }
                toast(R.string.toast_pairing_saved)
            }

            is PairingUpdateResult.Unchanged ->
                toast(R.string.toast_pairing_saved)

            PairingUpdateResult.Failed -> {
                UploadScheduling.disable(this)
                prefs.lastResult =
                    "Pairing was not saved because its privacy boundary could not persist."
                refreshStatus()
            }
        }
    }

    private fun onToggleCollection(enabled: Boolean) {
        if (enabled) {
            if (
                !GuardVisibilityPolicy.isSatisfied(this)
                && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            ) {
                setSwitchSilently(false)
                notificationPermissionLauncher.launch(
                    Manifest.permission.POST_NOTIFICATIONS,
                )
                return
            }
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
            if (
                !UsageAccessGuardRegistry.withBoundaryFence {
                    prefs.updateCollectionEnabled(true)
                }
            ) {
                UploadScheduling.disable(this)
                setSwitchSilently(false)
                refreshStatus()
                return
            }
            if (!startPrivacyGuardOrStop()) {
                setSwitchSilently(false)
                refreshStatus()
                return
            }
            UploadScheduling.enable(this)
            UploadScheduling.uploadNow(this)
        } else {
            UsageAccessGuardRegistry.withBoundaryFence {
                prefs.updateCollectionEnabled(false)
            }
            UsageAccessGuardService.stop(this)
            UploadScheduling.disable(this)
        }
        refreshStatus()
    }

    private fun uploadNow() {
        if (prefs.collectionQuarantined) {
            UploadScheduling.disable(this)
            prefs.lastResult =
                "Collection quarantined after a persistence failure; re-enable explicitly."
            refreshStatus()
            return
        }
        if (prefs.serverUrl.isNullOrBlank()) {
            toast(R.string.toast_pair_first)
            return
        }
        if (!UsageAccess.isGranted(this)) {
            toast(R.string.toast_grant_first)
            UsageAccess.openSettings(this)
            return
        }
        if (prefs.collectionEnabled && !startPrivacyGuardOrStop()) {
            refreshStatus()
            return
        }
        UploadScheduling.uploadNow(this)
        toast(R.string.toast_upload_scheduled)
    }

    private fun startPrivacyGuardOrStop(): Boolean {
        if (UsageAccessGuardService.start(this)) return true
        UsageAccessGuardRegistry.withBoundaryFence {
            prefs.updateCollectionEnabled(false)
        }
        UploadScheduling.disable(this)
        prefs.lastResult =
            "Collection stopped: foreground privacy guard could not start."
        return false
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
