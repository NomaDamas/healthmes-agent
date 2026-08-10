package com.healthmes.usagecollector.net

internal data class PermissionStatusPayload(
    val platform: String,
    val capability: String,
    val permissionStatus: String,
    val statusReason: String?,
    val statusObservedAt: String,
    val collectionGeneration: Long,
    val pairingRevision: Long,
    val queueDepth: Int,
)

internal fun permissionStatusPayload(
    granted: Boolean,
    statusObservedAt: String,
    collectionGeneration: Long,
    pairingRevision: Long,
    statusReason: String? = if (granted) null else "usage_access_revoked",
): PermissionStatusPayload =
    PermissionStatusPayload(
        platform = "android",
        capability = "aggregate",
        permissionStatus = if (granted) "granted" else "revoked",
        statusReason = statusReason,
        statusObservedAt = statusObservedAt,
        collectionGeneration = collectionGeneration,
        pairingRevision = pairingRevision,
        queueDepth = 0,
    )
