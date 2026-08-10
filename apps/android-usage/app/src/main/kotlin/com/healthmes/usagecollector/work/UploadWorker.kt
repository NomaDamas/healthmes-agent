package com.healthmes.usagecollector.work

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.healthmes.usagecollector.CollectionWindowState
import com.healthmes.usagecollector.CollectorPrefs
import com.healthmes.usagecollector.UsageAccess
import com.healthmes.usagecollector.UsageAccessGuardRegistry
import com.healthmes.usagecollector.UsageAccessGuardService
import com.healthmes.usagecollector.UsageAccessGuardToken
import com.healthmes.usagecollector.net.CollectionConfig
import com.healthmes.usagecollector.net.IngestClient
import com.healthmes.usagecollector.net.UploadSample
import com.healthmes.usagecollector.usage.HourlyBucketer
import com.healthmes.usagecollector.usage.SourcePrivacyFilter
import com.healthmes.usagecollector.usage.UsageSnapshotReader
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Reads usage events since the last successful upload (with a lookback margin
 * for intervals crossing the watermark), buckets them hourly, and POSTs them
 * to `POST /v1/app-usage/batch`.
 *
 * Watermark contract: on success the watermark moves to the *top of the
 * current hour*, so the still-growing hour is recomputed and re-sent on every
 * run — the server upserts on
 * (device_id, collection_generation, bucket_start, app_package) with
 * last-write-wins, which makes every upload idempotent within one collection
 * window.
 */
