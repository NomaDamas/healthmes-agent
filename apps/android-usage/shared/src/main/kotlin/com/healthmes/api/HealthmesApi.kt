package com.healthmes.api

import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL
import java.util.UUID

/**
 * Minimal bearer-token HTTP client for the non-glance app surface of the
 * paired HealthMes instance (issue #10): alert history, weekly report,
 * schedule-proposal actions, media upload, and food/medical capture.
 *
 * Same plain-HttpURLConnection, no-HTTP-library approach as
 * [com.healthmes.briefing.GlanceApiClient] — local-first, the paired base URL
 * is the only destination this ever talks to. Blocking I/O: call from a
 * background thread (WorkManager / Dispatchers.IO).
 */
class HealthmesApi(private val baseUrl: String, private val token: String?) {

    /** Transport-level result; HTTP status interpretation stays with callers. */
    sealed class Response {
        /** Any HTTP response, success or error (callers branch on [code]). */
        data class Http(val code: Int, val body: String) : Response() {
            val isSuccess: Boolean get() = code in 200..299
        }

        /** No HTTP conversation happened (DNS, refused, timeout, bad URL). */
        data class NetworkError(val reason: String) : Response()
    }

    fun get(path: String): Response = request("GET", path, body = null, contentType = null)

    fun postJson(path: String, json: String): Response =
        request("POST", path, json.toByteArray(Charsets.UTF_8), "application/json")

    /** Empty-body POST (the schedule-proposal accept/decline actions). */
    fun post(path: String): Response = request("POST", path, ByteArray(0), null)

    /**
     * `POST /v1/media` — multipart/form-data with the single `file` field the
     * endpoint reads. The server ignores the client filename by contract, so
     * a generic one is sent.
     */
    fun postMultipart(
        path: String,
        contentType: String,
        bytes: ByteArray,
        fieldName: String = "file",
        filename: String = "capture",
    ): Response {
        val boundary = "healthmes-${UUID.randomUUID()}"
        val body = Multipart.encode(boundary, fieldName, filename, contentType, bytes)
        return request("POST", path, body, "multipart/form-data; boundary=$boundary")
    }

    private fun request(
        method: String,
        path: String,
        body: ByteArray?,
        contentType: String?,
    ): Response {
        val endpoint = endpointOrNull(path)
            ?: return Response.NetworkError("invalid server URL: $baseUrl")
        val connection = try {
            endpoint.openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return Response.NetworkError(e.message ?: e.javaClass.simpleName)
        }
        return try {
            connection.requestMethod = method
            connection.connectTimeout = CONNECT_TIMEOUT_MS
            connection.readTimeout = READ_TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            token?.takeIf { it.isNotBlank() }?.let {
                connection.setRequestProperty("Authorization", "Bearer $it")
            }
            if (body != null) {
                contentType?.let { connection.setRequestProperty("Content-Type", it) }
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(body.size)
                connection.outputStream.use { it.write(body) }
            }
            val code = connection.responseCode
            val text = try {
                val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
            } catch (_: IOException) {
                ""
            }
            Response.Http(code, text)
        } catch (e: IOException) {
            Response.NetworkError(e.message ?: e.javaClass.simpleName)
        } finally {
            connection.disconnect()
        }
    }

    private fun endpointOrNull(path: String): URL? =
        try {
            val url = URL(baseUrl.trimEnd('/') + path)
            if (url.protocol == "http" || url.protocol == "https") url else null
        } catch (_: MalformedURLException) {
            null
        }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 10_000
        private const val READ_TIMEOUT_MS = 30_000
    }
}

/**
 * RFC 2046 multipart/form-data encoding for a single binary field. Pure so
 * the exact bytes `POST /v1/media` sends are JVM unit-testable.
 */
object Multipart {

    fun encode(
        boundary: String,
        fieldName: String,
        filename: String,
        contentType: String,
        bytes: ByteArray,
    ): ByteArray {
        val head = buildString {
            append("--").append(boundary).append(CRLF)
            append("Content-Disposition: form-data; name=\"").append(fieldName)
            append("\"; filename=\"").append(filename).append("\"").append(CRLF)
            append("Content-Type: ").append(contentType).append(CRLF)
            append(CRLF)
        }.toByteArray(Charsets.UTF_8)
        val tail = "$CRLF--$boundary--$CRLF".toByteArray(Charsets.UTF_8)
        return head + bytes + tail
    }

    private const val CRLF = "\r\n"
}
