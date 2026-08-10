package com.healthmes.usagecollector.net

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

data class CollectionConfig(
    val enabled: Boolean,
    val effectiveCollecting: Boolean,
    val blockedReason: String?,
    val excludedApps: Set<String>,
    val configRevision: Int,
    val collectionGeneration: Long,
)

/**
 * One serialized element of the `samples` array of `POST /v1/app-usage/batch`
 * (healthmes/api/app_usage.py, AppUsageSampleIn). `bucketStartIso` is a
 * top-of-hour ISO-8601 UTC instant, e.g. `2026-07-09T10:00:00Z`.
 */
data class UploadSample(
    val bucketStartIso: String,
    val appPackage: String,
    val foregroundSeconds: Int,
    val launches: Int,
    val category: String?,
    val bucketComplete: Boolean = true,
    val snapshotSequence: Long = 0L,
)

data class UploadBucketSnapshot(
    val bucketStartIso: String,
    val bucketComplete: Boolean,
    val snapshotSequence: Long,
    val sourceSetComplete: Boolean = true,
    val samples: List<UploadSample>,
) {
    init {
        require(sourceSetComplete || samples.isEmpty())
        require(samples.all { it.bucketStartIso == bucketStartIso })
        require(samples.all { it.bucketComplete == bucketComplete })
        require(samples.all { it.snapshotSequence == snapshotSequence })
        require(samples.map(UploadSample::appPackage).distinct().size == samples.size)
    }
}

/**
 * Plain HttpURLConnection client for the HealthMes ingest endpoint — no HTTP
 * library dependency for one POST. A clock-hour snapshot is never split
 * across requests: the manifest and all of its app rows travel together,
 * so the server can remove apps missing from a newer authoritative view.
 */
class IngestClient(private val baseUrl: String, private val token: String?) {

    sealed class Outcome {
        data class Success(
            val samplesSent: Int,
            val boundaryChangedAfterCommit: Boolean = false,
        ) : Outcome()

        /** Network/server hiccup — worth a WorkManager retry with backoff. */
        data class TransientFailure(val reason: String) : Outcome()

        /** The server understood and said no (4xx) — retrying won't help. */
        data class PermanentFailure(
            val reason: String,
        ) : Outcome()

        /** A local privacy boundary changed before an HTTP request was sent. */
        data object Cancelled : Outcome()
    }

    sealed class ConfigOutcome {
        data class Success(val config: CollectionConfig) : ConfigOutcome()
        data class TransientFailure(val reason: String) : ConfigOutcome()
        data class PermanentFailure(val reason: String) : ConfigOutcome()
        data object Cancelled : ConfigOutcome()
    }

