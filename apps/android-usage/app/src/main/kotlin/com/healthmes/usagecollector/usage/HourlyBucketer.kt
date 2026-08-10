package com.healthmes.usagecollector.usage

import kotlin.math.max
import kotlin.math.min

/**
 * Minimal projection of an [android.app.usage.UsageEvents.Event] so the
 * bucketing logic stays pure Kotlin and unit-testable on the JVM.
 */
data class AppForegroundEvent(
    val packageName: String,
    /** Activity class name; null on some OEM event streams. */
    val activityClass: String?,
    val timestampMs: Long,
    val kind: Kind,
) {
    enum class Kind {
        /** ACTIVITY_RESUMED / MOVE_TO_FOREGROUND. */
        RESUMED,

        /** ACTIVITY_PAUSED. The package may still be foreground. */
        PAUSED,

        /** ACTIVITY_STOPPED, or MOVE_TO_BACKGROUND on pre-Android 10. */
        STOPPED,
    }
}

/**
 * One app's aggregated foreground usage within one clock hour, matching one
 * element of the `samples` array of `POST /v1/app-usage/batch`
 * (healthmes/api/app_usage.py, AppUsageSampleIn) minus the category, which is
 * looked up Android-side.
 */
data class UsageBucket(
    /** Epoch millis floored to the hour (top-of-hour UTC instant). */
    val bucketStartMs: Long,
    val packageName: String,
    val foregroundSeconds: Int,
    val launches: Int,
)

/**
 * Folds a chronological stream of resume/pause events into hourly buckets of
 * foreground seconds and launch counts per package.
 *
 * Semantics:
 * - A package is "foreground" while at least one of its activities is resumed;
 *   nested/overlapping activities of the same package never double count.
 * - A "launch" is a background-to-foreground transition, attributed to the
 *   bucket containing the resume timestamp.
 * - Foreground intervals are split across hour boundaries (epoch hours == UTC
 *   hours, so buckets align with the server's UTC hourly buckets).
 * - An orphan pause (no matching resume inside the window) means the app was
 *   already foreground when the window started: its time is counted from the
 *   window start (or from the package's previous interval end), but no launch
 *   is recorded.
 * - Intervals still open at the window end are counted up to the window end.
 *   The caller re-queries the growing hour and uploads it with a newer ordered
 *   snapshot sequence until a fully covered hour is sealed.
 */
object HourlyBucketer {

    const val HOUR_MS: Long = 3_600_000L

    /** Server-side cap for one app in one hourly bucket. */
    const val MAX_BUCKET_SECONDS: Int = 3600

    fun floorToHour(timestampMs: Long): Long = (timestampMs / HOUR_MS) * HOUR_MS

