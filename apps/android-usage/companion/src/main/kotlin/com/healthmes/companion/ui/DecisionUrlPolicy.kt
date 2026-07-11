package com.healthmes.companion.ui

import java.net.URI

/**
 * Trust boundary for the in-app decision viewer — the Android mirror of the
 * iOS `AppRouter.isAllowedViewerURL` rule.
 *
 * `MainActivity` is an exported launcher activity, so ANY installed app can
 * `startActivity()` it with arbitrary `EXTRA_DESTINATION`/`EXTRA_DECISION_URL`
 * extras. Deep-link URLs therefore arrive from OUTSIDE the app and — unlike
 * URLs read from the paired server's own payloads — must be validated before
 * the viewer (Custom Tabs or the JS-enabled WebView fallback) renders them:
 * http(s) only, and the host must match the paired instance. Local-first
 * stays intact — the in-app viewer never opens a third-party origin
 * (issue #10 acceptance: no network destination other than the paired
 * instance).
 *
 * Pure `java.net.URI` (no android.net.Uri) so the rule is JVM unit-testable
 * (see DecisionUrlPolicyTest).
 */
object DecisionUrlPolicy {

    /**
     * True only when [url] is http(s) AND its host equals the host of the
     * paired [pairedBaseUrl] (case-insensitive). False for anything
     * unparseable, hostless, non-http(s), or while unpaired. Host-only on
     * purpose, matching iOS: scheme/port variants of the paired host are the
     * user's own machine.
     */
    fun isAllowedViewerUrl(url: String?, pairedBaseUrl: String?): Boolean {
        val target = parse(url) ?: return false
        val scheme = target.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return false
        val targetHost = target.host?.lowercase() ?: return false
        val pairedHost = parse(pairedBaseUrl)?.host?.lowercase() ?: return false
        return targetHost == pairedHost
    }

    private fun parse(raw: String?): URI? =
        raw?.takeIf { it.isNotBlank() }?.let { runCatching { URI(it) }.getOrNull() }
}