    fun postPermissionStatus(
        deviceId: String,
        granted: Boolean,
        statusObservedAt: String,
        collectionGeneration: Long,
        pairingRevision: Long,
        statusReason: String? = if (granted) null else "usage_access_revoked",
        shouldContinue: () -> Boolean = { true },
    ): ConfigOutcome {
        if (!shouldContinue()) return ConfigOutcome.Cancelled
        val encodedDevice = URLEncoder.encode(deviceId, StandardCharsets.UTF_8.name())
        val endpoint = endpointOrNull(
            "/v1/activity/devices/$encodedDevice/status",
        ) ?: return ConfigOutcome.PermanentFailure("invalid server URL: $baseUrl")
        if (!shouldContinue()) return ConfigOutcome.Cancelled
        val connection = try {
            endpoint.openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return ConfigOutcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        }
        return try {
            connection.requestMethod = "POST"
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            token?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Authorization", "Bearer $it")
            }
            val status = permissionStatusPayload(
                granted,
                statusObservedAt,
                collectionGeneration,
                pairingRevision,
                statusReason,
            )
            val payload = JSONObject()
                .put("platform", status.platform)
                .put("capability", status.capability)
                .put("permission_status", status.permissionStatus)
                .put(
                    "status_reason",
                    status.statusReason ?: JSONObject.NULL,
                )
                .put("status_observed_at", status.statusObservedAt)
                .put("collection_generation", status.collectionGeneration)
                .put("pairing_revision", status.pairingRevision)
                .put("queue_depth", status.queueDepth)
                .toString()
            if (!shouldContinue()) return ConfigOutcome.Cancelled
            connection.outputStream.use {
                it.write(payload.toByteArray(Charsets.UTF_8))
            }
            collectionConfigOutcome(connection)
        } catch (e: IOException) {
            ConfigOutcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            ConfigOutcome.TransientFailure("invalid collection status: ${e.message}")
        } finally {
            connection.disconnect()
        }
    }

    fun getCollectionConfig(
        deviceId: String,
        shouldContinue: () -> Boolean = { true },
    ): ConfigOutcome {
        if (!shouldContinue()) return ConfigOutcome.Cancelled
        val encodedDevice = URLEncoder.encode(deviceId, StandardCharsets.UTF_8.name())
        val endpoint = endpointOrNull("/v1/activity/devices/$encodedDevice/collection")
            ?: return ConfigOutcome.PermanentFailure("invalid server URL: $baseUrl")
        if (!shouldContinue()) return ConfigOutcome.Cancelled
        val connection = try {
            endpoint.openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return ConfigOutcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        }
        return try {
            connection.requestMethod = "GET"
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            token?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Authorization", "Bearer $it")
            }
            collectionConfigOutcome(connection)
        } catch (e: IOException) {
            ConfigOutcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            ConfigOutcome.TransientFailure("invalid collection config: ${e.message}")
        } finally {
            connection.disconnect()
        }
    }

    private fun collectionConfigOutcome(
        connection: HttpURLConnection,
    ): ConfigOutcome {
        val code = connection.responseCode
        return when {
            code in 200..299 -> {
                val body = connection.inputStream.use {
                    it.readBytes().toString(Charsets.UTF_8)
                }
                ConfigOutcome.Success(parseCollectionConfig(body))
            }

            code == 408 || code == 425 || code == 429 || code >= 500 ->
                ConfigOutcome.TransientFailure(
                    "HTTP $code ${bodySnippet(connection)}".trim(),
                )

            else ->
                ConfigOutcome.PermanentFailure(
                    "HTTP $code ${bodySnippet(connection)}".trim(),
                )
        }
    }

    fun postBatch(
        deviceId: String,
        snapshots: List<UploadBucketSnapshot>,
        timezone: String,
        collectionRevision: Int,
        collectionGeneration: Long,
        pairingRevision: Long,
        shouldContinue: () -> Boolean = { true },
    ): Outcome {
        val endpoint = endpointOrNull(ENDPOINT_PATH)
            ?: return Outcome.PermanentFailure("invalid server URL: $baseUrl")
        val ordered = snapshots.sortedBy(UploadBucketSnapshot::bucketStartIso)
        if (ordered.any { it.samples.size > MAX_SAMPLES_PER_POST }) {
            return Outcome.PermanentFailure(
                "one hourly snapshot exceeds the server's " +
                    "$MAX_SAMPLES_PER_POST-sample limit"
            )
        }
        val chunks = packSnapshotChunks(
            ordered,
            maxSamples = MAX_SAMPLES_PER_POST,
            maxSnapshots = MAX_SNAPSHOTS_PER_POST,
        )
        var sent = 0
        for (packed in chunks) {
            if (!shouldContinue()) return Outcome.Cancelled
            when (
                val outcome = postChunk(
                    endpoint,
                    deviceId,
                    packed,
                    timezone,
                    collectionRevision,
                    collectionGeneration,
                    pairingRevision,
                    shouldContinue,
                )
            ) {
                is Outcome.Success -> {
                    sent += outcome.samplesSent
                    if (outcome.boundaryChangedAfterCommit) {
                        return Outcome.Success(
                            samplesSent = sent,
                            boundaryChangedAfterCommit = true,
                        )
                    }
                }
                is Outcome.TransientFailure -> return outcome
                is Outcome.PermanentFailure ->
                    return Outcome.PermanentFailure(outcome.reason)

                Outcome.Cancelled -> return Outcome.Cancelled
            }
        }
        return Outcome.Success(samplesSent = sent)
    }

    private fun postChunk(
        endpoint: URL,
        deviceId: String,
        snapshots: List<UploadBucketSnapshot>,
        timezone: String,
        collectionRevision: Int,
        collectionGeneration: Long,
        pairingRevision: Long,
        shouldContinue: () -> Boolean,
    ): Outcome {
        if (!shouldContinue()) return Outcome.Cancelled
        val samples = snapshots.flatMap(UploadBucketSnapshot::samples)
        val payload = JSONObject()
            .put("device_id", deviceId)
            .put("timezone", timezone)
            .put("collection_revision", collectionRevision)
            .put("collection_generation", collectionGeneration)
            .put("pairing_revision", pairingRevision)
            .put(
                "bucket_snapshots",
                JSONArray().apply {
                    snapshots.forEach { snapshot ->
                        put(
                            JSONObject()
                                .put("bucket_start", snapshot.bucketStartIso)
                                .put("bucket_complete", snapshot.bucketComplete)
                                .put("snapshot_sequence", snapshot.snapshotSequence)
                                .put("source_set_complete", snapshot.sourceSetComplete)
                                .put(
                                    "app_packages",
                                    JSONArray(
                                        snapshot.samples
                                            .map(UploadSample::appPackage)
                                            .sorted(),
                                    ),
                                )
                        )
                    }
                },
            )
            .put(
                "samples",
                JSONArray().apply {
                    samples.forEach { sample ->
                        put(
                            JSONObject()
                                .put("bucket_start", sample.bucketStartIso)
                                .put("app_package", sample.appPackage)
                                .put("foreground_seconds", sample.foregroundSeconds)
                                .put("launches", sample.launches)
                                .put("category", sample.category ?: JSONObject.NULL)
                                .put("bucket_complete", sample.bucketComplete)
                                .put("snapshot_sequence", sample.snapshotSequence)
                        )
                    }
                },
            )
            .toString()

        val connection = try {
            endpoint.openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return Outcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        }
        return try {
            connection.requestMethod = "POST"
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            token?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Authorization", "Bearer $it")
            }
            if (!shouldContinue()) return Outcome.Cancelled
            connection.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            val code = connection.responseCode
            val outcome = when {
                code in 200..299 -> Outcome.Success(samples.size)
                code == 409 -> {
                    val error = errorBody(connection)
                    val reason = "HTTP $code ${error.snippet}".trim()
                    when (activityConflictDisposition(error.code)) {
                        ActivityConflictDisposition.RETRY ->
                            Outcome.TransientFailure(reason)

                        ActivityConflictDisposition.FAIL_CLOSED ->
                            Outcome.PermanentFailure(reason)
                    }
                }

                code == 408 || code == 425 || code == 429 || code >= 500 ->
                    Outcome.TransientFailure("HTTP $code ${bodySnippet(connection)}".trim())

                else ->
                    Outcome.PermanentFailure("HTTP $code ${bodySnippet(connection)}".trim())
            }
            postResponseBoundaryOutcome(
                outcome,
                boundaryStillCurrent = shouldContinue(),
            )
        } catch (e: IOException) {
            Outcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        } finally {
            connection.disconnect()
        }
    }

    private fun endpointOrNull(path: String): URL? {
        val secureBase = normalizedSecureServerUrl(baseUrl) ?: return null
        return try {
            URL(secureBase + path)
        } catch (_: MalformedURLException) {
            null
        }
    }

    private data class ErrorBody(
        val snippet: String,
        val code: String?,
    )

    private fun errorBody(connection: HttpURLConnection): ErrorBody {
        val snippet = bodySnippet(connection)
        val code = try {
            JSONObject(snippet)
                .optJSONObject("error")
                ?.optString("code")
                ?.takeIf { it.isNotBlank() }
        } catch (_: RuntimeException) {
            null
        }
        return ErrorBody(snippet, code)
    }

    private fun bodySnippet(connection: HttpURLConnection): String =
        try {
            val stream = connection.errorStream ?: connection.inputStream
            stream?.use { it.readBytes().toString(Charsets.UTF_8).take(200) }.orEmpty()
        } catch (_: IOException) {
            ""
        }

    private companion object {
        const val ENDPOINT_PATH = "/v1/app-usage/batch"
        const val MAX_SAMPLES_PER_POST = 1000
        const val MAX_SNAPSHOTS_PER_POST = 500
        const val CONNECT_TIMEOUT_MS = 15_000
        const val READ_TIMEOUT_MS = 30_000
    }
}

