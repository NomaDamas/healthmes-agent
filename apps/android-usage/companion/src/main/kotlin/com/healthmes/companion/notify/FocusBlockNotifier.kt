package com.healthmes.companion.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.healthmes.briefing.BriefingDisplayState
import com.healthmes.briefing.GlanceBriefing
import com.healthmes.companion.MainActivity
import com.healthmes.companion.R
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Ongoing "current focus block" notification (issue #10): while a block from
 * the briefing's `next_blocks` is active, a quiet non-dismissable
 * notification shows its title and counts down the remaining time.
 *
 * Battery-honest by construction — there is NO foreground service and no
 * process stays alive: the 15-minute [com.healthmes.companion.work.RefreshWorker]
 * poll posts/updates it, the OS chronometer (`setChronometerCountDown`)
 * ticks the remaining time for free, and `setTimeoutAfter` auto-dismisses at
 * block end so a poll gap never leaves a stale "active" block up. This is
 * also the wrist story for now: Wear OS bridges phone notifications by
 * default, so the running block reaches the watch without an on-watch
 * ongoing-activity implementation (a native `androidx.wear.ongoing` surface
 * stays with the domain expert's watch UX pass —
 * docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
object FocusBlockNotifier {

    const val CHANNEL_ID = "healthmes_focus_block"
    const val NOTIFICATION_ID = 4211

    /** Poll hook: post/update while a block is active, else clear. */
    fun update(context: Context, briefing: GlanceBriefing?, nowMs: Long = System.currentTimeMillis()) {
        val block = briefing?.let { FocusBlockLogic.activeBlock(it.nextBlocks, nowMs) }
        if (block == null) {
            NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID)
            return
        }
        ensureChannel(context)

        val endMs = BriefingDisplayState.parseIsoInstant(block.endIso).toEpochMilli()
        val zone = briefing.zoneOrDefault()
        val endLocal = HOUR_MINUTE.format(
            BriefingDisplayState.parseIsoInstant(block.endIso).atZone(zone)
        )
        val title = block.title ?: context.getString(R.string.focus_untitled)
        val demand = block.energyDemand?.let { " · ${context.getString(R.string.home_block_demand, it)}" }.orEmpty()

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(context.getString(R.string.focus_until, endLocal) + demand)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            // OS-side countdown to the block end — no process needed.
            .setWhen(endMs)
            .setUsesChronometer(true)
            .setChronometerCountDown(true)
            // Self-cleaning between polls: gone the moment the block ends.
            .setTimeoutAfter(endMs - nowMs)
            .setContentIntent(
                PendingIntent.getActivity(
                    context,
                    // Dedicated request code: this extras-less home intent is
                    // filterEquals-identical to the other notifiers' taps, so
                    // sharing a code would wipe their extras on every poll
                    // (registry note on NotificationActionPlan).
                    NotificationActionPlan.REQUEST_FOCUS_BLOCK_TAP,
                    MainActivity.homeIntent(context),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                )
            )
            .build()

        val manager = NotificationManagerCompat.from(context)
        if (manager.areNotificationsEnabled()) {
            try {
                manager.notify(NOTIFICATION_ID, notification)
            } catch (_: SecurityException) {
                // Permission revoked mid-flight; the widget still shows the block.
            }
        }
    }

    private fun GlanceBriefing.zoneOrDefault(): ZoneId =
        runCatching { ZoneId.of(timezone) }.getOrDefault(ZoneId.systemDefault())

    private val HOUR_MINUTE = DateTimeFormatter.ofPattern("HH:mm")

    private fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(R.string.focus_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = context.getString(R.string.focus_channel_description)
        }
        manager.createNotificationChannel(channel)
    }
}