class UploadWorker(appContext: Context, params: WorkerParameters) :
    Worker(appContext, params) {

    override fun doWork(): Result {
        val context = applicationContext
        val prefs = CollectorPrefs(context)
        val isOneShot = inputData.getBoolean(KEY_FORCE, false)
        val quarantined = prefs.collectionQuarantined
        val collectionEnabled = prefs.collectionEnabled
        val permissionGrantedAtStart = UsageAccess.isGranted(context)
        // A one-shot job may report a revoke while collection is off, but it
        // never reads UsageStats or uploads activity unless collection is on.
        if (
            !uploadAllowed(
                collectionEnabled = collectionEnabled,
                collectionQuarantined = quarantined,
            )
        ) {
            if (quarantined) {
                UploadScheduling.disable(context)
                prefs.lastResult =
                    "Collection quarantined after a persistence failure; re-enable explicitly."
                return Result.failure()
            }
            if (
                isOneShot
                && !permissionGrantedAtStart
                && !prefs.serverUrl.isNullOrBlank()
            ) {
                return stopForRevokedPermission(
                    prefs,
                    IngestClient(checkNotNull(prefs.serverUrl), prefs.token),
                    System.currentTimeMillis(),
                )
            }
            return Result.success()
        }

        val serverUrl = prefs.serverUrl
        if (serverUrl.isNullOrBlank()) {
            prefs.lastResult = "Not paired: save the server URL first."
            return Result.failure()
        }
        val nowMs = System.currentTimeMillis()
        val currentTimezone = ZoneId.systemDefault().id
        val client = IngestClient(serverUrl, prefs.token)
        if (!permissionGrantedAtStart) {
            return stopForRevokedPermission(prefs, client, nowMs)
        }
        val initialGuardToken = UsageAccessGuardRegistry.snapshot()
        if (initialGuardToken == null) {
            prefs.lastResult =
                "Collection paused: foreground privacy guard is not active."
            return Result.success()
        }
        var guardToken: UsageAccessGuardToken = initialGuardToken
        if (!guardAllowsCollection(prefs, guardToken)) {
            return collectionWindowChanged(prefs)
        }

        val statusObservedAt = Instant.ofEpochMilli(nowMs).toString()
        var stableConfig: CollectionConfig? = null
        var stableState: CollectionWindowState? = null
        for (attempt in 0 until MAX_BOUNDARY_SYNC_ATTEMPTS) {
            val stateBeforeStatus = prefs.collectionWindowState()
            if (
                stateBeforeStatus.collectionGeneration
                != guardToken.collectionGeneration
                || !guardAllowsCollection(prefs, guardToken)
            ) {
                return collectionWindowChanged(prefs)
            }
            if (!UsageAccess.isGranted(context)) {
                return stopForRevokedPermission(
                    prefs,
                    client,
                    System.currentTimeMillis(),
                )
            }
            // Reporting granted before reading config clears a previous
            // server-side revoke only for this exact durable generation.
            val config = when (
                val outcome = client.postPermissionStatus(
                    prefs.deviceId,
                    granted = true,
                    statusObservedAt = statusObservedAt,
                    collectionGeneration = stateBeforeStatus.collectionGeneration,
                )
            ) {
                is IngestClient.ConfigOutcome.Success -> outcome.config
                is IngestClient.ConfigOutcome.TransientFailure -> {
                    prefs.lastResult =
                        "Permission/config refresh failed, will retry: " +
                            "${outcome.reason} (${stamp()})"
                    return Result.retry()
                }

                is IngestClient.ConfigOutcome.PermanentFailure -> {
                    prefs.lastResult =
                        "Permission/config refresh rejected: " +
                            "${outcome.reason} (${stamp()})"
                    return Result.failure()
                }
            }
            val stateAfterStatus = prefs.collectionWindowState()
            if (!UsageAccess.isGranted(context)) {
                return stopForRevokedPermission(
                    prefs,
                    client,
                    System.currentTimeMillis(),
                )
            }
            if (
                config.collectionGeneration
                != stateBeforeStatus.collectionGeneration
                || stateAfterStatus.collectionGeneration
                != stateBeforeStatus.collectionGeneration
                || !guardAllowsCollection(prefs, guardToken)
            ) {
                return collectionWindowChanged(prefs)
            }

            val blocked = !config.enabled || !config.effectiveCollecting
            val timezoneChanged = timezoneBoundaryRequired(
                storedTimezone = stateBeforeStatus.collectionTimezone,
                currentTimezone = currentTimezone,
            )
            val boundaryRequired = (
                stateBeforeStatus.collectionRevision != config.configRevision
                    || timezoneChanged
                    || (blocked && stateBeforeStatus.collectionSinceMs != 0L)
                    || (!blocked && stateBeforeStatus.collectionSinceMs <= 0L)
                )
            if (boundaryRequired) {
                val collectionSinceMs = if (blocked) 0L else nowMs
                if (
                    !prefs.persistCollectionWindow(
                        collectionRevision = config.configRevision,
                        collectionSinceMs = collectionSinceMs,
                        watermarkMs = HourlyBucketer.floorToHour(nowMs),
                        collectionTimezone = currentTimezone,
                    )
                ) {
                    return persistenceFailure(
                        prefs,
                        "Collection stopped: synchronized boundary persistence " +
                            "failed (${stamp()}).",
                    )
                }
                val advanced = UsageAccessGuardRegistry.advanceGeneration(
                    expected = guardToken,
                    collectionGeneration = prefs
                        .collectionWindowState()
                        .collectionGeneration,
                ) ?: return collectionWindowChanged(prefs)
                guardToken = advanced
                continue
            }
            if (blocked) {
                prefs.lastResult =
                    "Collection blocked by server: " +
                        "${config.blockedReason ?: "disabled"} (${stamp()})."
                return Result.success()
            }
            stableConfig = config
            stableState = stateBeforeStatus
            break
        }
        val config = stableConfig
        val collectionState = stableState
        if (config == null || collectionState == null) {
            prefs.lastResult =
                "Collection boundary changed repeatedly; will retry (${stamp()})."
            return Result.retry()
        }
        if (
            collectionState.collectionGeneration != guardToken.collectionGeneration
            || !guardAllowsCollection(prefs, guardToken)
        ) {
            return collectionWindowChanged(prefs)
        }
        if (!UsageAccess.isGranted(context)) {
            return stopForRevokedPermission(
                prefs,
                client,
                System.currentTimeMillis(),
            )
        }
        val collectionSinceMs = collectionState.collectionSinceMs
        val watermarkMs = collectionState.watermarkMs.takeIf { it > 0 }
            ?: collectionSinceMs
        val queryWindow = activityQueryWindow(
            collectionSinceMs,
            watermarkMs,
            nowMs,
        )

        val reader = UsageSnapshotReader(context)
        val events = SourcePrivacyFilter.filter(
            reader.readEvents(queryWindow.beginMs, queryWindow.endMs),
            config.excludedApps,
        )
        if (
            !guardAllowsCollection(prefs, guardToken)
        ) {
            return collectionWindowChanged(prefs)
        }
        val buckets = HourlyBucketer.bucket(
            events,
            queryWindow.beginMs,
            queryWindow.endMs,
        )
        if (!UsageAccess.isGranted(context)) {
            return stopForRevokedPermission(
                prefs,
                client,
                System.currentTimeMillis(),
            )
        }
        if (buckets.isEmpty()) {
            if (!UsageAccess.isGranted(context)) {
                return stopForRevokedPermission(
                    prefs,
                    client,
                    System.currentTimeMillis(),
                )
            }
            if (!guardAllowsCollection(prefs, guardToken)) {
                return collectionWindowChanged(prefs)
            }
            if (
                !prefs.advanceWatermarkIfCurrent(
                    expectedGeneration = collectionState.collectionGeneration,
                    watermarkMs = HourlyBucketer.floorToHour(queryWindow.endMs),
                )
            ) {
                return watermarkFailureOrBoundaryChange(
                    prefs,
                    collectionState.collectionGeneration,
                )
            }
            prefs.lastResult =
                if (queryWindow.hasMoreBacklog) {
                    "No activity in one backlog window; catch-up will continue (${stamp()})."
                } else {
                    "Nothing to upload (${stamp()})."
                }
            return Result.success()
        }

        val samples = buckets.map { bucket ->
            UploadSample(
                bucketStartIso = Instant.ofEpochMilli(bucket.bucketStartMs).toString(),
                appPackage = bucket.packageName,
                foregroundSeconds = bucket.foregroundSeconds,
                launches = bucket.launches,
                category = reader.categoryOf(bucket.packageName),
            )
        }

        val outcome = client.postBatch(
            prefs.deviceId,
            samples,
            collectionState.collectionTimezone ?: currentTimezone,
            config.configRevision,
            collectionState.collectionGeneration,
            shouldContinue = {
                UsageAccess.isGranted(context) &&
                    guardAllowsCollection(prefs, guardToken)
            },
        )
        return when (outcome) {
            is IngestClient.Outcome.Success -> {
                if (!UsageAccess.isGranted(context)) {
                    return stopForRevokedPermission(
                        prefs,
                        client,
                        System.currentTimeMillis(),
                    )
                }
                if (!guardAllowsCollection(prefs, guardToken)) {
                    return collectionWindowChanged(prefs)
                }
                if (
                    !prefs.advanceWatermarkIfCurrent(
                        expectedGeneration = collectionState.collectionGeneration,
                        watermarkMs = HourlyBucketer.floorToHour(queryWindow.endMs),
                    )
                ) {
                    return watermarkFailureOrBoundaryChange(
                        prefs,
                        collectionState.collectionGeneration,
                    )
                }
                prefs.lastResult =
                    if (queryWindow.hasMoreBacklog && outcome.samplesDiscarded == 0) {
                        "Uploaded ${outcome.samplesSent} backlog samples; " +
                            "catch-up will continue (${stamp()})."
                    } else if (outcome.samplesDiscarded == 0) {
                        "Uploaded ${outcome.samplesSent} samples (${stamp()})."
                    } else {
                        "Uploaded ${outcome.samplesSent}; discarded " +
                            "${outcome.samplesDiscarded} rejected samples (${stamp()})."
                    }
                Result.success()
            }

            is IngestClient.Outcome.TransientFailure -> {
                prefs.lastResult = "Upload failed, will retry: ${outcome.reason} (${stamp()})"
                Result.retry()
            }

            is IngestClient.Outcome.PermanentFailure -> {
                prefs.lastResult = "Upload rejected: ${outcome.reason} (${stamp()})"
                Result.failure()
            }

            IngestClient.Outcome.Cancelled -> {
                if (!UsageAccess.isGranted(context)) {
                    stopForRevokedPermission(
                        prefs,
                        client,
                        System.currentTimeMillis(),
                    )
                } else {
                    collectionWindowChanged(prefs)
                }
            }
        }
    }

    private fun stopForRevokedPermission(
        prefs: CollectorPrefs,
        client: IngestClient,
        nowMs: Long,
    ): Result {
        if (UsageAccess.isGranted(applicationContext)) {
            prefs.lastResult =
                "Permission changed during collection; discarded the in-flight snapshot."
            return Result.success()
        }
        UsageAccessGuardRegistry.invalidate()
        val observation = prefs.observeUsageAccess(
            granted = false,
            nowMs = nowMs,
        )
        if (!observation.persisted) {
            return persistenceFailure(
                prefs,
                "Collection stopped: revoked boundary persistence failed (${stamp()}).",
            )
        }
        val revokedState = prefs.collectionWindowState()
        return when (
            val outcome = client.postPermissionStatus(
                prefs.deviceId,
                granted = false,
                statusObservedAt = Instant.ofEpochMilli(nowMs).toString(),
                collectionGeneration = revokedState.collectionGeneration,
            )
        ) {
            is IngestClient.ConfigOutcome.Success -> {
                val currentGeneration = prefs
                    .collectionWindowState()
                    .collectionGeneration
                if (
                    outcome.config.collectionGeneration
                    != revokedState.collectionGeneration
                    || currentGeneration != revokedState.collectionGeneration
                ) {
                    return collectionWindowChanged(prefs)
                }
                prefs.lastResult =
                    "Usage access revoked; collection boundary reset (${stamp()})."
                Result.success()
            }

            is IngestClient.ConfigOutcome.TransientFailure -> {
                prefs.lastResult =
                    "Usage access revoked; status report will retry: " +
                        "${outcome.reason} (${stamp()})"
                Result.retry()
            }

            is IngestClient.ConfigOutcome.PermanentFailure -> {
                prefs.lastResult =
                    "Usage access revoked; status report rejected: " +
                        "${outcome.reason} (${stamp()})"
                Result.failure()
            }
        }
    }

    private fun persistenceFailure(
        prefs: CollectorPrefs,
        message: String,
    ): Result {
        UsageAccessGuardService.stop(applicationContext)
        UploadScheduling.disable(applicationContext)
        prefs.lastResult = message
        return Result.failure()
    }

    private fun collectionWindowChanged(prefs: CollectorPrefs): Result {
        prefs.lastResult =
            "Collection window changed; discarded the in-flight usage snapshot."
        return Result.success()
    }

    private fun watermarkFailureOrBoundaryChange(
        prefs: CollectorPrefs,
        expectedGeneration: Long,
    ): Result {
        if (prefs.collectionWindowIsCurrent(expectedGeneration)) {
            prefs.lastResult =
                "Upload completed but watermark persistence failed; will replay safely."
            return Result.retry()
        }
        return collectionWindowChanged(prefs)
    }

    private fun guardAllowsCollection(
        prefs: CollectorPrefs,
        token: UsageAccessGuardToken,
    ): Boolean =
        UsageAccessGuardRegistry.isCurrent(token) &&
            prefs.collectionWindowIsCurrent(
                expectedGeneration = token.collectionGeneration,
            )

    private fun stamp(): String =
        TIME_FORMAT.withZone(ZoneId.systemDefault()).format(Instant.now())

    companion object {
        /**
         * Marks one-shot work. With collection off, it may only report a
         * revoked permission; it never bypasses the activity-upload gate.
         */
        const val KEY_FORCE = "force"
        const val MAX_BOUNDARY_SYNC_ATTEMPTS = 4

        private val TIME_FORMAT = DateTimeFormatter.ofPattern("MMM d HH:mm")
    }
}
