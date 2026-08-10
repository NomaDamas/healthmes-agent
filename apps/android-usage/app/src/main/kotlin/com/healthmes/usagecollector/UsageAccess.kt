package com.healthmes.usagecollector

import android.app.AppOpsManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import android.provider.Settings
import com.healthmes.usagecollector.work.UploadScheduling

/**
 * PACKAGE_USAGE_STATS is "special access": it cannot be requested at runtime,
 * the user must flip a switch under Settings > Apps > Special app access >
 * Usage access. This helper checks the app op and deep-links to that screen.
 */
object UsageAccess {

    fun isGranted(context: Context): Boolean {
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            appOps.unsafeCheckOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS,
                Process.myUid(),
                context.packageName,
            )
        } else {
            @Suppress("DEPRECATION")
            appOps.checkOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS,
                Process.myUid(),
                context.packageName,
            )
        }
        return if (mode == AppOpsManager.MODE_DEFAULT) {
            context.checkCallingOrSelfPermission(
                android.Manifest.permission.PACKAGE_USAGE_STATS
            ) == PackageManager.PERMISSION_GRANTED
        } else {
            mode == AppOpsManager.MODE_ALLOWED
        }
    }

    /**
     * Opens the system "Usage access" screen after synchronously closing the
     * current readable window. Returning from settings starts the foreground
     * guard in a second boundary under the observed grant state.
     */
    fun openSettings(context: Context): Boolean {
        val prefs = CollectorPrefs(context)
        val nowMs = System.currentTimeMillis()
        UsageAccessGuardRegistry.invalidate()
        if (
            !prefs.markUsageSettingsOpened(
                nowMs = nowMs,
                currentlyGranted = isGranted(context),
            )
        ) {
            UsageAccessGuardService.stop(context)
            prefs.lastResult =
                "Usage access settings blocked: privacy boundary could not be saved."
            UploadScheduling.disable(context)
            return false
        }
        UsageAccessGuardService.stop(context)
        return try {
            context.startActivity(
                Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            true
        } catch (_: ActivityNotFoundException) {
            val observation = prefs.observeUsageAccess(
                granted = isGranted(context),
                nowMs = System.currentTimeMillis(),
            )
            if (!observation.persisted) {
                UploadScheduling.disable(context)
            } else if (prefs.collectionEnabled) {
                UsageAccessGuardService.start(context)
            }
            false
        }
    }
}
