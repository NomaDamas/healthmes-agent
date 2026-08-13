package com.healthmes.briefing

import java.time.Instant
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * Everything a glanceable surface (home-screen widget, Wear tile,
 * complication) needs, pre-formatted from a [GlanceBriefing].
 *
 * Pure Kotlin/java.time so the mapping is JVM unit-testable
 * (companion/src/test). Visual composition on top of this state is
 * deliberately placeholder — the final watch/widget UX is the healthcare
 * domain expert's deliverable (docs/design/WATCH-NOTIFICATIONS.ko.md).
 */
data class BriefingDisplayState(
    /** "72" or [NO_SCORE] when no window is persisted. */
    val scoreText: String,
    /** Raw 0-100 score for ranged renderings (complication gauge), or null. */
    val score: Int?,
    /** Freshness ladder from the server: "high" | "medium" | "low". */
    val confidence: String,
    /** e.g. "14:00-15:30 Deep work: PLAN review", null when nothing is next. */
    val nextBlockLine: String?,
    /** Energy demand of that block ("low"|"med"|"high") or null. */
    val nextBlockDemand: String?,
    /** Unresolved alert count of the last 24 h. */
    val alertCount: Int,
    /** Summary of the most recent unresolved alert, or null. */
    val alertSummary: String?,
    /** Best browser-tappable drill-down: top alert's decision, else latest. */
    val decisionUrl: String?,
    /** Server-side generation instant (epoch millis) for staleness display. */
    val generatedAtMs: Long,
) {

    companion object {

        const val NO_SCORE = "--"
        private const val UNTITLED_BLOCK = "(untitled block)"
        private val HOUR_MINUTE = DateTimeFormatter.ofPattern("HH:mm")

        /**
         * Maps a parsed briefing to display state. Times are rendered in the
         * server-reported user timezone ([GlanceBriefing.timezone]) so every
         * surface agrees with the energy curve's local-day framing;
         * [zoneOverride] pins the zone in tests. An unparseable timezone
         * falls back to the device zone.
         */
        fun from(briefing: GlanceBriefing, zoneOverride: ZoneId? = null): BriefingDisplayState {
            val zone = zoneOverride
                ?: runCatching { ZoneId.of(briefing.timezone) }.getOrDefault(ZoneId.systemDefault())

            val nextBlock = briefing.nextBlocks.firstOrNull()
            val nextBlockLine = nextBlock?.let { block ->
                val start = HOUR_MINUTE.format(parseIsoInstant(block.startIso).atZone(zone))
                val end = HOUR_MINUTE.format(parseIsoInstant(block.endIso).atZone(zone))
                "$start-$end ${block.title ?: UNTITLED_BLOCK}"
            }

            return BriefingDisplayState(
                scoreText = briefing.energy.score?.toString() ?: NO_SCORE,
                score = briefing.energy.score,
                confidence = briefing.energy.confidence,
                nextBlockLine = nextBlockLine,
                nextBlockDemand = nextBlock?.energyDemand,
                alertCount = briefing.alerts.unresolvedCount,
                alertSummary = briefing.alerts.top?.summary,
                decisionUrl = briefing.alerts.top?.decisionUrl ?: briefing.latestDecision?.url,
                generatedAtMs = parseIsoInstant(briefing.generatedAtIso).toEpochMilli(),
            )
        }

        /**
         * Parses the ISO-8601 instants the server emits. Pydantic serializes
         * aware UTC datetimes with a `Z` suffix and any explicit offset
         * (`+00:00`) is accepted too — but store-backed endpoints (schedule
         * proposals, food logs) serialize sqlite's NAIVE datetimes verbatim:
         * `2026-07-11T14:51:20.497821`, no zone designator. Every persisted
         * datetime in the healthmes store is UTC by contract, so naive parses
         * as UTC (same rule as the iOS client's `parseNaiveUTC` — found live:
         * the proposals list crashed against a real sqlite instance without
         * this; glance/alerts always send `Z`).
         */
        fun parseIsoInstant(iso: String): Instant =
            runCatching { Instant.parse(iso) }
                .recoverCatching { OffsetDateTime.parse(iso).toInstant() }
                .getOrElse { LocalDateTime.parse(iso).toInstant(ZoneOffset.UTC) }
    }
}
