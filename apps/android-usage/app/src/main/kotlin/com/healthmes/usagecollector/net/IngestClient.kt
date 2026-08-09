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
)

/**
 * Plain HttpURLConnection client for the HealthMes ingest endpoint — no HTTP
 * library dependency for one POST. Batches above [MAX_SAMPLES_PER_POST] are
 * chunked (the server caps one batch at 1000 samples); because ingest is an
 * upsert on (device_id, bucket_start, app_package), re-sending after a partial
 * failure is safe.
 */
class IngestClient(private val baseUrl: String, private val token: String?) {

    sealed class Outcome {
        data class Success(
            val samplesSent: Int,
            val samplesDiscarded: Int = 0,
        ) : Outcome()

        /** Network/server hiccup — worth a WorkManager retry with backoff. */
        data class TransientFailure(val reason: String) : Outcome()

        /** The server understood and said no (4xx) — retrying won't help. */
        data class PermanentFailure(
            val reason: String,
            val isolateRejectedSamples: Boolean = false,
        ) : Outcome()

        /** A local privacy boundary changed before the next HTTP chunk. */
        data object Cancelled : Outcome()
    }

    sealed class ConfigOutcome {
        data class Success(val config: CollectionConfig) : ConfigOutcome()
        data class TransientFailure(val reason: String) : ConfigOutcome()
        data class PermanentFailure(val reason: String) : ConfigOutcome()
    }

    fun postPermissionStatus(
        deviceId: String,
        granted: Boolean,
    ): ConfigOutcome {
        val encodedDevice = URLEncoder.encode(deviceId, StandardCharsets.UTF_8.name())
        val endpoint = endpointOrNull(
            "/v1/activity/devices/$encodedDevice/status",
        ) ?: return ConfigOutcome.PermanentFailure("invalid server URL: $baseUrl")
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
            val status = permissionStatusPayload(granted)
            val payload = JSONObject()
                .put("platform", status.platform)
                .put("capability", status.capability)
                .put("permission_status", status.permissionStatus)
                .put(
                    "status_reason",
                    status.statusReason ?: JSONObject.NULL,
                )
                .put("queue_depth", status.queueDepth)
                .toString()
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

    fun getCollectionConfig(deviceId: String): ConfigOutcome {
        val encodedDevice = URLEncoder.encode(deviceId, StandardCharsets.UTF_8.name())
        val endpoint = endpointOrNull("/v1/activity/devices/$encodedDevice/collection")
            ?: return ConfigOutcome.PermanentFailure("invalid server URL: $baseUrl")
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
                val payload = JSONObject(body)
                val excluded = payload.optJSONArray("excluded_apps")
                val excludedApps = buildSet {
                    if (excluded != null) {
                        for (index in 0 until excluded.length()) {
                            excluded.optString(index)
                                .trim()
                                .takeIf { it.isNotEmpty() }
                                ?.let(::add)
                        }
                    }
                }
                ConfigOutcome.Success(
                    CollectionConfig(
                        enabled = payload.optBoolean("enabled", true),
                        effectiveCollecting =
                            payload.optBoolean("effective_collecting", false),
                        blockedReason =
                            payload.optString("blocked_reason")
                                .takeIf { it.isNotBlank() && it != "null" },
                        excludedApps = excludedApps,
                        configRevision = payload.optInt("config_revision", 0),
                    ),
                )
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
        samples: List<UploadSample>,
        timezone: String,
        collectionRevision: Int,
        shouldContinue: () -> Boolean = { true },
    ): Outcome {
        val endpoint = endpointOrNull(ENDPOINT_PATH)
            ?: return Outcome.PermanentFailure("invalid server URL: $baseUrl")
        val ordered = samples.sortedWith(
            compareBy<UploadSample>(
                UploadSample::bucketStartIso,
                UploadSample::appPackage,
            ),
        )
        val result = uploadWithIsolation(
            ordered,
            maxChunkSize = MAX_SAMPLES_PER_POST,
            shouldContinue = shouldContinue,
        ) { chunk ->
            when (
                val outcome = postChunk(
                    endpoint,
                    deviceId,
                    chunk,
                    timezone,
                    collectionRevision,
                )
            ) {
                is Outcome.Success -> ChunkUploadResult.Success
                is Outcome.TransientFailure ->
                    ChunkUploadResult.TransientFailure(outcome.reason)

                is Outcome.PermanentFailure ->
                    if (outcome.isolateRejectedSamples) {
                        ChunkUploadResult.IsolatableFailure(outcome.reason)
                    } else {
                        ChunkUploadResult.PermanentFailure(outcome.reason)
                    }

                Outcome.Cancelled -> ChunkUploadResult.Cancelled
            }
        }
        if (result.cancelled) return Outcome.Cancelled
        val failure = result.failure
        if (failure != null) {
            return if (failure.transient) {
                Outcome.TransientFailure(failure.reason)
            } else {
                Outcome.PermanentFailure(failure.reason)
            }
        }
        return Outcome.Success(
            samplesSent = result.sent,
            samplesDiscarded = result.discarded.size,
        )
    }

    private fun endpointOrNull(path: String): URL? =
        try {
            val url = URL(baseUrl.trimEnd('/') + path)
            if (url.protocol == "http" || url.protocol == "https") url else null
        } catch (_: MalformedURLException) {
            null
        }

    private fun postChunk(
        endpoint: URL,
        deviceId: String,
        chunk: List<UploadSample>,
        timezone: String,
        collectionRevision: Int,
    ): Outcome {
        val payload = JSONObject()
            .put("device_id", deviceId)
            .put("timezone", timezone)
            .put("collection_revision", collectionRevision)
            .put(
                "samples",
                JSONArray().apply {
                    chunk.forEach { sample ->
                        put(
                            JSONObject()
                                .put("bucket_start", sample.bucketStartIso)
                                .put("app_package", sample.appPackage)
                                .put("foreground_seconds", sample.foregroundSeconds)
                                .put("launches", sample.launches)
                                .put("category", sample.category ?: JSONObject.NULL)
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
            connection.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            val code = connection.responseCode
            when {
                code in 200..299 -> Outcome.Success(chunk.size)
                code == 409 -> {
                    val error = errorBody(connection)
                    val reason = "HTTP $code ${error.snippet}".trim()
                    when (activityConflictDisposition(error.code)) {
                        ActivityConflictDisposition.RETRY ->
                            Outcome.TransientFailure(reason)

                        ActivityConflictDisposition.ISOLATE_REJECTED_SAMPLE ->
                            Outcome.PermanentFailure(
                                reason,
                                isolateRejectedSamples = true,
                            )

                        ActivityConflictDisposition.FAIL_CLOSED ->
                            Outcome.PermanentFailure(reason)
                    }
                }

                code == 408 || code == 425 || code == 429 || code >= 500 ->
                    Outcome.TransientFailure("HTTP $code ${bodySnippet(connection)}".trim())

                else ->
                    Outcome.PermanentFailure("HTTP $code ${bodySnippet(connection)}".trim())
            }
        } catch (e: IOException) {
            Outcome.TransientFailure(e.message ?: e.javaClass.simpleName)
        } finally {
            connection.disconnect()
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
        const val MAX_SAMPLES_PER_POST = 500
        const val CONNECT_TIMEOUT_MS = 15_000
        const val READ_TIMEOUT_MS = 30_000
    }
}
