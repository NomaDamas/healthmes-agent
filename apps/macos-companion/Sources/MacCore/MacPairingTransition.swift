import Foundation

enum MacPairingTransitionError: LocalizedError {
    case notificationCleanupFailed
    case commitFailed

    var errorDescription: String? {
        switch self {
        case .notificationCleanupFailed:
            return "HealthMes could not remove notifications from the previous account."
        case .commitFailed:
            return "HealthMes could not finish the connection change securely."
        }
    }
}

@MainActor
enum MacPairingTransitionCoordinator {
    typealias Cleanup = () async -> Bool

    static func replace(
        baseURLString: String,
        token: String,
        store: PairingStore = .shared,
        cleanup: Cleanup
    ) async throws -> Pairing {
        let candidate = try PairingStore.validatedPairing(
            baseURLString: baseURLString,
            token: token
        )
        await PairingRelayGate.shared.fenceAndWait()
        defer {
            PairingRelayGate.shared.reopenIfStable(store: store)
        }

        _ = try await recoverPendingTransition(
            store: store,
            cleanup: cleanup
        )
        if store.load() == candidate {
            return candidate
        }

        _ = try store.beginReplacement(with: candidate)
        do {
            guard await cleanup() else {
                throw MacPairingTransitionError.notificationCleanupFailed
            }
        } catch {
            store.abortPendingTransition()
            throw error
        }
        try store.markPendingTransitionCleanupStarted()
        guard let committed = try store.commitPendingTransition() else {
            throw MacPairingTransitionError.commitFailed
        }
        return committed
    }

    static func unpair(
        store: PairingStore = .shared,
        cleanup: Cleanup
    ) async throws {
        await PairingRelayGate.shared.fenceAndWait()
        defer {
            PairingRelayGate.shared.reopenIfStable(store: store)
        }

        _ = try await recoverPendingTransition(
            store: store,
            cleanup: cleanup
        )
        guard store.load() != nil || store.hasPersistedPairingState else {
            return
        }

        _ = try store.beginUnpair()
        do {
            guard await cleanup() else {
                throw MacPairingTransitionError.notificationCleanupFailed
            }
        } catch {
            store.abortPendingTransition()
            throw error
        }
        try store.markPendingTransitionCleanupStarted()
        guard try store.commitPendingTransition() == nil else {
            throw MacPairingTransitionError.commitFailed
        }
    }

    static func recover(
        store: PairingStore = .shared,
        cleanup: Cleanup
    ) async throws -> Pairing? {
        await PairingRelayGate.shared.fenceAndWait()
        defer {
            PairingRelayGate.shared.reopenIfStable(store: store)
        }
        return try await recoverPendingTransition(
            store: store,
            cleanup: cleanup
        )
    }

    private static func recoverPendingTransition(
        store: PairingStore,
        cleanup: Cleanup
    ) async throws -> Pairing? {
        guard store.hasPendingTransition else {
            return store.load()
        }
        guard store.pendingTransition() != nil else {
            throw PairingError.transitionInProgress
        }
        guard await cleanup() else {
            store.abortPendingTransition()
            throw MacPairingTransitionError.notificationCleanupFailed
        }
        try store.markPendingTransitionCleanupStarted()
        return try store.commitPendingTransition()
    }
}
