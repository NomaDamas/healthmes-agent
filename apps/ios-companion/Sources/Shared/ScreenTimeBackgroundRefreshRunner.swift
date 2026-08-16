import Foundation

/// Testable ownership boundary around one BGAppRefreshTask execution.
///
/// The app runtime supplies the Apple task completion callback. Expiration
/// cancels the operation and wins completion exactly once.
@MainActor
final class ScreenTimeBackgroundRefreshRunner {
    typealias Operation = @MainActor @Sendable () async -> Bool
    typealias Completion = @MainActor @Sendable (Bool) -> Void

    private let operation: Operation
    private var completion: Completion?
    private var work: Task<Void, Never>?

    init(
        operation: @escaping Operation,
        completion: @escaping Completion
    ) {
        self.operation = operation
        self.completion = completion
    }

    func start() {
        guard work == nil, completion != nil else { return }
        let operation = operation
        work = Task { @MainActor [weak self] in
            let success = await operation()
            guard !Task.isCancelled else { return }
            self?.finish(success: success)
        }
    }

    func expire() {
        let work = work
        self.work = nil
        work?.cancel()
        finish(success: false)
    }

    private func finish(success: Bool) {
        guard let completion else { return }
        self.completion = nil
        work = nil
        completion(success)
    }
}