internal fun packSnapshotChunks(
    snapshots: List<UploadBucketSnapshot>,
    maxSamples: Int,
    maxSnapshots: Int,
): List<List<UploadBucketSnapshot>> {
    require(maxSamples > 0)
    require(maxSnapshots > 0)
    val chunks = mutableListOf<List<UploadBucketSnapshot>>()
    var current = mutableListOf<UploadBucketSnapshot>()
    var currentSamples = 0
    for (snapshot in snapshots) {
        require(snapshot.samples.size <= maxSamples)
        val wouldOverflow = current.isNotEmpty() && (
            current.size >= maxSnapshots ||
                currentSamples + snapshot.samples.size > maxSamples
            )
        if (wouldOverflow) {
            chunks += current.toList()
            current = mutableListOf()
            currentSamples = 0
        }
        current += snapshot
        currentSamples += snapshot.samples.size
    }
    if (current.isNotEmpty()) {
        chunks += current.toList()
    }
    return chunks
}


internal fun postResponseBoundaryOutcome(
    outcome: IngestClient.Outcome,
    boundaryStillCurrent: Boolean,
): IngestClient.Outcome {
    if (boundaryStillCurrent) return outcome
    return if (outcome is IngestClient.Outcome.Success) {
        outcome.copy(boundaryChangedAfterCommit = true)
    } else {
        IngestClient.Outcome.Cancelled
    }
}


