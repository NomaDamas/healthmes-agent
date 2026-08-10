package com.healthmes.usagecollector

internal data class PermissionObservationPlan(
    val previousGranted: Boolean?,
    val boundaryReset: Boolean,
    val state: CollectionWindowState,
)

internal fun permissionObservationPlan(
    current: CollectionWindowState,
    granted: Boolean,
    nowMs: Long,
    boundaryWatermarkMs: Long,
    timezone: String,
    forceBoundary: Boolean,
): PermissionObservationPlan {
    val resetBoundary = forceBoundary ||
        current.usageSettingsPending ||
        current.usageAccessGranted == null ||
        current.usageAccessGranted != granted
    val target = if (resetBoundary) {
        current.copy(
            collectionGeneration = current.collectionGeneration + 1L,
            collectionSinceMs = nowMs,
            watermarkMs = boundaryWatermarkMs,
            collectionTimezone = timezone,
            usageAccessGranted = granted,
            usageSettingsPending = false,
        )
    } else {
        current
    }
    return PermissionObservationPlan(
        previousGranted = current.usageAccessGranted,
        boundaryReset = resetBoundary,
        state = target,
    )
}

internal fun permissionObservationResult(
    plan: PermissionObservationPlan,
    persisted: Boolean,
): PermissionObservationResult =
    PermissionObservationResult(
        persisted = persisted,
        boundaryReset = persisted && plan.boundaryReset,
        previousGranted = plan.previousGranted,
        committedState = plan.state.takeIf { persisted },
    )

internal fun recoveryCollectionGeneration(
    localGeneration: Long,
    serverGeneration: Long,
): Long? {
    if (localGeneration < 0L || serverGeneration < 0L) return null
    val highest = maxOf(localGeneration, serverGeneration)
    return if (highest == Long.MAX_VALUE) null else highest + 1L
}

internal fun newInstallScopedDeviceId(randomUuid: String): String {
    val compact = randomUuid
        .lowercase()
        .filter { it in 'a'..'f' || it in '0'..'9' }
        .take(32)
    require(compact.length == 32) {
        "install-scoped device identity requires a UUID-sized random value"
    }
    return "android-install-$compact"
}
