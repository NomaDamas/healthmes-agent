package com.healthmes.usagecollector.work

import com.healthmes.usagecollector.usage.HourlyBucketer
import kotlin.math.max

internal data class CollectionBoundary(
    val collectionSinceMs: Long,
    val watermarkMs: Long,
)

/**
 * A privacy stop creates a hard local boundary. Regranting Usage Access may
 * only query activity observed after this instant.
 */
internal fun revokedPermissionBoundary(nowMs: Long): CollectionBoundary =
    CollectionBoundary(
        collectionSinceMs = nowMs,
        watermarkMs = HourlyBucketer.floorToHour(nowMs),
    )

internal fun activityQueryBegin(
    collectionSinceMs: Long,
    watermarkMs: Long,
    nowMs: Long,
): Long =
    max(
        collectionSinceMs,
        max(
            watermarkMs - LOOKBACK_MS,
            nowMs - MAX_WINDOW_MS,
        ),
    )

internal fun uploadAllowed(
    collectionEnabled: Boolean,
    collectionQuarantined: Boolean,
): Boolean =
    !collectionQuarantined && collectionEnabled

internal fun collectionWindowLeaseAllowed(
    expectedGeneration: Long,
    currentGeneration: Long,
    collectionEnabled: Boolean,
    collectionQuarantined: Boolean,
): Boolean =
    expectedGeneration == currentGeneration &&
        uploadAllowed(
            collectionEnabled = collectionEnabled,
            collectionQuarantined = collectionQuarantined,
        )

internal fun timezoneBoundaryRequired(
    storedTimezone: String?,
    currentTimezone: String,
): Boolean =
    storedTimezone != currentTimezone

/**
 * Persist a privacy boundary with a durable fail-closed marker already armed.
 *
 * The primary encrypted state is never touched when arming fails. If the
 * primary commit or final clear fails, the marker remains armed for restart.
 */
internal fun commitFailClosedBoundary(
    armQuarantine: () -> Boolean,
    commitState: () -> Boolean,
    clearQuarantine: () -> Boolean,
): Boolean {
    if (!armQuarantine()) return false
    if (!commitState()) return false
    return clearQuarantine()
}

private const val HOUR_MS = HourlyBucketer.HOUR_MS
private const val LOOKBACK_MS = 6 * HOUR_MS
private const val MAX_WINDOW_MS = 7 * 24 * HOUR_MS
