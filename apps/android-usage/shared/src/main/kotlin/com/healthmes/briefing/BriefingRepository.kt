package com.healthmes.briefing

import android.content.Context

/**
 * Cache-through access to the glance briefing, shared by the phone widget's
 * 15-minute WorkManager refresh and the on-demand Wear tile/complication
 * requests. Blocking network I/O — call from a background thread.
 */
class BriefingRepository(context: Context) {

    val prefs = PairingPrefs(context)

    sealed class RefreshOutcome {
        /** New payload fetched, parsed, and cached. */
        data class Updated(val briefing: GlanceBriefing) : RefreshOutcome()

        /** Server said 304 — cached payload untouched, freshness bumped. */
        data class Unchanged(val briefing: GlanceBriefing?) : RefreshOutcome()

        data class Failed(val reason: String, val transient: Boolean) : RefreshOutcome()

        /** No server URL saved yet — pairing screen first. */
        data object NotPaired : RefreshOutcome()
    }

    /** Last successfully cached briefing, or null (never fetched / unparseable). */
    fun cached(): GlanceBriefing? =
        prefs.cachedBriefingJson?.let { json -> runCatching { GlanceBriefing.parse(json) }.getOrNull() }

    /**
     * True while the cache is younger than the endpoint's `max-age=300`;
     * request-driven surfaces (tile, complication) then skip the network
     * round trip entirely.
     */
    fun cacheIsFresh(nowMs: Long = System.currentTimeMillis()): Boolean =
        prefs.cachedBriefingJson != null &&
            prefs.lastFetchMs > 0 &&
            nowMs - prefs.lastFetchMs < CACHE_MAX_AGE_MS

    /**
     * Conditional GET with the cached ETag (`If-None-Match`), per the
     * endpoint's caching contract. On 200 the payload must parse against the
     * contract or the fetch is treated as failed (cached copy retained).
     */
    fun refresh(): RefreshOutcome {
        val serverUrl = prefs.serverUrl ?: return RefreshOutcome.NotPaired
        val client = GlanceApiClient(serverUrl, prefs.token)
        return when (val result = client.fetch(prefs.cachedEtag)) {
            is GlanceApiClient.Result.Fresh -> {
                val briefing = try {
                    GlanceBriefing.parse(result.body)
                } catch (e: org.json.JSONException) {
                    return RefreshOutcome.Failed(
                        "unparseable payload: ${e.message}",
                        transient = false,
                    )
                }
                prefs.cachedBriefingJson = result.body
                prefs.cachedEtag = result.etag
                prefs.lastFetchMs = System.currentTimeMillis()
                RefreshOutcome.Updated(briefing)
            }

            is GlanceApiClient.Result.NotModified -> {
                prefs.lastFetchMs = System.currentTimeMillis()
                RefreshOutcome.Unchanged(cached())
            }

            is GlanceApiClient.Result.TransientFailure ->
                RefreshOutcome.Failed(result.reason, transient = true)

            is GlanceApiClient.Result.PermanentFailure ->
                RefreshOutcome.Failed(result.reason, transient = false)
        }
    }

    /**
     * For request-driven surfaces: cached briefing if fresh, otherwise a
     * best-effort refresh falling back to whatever is cached.
     */
    fun freshOrCached(): GlanceBriefing? {
        if (cacheIsFresh()) return cached()
        return when (val outcome = refresh()) {
            is RefreshOutcome.Updated -> outcome.briefing
            is RefreshOutcome.Unchanged -> outcome.briefing
            else -> cached()
        }
    }

    companion object {
        /** Mirrors Cache-Control: private, max-age=300 on the endpoint. */
        const val CACHE_MAX_AGE_MS = 300_000L
    }
}
