package com.healthmes.briefing

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Encrypted at-rest store for the pairing state (HealthMes base URL + bearer
 * token) plus the glance-payload cache. Same Jetpack Security pattern as the
 * collector app's CollectorPrefs (AndroidKeyStore-held AES-256-GCM master
 * key) — duplicated on purpose so the collector module stays untouched.
 *
 * Both :companion and :wear are separate processes/APKs, so each gets its own
 * pref file instance; the schema is shared here.
 */
class PairingPrefs(context: Context) {

    private val appContext = context.applicationContext

    private val prefs: SharedPreferences by lazy {
        val masterKey = MasterKey.Builder(appContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            appContext,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    /** Base URL of the user's HealthMes instance, e.g. http://192.168.1.20:8100 */
    var serverUrl: String?
        get() = prefs.getString(KEY_SERVER_URL, null)
        set(value) = prefs.edit { putString(KEY_SERVER_URL, value) }

    /** Optional API token, sent as `Authorization: Bearer <token>`. */
    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)
        set(value) = prefs.edit { putString(KEY_TOKEN, value) }

    val isPaired: Boolean
        get() = !serverUrl.isNullOrBlank()

    /** Strong ETag of [cachedBriefingJson], replayed as `If-None-Match`. */
    var cachedEtag: String?
        get() = prefs.getString(KEY_ETAG, null)
        set(value) = prefs.edit { putString(KEY_ETAG, value) }

    /** Last successfully fetched + parsed glance payload (raw JSON). */
    var cachedBriefingJson: String?
        get() = prefs.getString(KEY_BRIEFING_JSON, null)
        set(value) = prefs.edit { putString(KEY_BRIEFING_JSON, value) }

    /** Epoch millis of the last successful fetch (200 or 304); 0 = never. */
    var lastFetchMs: Long
        get() = prefs.getLong(KEY_LAST_FETCH_MS, 0L)
        set(value) = prefs.edit { putLong(KEY_LAST_FETCH_MS, value) }

    /**
     * Watermark for the local rising-count notification heuristic:
     * `alerts.unresolved_count` as of the last handled refresh. -1 = no
     * baseline yet (first fetch establishes one without notifying).
     */
    var lastSeenAlertCount: Int
        get() = prefs.getInt(KEY_LAST_SEEN_ALERTS, -1)
        set(value) = prefs.edit { putInt(KEY_LAST_SEEN_ALERTS, value) }

    /** Human-readable outcome of the last refresh (shown in pairing UIs). */
    var lastResult: String?
        get() = prefs.getString(KEY_LAST_RESULT, null)
        set(value) = prefs.edit { putString(KEY_LAST_RESULT, value) }

    /** Wipes pairing and cache (the pairing screens' "clear" action). */
    fun clear() = prefs.edit { clear() }

    private companion object {
        const val FILE_NAME = "healthmes_briefing"
        const val KEY_SERVER_URL = "server_url"
        const val KEY_TOKEN = "token"
        const val KEY_ETAG = "etag"
        const val KEY_BRIEFING_JSON = "briefing_json"
        const val KEY_LAST_FETCH_MS = "last_fetch_ms"
        const val KEY_LAST_SEEN_ALERTS = "last_seen_alert_count"
        const val KEY_LAST_RESULT = "last_result"
    }
}
