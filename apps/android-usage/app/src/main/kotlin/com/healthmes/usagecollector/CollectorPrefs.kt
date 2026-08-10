package com.healthmes.usagecollector

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.healthmes.usagecollector.work.UploadScheduling
import com.healthmes.usagecollector.work.collectionWindowLeaseAllowed
import com.healthmes.usagecollector.work.commitFailClosedBoundary
import com.healthmes.usagecollector.work.reservePersistedSnapshotSequence
import java.time.ZoneId
import java.util.UUID

internal data class CollectionWindowState(
    val collectionGeneration: Long,
    val pairingRevision: Long = 0L,
    val collectionRevision: Int,
    val collectionSinceMs: Long,
    val watermarkMs: Long,
    val collectionTimezone: String?,
    val usageAccessGranted: Boolean?,
    val usageSettingsPending: Boolean,
)

internal data class PairingSnapshot(
    val serverUrl: String?,
    val token: String?,
    val revision: Long,
)

internal sealed interface PairingUpdateResult {
    data class Updated(
        val state: CollectionWindowState,
        val pairing: PairingSnapshot,
    ) : PairingUpdateResult

    data class Unchanged(
        val pairing: PairingSnapshot,
    ) : PairingUpdateResult

    data object Failed : PairingUpdateResult
}

internal data class PermissionObservationResult(
    val persisted: Boolean,
    val boundaryReset: Boolean,
    val previousGranted: Boolean?,
    val committedState: CollectionWindowState?,
)

internal sealed interface CollectionWindowUpdateResult {
    data class Updated(
        val state: CollectionWindowState,
    ) : CollectionWindowUpdateResult

    data object Stale : CollectionWindowUpdateResult
    data object Failed : CollectionWindowUpdateResult
}

internal sealed interface SnapshotSequenceReservationResult {
    data class Reserved(
        val sequence: Long,
    ) : SnapshotSequenceReservationResult

