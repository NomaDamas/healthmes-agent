package com.healthmes.usagecollector

internal data class UsageAccessGuardToken(
    val processEpoch: String,
    val serviceInstance: Long,
    val publicationRevision: Long,
    val collectionGeneration: Long,
    val pairingRevision: Long,
)

internal data class UsageAccessGuardLease(
    val processEpoch: String,
    val serviceInstance: Long,
)

internal data class UsageAccessGuardPublication(
    val lease: UsageAccessGuardLease,
    val publicationRevision: Long,
)

/**
 * Process-local proof that the foreground permission guard is alive.
 *
 * The token intentionally cannot survive process death. A WorkManager process
 * started without the foreground guard must not read historical UsageStats.
 */
internal object UsageAccessGuardRegistry {

    private var nextServiceInstance = 0L
    private var publicationRevision = 0L
    private var boundaryFenceDepth = 0
    private var activeLease: UsageAccessGuardLease? = null
    private var activeToken: UsageAccessGuardToken? = null
    private var deferredRecheckPending = false
    private var deferredRecheck: (() -> Unit)? = null

    @Synchronized
    fun activateService(
        processEpoch: String,
        deferredRecheck: () -> Unit = {},
    ): UsageAccessGuardLease {
        publicationRevision += 1
        activeToken = null
        nextServiceInstance += 1
        return UsageAccessGuardLease(
            processEpoch = processEpoch,
            serviceInstance = nextServiceInstance,
        ).also {
            activeLease = it
            this.deferredRecheck = deferredRecheck
            deferredRecheckPending = boundaryFenceDepth > 0
        }
    }

    /**
     * Invalidates the previous token before one service boundary transition.
     * A later settings/revoke/destroy fence makes the returned publication
     * stale, so callback work that finishes late cannot resurrect collection.
     */
    @Synchronized
    fun beginPublication(
        lease: UsageAccessGuardLease,
    ): UsageAccessGuardPublication? {
        if (activeLease != lease) return null
        if (boundaryFenceDepth > 0) {
            deferredRecheckPending = true
            return null
        }
        publicationRevision += 1
        activeToken = null
        return UsageAccessGuardPublication(
            lease = lease,
            publicationRevision = publicationRevision,
        )
    }

    @Synchronized
    fun publish(
        publication: UsageAccessGuardPublication,
        collectionGeneration: Long,
        pairingRevision: Long,
    ): UsageAccessGuardToken? {
        if (
            activeLease != publication.lease
            || publicationRevision != publication.publicationRevision
            || boundaryFenceDepth > 0
        ) {
            return null
        }
        return UsageAccessGuardToken(
            processEpoch = publication.lease.processEpoch,
            serviceInstance = publication.lease.serviceInstance,
            publicationRevision = publication.publicationRevision,
            collectionGeneration = collectionGeneration,
            pairingRevision = pairingRevision,
        ).also {
            activeToken = it
        }
    }

    /**
     * Fence an AppOps callback before it waits for serialized service work.
     *
     * The callback cannot know whether the change is a revoke until Android is
     * queried, so every observed boundary invalidates the current token. A
     * later reevaluation may publish a fresh token for the observed state.
     */
    @Synchronized
    fun invalidateForObservedBoundary(
        lease: UsageAccessGuardLease,
    ): Boolean {
        if (activeLease != lease) return false
        publicationRevision += 1
        activeToken = null
        return true
    }

    @Synchronized
    fun snapshot(): UsageAccessGuardToken? = activeToken

    @Synchronized
    fun isCurrent(expected: UsageAccessGuardToken): Boolean =
        tokenIsCurrent(expected)

    @Synchronized
    fun advanceGeneration(
        expected: UsageAccessGuardToken,
        collectionGeneration: Long,
    ): UsageAccessGuardToken? {
        if (!tokenIsCurrent(expected)) return null
        return expected.copy(collectionGeneration = collectionGeneration).also {
            activeToken = it
        }
    }

    /**
     * UsageStats reads may be slow. Validate the exact token before and after
     * the read without holding the lifecycle monitor while Android is queried.
     */
    fun <T> readIfCurrent(
        expected: UsageAccessGuardToken,
        shouldContinue: () -> Boolean = { true },
        read: () -> T,
    ): T? {
        if (!shouldContinue()) return null
        synchronized(this) {
            if (!tokenIsCurrent(expected)) return null
        }
        if (!shouldContinue()) return null
        val value = read()
        if (!shouldContinue()) return null
        return synchronized(this) {
            value.takeIf { tokenIsCurrent(expected) }
        }
    }

    /**
     * Atomically closes token publication around a durable privacy-boundary
     * write. This prevents the settings UI or a revoke observation from racing
     * a callback that already checked the old preferences.
     */
    fun <T> withBoundaryFence(block: () -> T): T {
        synchronized(this) {
            boundaryFenceDepth += 1
            publicationRevision += 1
            activeToken = null
            if (activeLease != null) {
                deferredRecheckPending = true
            }
        }
        return try {
            block()
        } finally {
            val recheck = synchronized(this) {
                boundaryFenceDepth -= 1
                publicationRevision += 1
                activeToken = null
                if (boundaryFenceDepth == 0 && deferredRecheckPending) {
                    deferredRecheckPending = false
                    deferredRecheck
                } else {
                    null
                }
            }
            recheck?.invoke()
        }
    }

    @Synchronized
    fun closeService(lease: UsageAccessGuardLease) {
        if (activeLease != lease) return
        publicationRevision += 1
        activeToken = null
        activeLease = null
        deferredRecheckPending = false
        deferredRecheck = null
    }

    @Synchronized
    fun invalidate(processEpoch: String? = null) {
        if (
            processEpoch == null
            || activeLease?.processEpoch == processEpoch
            || activeToken?.processEpoch == processEpoch
        ) {
            publicationRevision += 1
            activeToken = null
        }
    }

    private fun tokenIsCurrent(expected: UsageAccessGuardToken): Boolean =
        activeToken == expected
            && activeLease?.processEpoch == expected.processEpoch
            && activeLease?.serviceInstance == expected.serviceInstance
            && boundaryFenceDepth == 0
            && publicationRevision == expected.publicationRevision
}
