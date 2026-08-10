package com.healthmes.usagecollector

import android.app.AppOpsManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.healthmes.usagecollector.work.UploadScheduling
import java.util.UUID

/**
 * Foreground privacy guard for Usage Access collection.
 *
 * Android does not expose historical permission transitions. Keeping the
 * AppOps listener in a foreground service lets workers prove that collection
 * stayed inside a currently observed process window. Process death destroys
 * the in-memory token, so a worker cannot import the unobserved gap.
 */
class UsageAccessGuardService : Service(), AppOpsManager.OnOpChangedListener {

    private val processEpoch = UUID.randomUUID().toString()
    private lateinit var prefs: CollectorPrefs
    private lateinit var appOps: AppOpsManager

    override fun onCreate() {
        super.onCreate()
        val foregroundType = if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
        ) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        } else {
            0
        }
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            foregroundNotification(),
            foregroundType,
        )
        prefs = CollectorPrefs(this)
        appOps = getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        appOps.startWatchingMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            packageName,
            this,
        )
    }

    @Synchronized
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val state = prefs.collectionWindowState()
        val userStarted = intent?.action == ACTION_USER_START
        if (state.usageSettingsPending && !userStarted) {
            UsageAccessGuardRegistry.invalidate(processEpoch)
            stopSelf()
            return START_NOT_STICKY
        }
        if (!currentGuardIsUsable(state)) {
            establishGuard(
                trigger = if (userStarted) "user_start" else "service_restart",
                allowPending = userStarted,
            )
        }
        return START_STICKY
    }

    override fun onOpChanged(op: String?, packageName: String?) {
        if (
            op == AppOpsManager.OPSTR_GET_USAGE_STATS
            && (packageName == null || packageName == this.packageName)
        ) {
            establishGuard(
                trigger = "app_ops_change",
                allowPending = false,
            )
        }
    }

    @Synchronized
    private fun establishGuard(
        trigger: String,
        allowPending: Boolean,
    ) {
        UsageAccessGuardRegistry.invalidate(processEpoch)
        if (prefs.collectionQuarantined) {
            failClosed(
                "Collection quarantined after a persistence failure; re-enable explicitly.",
            )
            return
        }
        if (prefs.collectionWindowState().usageSettingsPending && !allowPending) {
            stopSelf()
            return
        }
        if (!prefs.collectionEnabled) {
            stopSelf()
            return
        }

        val granted = UsageAccess.isGranted(this)
        val observation = prefs.observeUsageAccess(
            granted = granted,
            nowMs = System.currentTimeMillis(),
            forceBoundary = true,
        )
        if (!observation.persisted) {
            failClosed(
                "Collection stopped: privacy boundary persistence failed ($trigger).",
            )
            return
        }

        val state = prefs.collectionWindowState()
        if (granted) {
            UsageAccessGuardRegistry.publish(
                UsageAccessGuardToken(
                    processEpoch = processEpoch,
                    collectionGeneration = state.collectionGeneration,
                )
            )
            prefs.lastResult =
                "Foreground privacy guard active; collection window reset."
        } else if (!granted) {
            prefs.lastResult = "Usage access revoked; collection boundary reset."
        }

        if (
            !prefs.serverUrl.isNullOrBlank()
        ) {
            UploadScheduling.uploadNow(this)
        }
    }

    private fun currentGuardIsUsable(state: CollectionWindowState): Boolean {
        val token = UsageAccessGuardRegistry.snapshot() ?: return false
        return token.processEpoch == processEpoch &&
            token.collectionGeneration == state.collectionGeneration &&
            !state.usageSettingsPending &&
            prefs.collectionEnabled &&
            UsageAccess.isGranted(this)
    }

    private fun failClosed(message: String) {
        UsageAccessGuardRegistry.invalidate(processEpoch)
        UploadScheduling.disable(this)
        prefs.lastResult = message
        stopSelf()
    }

    private fun foregroundNotification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                getString(R.string.guard_notification_channel),
                NotificationManager.IMPORTANCE_LOW,
            )
        )
        return Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(R.string.guard_notification_title))
            .setContentText(getString(R.string.guard_notification_text))
            .setOngoing(true)
            .build()
    }

    @Synchronized
    override fun onDestroy() {
        UsageAccessGuardRegistry.invalidate(processEpoch)
        if (::appOps.isInitialized) {
            appOps.stopWatchingMode(this)
        }
        ServiceCompat.stopForeground(
            this,
            ServiceCompat.STOP_FOREGROUND_REMOVE,
        )
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val ACTION_USER_START =
            "com.healthmes.usagecollector.action.START_USAGE_GUARD"
        private const val NOTIFICATION_CHANNEL_ID = "healthmes_usage_guard"
        private const val NOTIFICATION_ID = 4301

        fun start(context: Context): Boolean =
            runCatching {
                ContextCompat.startForegroundService(
                    context,
                    Intent(context, UsageAccessGuardService::class.java)
                        .setAction(ACTION_USER_START),
                )
                true
            }.getOrDefault(false)

        fun stop(context: Context) {
            UsageAccessGuardRegistry.invalidate()
            context.stopService(
                Intent(context, UsageAccessGuardService::class.java),
            )
        }
    }
}
