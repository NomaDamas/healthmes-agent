package com.healthmes.usagecollector.net

import java.net.URI

internal fun normalizedSecureServerUrl(raw: String): String? {
    val trimmed = raw.trim().trimEnd('/')
    val parsed = runCatching { URI(trimmed) }.getOrNull() ?: return null
    if (parsed.scheme?.lowercase() != "https") return null
    if (parsed.host.isNullOrBlank()) return null
    if (parsed.userInfo != null || parsed.query != null || parsed.fragment != null) return null
    if (parsed.path?.takeIf { it.isNotEmpty() && it != "/" } != null) return null
    return trimmed
}
