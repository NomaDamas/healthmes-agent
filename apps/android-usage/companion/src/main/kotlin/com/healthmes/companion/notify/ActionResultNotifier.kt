package com.healthmes.companion.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.healthmes.companion.MainActivity

/**
 * Low-importance outcome toast-as-notification for the alert action buttons
 * (the tap happens with the app closed, so a real Toast may not be visible).
 * When the outcome needs a human choice (ambiguous / already resolved), the
 * tap opens the proposals screen.
 */
object ActionResultNotifier {

    const val CHANNEL_ID = "healthmes_action_results"
    const val NOTIFICATION_ID = 4212

    fun notify(context: Context, message: String, openProposals: Boolean) {
        ensureChannel(context)
        val intent = if (openProposals) {
            MainActivity.destinationIntent(context, NotificationActionPlan.DEST_PROPOSALS)
        } else {
            MainActivity.homeIntent(context)
        }
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(com.healthmes.companion.R.drawable.ic_notification)
            .setContentTitle(message)
            .setContentIntent(
                PendingIntent.getActivity(
                    context,
                    // Dedicated request code — see the registry note on
                    // NotificationActionPlan (extras survive other notifiers'
                    // FLAG_UPDATE_CURRENT updates only with a distinct code).
                    NotificationActionPlan.REQUEST_ACTION_RESULT_TAP,
                    intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                )
            )
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        val manager = NotificationManagerCompat.from(context)
        if (manager.areNotificationsEnabled()) {
            try {
                manager.notify(NOTIFICATION_ID, notification)
            } catch (_: SecurityException) {
                // Permission revoked mid-flight; nothing else to do.
            }
        }
    }

    private fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(com.healthmes.companion.R.string.action_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description =
                context.getString(com.healthmes.companion.R.string.action_channel_description)
        }
        manager.createNotificationChannel(channel)
    }
}