    fun bucket(
        events: List<AppForegroundEvent>,
        windowStartMs: Long,
        windowEndMs: Long,
    ): List<UsageBucket> {
        require(windowEndMs >= windowStartMs) { "windowEndMs must be >= windowStartMs" }

        val foregroundMs = HashMap<Pair<Long, String>, Long>()
        val launches = HashMap<Pair<Long, String>, Int>()

        fun addInterval(packageName: String, fromMs: Long, toMs: Long) {
            val from = max(fromMs, windowStartMs)
            val to = min(toMs, windowEndMs)
            var hour = floorToHour(from)
            while (hour < to) {
                val overlap = min(to, hour + HOUR_MS) - max(from, hour)
                if (overlap > 0) {
                    foregroundMs.merge(hour to packageName, overlap, Long::plus)
                }
                hour += HOUR_MS
            }
        }

        class Tracker {
            val resumedActivities = HashSet<String>()
            var anonymousResumedActivities: Int = 0
            var anonymousPausedActivities: Int = 0
            var foregroundSince: Long? = null
            var pendingPauseAtMs: Long? = null
            var lastIntervalEndMs: Long = windowStartMs

            fun hasResumedActivity(): Boolean =
                resumedActivities.isNotEmpty() || anonymousResumedActivities > 0
        }

        val trackers = HashMap<String, Tracker>()
        val sorted = events
            .filter { it.timestampMs in windowStartMs..windowEndMs }
            .sortedBy { it.timestampMs }

        for (event in sorted) {
            val tracker = trackers.getOrPut(event.packageName) { Tracker() }
            when (event.kind) {
                AppForegroundEvent.Kind.RESUMED -> {
                    if (tracker.foregroundSince == null) {
                        tracker.foregroundSince = event.timestampMs
                        launches.merge(
                            floorToHour(event.timestampMs) to event.packageName,
                            1,
                            Int::plus,
                        )
                    }
                    if (event.activityClass == null) {
                        tracker.anonymousResumedActivities += 1
                    } else {
                        tracker.resumedActivities.add(event.activityClass)
                    }
                    // Android commonly emits A.PAUSED before B.RESUMED for an
                    // in-package screen transition. Keep the package session
                    // open so the transition is not counted as a new launch.
                    tracker.pendingPauseAtMs = null
                }

                AppForegroundEvent.Kind.PAUSED -> {
                    if (event.activityClass == null) {
                        if (tracker.anonymousResumedActivities > 0) {
                            tracker.anonymousResumedActivities -= 1
                        }
                        tracker.anonymousPausedActivities += 1
                    } else {
                        tracker.resumedActivities.remove(event.activityClass)
                    }
                    if (!tracker.hasResumedActivity()) {
                        tracker.pendingPauseAtMs = event.timestampMs
                    }
                }

                AppForegroundEvent.Kind.STOPPED -> {
                    if (event.activityClass == null) {
                        if (tracker.anonymousPausedActivities > 0) {
                            // PAUSED already removed this anonymous activity.
                            tracker.anonymousPausedActivities -= 1
                        } else if (tracker.anonymousResumedActivities > 0) {
                            tracker.anonymousResumedActivities -= 1
                        }
                    } else {
                        tracker.resumedActivities.remove(event.activityClass)
                    }
                    if (
                        !tracker.hasResumedActivity() &&
                        (
                            tracker.foregroundSince != null ||
                                tracker.pendingPauseAtMs != null
                            )
                    ) {
                        // STOPPED can arrive after PAUSED. Attribute the end to
                        // the pause, not the later lifecycle cleanup event.
                        val endedAt = tracker.pendingPauseAtMs
                            ?.coerceAtMost(event.timestampMs)
                            ?: event.timestampMs
                        val since = tracker.foregroundSince
                            ?: tracker.lastIntervalEndMs
                        if (endedAt > since) {
                            addInterval(event.packageName, since, endedAt)
                        }
                        tracker.lastIntervalEndMs =
                            max(tracker.lastIntervalEndMs, endedAt)
                        tracker.foregroundSince = null
                        tracker.pendingPauseAtMs = null
                    }
                }
            }
        }

        // Close intervals still open at the window end. A final PAUSED without
        // its STOPPED pair ends at the pause; the next overlapping query can
        // still correct that provisional hour if a same-package resume follows.
        for ((packageName, tracker) in trackers) {
            val endedAt = tracker.pendingPauseAtMs ?: windowEndMs
            val since = tracker.foregroundSince ?: tracker.lastIntervalEndMs
            if (
                (tracker.foregroundSince != null || tracker.pendingPauseAtMs != null) &&
                endedAt > since
            ) {
                addInterval(packageName, since, endedAt)
            }
        }

        val keys = foregroundMs.keys + launches.keys
        return keys
            .map { key ->
                UsageBucket(
                    bucketStartMs = key.first,
                    packageName = key.second,
                    foregroundSeconds = min(
                        ((foregroundMs[key] ?: 0L) / 1000L).toInt(),
                        MAX_BUCKET_SECONDS,
                    ),
                    launches = launches[key] ?: 0,
                )
            }
            .filter { it.foregroundSeconds > 0 || it.launches > 0 }
            .sortedWith(compareBy({ it.bucketStartMs }, { it.packageName }))
    }
}
