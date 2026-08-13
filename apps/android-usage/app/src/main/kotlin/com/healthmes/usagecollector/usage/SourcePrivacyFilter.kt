package com.healthmes.usagecollector.usage

import java.util.Locale

/** Drops excluded package identities before bucketing or category lookup. */
object SourcePrivacyFilter {

    fun filter(
        events: List<AppForegroundEvent>,
        excludedApps: Set<String>,
    ): List<AppForegroundEvent> {
        val normalized = excludedApps
            .mapTo(HashSet()) { it.trim().lowercase(Locale.ROOT) }
        return events.filterNot {
            it.packageName.trim().lowercase(Locale.ROOT) in normalized
        }
    }
}
