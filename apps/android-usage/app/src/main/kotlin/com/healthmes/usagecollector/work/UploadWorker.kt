package com.healthmes.usagecollector.work

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.healthmes.usagecollector.CollectionWindowState
import com.healthmes.usagecollector.CollectionWindowUpdateResult
import com.healthmes.usagecollector.CollectorPrefs
import com.healthmes.usagecollector.GuardVisibilityPolicy
import com.healthmes.usagecollector.SnapshotSequenceReservationResult
import com.healthmes.usagecollector.UsageAccess
import com.healthmes.usagecollector.UsageAccessGuardRegistry
import com.healthmes.usagecollector.UsageAccessGuardService
import com.healthmes.usagecollector.UsageAccessGuardToken
import com.healthmes.usagecollector.net.CollectionConfig
import com.healthmes.usagecollector.net.IngestClient
import com.healthmes.usagecollector.net.UploadBucketSnapshot
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
 * Watermark contract: on success the watermark moves to the top of the last
 * queried hour, so provisional hours are recomputed on later runs. Every
 * upload first reserves a durable, strictly increasing `snapshot_sequence`.
 * The server accepts newer provisional snapshots in that order, treats exact
 * replays as idempotent, rejects stale conflicts, and never reopens a completed
 * hour within one collection generation. An event-free UsageEvents hour is
 * sent with an incomplete source set, so it cannot erase a foreground session
 * that began before the query lookback. The server also validates the install's
 * pairing revision in the same ingest transaction.
 */
