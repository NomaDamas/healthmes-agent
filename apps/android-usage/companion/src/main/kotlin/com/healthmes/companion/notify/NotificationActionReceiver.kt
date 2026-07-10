package com.healthmes.companion.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.core.app.NotificationManagerCompat
import com.healthmes.companion.R

/**
 * STUB: the §8.5 grammar buttons (Apply / Adjust / Keep as is) land here.
 * They only dismiss the notification and point the user at Telegram — wiring
 * them to real schedule actions requires the server-push path (future work)
 * and the domain expert's interaction design
 * (docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
class NotificationActionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        NotificationManagerCompat.from(context).cancel(AlertNotifier.NOTIFICATION_ID)
        Toast.makeText(context, R.string.toast_action_stub, Toast.LENGTH_LONG).show()
    }

    companion object {
        const val EXTRA_ACTION = "healthmes.notification.action"
    }
}
