import Foundation

public struct NotificationSurfaceSnapshot: Equatable, Sendable {
    public let pendingCount: Int
    public let deliveredCount: Int

    public init(pendingCount: Int, deliveredCount: Int) {
        self.pendingCount = pendingCount
        self.deliveredCount = deliveredCount
    }

    public var isEmpty: Bool {
        pendingCount == 0 && deliveredCount == 0
    }
}

public struct NotificationSurface: @unchecked Sendable {
    public let removeAll: @Sendable () -> Void
    public let snapshot: @Sendable () async -> NotificationSurfaceSnapshot

    public init(
        removeAll: @escaping @Sendable () -> Void,
        snapshot: @escaping @Sendable () async -> NotificationSurfaceSnapshot
    ) {
        self.removeAll = removeAll
        self.snapshot = snapshot
    }
}

public struct NotificationDeletionBarrier {
    public enum Failure: Error, Equatable {
        case timedOut
        case cancelled
    }

    private let maximumPolls: Int
    private let requiredEmptyPolls: Int
    private let sleep: @Sendable () async throws -> Void

    public init(
        maximumPolls: Int = 40,
        requiredEmptyPolls: Int = 2,
        sleep: @escaping @Sendable () async throws -> Void = {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
    ) {
        self.maximumPolls = max(1, maximumPolls)
        self.requiredEmptyPolls = max(1, requiredEmptyPolls)
        self.sleep = sleep
    }

    /// Re-issues removal while the notification daemon drains, and requires
    /// consecutive empty observations before allowing account changes.
    public func clear(
        using surface: NotificationSurface
    ) async -> Result<Void, Failure> {
        surface.removeAll()
        var emptyPolls = 0

        for poll in 0..<maximumPolls {
            if Task.isCancelled {
                return .failure(.cancelled)
            }
            if poll > 0 {
                do {
                    try await sleep()
                } catch {
                    return .failure(
                        Task.isCancelled ? .cancelled : .timedOut
                    )
                }
                if Task.isCancelled {
                    return .failure(.cancelled)
                }
            }
            let snapshot = await surface.snapshot()
            if Task.isCancelled {
                return .failure(.cancelled)
            }
            if snapshot.isEmpty {
                emptyPolls += 1
                if emptyPolls >= requiredEmptyPolls {
                    return .success(())
                }
            } else {
                emptyPolls = 0
                surface.removeAll()
            }
        }
        return .failure(.timedOut)
    }
}
