package com.healthmes.companion

import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import com.healthmes.briefing.BriefingRepository
import com.healthmes.companion.ui.CompanionApp
import com.healthmes.companion.ui.HealthmesTheme
import com.healthmes.companion.work.RefreshScheduling

/**
 * The single activity of the full companion app (issue #10). All screens are
 * Compose ([CompanionApp]); notification deep links arrive as intent extras
 * ([EXTRA_DESTINATION] / [EXTRA_DECISION_URL]) — `singleTask` launch mode
 * routes re-launches through [onNewIntent] into the running instance.
 */
class MainActivity : ComponentActivity() {

    /** Latest unconsumed deep-link request (consumed by the nav host). */
    private val pendingNav = mutableStateOf<NavRequest?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingNav.value = navRequestFrom(intent)

        // Self-heal the 15-minute refresh on every app open (idempotent).
        val repository = BriefingRepository(this)
        if (repository.prefs.isPaired) {
            RefreshScheduling.schedule(this)
        }

        setContent {
            HealthmesTheme {
                CompanionApp(
                    navRequest = pendingNav.value,
                    onNavConsumed = { pendingNav.value = null },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        navRequestFrom(intent)?.let { pendingNav.value = it }
    }

    /** A deep-link navigation request decoded from launch intent extras. */
    sealed class NavRequest {
        /** Switch to a tab by its route ("home", "proposals", …). */
        data class Tab(val route: String) : NavRequest()

        /** Open the in-app decision viewer on this tokenized URL. */
        data class Decision(val url: String) : NavRequest()
    }

    private fun navRequestFrom(intent: Intent?): NavRequest? {
        val destination = intent?.getStringExtra(EXTRA_DESTINATION) ?: return null
        return if (destination == DEST_DECISION) {
            intent.getStringExtra(EXTRA_DECISION_URL)?.let { NavRequest.Decision(it) }
        } else {
            NavRequest.Tab(destination)
        }
    }

    companion object {
        const val EXTRA_DESTINATION = "com.healthmes.companion.DESTINATION"
        const val EXTRA_DECISION_URL = "com.healthmes.companion.DECISION_URL"
        const val DEST_DECISION = "decision"

        private fun base(context: Context): Intent =
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

        /** Plain open (briefing home). */
        fun homeIntent(context: Context): Intent = base(context)

        /** Open on a specific tab route, e.g. the proposals screen. */
        fun destinationIntent(context: Context, destination: String): Intent =
            base(context).putExtra(EXTRA_DESTINATION, destination)

        /** Open the in-app decision viewer on a tokenized viewer URL. */
        fun decisionIntent(context: Context, url: String): Intent =
            base(context)
                .putExtra(EXTRA_DESTINATION, DEST_DECISION)
                .putExtra(EXTRA_DECISION_URL, url)
    }
}
