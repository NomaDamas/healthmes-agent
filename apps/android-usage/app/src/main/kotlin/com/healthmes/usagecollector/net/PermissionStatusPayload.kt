package com.healthmes.usagecollector.net

internal data class PermissionStatusPayload(
    val platform: String,
    val capability: String,
    val permissionStatus: String,
    val statusReason: String?,
    val queueDepth: Int,
)

internal fun permissionStatusPayload(granted: Boolean): PermissionStatusPayload =
    PermissionStatusPayload(
        platform = "android",
        capability = "aggregate",
        permissionStatus = if (granted) "granted" else "revoked",
        statusReason = if (granted) null else "usage_access_revoked",
        queueDepth = 0,
    )
