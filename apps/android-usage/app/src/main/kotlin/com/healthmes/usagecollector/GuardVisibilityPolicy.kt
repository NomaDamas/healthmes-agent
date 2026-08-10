package com.healthmes.usagecollector

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import androidx.core.app.NotificationManagerCompat

internal fun notificationPermissionAllowsVisibleGuard(
    apiLevel: Int,
    postNotificationsGranted: Boolean,
): Boolean =
    apiLevel < POST_NOTIFICATIONS_API || postNotificationsGranted

internal fun notificationSettingsAllowVisibleGuard(
    apiLevel: Int,
    postNotificationsGranted: Boolean,
    appNotificationsEnabled: Boolean,
    channelImportance: Int?,
): Boolean =
    notificationPermissionAllowsVisibleGuard(
        apiLevel = apiLevel,
        postNotificationsGranted = postNotificationsGranted,
    ) &&
        appNotificationsEnabled &&
        (
            apiLevel < NOTIFICATION_CHANNEL_API ||
                channelImportance == null ||
                channelImportance != NotificationManager.IMPORTANCE_NONE
            )

internal object GuardVisibilityPolicy {
    fun isSatisfied(context: Context): Boolean {
        val channelImportance = if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
        ) {
            context.getSystemService(NotificationManager::class.java)
                .getNotificationChannel(USAGE_GUARD_NOTIFICATION_CHANNEL_ID)
                ?.importance
        } else {
            null
        }
        return notificationSettingsAllowVisibleGuard(
            apiLevel = Build.VERSION.SDK_INT,
            postNotificationsGranted = ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED,
            appNotificationsEnabled = NotificationManagerCompat
                .from(context)
                .areNotificationsEnabled(),
            channelImportance = channelImportance,
        )
    }
}

internal const val USAGE_GUARD_NOTIFICATION_CHANNEL_ID =
    "healthmes_usage_guard"

private const val POST_NOTIFICATIONS_API = 33
private const val NOTIFICATION_CHANNEL_API = 26
