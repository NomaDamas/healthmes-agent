package com.healthmes.briefing

import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL

/**
 * Plain HttpURLConnection client for `GET /v1/briefing/glance` — same
 * no-HTTP-library approach as the collector's IngestClient. Local-first: the
 * paired base URL is the only destination this ever talks to.
 *
 * Honors the endpoint's caching contract: callers pass the last strong ETag
 * as `If-None-Match`; the server answers `304` with an empty body when the
 * payload content (excluding `generated_at`) is unchanged, in which case the
 * cached copy stays valid.
 */
class GlanceApiClient(private val baseUrl: String, private val token: String?) {

    sealed class Result {
        /** 200 — a new payload (and its ETag, when the server sent one). */
        data class Fresh(val body: String, val etag: String?) : Result()

        /** 304 — cached payload still current; freshness can be bumped. */
        data object NotModified : Result()

        /** Network hiccup or 5xx — retry later, keep showing cached data. */
        data class TransientFailure(val reason: String) : Result()

        /** 4xx (bad token, bad URL) — retrying without re-pairing won't help. */
        data class PermanentFailure(val reason: String) : Result()
    }

    fun fetch(cachedEtag: String?): Result {
        val endpoint = endpointOrNull()
            ?: return Result.PermanentFailure("invalid server URL: $baseUrl")

        val connection = try {
            endpoint.openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return Result.TransientFailure(e.message ?: e.javaClass.simpleName)
        }
        return try {
            connection.requestMethod = "GET"
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            token?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Authorization", "Bearer $it")
            }
            cachedEtag?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("If-None-Match", it)
            }
            when (val code = connection.responseCode) {
                HttpURLConnection.HTTP_OK -> {
                    val body = connection.inputStream.use {
                        it.readBytes().toString(Charsets.UTF_8)
                    }
                    Result.Fresh(body = body, etag = connection.getHeaderField("ETag"))
                }

                HttpURLConnection.HTTP_NOT_MODIFIED -> Result.NotModified

                408, 425, 429, in 500..599 ->
                    Result.TransientFailure("HTTP $code ${bodySnippet(connection)}".trim())

                else -> Result.PermanentFailure("HTTP $code ${bodySnippet(connection)}".trim())
            }
        } catch (e: IOException) {
            Result.TransientFailure(e.message ?: e.javaClass.simpleName)
        } finally {
            connection.disconnect()
        }
    }

    private fun endpointOrNull(): URL? =
        try {
            val url = URL(baseUrl.trimEnd('/') + ENDPOINT_PATH)
            if (url.protocol == "http" || url.protocol == "https") url else null
        } catch (_: MalformedURLException) {
            null
        }

    private fun bodySnippet(connection: HttpURLConnection): String =
        try {
            val stream = connection.errorStream ?: connection.inputStream
            stream?.use { it.readBytes().toString(Charsets.UTF_8).take(200) }.orEmpty()
        } catch (_: IOException) {
            ""
        }

    companion object {
        const val ENDPOINT_PATH = "/v1/briefing/glance"
        private const val CONNECT_TIMEOUT_MS = 10_000
        private const val READ_TIMEOUT_MS = 20_000
    }
}
