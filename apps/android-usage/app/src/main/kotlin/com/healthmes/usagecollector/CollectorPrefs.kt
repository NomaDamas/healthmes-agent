package com.healthmes.usagecollector

import android.annotation.SuppressLint
import android.content.Context
import android.content.SharedPreferences
import android.provider.Settings
import androidx.core.content.edit
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.UUID

/**
 * Encrypted at-rest store for the pairing state (server URL + ingest token),
 * the collection toggle, the upload watermark, and the last upload result.
 *
 * Backed by Jetpack Security's [EncryptedSharedPreferences] with an
 * AndroidKeyStore-held AES-256-GCM master key, so the token never sits in
 * plain-text XML on disk.
 */
class CollectorPrefs(context: Context) {

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

    var collectionEnabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(value) = prefs.edit { putBoolean(KEY_ENABLED, value) }

    /**
     * Top-of-hour epoch millis up to which buckets were successfully uploaded.
     * 0 means "never uploaded" (the worker backfills a default window).
     */
    var watermarkMs: Long
        get() = prefs.getLong(KEY_WATERMARK_MS, 0L)
        set(value) = prefs.edit { putLong(KEY_WATERMARK_MS, value) }

    /** Human-readable outcome of the last upload attempt (shown on screen). */
    var lastResult: String?
        get() = prefs.getString(KEY_LAST_RESULT, null)
        set(value) = prefs.edit { putString(KEY_LAST_RESULT, value) }

    /**
     * Stable per-device identifier for the server's `device_id` (<= 64 chars).
     * Uses ANDROID_ID (stable per device + signing key) with a random UUID
     * fallback; generated once and persisted.
     */
    val deviceId: String
        @SuppressLint("HardwareIds")
        get() {
            prefs.getString(KEY_DEVICE_ID, null)?.let { return it }
            val androidId = Settings.Secure.getString(
                appContext.contentResolver,
                Settings.Secure.ANDROID_ID,
            )
            val suffix = androidId?.takeIf { it.isNotBlank() }
                ?: UUID.randomUUID().toString().replace("-", "").take(16)
            val id = "android-$suffix".take(64)
            prefs.edit { putString(KEY_DEVICE_ID, id) }
            return id
        }

    private companion object {
        const val FILE_NAME = "healthmes_collector"
        const val KEY_SERVER_URL = "server_url"
        const val KEY_TOKEN = "token"
        const val KEY_ENABLED = "collection_enabled"
        const val KEY_WATERMARK_MS = "watermark_ms"
        const val KEY_LAST_RESULT = "last_result"
        const val KEY_DEVICE_ID = "device_id"
    }
}
