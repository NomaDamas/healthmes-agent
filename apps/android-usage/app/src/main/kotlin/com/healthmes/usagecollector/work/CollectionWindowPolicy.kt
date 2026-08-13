package com.healthmes.usagecollector.work

import com.healthmes.usagecollector.usage.HourlyBucketer
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.math.max

internal data class CollectionBoundary(
    val collectionSinceMs: Long,
    val watermarkMs: Long,
)

internal data class ActivityQueryWindow(
    val beginMs: Long,
    val endMs: Long,
    val hasMoreBacklog: Boolean,
) {
    val hasReadableRange: Boolean
        get() = beginMs < endMs
}

/**
 * A privacy stop creates a hard local boundary. Regranting Usage Access may
 * only query activity observed after this instant.
 */
internal fun revokedPermissionBoundary(nowMs: Long): CollectionBoundary =
    CollectionBoundary(
        collectionSinceMs = nowMs,
        watermarkMs = HourlyBucketer.floorToHour(nowMs),
    )

internal fun activityQueryWindow(
    collectionSinceMs: Long,
    watermarkMs: Long,
    nowMs: Long,
): ActivityQueryWindow {
    val requestedBeginMs = requestedQueryBeginMs(
        collectionSinceMs = collectionSinceMs,
        watermarkMs = watermarkMs,
    )
    if (requestedBeginMs >= nowMs) {
        return ActivityQueryWindow(
            beginMs = nowMs,
            endMs = nowMs,
            hasMoreBacklog = false,
        )
    }
    val maxEndMs = if (requestedBeginMs > Long.MAX_VALUE - MAX_WINDOW_MS) {
        Long.MAX_VALUE
    } else {
        requestedBeginMs + MAX_WINDOW_MS
    }
    val endMs = minOf(nowMs, maxEndMs)
    return ActivityQueryWindow(
        beginMs = requestedBeginMs,
        endMs = endMs,
        hasMoreBacklog = endMs < nowMs,
    )
}

internal fun clockRegressionBoundaryRequired(
    collectionSinceMs: Long,
    watermarkMs: Long,
    nowMs: Long,
): Boolean =
    collectionSinceMs > nowMs ||
        watermarkMs > nowMs ||
        requestedQueryBeginMs(
            collectionSinceMs = collectionSinceMs,
            watermarkMs = watermarkMs,
        ) > nowMs

internal fun bucketIsComplete(
    bucketStartMs: Long,
    queryBeginMs: Long,
    queryEndMs: Long,
    collectionSinceMs: Long,
    nowMs: Long,
    settlementGraceMs: Long = BUCKET_SETTLEMENT_GRACE_MS,
): Boolean {
    if (settlementGraceMs < 0L) return false
    if (bucketStartMs > Long.MAX_VALUE - HOUR_MS) return false
    val bucketEndMs = bucketStartMs + HOUR_MS
    if (bucketEndMs > Long.MAX_VALUE - settlementGraceMs) return false
    val settledAtMs = bucketEndMs + settlementGraceMs
    val requiredStartMs = max(bucketStartMs, collectionSinceMs)
    return requiredStartMs < bucketEndMs &&
        queryBeginMs <= requiredStartMs &&
        queryEndMs >= bucketEndMs &&
        nowMs >= settledAtMs
}

internal fun bucketStartsForWindow(
    beginMs: Long,
    endMs: Long,
): List<Long> {
    if (beginMs >= endMs) return emptyList()
    val first = HourlyBucketer.floorToHour(beginMs)
    val starts = mutableListOf<Long>()
    var current = first
    while (current < endMs) {
        starts += current
        if (current > Long.MAX_VALUE - HOUR_MS) break
        current += HOUR_MS
    }
    return starts
}

internal fun nextSnapshotSequence(
    previousSequence: Long,
    wallClockMs: Long,
): Long? {
    if (previousSequence < 0L || previousSequence == Long.MAX_VALUE) return null
    return max(previousSequence + 1L, wallClockMs.coerceAtLeast(0L))
}

internal fun reservePersistedSnapshotSequence(
    previousSequence: Long,
    wallClockMs: Long,
    persist: (Long) -> Boolean,
): Long? {
    val next = nextSnapshotSequence(
        previousSequence = previousSequence,
        wallClockMs = wallClockMs,
    ) ?: return null
    return next.takeIf(persist)
}

internal fun sensitiveContinuationAllowed(
    isStopped: Boolean,
    permissionGranted: Boolean,
    guardCurrent: Boolean,
): Boolean =
    !isStopped && permissionGranted && guardCurrent

internal fun uploadAllowed(
    collectionEnabled: Boolean,
    collectionQuarantined: Boolean,
): Boolean =
    !collectionQuarantined && collectionEnabled

/**
 * WorkManager may schedule periodic and user-triggered work at the same time.
 * Both run in this app process, so one lock keeps the privacy generation and
 * watermark transition single-writer without holding it across other apps.
 */
internal object UploadExecutionGate {
    private val lock = ReentrantLock()

    fun <T> runCancellable(
        isCancelled: () -> Boolean,
        onCancelled: () -> T,
        block: () -> T,
    ): T {
        while (true) {
            if (isCancelled()) return onCancelled()
            val acquired = try {
                lock.tryLock(LOCK_POLL_MS, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                return onCancelled()
            }
            if (!acquired) continue
            return try {
                if (isCancelled()) {
                    onCancelled()
                } else {
                    block()
                }
            } finally {
                lock.unlock()
            }
        }
    }

    private const val LOCK_POLL_MS = 50L
}

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

/** A timezone change relabels local-day boundaries and needs a new generation. */
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

private fun requestedQueryBeginMs(
    collectionSinceMs: Long,
    watermarkMs: Long,
): Long {
    val effectiveWatermarkMs = watermarkMs.takeIf { it > 0L }
        ?: collectionSinceMs
    val lookbackStartMs = if (
        effectiveWatermarkMs < Long.MIN_VALUE + LOOKBACK_MS
    ) {
        Long.MIN_VALUE
    } else {
        effectiveWatermarkMs - LOOKBACK_MS
    }
    return max(collectionSinceMs, lookbackStartMs)
}

private const val HOUR_MS = HourlyBucketer.HOUR_MS
private const val LOOKBACK_MS = 6 * HOUR_MS
private const val MAX_WINDOW_MS = 7 * 24 * HOUR_MS
internal const val BUCKET_SETTLEMENT_GRACE_MS = 15 * 60 * 1000L
