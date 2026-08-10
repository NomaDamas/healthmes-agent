package com.healthmes.usagecollector

import android.annotation.SuppressLint
import android.content.Context
import android.content.SharedPreferences
import android.provider.Settings
import androidx.core.content.edit
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.healthmes.usagecollector.work.UploadScheduling
import com.healthmes.usagecollector.work.collectionWindowLeaseAllowed
import com.healthmes.usagecollector.work.commitFailClosedBoundary
import java.time.ZoneId
import java.util.UUID

internal data class CollectionWindowState(
    val collectionGeneration: Long,
    val collectionRevision: Int,
    val collectionSinceMs: Long,
    val watermarkMs: Long,
    val collectionTimezone: String?,
    val usageAccessGranted: Boolean?,
    val usageSettingsPending: Boolean,
)

internal data class PermissionObservationResult(
    val persisted: Boolean,
    val boundaryReset: Boolean,
    val previousGranted: Boolean?,
)

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

    /**
     * Non-sensitive fail-closed latch stored separately from encrypted state.
     *
     * If an encrypted collection-boundary commit fails, this ordinary
     * SharedPreferences file survives process restart and blocks every upload
     * path until an explicit user re-enable establishes a fresh boundary.
     */
    private val safetyPrefs: SharedPreferences by lazy {
        appContext.getSharedPreferences(SAFETY_FILE_NAME, Context.MODE_PRIVATE)
    }

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

    internal val collectionQuarantined: Boolean
        get() = synchronized(COLLECTION_STATE_LOCK) {
            collectionQuarantinedLocked()
        }

    var collectionEnabled: Boolean
        get() = synchronized(COLLECTION_STATE_LOCK) {
            !collectionQuarantinedLocked() &&
                runCatching { prefs.getBoolean(KEY_ENABLED, false) }
                    .getOrElse {
                        enterQuarantineLocked()
                        false
                    }
        }
        set(value) {
            updateCollectionEnabled(value)
        }

    /**
     * Apply an explicit user toggle. Re-enabling is the only path allowed to
     * clear quarantine, and it does so only after synchronously committing a
     * fresh readable boundary and disabled-history watermark.
     */
    internal fun updateCollectionEnabled(
        value: Boolean,
        nowMs: Long = System.currentTimeMillis(),
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            if (!value && collectionQuarantinedLocked()) {
                enforceDisabledLocked()
                return@synchronized true
            }
            val state = runCatching { collectionWindowStateLocked() }
                .getOrElse {
                    enterQuarantineLocked()
                    return@synchronized false
                }
            val storedEnabled = runCatching {
                prefs.getBoolean(KEY_ENABLED, false)
            }.getOrElse {
                enterQuarantineLocked()
                return@synchronized false
            }
            if (!value && !storedEnabled) {
                return@synchronized true
            }
            val committed = commitCollectionState(
                collectionStateEditor(
                    state.copy(
                        collectionGeneration = state.collectionGeneration + 1,
                        collectionSinceMs = if (value) nowMs else 0L,
                        watermarkMs = floorToHour(nowMs),
                        collectionTimezone = ZoneId.systemDefault().id,
                    ),
                ).putBoolean(KEY_ENABLED, value),
            )
            if (!committed) {
                return@synchronized false
            }
            true
        }

    internal fun collectionWindowState(): CollectionWindowState =
        synchronized(COLLECTION_STATE_LOCK) {
            collectionWindowStateLocked()
        }

    internal fun collectionWindowIsCurrent(
        expectedGeneration: Long,
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            collectionWindowIsCurrentLocked(
                expectedGeneration = expectedGeneration,
            )
        }

    internal fun advanceWatermarkIfCurrent(
        expectedGeneration: Long,
        watermarkMs: Long,
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            if (
                !collectionWindowIsCurrentLocked(
                    expectedGeneration = expectedGeneration,
                )
            ) {
                return@synchronized false
            }
            val committed = runCatching {
                prefs.edit()
                    .putLong(KEY_WATERMARK_MS, watermarkMs)
                    .commit()
            }.getOrDefault(false)
            collectionStatePersistenceHealthy = committed
            committed
        }

    /**
     * Persist the privacy revision, readable boundary, and upload watermark in
     * one synchronous disk commit. Callers must stop collection when false is
     * returned.
     */
    internal fun persistCollectionWindow(
        collectionRevision: Int,
        collectionSinceMs: Long,
        watermarkMs: Long,
        collectionTimezone: String = ZoneId.systemDefault().id,
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized false
            }
            val current = collectionWindowStateLocked()
            commitCollectionState(
                collectionStateEditor(
                    current.copy(
                        collectionGeneration = current.collectionGeneration + 1,
                        collectionRevision = collectionRevision,
                        collectionSinceMs = collectionSinceMs,
                        watermarkMs = watermarkMs,
                        collectionTimezone = collectionTimezone,
                    ),
                ),
            )
        }

    /**
     * Mark a trip to Usage Access settings before leaving this process. The
     * pending bit forces another boundary reset when the app or worker next
     * observes the permission state.
     */
    internal fun markUsageSettingsOpened(
        nowMs: Long,
        currentlyGranted: Boolean,
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized false
            }
            val current = collectionWindowStateLocked()
            commitCollectionState(
                collectionStateEditor(
                    current.copy(
                        collectionGeneration = current.collectionGeneration + 1,
                        collectionSinceMs = nowMs,
                        watermarkMs = floorToHour(nowMs),
                        collectionTimezone = ZoneId.systemDefault().id,
                        usageAccessGranted = currentlyGranted,
                        usageSettingsPending = true,
                    ),
                ),
            )
        }

    /**
     * Persist an observed grant transition and reset the readable boundary.
     * Initial observation is treated as a transition so an installation never
     * imports UsageStats history from before HealthMes first ran.
     */
    internal fun observeUsageAccess(
        granted: Boolean,
        nowMs: Long,
    ): PermissionObservationResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized PermissionObservationResult(
                    persisted = false,
                    boundaryReset = false,
                    previousGranted = null,
                )
            }
            val current = collectionWindowStateLocked()
            val resetBoundary = (
                current.usageSettingsPending
                    || current.usageAccessGranted == null
                    || current.usageAccessGranted != granted
                )
            if (!resetBoundary && collectionStatePersistenceHealthy) {
                return@synchronized PermissionObservationResult(
                    persisted = true,
                    boundaryReset = false,
                    previousGranted = current.usageAccessGranted,
                )
            }
            val target = if (resetBoundary) {
                current.copy(
                    collectionGeneration = current.collectionGeneration + 1,
                    collectionSinceMs = nowMs,
                    watermarkMs = floorToHour(nowMs),
                    collectionTimezone = ZoneId.systemDefault().id,
                    usageAccessGranted = granted,
                    usageSettingsPending = false,
                )
            } else {
                current
            }
            val persisted = commitCollectionState(collectionStateEditor(target))
            PermissionObservationResult(
                persisted = persisted,
                boundaryReset = persisted && resetBoundary,
                previousGranted = current.usageAccessGranted,
            )
        }

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

    private fun collectionWindowStateLocked(): CollectionWindowState {
        val values = prefs.all
        return CollectionWindowState(
            collectionGeneration = values[KEY_COLLECTION_GENERATION] as? Long ?: 0L,
            collectionRevision = values[KEY_COLLECTION_REVISION] as? Int ?: -1,
            collectionSinceMs = values[KEY_COLLECTION_SINCE_MS] as? Long ?: 0L,
            watermarkMs = values[KEY_WATERMARK_MS] as? Long ?: 0L,
            collectionTimezone = values[KEY_COLLECTION_TIMEZONE] as? String,
            usageAccessGranted = (
                values[KEY_USAGE_ACCESS_GRANTED] as? Boolean
                ),
            usageSettingsPending = (
                values[KEY_USAGE_SETTINGS_PENDING] as? Boolean ?: false
            ),
        )
    }

    private fun collectionStateEditor(
        state: CollectionWindowState,
    ): SharedPreferences.Editor {
        val editor = prefs.edit()
            .putLong(KEY_COLLECTION_GENERATION, state.collectionGeneration)
            .putInt(KEY_COLLECTION_REVISION, state.collectionRevision)
            .putLong(KEY_COLLECTION_SINCE_MS, state.collectionSinceMs)
            .putLong(KEY_WATERMARK_MS, state.watermarkMs)
            .putBoolean(KEY_USAGE_SETTINGS_PENDING, state.usageSettingsPending)
        if (state.collectionTimezone == null) {
            editor.remove(KEY_COLLECTION_TIMEZONE)
        } else {
            editor.putString(KEY_COLLECTION_TIMEZONE, state.collectionTimezone)
        }
        return if (state.usageAccessGranted == null) {
            editor.remove(KEY_USAGE_ACCESS_GRANTED)
        } else {
            editor.putBoolean(KEY_USAGE_ACCESS_GRANTED, state.usageAccessGranted)
        }
    }

    private fun commitCollectionState(editor: SharedPreferences.Editor): Boolean {
        var quarantineArmed = false
        val committed = commitFailClosedBoundary(
            armQuarantine = {
                armQuarantineLocked().also { quarantineArmed = it }
            },
            commitState = {
                runCatching { editor.commit() }.getOrDefault(false)
            },
            clearQuarantine = ::clearQuarantineLocked,
        )
        collectionStatePersistenceHealthy = committed
        if (!committed) {
            if (quarantineArmed) {
                enforceDisabledLocked()
            } else {
                disableSchedulingLocked()
            }
        }
        return committed
    }

    private fun collectionQuarantinedLocked(): Boolean {
        if (collectionQuarantinedInMemory) return true
        return runCatching {
            safetyPrefs.getBoolean(KEY_COLLECTION_QUARANTINED, false)
        }.getOrElse {
            collectionQuarantinedInMemory = true
            true
        }
    }

    private fun collectionWindowIsCurrentLocked(
        expectedGeneration: Long,
    ): Boolean {
        if (collectionQuarantinedLocked()) return false
        val currentGeneration = runCatching {
            prefs.getLong(KEY_COLLECTION_GENERATION, 0L)
        }.getOrElse {
            enterQuarantineLocked()
            return false
        }
        val enabled = runCatching {
            prefs.getBoolean(KEY_ENABLED, false)
        }.getOrElse {
            enterQuarantineLocked()
            return false
        }
        return collectionWindowLeaseAllowed(
            expectedGeneration = expectedGeneration,
            currentGeneration = currentGeneration,
            collectionEnabled = enabled,
            collectionQuarantined = false,
        )
    }

    private fun armQuarantineLocked(): Boolean {
        collectionQuarantinedInMemory = true
        val armed = runCatching {
            safetyPrefs.edit()
                .putBoolean(KEY_SAFETY_INITIALIZED, true)
                .putBoolean(KEY_COLLECTION_QUARANTINED, true)
                .commit()
        }.getOrDefault(false)
        if (!armed) {
            collectionStatePersistenceHealthy = false
            disableSchedulingLocked()
        }
        return armed
    }

    private fun clearQuarantineLocked(): Boolean {
        val cleared = runCatching {
            safetyPrefs.edit()
                .putBoolean(KEY_SAFETY_INITIALIZED, true)
                .putBoolean(KEY_COLLECTION_QUARANTINED, false)
                .commit()
        }.getOrDefault(false)
        if (cleared) {
            collectionQuarantinedInMemory = false
            collectionStatePersistenceHealthy = true
        }
        return cleared
    }

    private fun enterQuarantineLocked() {
        collectionStatePersistenceHealthy = false
        collectionQuarantinedInMemory = true
        val armed = runCatching {
            safetyPrefs.edit()
                .putBoolean(KEY_SAFETY_INITIALIZED, true)
                .putBoolean(KEY_COLLECTION_QUARANTINED, true)
                .commit()
        }.getOrDefault(false)
        if (armed) {
            enforceDisabledLocked()
        } else {
            disableSchedulingLocked()
        }
    }

    private fun enforceDisabledLocked() {
        runCatching {
            prefs.edit()
                .putBoolean(KEY_ENABLED, false)
                .commit()
        }
        disableSchedulingLocked()
    }

    private fun disableSchedulingLocked() {
        runCatching { UploadScheduling.disable(appContext) }
    }

    private companion object {
        val COLLECTION_STATE_LOCK = Any()
        @Volatile
        var collectionStatePersistenceHealthy = true
        @Volatile
        var collectionQuarantinedInMemory = false

        const val FILE_NAME = "healthmes_collector"
        const val SAFETY_FILE_NAME = "healthmes_collector_safety"
        const val KEY_SERVER_URL = "server_url"
        const val KEY_TOKEN = "token"
        const val KEY_ENABLED = "collection_enabled"
        const val KEY_COLLECTION_GENERATION = "collection_generation"
        const val KEY_COLLECTION_SINCE_MS = "collection_since_ms"
        const val KEY_COLLECTION_REVISION = "collection_revision"
        const val KEY_WATERMARK_MS = "watermark_ms"
        const val KEY_COLLECTION_TIMEZONE = "collection_timezone"
        const val KEY_USAGE_ACCESS_GRANTED = "usage_access_granted"
        const val KEY_USAGE_SETTINGS_PENDING = "usage_settings_pending"
        const val KEY_SAFETY_INITIALIZED = "safety_initialized"
        const val KEY_COLLECTION_QUARANTINED = "collection_quarantined"
        const val KEY_LAST_RESULT = "last_result"
        const val KEY_DEVICE_ID = "device_id"
        const val HOUR_MS = 60L * 60L * 1000L

        fun floorToHour(valueMs: Long): Long = valueMs - valueMs % HOUR_MS
    }
}
