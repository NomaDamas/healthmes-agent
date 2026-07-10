package com.healthmes.usagecollector.net

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL

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
        data class Success(val samplesSent: Int) : Outcome()

        /** Network/server hiccup — worth a WorkManager retry with backoff. */
        data class TransientFailure(val reason: String) : Outcome()

        /** The server understood and said no (4xx) — retrying won't help. */
        data class PermanentFailure(val reason: String) : Outcome()
    }

    fun postBatch(deviceId: String, samples: List<UploadSample>): Outcome {
        val endpoint = endpointOrNull()
            ?: return Outcome.PermanentFailure("invalid server URL: $baseUrl")
        var sent = 0
        for (chunk in samples.chunked(MAX_SAMPLES_PER_POST)) {
            when (val outcome = postChunk(endpoint, deviceId, chunk)) {
                is Outcome.Success -> sent += chunk.size
                else -> return outcome
            }
        }
        return Outcome.Success(sent)
    }

    private fun endpointOrNull(): URL? =
        try {
            val url = URL(baseUrl.trimEnd('/') + ENDPOINT_PATH)
            if (url.protocol == "http" || url.protocol == "https") url else null
        } catch (_: MalformedURLException) {
            null
        }

    private fun postChunk(endpoint: URL, deviceId: String, chunk: List<UploadSample>): Outcome {
        val payload = JSONObject()
            .put("device_id", deviceId)
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
