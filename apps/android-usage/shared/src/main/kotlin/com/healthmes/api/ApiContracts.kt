package com.healthmes.api

import org.json.JSONException
import org.json.JSONObject

/**
 * Standard healthmes error envelope
 * (`{"error": {"code", "message", "detail"}}` — healthmes/api/errors.py).
 * `detail` keys the app reads: `current`/`requested` (409 invalid_transition),
 * `max_bytes` (413 payload_too_large).
 */
data class ApiError(
    val code: String,
    val message: String,
    /** invalid_transition detail: the proposal's current status. */
    val detailCurrent: String?,
    /** payload_too_large detail: the server's upload cap in bytes. */
    val detailMaxBytes: Long?,
) {
    companion object {
        /** Null when the body is not the standard envelope (e.g. empty 502). */
        fun parseOrNull(body: String): ApiError? =
            try {
                val error = JSONObject(body).getJSONObject("error")
                val detail = if (error.isNull("detail")) null else error.optJSONObject("detail")
                ApiError(
                    code = error.getString("code"),
                    message = error.getString("message"),
                    detailCurrent = detail?.takeIf { it.has("current") }?.getString("current"),
                    detailMaxBytes = detail?.takeIf { it.has("max_bytes") }?.getLong("max_bytes"),
                )
            } catch (_: JSONException) {
                null
            }
    }
}

/** Pagination block of every list envelope (healthmes/api/pagination.py). */
data class PageMeta(
    val totalCount: Int,
    val limit: Int,
    val offset: Int,
    val hasMore: Boolean,
) {
    companion object {
        @Throws(JSONException::class)
        fun parse(obj: JSONObject): PageMeta = PageMeta(
            totalCount = obj.getInt("total_count"),
            limit = obj.getInt("limit"),
            offset = obj.getInt("offset"),
            hasMore = obj.getBoolean("has_more"),
        )
    }
}

/** 201 body of `POST /v1/media` (healthmes/api/media.py). */
data class MediaUploadResult(
    /** e.g. "media/2026/07/<32hex>.jpg" — pass verbatim to capture creates. */
    val mediaPath: String,
    /** Canonical content type the server stored. */
    val contentType: String,
    val bytes: Long,
) {
    companion object {
        @Throws(JSONException::class)
        fun parse(json: String): MediaUploadResult {
            val root = JSONObject(json)
            return MediaUploadResult(
                mediaPath = root.getString("media_path"),
                contentType = root.getString("content_type"),
                bytes = root.getLong("bytes"),
            )
        }
    }
}

internal fun JSONObject.stringOrNull(key: String): String? =
    if (isNull(key)) null else getString(key)

internal fun JSONObject.intOrNull(key: String): Int? =
    if (isNull(key)) null else getInt(key)
