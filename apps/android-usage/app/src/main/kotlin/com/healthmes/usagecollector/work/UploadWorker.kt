package com.healthmes.usagecollector.work

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.healthmes.usagecollector.CollectorPrefs
import com.healthmes.usagecollector.UsageAccess
import com.healthmes.usagecollector.net.IngestClient
import com.healthmes.usagecollector.net.UploadSample
import com.healthmes.usagecollector.usage.HourlyBucketer
import com.healthmes.usagecollector.usage.UsageSnapshotReader
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.max

/**
 * Reads usage events since the last successful upload (with a lookback margin
 * for intervals crossing the watermark), buckets them hourly, and POSTs them
 * to `POST /v1/app-usage/batch`.
 *
 * Watermark contract: on success the watermark moves to the *top of the
 * current hour*, so the still-growing hour is recomputed and re-sent on every
 * run — the server upserts on (device_id, bucket_start, app_package) with
 * last-write-wins, which makes every upload idempotent.
 */
class UploadWorker(appContext: Context, params: WorkerParameters) :
    Worker(appContext, params) {

    override fun doWork(): Result {
        val context = applicationContext
        val prefs = CollectorPrefs(context)
        val forced = inputData.getBoolean(KEY_FORCE, false)
        // A stale periodic tick after the toggle went off is a no-op; the
        // one-shot "Upload now" path (forced) always tries.
        if (!forced && !prefs.collectionEnabled) return Result.success()

        val serverUrl = prefs.serverUrl
        if (serverUrl.isNullOrBlank()) {
            prefs.lastResult = "Not paired: save the server URL first."
            return Result.failure()
        }
        if (!UsageAccess.isGranted(context)) {
            prefs.lastResult = "Usage access not granted; nothing uploaded."
            return Result.failure()
        }

        val nowMs = System.currentTimeMillis()
        val watermarkMs = prefs.watermarkMs.takeIf { it > 0 } ?: (nowMs - DEFAULT_BACKFILL_MS)
        val queryBeginMs = max(watermarkMs - LOOKBACK_MS, nowMs - MAX_WINDOW_MS)

        val reader = UsageSnapshotReader(context)
        val events = reader.readEvents(queryBeginMs, nowMs)
        val buckets = HourlyBucketer.bucket(events, queryBeginMs, nowMs)
        if (buckets.isEmpty()) {
            prefs.watermarkMs = HourlyBucketer.floorToHour(nowMs)
            prefs.lastResult = "Nothing to upload (${stamp()})."
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

        val client = IngestClient(serverUrl, prefs.token)
        return when (val outcome = client.postBatch(prefs.deviceId, samples)) {
            is IngestClient.Outcome.Success -> {
                prefs.watermarkMs = HourlyBucketer.floorToHour(nowMs)
                prefs.lastResult = "Uploaded ${outcome.samplesSent} samples (${stamp()})."
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
        }
    }

    private fun stamp(): String =
        TIME_FORMAT.withZone(ZoneId.systemDefault()).format(Instant.now())

    companion object {
        /** Input-data flag: upload even while the periodic toggle is off. */
        const val KEY_FORCE = "force"

        private val TIME_FORMAT = DateTimeFormatter.ofPattern("MMM d HH:mm")
        private const val HOUR_MS = HourlyBucketer.HOUR_MS
        private const val DEFAULT_BACKFILL_MS = 24 * HOUR_MS
        private const val LOOKBACK_MS = 6 * HOUR_MS
        private const val MAX_WINDOW_MS = 7 * 24 * HOUR_MS
    }
}
