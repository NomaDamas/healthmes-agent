package com.healthmes.companion.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.healthmes.companion.PairingActivity
import com.healthmes.companion.R

/**
 * Dedicated channel + renderer for the §8.5 grammar (see
 * [NotificationGrammar]): observation as the title, the three grammar lines
 * as BigText, three stub action buttons, and a "why this?" deep link into the
 * decision viewer (`decision_url` is browser-tappable as-is — any viewer
 * token is already embedded by the server).
 */
object AlertNotifier {

    const val CHANNEL_ID = "healthmes_briefing_alerts"
    const val NOTIFICATION_ID = 4210

    fun notify(context: Context, grammar: NotificationGrammar) {
        ensureChannel(context)

        val contentIntent = grammar.decisionUrl?.let { url ->
            PendingIntent.getActivity(
                context,
                0,
                Intent(Intent.ACTION_VIEW, Uri.parse(url)),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        } ?: PendingIntent.getActivity(
            context,
            0,
            Intent(context, PairingActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(grammar.observation)
            .setContentText(grammar.evidence)
            .setStyle(NotificationCompat.BigTextStyle().bigText(grammar.bigText()))
            .setContentIntent(contentIntent)
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            // Grammar buttons — stubs until server push + action endpoints
            // exist (the receiver explains that on tap).
            .addAction(0, context.getString(R.string.notification_action_apply), actionStub(context, "apply"))
            .addAction(0, context.getString(R.string.notification_action_adjust), actionStub(context, "adjust"))
            .addAction(0, context.getString(R.string.notification_action_keep), actionStub(context, "keep"))
            .build()

        val manager = NotificationManagerCompat.from(context)
        if (manager.areNotificationsEnabled()) {
            try {
                manager.notify(NOTIFICATION_ID, notification)
            } catch (_: SecurityException) {
                // POST_NOTIFICATIONS revoked between check and notify — the
                // briefing stays visible on the widget either way.
            }
        }
    }

    private fun actionStub(context: Context, action: String): PendingIntent =
        PendingIntent.getBroadcast(
            context,
            action.hashCode(),
            Intent(context, NotificationActionReceiver::class.java)
                .putExtra(NotificationActionReceiver.EXTRA_ACTION, action),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    private fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = context.getString(R.string.notification_channel_description)
        }
        manager.createNotificationChannel(channel)
    }
}
