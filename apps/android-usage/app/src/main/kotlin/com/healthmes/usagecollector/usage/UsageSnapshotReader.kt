package com.healthmes.usagecollector.usage

import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build

/**
 * Android-bound side of collection: drains [UsageStatsManager.queryEvents]
 * into pure [AppForegroundEvent]s and resolves each package's
 * [ApplicationInfo.category] to a stable lower-case label for the server's
 * `category` field.
 */
class UsageSnapshotReader(context: Context) {

    private val appContext = context.applicationContext
    private val categoryCache = HashMap<String, String?>()

    fun readEvents(beginMs: Long, endMs: Long): List<AppForegroundEvent> {
        val usageStatsManager =
            appContext.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val usageEvents = usageStatsManager.queryEvents(beginMs, endMs)
        val out = ArrayList<AppForegroundEvent>()
        val event = UsageEvents.Event()
        while (usageEvents.hasNextEvent()) {
            usageEvents.getNextEvent(event)
            val kind = kindOf(event.eventType) ?: continue
            val packageName = event.packageName ?: continue
            out.add(
                AppForegroundEvent(
                    packageName = packageName,
                    activityClass = event.className,
                    timestampMs = event.timeStamp,
                    kind = kind,
                )
            )
        }
        return out
    }

    /** Stable label for the server; null when Android has no category. */
    fun categoryOf(packageName: String): String? =
        categoryCache.getOrPut(packageName) {
            when (applicationInfoOrNull(packageName)?.category) {
                ApplicationInfo.CATEGORY_GAME -> "game"
                ApplicationInfo.CATEGORY_AUDIO -> "audio"
                ApplicationInfo.CATEGORY_VIDEO -> "video"
                ApplicationInfo.CATEGORY_IMAGE -> "image"
                ApplicationInfo.CATEGORY_SOCIAL -> "social"
                ApplicationInfo.CATEGORY_NEWS -> "news"
                ApplicationInfo.CATEGORY_MAPS -> "maps"
                ApplicationInfo.CATEGORY_PRODUCTIVITY -> "productivity"
                CATEGORY_ACCESSIBILITY -> "accessibility"
                else -> null
            }
        }

    private fun applicationInfoOrNull(packageName: String): ApplicationInfo? =
        try {
            val packageManager = appContext.packageManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                packageManager.getApplicationInfo(
                    packageName,
                    PackageManager.ApplicationInfoFlags.of(0),
                )
            } else {
                @Suppress("DEPRECATION")
                packageManager.getApplicationInfo(packageName, 0)
            }
        } catch (_: PackageManager.NameNotFoundException) {
            // Uninstalled since the usage event was recorded, or hidden by
            // package visibility filtering.
            null
        }

    private fun kindOf(eventType: Int): AppForegroundEvent.Kind? =
        @Suppress("DEPRECATION") // MOVE_TO_* are the pre-API-29 names (same int values).
        when (eventType) {
            UsageEvents.Event.MOVE_TO_FOREGROUND -> AppForegroundEvent.Kind.RESUMED
            UsageEvents.Event.MOVE_TO_BACKGROUND,
            EVENT_ACTIVITY_STOPPED,
            -> AppForegroundEvent.Kind.PAUSED

            else -> null
        }

    private companion object {
        /** UsageEvents.Event.ACTIVITY_STOPPED (API 29 constant, inlined int). */
        const val EVENT_ACTIVITY_STOPPED = 23

        /** ApplicationInfo.CATEGORY_ACCESSIBILITY (API 31 constant, inlined int). */
        const val CATEGORY_ACCESSIBILITY = 8
    }
}
