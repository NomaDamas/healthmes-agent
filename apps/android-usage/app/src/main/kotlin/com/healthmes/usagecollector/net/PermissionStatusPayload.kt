package com.healthmes.usagecollector.net

internal data class PermissionStatusPayload(
    val platform: String,
    val capability: String,
    val permissionStatus: String,
    val statusReason: String?,
    val statusObservedAt: String,
    val collectionGeneration: Long,
    val queueDepth: Int,
)

internal fun permissionStatusPayload(
    granted: Boolean,
    statusObservedAt: String,
    collectionGeneration: Long,
): PermissionStatusPayload =
    PermissionStatusPayload(
        platform = "android",
        capability = "aggregate",
        permissionStatus = if (granted) "granted" else "revoked",
        statusReason = if (granted) null else "usage_access_revoked",
        statusObservedAt = statusObservedAt,
        collectionGeneration = collectionGeneration,
        queueDepth = 0,
    )
