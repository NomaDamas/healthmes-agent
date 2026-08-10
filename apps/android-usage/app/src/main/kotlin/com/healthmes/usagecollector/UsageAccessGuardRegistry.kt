package com.healthmes.usagecollector

internal data class UsageAccessGuardToken(
    val processEpoch: String,
    val collectionGeneration: Long,
)

/**
 * Process-local proof that the foreground permission guard is alive.
 *
 * The token intentionally cannot survive process death. A WorkManager process
 * started without the foreground guard must not read historical UsageStats.
 */
internal object UsageAccessGuardRegistry {

    private var activeToken: UsageAccessGuardToken? = null

    @Synchronized
    fun publish(token: UsageAccessGuardToken) {
        activeToken = token
    }

    @Synchronized
    fun snapshot(): UsageAccessGuardToken? = activeToken

    @Synchronized
    fun isCurrent(expected: UsageAccessGuardToken): Boolean =
        activeToken == expected

    @Synchronized
    fun advanceGeneration(
        expected: UsageAccessGuardToken,
        collectionGeneration: Long,
    ): UsageAccessGuardToken? {
        if (activeToken != expected) return null
        return expected.copy(collectionGeneration = collectionGeneration).also {
            activeToken = it
        }
    }

    @Synchronized
    fun invalidate(processEpoch: String? = null) {
        if (processEpoch == null || activeToken?.processEpoch == processEpoch) {
            activeToken = null
        }
    }
}
