package com.healthmes.companion.ui

import android.content.Context
import com.healthmes.api.HealthmesApi
import com.healthmes.briefing.BriefingRepository
import com.healthmes.briefing.PairingPrefs

/**
 * Screen-facing service locator (deliberately tiny — no DI framework):
 * the shared briefing cache/refresh and a bearer client for the app
 * endpoints, both bound to the paired instance. Local-first: every network
 * call in the app goes through one of these two.
 */
class AppServices(context: Context) {

    val repository = BriefingRepository(context)

    val prefs: PairingPrefs get() = repository.prefs

    /** Null while unpaired — screens show their "pair first" state then. */
    fun api(): HealthmesApi? =
        prefs.serverUrl?.takeIf { it.isNotBlank() }?.let { HealthmesApi(it, prefs.token) }
}