internal fun parseCollectionConfig(body: String): CollectionConfig {
    val payload = JSONObject(body)

    fun requiredBoolean(key: String): Boolean {
        if (!payload.has(key)) {
            throw IllegalArgumentException("missing $key")
        }
        return payload.get(key) as? Boolean
            ?: throw IllegalArgumentException("$key must be a boolean")
    }

    val blockedReason = when {
        !payload.has("blocked_reason") ->
            throw IllegalArgumentException("missing blocked_reason")
        payload.isNull("blocked_reason") -> null
        payload.get("blocked_reason") is String ->
            (payload.get("blocked_reason") as String)
                .takeIf { it.isNotBlank() }
                ?: throw IllegalArgumentException(
                    "blocked_reason must be non-empty or null",
                )
        else -> throw IllegalArgumentException(
            "blocked_reason must be a string or null",
        )
    }

    val excluded = payload.opt("excluded_apps") as? JSONArray
        ?: throw IllegalArgumentException("excluded_apps must be an array")
    val excludedApps = buildSet {
        for (index in 0 until excluded.length()) {
            val value = excluded.get(index) as? String
                ?: throw IllegalArgumentException(
                    "excluded_apps must contain only strings",
                )
            value.trim()
                .takeIf { it.isNotEmpty() }
                ?.let(::add)
                ?: throw IllegalArgumentException(
                    "excluded_apps must not contain blank strings",
                )
        }
    }

    if (!payload.has("config_revision")) {
        throw IllegalArgumentException("missing config_revision")
    }
    val configRevision = when (val revision = payload.get("config_revision")) {
        is Int -> revision.takeIf { it >= 0 }
        is Long -> revision
            .takeIf { it in 0..Int.MAX_VALUE.toLong() }
            ?.toInt()
        else -> null
    } ?: throw IllegalArgumentException(
        "config_revision must be a non-negative 32-bit integer",
    )
    if (!payload.has("collection_generation")) {
        throw IllegalArgumentException("missing collection_generation")
    }
    val collectionGeneration = when (
        val generation = payload.get("collection_generation")
    ) {
        is Int -> generation.takeIf { it >= 0 }?.toLong()
        is Long -> generation.takeIf { it >= 0 }
        else -> null
    } ?: throw IllegalArgumentException(
        "collection_generation must be a non-negative 64-bit integer",
    )
    return CollectionConfig(
        enabled = requiredBoolean("enabled"),
        effectiveCollecting = requiredBoolean("effective_collecting"),
        blockedReason = blockedReason,
        excludedApps = excludedApps,
        configRevision = configRevision,
        collectionGeneration = collectionGeneration,
    )
}
