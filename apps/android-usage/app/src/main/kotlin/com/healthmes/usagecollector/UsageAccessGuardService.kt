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
import android.os.Handler
import android.os.IBinder
import android.os.Looper
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
    private lateinit var serviceLease: UsageAccessGuardLease
    private val mainHandler = Handler(Looper.getMainLooper())
    @Volatile
    private var destroyed = false

    override fun onCreate() {
        super.onCreate()
        prefs = CollectorPrefs(this)
        ensureNotificationChannel()
        if (!GuardVisibilityPolicy.isSatisfied(this)) {
            stopForInvisibleGuard()
            return
        }
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
        appOps = getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        serviceLease = UsageAccessGuardRegistry.activateService(processEpoch) {
            mainHandler.post {
                establishGuard(
                    trigger = "deferred_boundary_recheck",
                    allowPending = false,
                    forceBoundaryWhenDenied = false,
                )
            }
        }
        appOps.startWatchingMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            packageName,
            this,
        )
    }

    @Synchronized
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (destroyed) return START_NOT_STICKY
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
        if (destroyed) return
        if (
            op == AppOpsManager.OPSTR_GET_USAGE_STATS
            && (packageName == null || packageName == this.packageName)
        ) {
            if (
                !::serviceLease.isInitialized
                || !UsageAccessGuardRegistry.invalidateForObservedBoundary(
                    serviceLease,
                )
            ) {
                return
            }
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
        forceBoundaryWhenDenied: Boolean = true,
    ) {
        if (destroyed) return
        if (!GuardVisibilityPolicy.isSatisfied(this)) {
            stopForInvisibleGuard()
            return
        }
        val publication = UsageAccessGuardRegistry.beginPublication(serviceLease)
            ?: return
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
            // A deferred granted result closes any unobserved revoke gap.
            // A still-denied result was already fenced by the worker and must
            // not create an endless boundary/upload loop.
            forceBoundary = granted || forceBoundaryWhenDenied,
        )
        if (!observation.persisted) {
            failClosed(
                "Collection stopped: privacy boundary persistence failed ($trigger).",
            )
            return
        }

        val state = observation.committedState
        if (state == null) {
            failClosed(
                "Collection stopped: committed privacy boundary was unavailable ($trigger).",
            )
            return
        }
        if (granted) {
            val token = UsageAccessGuardRegistry.publish(
                publication = publication,
                collectionGeneration = state.collectionGeneration,
                pairingRevision = state.pairingRevision,
            )
            if (token == null) {
                prefs.lastResult =
                    "Collection boundary changed before the privacy guard became active."
                return
            }
            prefs.lastResult =
                "Foreground privacy guard active; collection window reset."
        } else if (!granted) {
            prefs.lastResult = "Usage access revoked; collection boundary reset."
        }

        if (
            !prefs.serverUrl.isNullOrBlank()
            && (granted || observation.boundaryReset)
        ) {
            UploadScheduling.uploadNow(this)
        }
    }

    private fun currentGuardIsUsable(state: CollectionWindowState): Boolean {
        val token = UsageAccessGuardRegistry.snapshot() ?: return false
        return token.processEpoch == processEpoch &&
            token.serviceInstance == serviceLease.serviceInstance &&
            token.collectionGeneration == state.collectionGeneration &&
            token.pairingRevision == state.pairingRevision &&
            !state.usageSettingsPending &&
            prefs.collectionEnabled &&
            GuardVisibilityPolicy.isSatisfied(this) &&
            UsageAccess.isGranted(this)
    }

    private fun stopForInvisibleGuard() {
        destroyed = true
        val disabled = UsageAccessGuardRegistry.withBoundaryFence {
            prefs.updateCollectionEnabled(false)
        }
        if (::serviceLease.isInitialized) {
            UsageAccessGuardRegistry.closeService(serviceLease)
        } else {
            UsageAccessGuardRegistry.invalidate(processEpoch)
        }
        UploadScheduling.disable(this)
        prefs.lastResult = if (disabled) {
            "Collection stopped: notification permission is required " +
                "for the visible privacy guard."
        } else {
            "Collection stopped: notification visibility boundary could not be saved."
        }
        stopSelf()
    }

    private fun failClosed(message: String) {
        UsageAccessGuardRegistry.invalidate(processEpoch)
        UploadScheduling.disable(this)
        prefs.lastResult = message
        stopSelf()
    }

    private fun foregroundNotification(): Notification {
        ensureNotificationChannel()
        return Notification.Builder(this, USAGE_GUARD_NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(R.string.guard_notification_title))
            .setContentText(getString(R.string.guard_notification_text))
            .setOngoing(true)
            .build()
    }

    private fun ensureNotificationChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(
                USAGE_GUARD_NOTIFICATION_CHANNEL_ID,
                getString(R.string.guard_notification_channel),
                NotificationManager.IMPORTANCE_LOW,
            )
        )
    }

    @Synchronized
    override fun onDestroy() {
        destroyed = true
        if (::serviceLease.isInitialized) {
            UsageAccessGuardRegistry.closeService(serviceLease)
        } else {
            UsageAccessGuardRegistry.invalidate(processEpoch)
        }
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
        private const val NOTIFICATION_ID = 4301

        fun start(context: Context): Boolean =
            if (!GuardVisibilityPolicy.isSatisfied(context)) {
                false
            } else {
                runCatching {
                    ContextCompat.startForegroundService(
                        context,
                        Intent(context, UsageAccessGuardService::class.java)
                            .setAction(ACTION_USER_START),
                    )
                    true
                }.getOrDefault(false)
            }

        fun stop(context: Context) {
            UsageAccessGuardRegistry.invalidate()
            context.stopService(
                Intent(context, UsageAccessGuardService::class.java),
            )
        }
    }
}
