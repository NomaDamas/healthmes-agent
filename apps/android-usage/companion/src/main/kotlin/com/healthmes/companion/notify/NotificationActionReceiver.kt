package com.healthmes.companion.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationManagerCompat
import com.healthmes.companion.work.ProposalActionWorker

/**
 * The §8.5 grammar buttons (✅ Apply / ❌ Keep as is) land here: dismiss the
 * alert notification and enqueue the real schedule-proposal call as a
 * one-shot WorkManager job ([ProposalActionWorker] — bearer client against
 * the paired instance; outcome arrives as a small result notification).
 * ✏️ Adjust bypasses this receiver and deep-links straight into the app's
 * proposals screen.
 */
class NotificationActionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        NotificationManagerCompat.from(context).cancel(AlertNotifier.NOTIFICATION_ID)
        val action = intent.getStringExtra(EXTRA_ACTION) ?: return
        if (action != ProposalActionWorker.ACTION_ACCEPT &&
            action != ProposalActionWorker.ACTION_DECLINE
        ) {
            return
        }
        ProposalActionWorker.enqueue(
            context,
            action,
            intent.getStringExtra(EXTRA_PROPOSAL_ID),
        )
    }

    companion object {
        const val EXTRA_ACTION = "healthmes.notification.action"
        const val EXTRA_PROPOSAL_ID = "healthmes.notification.proposal_id"
    }
}