class UploadWorker(appContext: Context, params: WorkerParameters) :
    Worker(appContext, params) {

    override fun doWork(): Result =
        UploadExecutionGate.runCancellable(
            isCancelled = { isStopped },
            onCancelled = { Result.success() },
        ) {
            doSerializedWork()
        }

    private fun doSerializedWork(): Result {
        if (isStopped) return Result.success()
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
        if (!GuardVisibilityPolicy.isSatisfied(context)) {
            return stopForInvisibleGuard(prefs)
        }
        if (isStopped) return Result.success()

        val initialGuardToken = UsageAccessGuardRegistry.snapshot()
        if (initialGuardToken == null) {
            prefs.lastResult =
                "Collection paused: foreground privacy guard is not active."
            return Result.success()
        }
        var guardToken: UsageAccessGuardToken = initialGuardToken
        val pairing = prefs.pairingSnapshot()
        val serverUrl = pairing.serverUrl
        if (serverUrl.isNullOrBlank()) {
            prefs.lastResult = "Not paired: save the server URL first."
            return Result.failure()
        }
        if (pairing.revision != guardToken.pairingRevision) {
            return collectionWindowChanged(prefs)
        }
        val nowMs = System.currentTimeMillis()
        val currentTimezone = ZoneId.systemDefault().id
        val client = IngestClient(serverUrl, pairing.token)
        if (!permissionGrantedAtStart) {
            return stopForRevokedPermission(prefs, client, nowMs)
        }
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
            if (isStopped) return Result.success()
            // Reporting granted before reading config clears a previous
            // server-side revoke only for this exact durable generation.
            val statusOutcome = client.postPermissionStatus(
                prefs.deviceId,
                granted = true,
                statusObservedAt = statusObservedAt,
                collectionGeneration = stateBeforeStatus.collectionGeneration,
                pairingRevision = stateBeforeStatus.pairingRevision,
                shouldContinue = {
                    sensitiveContinuationAllowed(
                        isStopped = isStopped,
                        permissionGranted = UsageAccess.isGranted(context),
                        guardCurrent = guardAllowsCollection(prefs, guardToken),
                    )
                },
            )
            if (isStopped) return Result.success()
            if (!GuardVisibilityPolicy.isSatisfied(context)) {
                return stopForInvisibleGuard(prefs)
            }
            val config = when (statusOutcome) {
                is IngestClient.ConfigOutcome.Success -> statusOutcome.config
                is IngestClient.ConfigOutcome.TransientFailure -> {
                    prefs.lastResult =
                        "Permission/config refresh failed, will retry: " +
                            "${statusOutcome.reason} (${stamp()})"
                    return Result.retry()
                }

                is IngestClient.ConfigOutcome.PermanentFailure -> {
                    prefs.lastResult =
                        "Permission/config refresh rejected: " +
                            "${statusOutcome.reason} (${stamp()})"
                    return Result.failure()
                }

                IngestClient.ConfigOutcome.Cancelled ->
                    return collectionWindowChanged(prefs)
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
                stateAfterStatus.collectionGeneration
                != stateBeforeStatus.collectionGeneration
                || !guardAllowsCollection(prefs, guardToken)
            ) {
                return collectionWindowChanged(prefs)
            }
            if (
                config.collectionGeneration
                != stateBeforeStatus.collectionGeneration
            ) {
                val recovery = prefs.recoverCollectionGenerationIfCurrent(
                    expectedGeneration = stateBeforeStatus.collectionGeneration,
                    serverGeneration = config.collectionGeneration,
                    collectionRevision = config.configRevision,
                    nowMs = nowMs,
                    collectionTimezone = currentTimezone,
                )
                val recoveredState = when (recovery) {
                    is CollectionWindowUpdateResult.Updated -> recovery.state
                    CollectionWindowUpdateResult.Stale ->
                        return collectionWindowChanged(prefs)

                    CollectionWindowUpdateResult.Failed ->
                        return persistenceFailure(
                            prefs,
                            "Collection stopped: server generation mismatch " +
                                "requires recovery, but its boundary could not " +
                                "be saved (${stamp()}).",
                        )
                }
                val advanced = UsageAccessGuardRegistry.advanceGeneration(
                    expected = guardToken,
                    collectionGeneration = recoveredState.collectionGeneration,
                ) ?: return collectionWindowChanged(prefs)
                guardToken = advanced
                prefs.lastResult =
                    "Recovered server generation mismatch with a new privacy " +
                        "boundary (${stamp()})."
                continue
            }

            val blocked = !config.enabled || !config.effectiveCollecting
            val timezoneChanged = timezoneBoundaryRequired(
                storedTimezone = stateBeforeStatus.collectionTimezone,
                currentTimezone = currentTimezone,
            )
            val clockRegressed = clockRegressionBoundaryRequired(
                collectionSinceMs = stateBeforeStatus.collectionSinceMs,
                watermarkMs = stateBeforeStatus.watermarkMs,
                nowMs = nowMs,
            )
            val boundaryRequired = (
                stateBeforeStatus.collectionRevision != config.configRevision
                    || timezoneChanged
                    || (!blocked && clockRegressed)
                    || (blocked && stateBeforeStatus.collectionSinceMs != 0L)
                    || (!blocked && stateBeforeStatus.collectionSinceMs <= 0L)
                )
            if (boundaryRequired) {
                val collectionSinceMs = if (blocked) 0L else nowMs
                val update = prefs.persistCollectionWindowIfCurrent(
                    expectedGeneration = stateBeforeStatus.collectionGeneration,
                    collectionRevision = config.configRevision,
                    collectionSinceMs = collectionSinceMs,
                    watermarkMs = HourlyBucketer.floorToHour(nowMs),
                    collectionTimezone = currentTimezone,
                )
                val updatedState = when (update) {
                    is CollectionWindowUpdateResult.Updated -> update.state
                    CollectionWindowUpdateResult.Stale ->
                        return collectionWindowChanged(prefs)

                    CollectionWindowUpdateResult.Failed ->
                        return persistenceFailure(
                            prefs,
                            "Collection stopped: synchronized boundary persistence " +
                                "failed (${stamp()}).",
                        )
                }
                val advanced = UsageAccessGuardRegistry.advanceGeneration(
                    expected = guardToken,
                    collectionGeneration = updatedState.collectionGeneration,
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
            if (!GuardVisibilityPolicy.isSatisfied(context)) {
                return stopForInvisibleGuard(prefs)
            }
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
        if (!queryWindow.hasReadableRange) {
            prefs.lastResult =
                "No readable activity interval yet; waiting for the next run (${stamp()})."
            return Result.success()
        }

        val reader = UsageSnapshotReader(context)
        if (isStopped) return Result.success()
        val guardedEvents = UsageAccessGuardRegistry.readIfCurrent(
            expected = guardToken,
            shouldContinue = {
                sensitiveContinuationAllowed(
                    isStopped = isStopped,
                    permissionGranted = UsageAccess.isGranted(context),
                    guardCurrent = guardAllowsCollection(prefs, guardToken),
                )
            },
        ) {
            reader.readEvents(queryWindow.beginMs, queryWindow.endMs)
        }
        if (guardedEvents == null) {
            if (isStopped) return Result.success()
            if (!GuardVisibilityPolicy.isSatisfied(context)) {
                return stopForInvisibleGuard(prefs)
            }
            if (!UsageAccess.isGranted(context)) {
                return stopForRevokedPermission(
                    prefs,
                    client,
                    System.currentTimeMillis(),
                )
            }
            return collectionWindowChanged(prefs)
        }
        if (isStopped) return Result.success()
        if (!GuardVisibilityPolicy.isSatisfied(context)) {
            return stopForInvisibleGuard(prefs)
        }
        if (!UsageAccess.isGranted(context)) {
            return stopForRevokedPermission(
                prefs,
                client,
                System.currentTimeMillis(),
            )
        }
        val events = SourcePrivacyFilter.filter(
            guardedEvents,
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
        if (isStopped) return Result.success()
        val snapshotSequence = when (
            val reservation = prefs.reserveSnapshotSequenceIfCurrent(
                expectedGeneration = collectionState.collectionGeneration,
                wallClockMs = nowMs,
            )
        ) {
            is SnapshotSequenceReservationResult.Reserved ->
                reservation.sequence

            SnapshotSequenceReservationResult.Stale ->
                return collectionWindowChanged(prefs)

            SnapshotSequenceReservationResult.Failed ->
                return persistenceFailure(
                    prefs,
                    "Collection stopped: snapshot sequence persistence " +
                        "failed (${stamp()}).",
                )
        }
        if (
            !sensitiveContinuationAllowed(
                isStopped = isStopped,
                permissionGranted = UsageAccess.isGranted(context),
                guardCurrent = guardAllowsCollection(prefs, guardToken),
            )
        ) {
            if (isStopped) return Result.success()
            if (!GuardVisibilityPolicy.isSatisfied(context)) {
                return stopForInvisibleGuard(prefs)
            }
            if (!UsageAccess.isGranted(context)) {
                return stopForRevokedPermission(
                    prefs,
                    client,
                    System.currentTimeMillis(),
                )
            }
            return collectionWindowChanged(prefs)
        }
        val bucketsByStart = buckets.groupBy { it.bucketStartMs }
        val snapshots = bucketStartsForWindow(
            beginMs = queryWindow.beginMs,
            endMs = queryWindow.endMs,
        ).map { bucketStartMs ->
            val complete = bucketIsComplete(
                bucketStartMs = bucketStartMs,
                queryBeginMs = queryWindow.beginMs,
                queryEndMs = queryWindow.endMs,
                collectionSinceMs = collectionSinceMs,
                nowMs = nowMs,
            )
            val samples = bucketsByStart[bucketStartMs].orEmpty().map { bucket ->
                UploadSample(
                    bucketStartIso = Instant.ofEpochMilli(bucketStartMs).toString(),
                    appPackage = bucket.packageName,
                    foregroundSeconds = bucket.foregroundSeconds,
                    launches = bucket.launches,
                    category = reader.categoryOf(bucket.packageName),
                    bucketComplete = complete,
                    snapshotSequence = snapshotSequence,
                )
            }
            UploadBucketSnapshot(
                bucketStartIso = Instant.ofEpochMilli(bucketStartMs).toString(),
                bucketComplete = complete,
                snapshotSequence = snapshotSequence,
                sourceSetComplete = samples.isNotEmpty(),
                samples = samples,
            )
        }
        if (snapshots.isEmpty()) {
            prefs.lastResult =
                "No readable activity buckets yet; waiting for the next run (${stamp()})."
            return Result.success()
        }
        if (isStopped) return Result.success()

        val outcome = client.postBatch(
            prefs.deviceId,
            snapshots,
            collectionState.collectionTimezone ?: currentTimezone,
            config.configRevision,
            collectionState.collectionGeneration,
            collectionState.pairingRevision,
            shouldContinue = {
                sensitiveContinuationAllowed(
                    isStopped = isStopped,
                    permissionGranted = UsageAccess.isGranted(context),
                    guardCurrent = guardAllowsCollection(prefs, guardToken),
                )
            },
        )
        if (isStopped) return Result.success()
        if (!GuardVisibilityPolicy.isSatisfied(context)) {
            return stopForInvisibleGuard(prefs)
        }
        return when (outcome) {
            is IngestClient.Outcome.Success -> {
                if (
                    outcome.boundaryChangedAfterCommit
                    || !UsageAccess.isGranted(context)
                    || !guardAllowsCollection(prefs, guardToken)
                ) {
                    return committedBeforeBoundaryChanged(
                        prefs,
                        client,
                        outcome.samplesSent,
                    )
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
                        "Uploaded ${outcome.samplesSent} backlog samples; " +
                            "catch-up will continue (${stamp()})."
                    } else {
                        "Uploaded ${snapshots.size} hourly snapshots with " +
                            "${outcome.samplesSent} app rows (${stamp()})."
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
                if (isStopped) {
                    Result.success()
                } else if (!GuardVisibilityPolicy.isSatisfied(context)) {
                    stopForInvisibleGuard(prefs)
                } else if (!UsageAccess.isGranted(context)) {
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
        val expectedPairingRevision = prefs.pairingSnapshot().revision
        val observation = UsageAccessGuardRegistry.withBoundaryFence {
            if (UsageAccess.isGranted(applicationContext)) {
                null
            } else {
                prefs.observeUsageAccess(
                    granted = false,
                    nowMs = nowMs,
                )
            }
        }
        if (observation == null) {
            prefs.lastResult =
                "Permission changed during collection; discarded the in-flight snapshot."
            return Result.success()
        }
        val revokedState = observation.committedState
        if (!observation.persisted || revokedState == null) {
            return persistenceFailure(
                prefs,
                "Collection stopped: revoked boundary persistence failed (${stamp()}).",
            )
        }
        if (isStopped) return Result.success()
        val statusOutcome = client.postPermissionStatus(
            prefs.deviceId,
            granted = false,
            statusObservedAt = Instant.ofEpochMilli(nowMs).toString(),
            collectionGeneration = revokedState.collectionGeneration,
            pairingRevision = revokedState.pairingRevision,
            shouldContinue = {
                !isStopped &&
                    prefs.pairingSnapshot().revision == expectedPairingRevision
            },
        )
        if (isStopped) return Result.success()
        return when (statusOutcome) {
            is IngestClient.ConfigOutcome.Success -> {
                val currentGeneration = prefs
                    .collectionWindowState()
                    .collectionGeneration
                if (
                    statusOutcome.config.collectionGeneration
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
                        "${statusOutcome.reason} (${stamp()})"
                Result.retry()
            }

            is IngestClient.ConfigOutcome.PermanentFailure -> {
                prefs.lastResult =
                    "Usage access revoked; status report rejected: " +
                        "${statusOutcome.reason} (${stamp()})"
                Result.failure()
            }

            IngestClient.ConfigOutcome.Cancelled -> Result.success()
        }
    }

    private fun committedBeforeBoundaryChanged(
        prefs: CollectorPrefs,
        formerClient: IngestClient,
        samplesSent: Int,
    ): Result {
        val current = prefs.collectionWindowState()
        val permissionGranted = UsageAccess.isGranted(applicationContext)
        val reason = if (permissionGranted) {
            "local_collection_boundary_changed"
        } else {
            "usage_access_revoked"
        }
        val closure = formerClient.postPermissionStatus(
            prefs.deviceId,
            granted = false,
            statusObservedAt = Instant.ofEpochMilli(
                System.currentTimeMillis(),
            ).toString(),
            collectionGeneration = current.collectionGeneration,
            pairingRevision = current.pairingRevision,
            statusReason = reason,
            shouldContinue = { !isStopped },
        )
        val closureText = when (closure) {
            is IngestClient.ConfigOutcome.Success ->
                "the former server acknowledged the boundary closure request"

            is IngestClient.ConfigOutcome.TransientFailure ->
                "the former server boundary closure was not confirmed: " +
                    closure.reason

            is IngestClient.ConfigOutcome.PermanentFailure ->
                "the former server rejected boundary closure: " +
                    closure.reason

            IngestClient.ConfigOutcome.Cancelled ->
                "the former server boundary closure was cancelled"
        }
        prefs.lastResult =
            "Server accepted $samplesSent app rows before the local boundary " +
                "changed; $closureText. The local watermark was not advanced " +
                "(${stamp()})."
        return Result.success()
    }

    private fun stopForInvisibleGuard(prefs: CollectorPrefs): Result {
        val disabled = UsageAccessGuardRegistry.withBoundaryFence {
            prefs.updateCollectionEnabled(false)
        }
        UsageAccessGuardService.stop(applicationContext)
        UploadScheduling.disable(applicationContext)
        prefs.lastResult = if (disabled) {
            "Collection stopped: notification permission is required " +
                "for the visible privacy guard."
        } else {
            "Collection stopped: notification visibility boundary could not be saved."
        }
        return if (disabled) Result.success() else Result.failure()
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
            "Collection window changed; stopped using the in-flight local snapshot. " +
                "A request already accepted by a server is not rolled back."
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
        GuardVisibilityPolicy.isSatisfied(applicationContext) &&
            UsageAccessGuardRegistry.isCurrent(token) &&
            prefs.collectionLeaseIsCurrent(
                expectedGeneration = token.collectionGeneration,
                expectedPairingRevision = token.pairingRevision,
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
