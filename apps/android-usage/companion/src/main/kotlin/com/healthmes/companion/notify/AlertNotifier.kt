package com.healthmes.companion.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.healthmes.companion.MainActivity
import com.healthmes.companion.R

/**
 * Dedicated channel + renderer for the §8.5 grammar (see
 * [NotificationGrammar]): observation as the title, the three grammar lines
 * as BigText, REAL action buttons (issue #10) and a "why this?" tap-through:
 *
 * - ✅ Apply / ❌ Keep as is → [com.healthmes.companion.work.ProposalActionWorker]
 *   via the broadcast receiver → the schedule-proposal accept/decline
 *   endpoints (bearer client, one-shot WorkManager).
 * - ✏️ Adjust → the in-app proposals screen.
 * - Content tap → the in-app decision viewer on `decision_url` (viewer token
 *   already embedded by the server), or the briefing home without one.
 *
 * The intent wiring follows [NotificationActionPlan] (JVM-tested mapping).
 */
object AlertNotifier {

    const val CHANNEL_ID = "healthmes_briefing_alerts"
    const val NOTIFICATION_ID = 4210

    fun notify(context: Context, grammar: NotificationGrammar) {
        ensureChannel(context)
        val plan = NotificationActionPlan.from(grammar)

        // Dedicated request code (never 0): see the registry note on
        // NotificationActionPlan — a shared code would let other notifiers'
        // FLAG_UPDATE_CURRENT updates clobber the decision-URL extras here.
        val contentIntent = when (val tap = plan.contentTap) {
            is NotificationActionPlan.ContentTap.Decision ->
                activityIntent(
                    context,
                    NotificationActionPlan.REQUEST_ALERT_CONTENT_TAP,
                    MainActivity.decisionIntent(context, tap.url),
                )

            is NotificationActionPlan.ContentTap.Home ->
                activityIntent(
                    context,
                    NotificationActionPlan.REQUEST_ALERT_CONTENT_TAP,
                    MainActivity.homeIntent(context),
                )
        }

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(grammar.observation)
            .setContentText(grammar.evidence)
            .setStyle(NotificationCompat.BigTextStyle().bigText(grammar.bigText()))
            .setContentIntent(contentIntent)
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .addAction(
                0,
                context.getString(R.string.notification_action_apply),
                broadcastIntent(context, plan.accept),
            )
            .addAction(
                0,
                context.getString(R.string.notification_action_adjust),
                activityIntent(
                    context,
                    NotificationActionPlan.REQUEST_ADJUST,
                    MainActivity.destinationIntent(context, plan.adjustDestination),
                ),
            )
            .addAction(
                0,
                context.getString(R.string.notification_action_keep),
                broadcastIntent(context, plan.decline),
            )
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

    private fun broadcastIntent(
        context: Context,
        spec: NotificationActionPlan.ActionSpec,
    ): PendingIntent =
        PendingIntent.getBroadcast(
            context,
            spec.requestCode,
            Intent(context, NotificationActionReceiver::class.java)
                .putExtra(NotificationActionReceiver.EXTRA_ACTION, spec.wireAction)
                .putExtra(NotificationActionReceiver.EXTRA_PROPOSAL_ID, spec.proposalId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    private fun activityIntent(context: Context, requestCode: Int, intent: Intent): PendingIntent =
        PendingIntent.getActivity(
            context,
            requestCode,
            intent,
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
