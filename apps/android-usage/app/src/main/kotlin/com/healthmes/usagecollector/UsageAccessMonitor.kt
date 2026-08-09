package com.healthmes.usagecollector

import android.app.Activity
import android.app.AppOpsManager
import android.app.Application
import android.os.Bundle
import com.healthmes.usagecollector.work.UploadScheduling

/**
 * Process-lifetime privacy guard for Usage Access transitions.
 *
 * Android exposes current AppOps state, not historical grant intervals. This
 * monitor combines process callbacks, activity resume, worker checks, and a
 * pre-settings boundary to cover every transition the app can observe.
 */
internal class UsageAccessMonitor(
    private val application: Application,
) : AppOpsManager.OnOpChangedListener, Application.ActivityLifecycleCallbacks {

    private val context = application.applicationContext
    private val prefs = CollectorPrefs(context)
    private val appOps =
        context.getSystemService(android.content.Context.APP_OPS_SERVICE) as AppOpsManager

    fun start() {
        application.registerActivityLifecycleCallbacks(this)
        appOps.startWatchingMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            context.packageName,
            this,
        )
        observePermission("process_start")
    }

    override fun onOpChanged(op: String?, packageName: String?) {
        if (
            op == AppOpsManager.OPSTR_GET_USAGE_STATS
            && (packageName == null || packageName == context.packageName)
        ) {
            observePermission("app_ops_change")
        }
    }

    override fun onActivityResumed(activity: Activity) {
        observePermission("activity_resume")
    }

    @Synchronized
    private fun observePermission(trigger: String) {
        if (prefs.collectionQuarantined) {
            UploadScheduling.disable(context)
            prefs.lastResult =
                "Collection quarantined after a persistence failure; re-enable explicitly."
            return
        }
        val granted = UsageAccess.isGranted(context)
        val observation = prefs.observeUsageAccess(
            granted = granted,
            nowMs = System.currentTimeMillis(),
        )
        if (!observation.persisted) {
            UploadScheduling.disable(context)
            prefs.lastResult =
                "Collection stopped: privacy boundary persistence failed ($trigger)."
            return
        }
        if (!granted) {
            prefs.lastResult = "Usage access revoked; collection boundary reset."
        }
        val shouldReport = (
            !granted
                || observation.boundaryReset
                || observation.previousGranted != granted
            )
        if (
            shouldReport
            && !prefs.serverUrl.isNullOrBlank()
            && (!granted || prefs.collectionEnabled)
        ) {
            UploadScheduling.uploadNow(context)
        }
    }

    override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) = Unit

    override fun onActivityStarted(activity: Activity) = Unit

    override fun onActivityPaused(activity: Activity) = Unit

    override fun onActivityStopped(activity: Activity) = Unit

    override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) = Unit

    override fun onActivityDestroyed(activity: Activity) = Unit
}