    data object Stale : SnapshotSequenceReservationResult
    data object Failed : SnapshotSequenceReservationResult
}

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

    /** HTTPS base URL of the user's HealthMes instance. */
    val serverUrl: String?
        get() = prefs.getString(KEY_SERVER_URL, null)

    /** Optional API token, sent as `Authorization: Bearer <token>`. */
    val token: String?
        get() = prefs.getString(KEY_TOKEN, null)

    internal fun pairingSnapshot(): PairingSnapshot =
        synchronized(COLLECTION_STATE_LOCK) {
            pairingSnapshotLocked()
        }

    /**
     * Save URL and token as one hard privacy boundary.
     *
     * The generation and pairing revision advance in the same synchronous
     * commit, so a worker cannot start another chunk after this update. A
     * request already accepted by the former server cannot be rolled back
     * locally; the worker reports that commit and best-effort closes the former
     * server boundary.
     */
    internal fun updatePairing(
        serverUrl: String,
        token: String?,
        nowMs: Long = System.currentTimeMillis(),
    ): PairingUpdateResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized PairingUpdateResult.Failed
            }
            val currentPairing = pairingSnapshotLocked()
            if (
                currentPairing.serverUrl == serverUrl &&
                currentPairing.token == token
            ) {
                return@synchronized PairingUpdateResult.Unchanged(
                    currentPairing
                )
            }
            val current = runCatching { collectionWindowStateLocked() }
                .getOrElse {
                    enterQuarantineLocked()
                    return@synchronized PairingUpdateResult.Failed
                }
            val enabled = runCatching {
                prefs.getBoolean(KEY_ENABLED, false)
            }.getOrElse {
                enterQuarantineLocked()
                return@synchronized PairingUpdateResult.Failed
            }
            if (
                current.collectionGeneration == Long.MAX_VALUE ||
                current.pairingRevision == Long.MAX_VALUE
            ) {
                enterQuarantineLocked()
                return@synchronized PairingUpdateResult.Failed
            }
            val target = current.copy(
                collectionGeneration = current.collectionGeneration + 1L,
                pairingRevision = current.pairingRevision + 1L,
                collectionSinceMs = if (enabled) nowMs else 0L,
                watermarkMs = floorToHour(nowMs),
                collectionTimezone = ZoneId.systemDefault().id,
            )
            val editor = collectionStateEditor(target)
                .putString(KEY_SERVER_URL, serverUrl)
            if (token == null) {
                editor.remove(KEY_TOKEN)
            } else {
                editor.putString(KEY_TOKEN, token)
            }
            if (!commitCollectionState(editor)) {
                return@synchronized PairingUpdateResult.Failed
            }
            PairingUpdateResult.Updated(
                state = target,
                pairing = PairingSnapshot(
                    serverUrl = serverUrl,
                    token = token,
                    revision = target.pairingRevision,
                ),
            )
        }

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

    internal fun collectionLeaseIsCurrent(
        expectedGeneration: Long,
        expectedPairingRevision: Long,
    ): Boolean =
        synchronized(COLLECTION_STATE_LOCK) {
            collectionWindowIsCurrentLocked(
                expectedGeneration = expectedGeneration,
            ) && runCatching {
                prefs.getLong(KEY_PAIRING_REVISION, 0L)
            }.getOrElse {
                enterQuarantineLocked()
                return@synchronized false
            } == expectedPairingRevision
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
            val currentWatermark = runCatching {
                prefs.getLong(KEY_WATERMARK_MS, 0L)
            }.getOrElse {
                enterQuarantineLocked()
                return@synchronized false
            }
            val nextWatermark = maxOf(currentWatermark, watermarkMs)
            if (nextWatermark == currentWatermark) {
                return@synchronized true
            }
            val committed = runCatching {
                prefs.edit()
                    .putLong(KEY_WATERMARK_MS, nextWatermark)
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
    internal fun persistCollectionWindowIfCurrent(
        expectedGeneration: Long,
        collectionRevision: Int,
        collectionSinceMs: Long,
        watermarkMs: Long,
        collectionTimezone: String = ZoneId.systemDefault().id,
    ): CollectionWindowUpdateResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized CollectionWindowUpdateResult.Failed
            }
            val current = collectionWindowStateLocked()
            if (
                !collectionWindowIsCurrentLocked(
                    expectedGeneration = expectedGeneration,
                )
            ) {
                return@synchronized CollectionWindowUpdateResult.Stale
            }
            val target = current.copy(
                collectionGeneration = current.collectionGeneration + 1,
                collectionRevision = collectionRevision,
                collectionSinceMs = collectionSinceMs,
                watermarkMs = watermarkMs,
                collectionTimezone = collectionTimezone,
            )
            val committed = commitCollectionState(
                collectionStateEditor(
                    target,
                ),
            )
            if (committed) {
                CollectionWindowUpdateResult.Updated(target)
            } else {
                CollectionWindowUpdateResult.Failed
            }
        }

    /**
     * Move past a server generation left by an older install identity.
     *
     * The new generation is greater than both sides and starts at a fresh
     * local privacy boundary. This avoids a permanent handshake loop while
     * never relabeling activity from the mismatched generation.
     */
    internal fun recoverCollectionGenerationIfCurrent(
        expectedGeneration: Long,
        serverGeneration: Long,
        collectionRevision: Int,
        nowMs: Long,
        collectionTimezone: String = ZoneId.systemDefault().id,
    ): CollectionWindowUpdateResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized CollectionWindowUpdateResult.Failed
            }
            if (
                !collectionWindowIsCurrentLocked(
                    expectedGeneration = expectedGeneration,
                )
            ) {
                return@synchronized CollectionWindowUpdateResult.Stale
            }
            val current = collectionWindowStateLocked()
            val recoveredGeneration = recoveryCollectionGeneration(
                localGeneration = current.collectionGeneration,
                serverGeneration = serverGeneration,
            ) ?: run {
                enterQuarantineLocked()
                return@synchronized CollectionWindowUpdateResult.Failed
            }
            val target = current.copy(
                collectionGeneration = recoveredGeneration,
                collectionRevision = collectionRevision,
                collectionSinceMs = nowMs,
                watermarkMs = floorToHour(nowMs),
                collectionTimezone = collectionTimezone,
            )
            if (commitCollectionState(collectionStateEditor(target))) {
                CollectionWindowUpdateResult.Updated(target)
            } else {
                CollectionWindowUpdateResult.Failed
            }
        }

    /**
     * Reserve one durable sequence for an authoritative snapshot upload.
     *
     * The sequence is global to this collector and synchronously committed
     * before any network request. This keeps later snapshots strictly ordered
     * across process restarts, same-millisecond runs, and wall-clock rollback.
     */
    internal fun reserveSnapshotSequenceIfCurrent(
        expectedGeneration: Long,
        wallClockMs: Long,
    ): SnapshotSequenceReservationResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized SnapshotSequenceReservationResult.Failed
            }
            if (
                !collectionWindowIsCurrentLocked(
                    expectedGeneration = expectedGeneration,
                )
            ) {
                return@synchronized SnapshotSequenceReservationResult.Stale
            }
            val previousSequence = runCatching {
                prefs.getLong(KEY_SNAPSHOT_SEQUENCE, 0L)
            }.getOrElse {
                enterQuarantineLocked()
                return@synchronized SnapshotSequenceReservationResult.Failed
            }
            val sequence = reservePersistedSnapshotSequence(
                previousSequence = previousSequence,
                wallClockMs = wallClockMs,
                persist = { next ->
                    commitCollectionState(
                        prefs.edit().putLong(KEY_SNAPSHOT_SEQUENCE, next),
                    )
                },
            ) ?: run {
                if (!collectionQuarantinedLocked()) {
                    enterQuarantineLocked()
                }
                return@synchronized SnapshotSequenceReservationResult.Failed
            }
            SnapshotSequenceReservationResult.Reserved(sequence)
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
        forceBoundary: Boolean = false,
    ): PermissionObservationResult =
        synchronized(COLLECTION_STATE_LOCK) {
            if (collectionQuarantinedLocked()) {
                return@synchronized PermissionObservationResult(
                    persisted = false,
                    boundaryReset = false,
                    previousGranted = null,
                    committedState = null,
                )
            }
            val current = collectionWindowStateLocked()
            val plan = permissionObservationPlan(
                current = current,
                granted = granted,
                nowMs = nowMs,
                boundaryWatermarkMs = floorToHour(nowMs),
                timezone = ZoneId.systemDefault().id,
                forceBoundary = forceBoundary,
            )
            if (!plan.boundaryReset && collectionStatePersistenceHealthy) {
                return@synchronized permissionObservationResult(
                    plan = plan,
                    persisted = true,
                )
            }
            val persisted = commitCollectionState(
                collectionStateEditor(plan.state),
            )
            permissionObservationResult(
                plan = plan,
                persisted = persisted,
            )
        }

    /** Human-readable outcome of the last upload attempt (shown on screen). */
    var lastResult: String?
        get() = prefs.getString(KEY_LAST_RESULT, null)
        set(value) = prefs.edit { putString(KEY_LAST_RESULT, value) }

    /** Install-scoped identifier; reinstalling cannot inherit a stale server generation. */
    val deviceId: String
        get() {
            prefs.getString(KEY_DEVICE_ID, null)?.let { return it }
            val id = newInstallScopedDeviceId(UUID.randomUUID().toString())
            prefs.edit { putString(KEY_DEVICE_ID, id) }
            return id
        }

    private fun collectionWindowStateLocked(): CollectionWindowState {
        val values = prefs.all
        return CollectionWindowState(
            collectionGeneration = values[KEY_COLLECTION_GENERATION] as? Long ?: 0L,
            pairingRevision = values[KEY_PAIRING_REVISION] as? Long ?: 0L,
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
            .putLong(KEY_PAIRING_REVISION, state.pairingRevision)
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

    private fun pairingSnapshotLocked(): PairingSnapshot {
        val values = prefs.all
        return PairingSnapshot(
            serverUrl = values[KEY_SERVER_URL] as? String,
            token = values[KEY_TOKEN] as? String,
            revision = values[KEY_PAIRING_REVISION] as? Long ?: 0L,
        )
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
        UsageAccessGuardService.stop(appContext)
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
        const val KEY_PAIRING_REVISION = "pairing_revision"
        const val KEY_COLLECTION_SINCE_MS = "collection_since_ms"
        const val KEY_COLLECTION_REVISION = "collection_revision"
        const val KEY_WATERMARK_MS = "watermark_ms"
        const val KEY_COLLECTION_TIMEZONE = "collection_timezone"
        const val KEY_SNAPSHOT_SEQUENCE = "snapshot_sequence"
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
