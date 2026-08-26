import Combine
import Foundation

@MainActor
public final class WearableManagementModel: ObservableObject {
    @Published public private(set) var snapshot: WearablesManagementSnapshot?
    @Published public private(set) var isLoading = false
    @Published public private(set) var busyProviders: Set<String> = []
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var noticeMessage: String?
    @Published public private(set) var authorizationURL: URL?
    @Published public private(set) var lastMutation: WearableMutationResponse?

    private let client: WearableManagementClient
    private var readGeneration: UInt = 0
    private var activeReadTokens: Set<UInt> = []

    public init(client: WearableManagementClient = WearableManagementClient()) {
        self.client = client
    }

    public func load() async {
        let token = beginLoad()
        defer { finishLoad(token) }
        do {
            let loaded = try await client.fetchSnapshot()
            guard isCurrentRead(token) else { return }
            snapshot = loaded
            errorMessage = nil
        } catch {
            guard isCurrentRead(token) else { return }
            errorMessage = describe(error)
            if case WearableManagementClientError.notPaired = error {
                snapshot = nil
            }
        }
    }

    public func reset() {
        invalidateReads(clearLoadingState: true)
        busyProviders = []
        snapshot = nil
        errorMessage = nil
        noticeMessage = nil
        authorizationURL = nil
        lastMutation = nil
    }

    public func clearAuthorization() {
        authorizationURL = nil
    }

    public func connection(for provider: String) -> WearableConnectionDescriptor? {
        snapshot?.connection(for: provider)
    }

    public func devices(for provider: String) -> [WearableDataSourceDescriptor] {
        snapshot?.dataSources(for: provider) ?? []
    }

    public func historicalLimit(for provider: String) -> Int {
        guard let descriptor = snapshot?.providers.first(where: {
            $0.provider == provider
        }) else {
            return 365
        }
        return actionPolicy(for: descriptor).historicalLimit
    }

    public func safeHistoricalDays(
        for provider: String,
        requested: Int
    ) -> Int {
        min(max(requested, 1), historicalLimit(for: provider))
    }

    public func isBusy(_ provider: String) -> Bool {
        busyProviders.contains(provider)
    }

    public func actionPolicy(
        for provider: WearableProviderDescriptor
    ) -> WearableProviderActionPolicy {
        snapshot?.actionPolicy(for: provider)
            ?? WearableProviderActionPolicy(
                connectionStatus: "not_connected",
                isActive: false,
                canAuthorize: provider.managementSupported
                    && provider.hasCloudAPI && provider.isEnabled,
                canSync: false,
                canHistoricalSync: false,
                canDisconnect: false,
                historicalLimit: min(provider.maxHistoricalDays ?? 365, 365)
            )
    }

    public func authorize(provider: String) async {
        await run(provider: provider) {
            let response = try await self.client.authorize(provider: provider)
            self.authorizationURL = response.authorizationURL
            self.noticeMessage = "Complete the provider authorization in your browser, then refresh."
        }
    }

    public func disconnect(provider: String) async {
        await run(provider: provider) {
            let response = try await self.client.disconnect(provider: provider)
            self.lastMutation = response
            self.noticeMessage = Self.mutationMessage(response)
            await self.reloadAfterAction()
        }
    }

    public func sync(provider: String) async {
        await run(provider: provider) {
            let response = try await self.client.sync(provider: provider)
            self.lastMutation = response
            self.noticeMessage = Self.mutationMessage(response)
            await self.reloadAfterAction()
        }
    }

    public func syncHistorical(provider: String, days: Int) async {
        let limit = historicalLimit(for: provider)
        guard days <= limit else {
            errorMessage = "\(provider) historical sync supports at most \(limit) days."
            return
        }
        await run(provider: provider) {
            let response = try await self.client.syncHistorical(
                provider: provider,
                days: days
            )
            self.lastMutation = response
            self.noticeMessage = Self.mutationMessage(response)
            await self.reloadAfterAction()
        }
    }

    private static func mutationMessage(
        _ response: WearableMutationResponse
    ) -> String {
        var message = response.message
            ?? (response.operation == "disconnect"
                ? "The provider was disconnected."
                : "Wearable sync requested.")
        if let method = response.method, !method.isEmpty {
            message += " Method: \(WearableManagementPresentation.humanize(method))."
        }
        if let taskID = response.taskID, !taskID.isEmpty {
            message += " Task queued."
        }
        if let days = response.days {
            message += " Range: \(days) days."
        } else if response.operation == "historical_sync" {
            message += " The provider applies its own history limit."
        }
        return message
    }

    private func run(
        provider: String,
        operation: @escaping () async throws -> Void
    ) async {
        guard !provider.isEmpty, !busyProviders.contains(provider) else { return }
        invalidateReads()
        let actionGeneration = readGeneration
        busyProviders.insert(provider)
        errorMessage = nil
        noticeMessage = nil
        defer {
            busyProviders.remove(provider)
        }
        do {
            try await operation()
        } catch {
            guard actionGeneration == readGeneration else { return }
            errorMessage = describe(error)
        }
    }

    private func reloadAfterAction() async {
        await load()
    }

    private func beginLoad() -> UInt {
        readGeneration &+= 1
        let token = readGeneration
        activeReadTokens.insert(token)
        isLoading = true
        return token
    }

    private func finishLoad(_ token: UInt) {
        activeReadTokens.remove(token)
        isLoading = !activeReadTokens.isEmpty
    }

    private func invalidateReads(clearLoadingState: Bool = false) {
        readGeneration &+= 1
        guard clearLoadingState else { return }
        activeReadTokens.removeAll()
        isLoading = false
    }

    private func isCurrentRead(_ token: UInt) -> Bool {
        token == readGeneration
    }

    private func describe(_ error: Error) -> String {
        if let localized = error as? LocalizedError,
            let description = localized.errorDescription
        {
            return description
        }
        return error.localizedDescription
    }
}
