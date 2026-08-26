import Combine
import Foundation

@MainActor
public final class SettingsHubModel: ObservableObject {
    @Published public private(set) var snapshot: SettingsHubSnapshot?
    @Published public private(set) var isLoading = false
    @Published public private(set) var busySourceIDs: Set<String> = []
    @Published public private(set) var busyProviders: Set<String> = []
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var sourceMessages: [String: String] = [:]
    @Published public private(set) var noticeMessage: String?
    @Published public private(set) var authorizationURL: URL?
    @Published public private(set) var lastUpdated: Date?

    private let client: SettingsHubClient
    private let inputClient: InputControlPlaneClient
    private let wearableClient: WearableManagementClient
    private let pairingStore: PairingStore
    private struct OperationContext {
        let generation: UInt
        let pairing: Pairing
        let identity: PairingCacheIdentity
    }

    private var operationGeneration: UInt = 0
    private var invalidatedThroughGeneration: UInt = 0
    private var latestLoadGeneration: UInt?
    private var latestActionGeneration: UInt?
    private var sourceBusyOwners: [String: UInt] = [:]
    private var providerBusyOwners: [String: UInt] = [:]

    public init(
        client: SettingsHubClient? = nil,
        inputClient: InputControlPlaneClient? = nil,
        wearableClient: WearableManagementClient? = nil,
        pairingStore: PairingStore = .shared
    ) {
        self.pairingStore = pairingStore
        self.client = client ?? SettingsHubClient(pairingStore: pairingStore)
        self.inputClient = inputClient
            ?? InputControlPlaneClient(pairingStore: pairingStore)
        self.wearableClient = wearableClient
            ?? WearableManagementClient(pairingStore: pairingStore)
    }

    public var inputs: [InputSourceDescriptor] {
        snapshot?.inputs ?? []
    }

    public var readiness: SetupReadiness? {
        snapshot?.readiness
    }

    public var wearables: WearablesManagementSnapshot? {
        snapshot?.wearables
    }

    public var wearablesState: SettingsHubWearablesState? {
        snapshot?.wearablesState
    }

    public var wearablesError: String? {
        snapshot?.wearablesError
    }

    public func source(_ sourceID: String) -> InputSourceDescriptor? {
        snapshot?.source(sourceID)
    }

    public func connection(
        for provider: String
    ) -> WearableConnectionDescriptor? {
        wearables?.connection(for: provider)
    }

    public func devices(
        for provider: String
    ) -> [WearableDataSourceDescriptor] {
        wearables?.dataSources(for: provider) ?? []
    }

    public func wearableControl(
        for provider: String
    ) -> WearableProviderActionPolicyDescriptor? {
        snapshot?.wearableControl(for: provider)
    }

    public func isBusy(sourceID: String) -> Bool {
        busySourceIDs.contains(sourceID)
    }

    public func isBusy(provider: String) -> Bool {
        busyProviders.contains(provider)
    }

    public func actionPolicy(
        for provider: WearableProviderDescriptor
    ) -> WearableProviderActionPolicy {
        if let control = wearableControl(for: provider.provider) {
            return control.policy()
        }
        return wearables?.actionPolicy(for: provider)
            ?? WearableProviderActionPolicy(
                connectionStatus: "not_connected",
                isActive: false,
                canAuthorize: false,
                canSync: false,
                canHistoricalSync: false,
                canDisconnect: false,
                historicalLimit: min(provider.maxHistoricalDays ?? 365, 365)
            )
    }

    public func historicalLimit(for provider: String) -> Int {
        wearableControl(for: provider)?.historicalLimit
            ?? wearables?.providers.first(where: {
                $0.provider == provider
            }).map {
                min($0.maxHistoricalDays ?? 365, 365)
            }
            ?? 365
    }

    public func safeHistoricalDays(
        for provider: String,
        requested: Int
    ) -> Int {
        min(max(requested, 1), historicalLimit(for: provider))
    }

    public func load() async {
        let context = beginLoad()
        isLoading = true
        errorMessage = nil
        guard let context else {
            isLoading = false
            snapshot = nil
            latestLoadGeneration = nil
            return
        }
        guard
            isCurrentLoad(context)
        else { return }
        defer {
            if isCurrentLoad(context) {
                isLoading = false
                latestLoadGeneration = nil
            }
        }
        do {
            let loaded = try await client.fetchSnapshot()
            guard isCurrentLoad(context) else {
                return
            }
            snapshot = loaded
            lastUpdated = Date()
        } catch {
            guard isCurrentLoad(context) else {
                return
            }
            errorMessage = describe(error)
            if case SettingsHubClientError.notPaired = error {
                snapshot = nil
            }
        }
    }

    public func reset() {
        invalidateOperations()
        snapshot = nil
        isLoading = false
        busySourceIDs = []
        busyProviders = []
        errorMessage = nil
        sourceMessages = [:]
        noticeMessage = nil
        authorizationURL = nil
        lastUpdated = nil
        latestLoadGeneration = nil
        sourceBusyOwners.removeAll()
        providerBusyOwners.removeAll()
    }

    public func clearAuthorization() {
        authorizationURL = nil
    }

    public func setDecisionAccess(
        _ enabled: Bool,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "decision_access_enabled", scope: "domain"),
            source.decisionAccessEnabled != enabled
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                decisionAccessEnabled: enabled
            )
        )
    }

    public func setSourceEnabled(
        _ enabled: Bool,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "source_enabled", scope: "source"),
            source.sourceEnabled != enabled
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                sourceEnabled: enabled
            )
        )
    }

    public func setRetention(
        _ preset: String,
        dataClass: String,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "retention", scope: "data_class"),
            source.retentionAllowedValues.contains(preset),
            source.retention.first(where: { $0.dataClass == dataClass })?.preset
                != preset
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                retention: [dataClass: preset]
            )
        )
    }

    public func setInstanceEnabled(
        _ enabled: Bool,
        instanceID: String,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "enabled", scope: "instance"),
            source.instances.first(where: { $0.instanceID == instanceID })?.enabled
                != enabled
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                instanceID: instanceID,
                enabled: enabled
            )
        )
    }

    public func setExcludedApps(
        _ excludedApps: [String],
        instanceID: String,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "excluded_apps", scope: "instance"),
            source.instances.contains(where: { $0.instanceID == instanceID })
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                instanceID: instanceID,
                excludedApps: excludedApps
            )
        )
    }

    public func pauseInstance(
        until: Date,
        instanceID: String,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "paused_until", scope: "instance"),
            source.instances.contains(where: { $0.instanceID == instanceID })
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                instanceID: instanceID,
                pauseUpdate: .paused(until: until)
            )
        )
    }

    public func resumeInstance(
        instanceID: String,
        for sourceID: String
    ) async {
        guard
            let source = source(sourceID),
            source.supports(setting: "paused_until", scope: "instance"),
            source.instances.contains(where: { $0.instanceID == instanceID })
        else { return }
        await mutateInput(
            sourceID: sourceID,
            update: InputControlPlaneSettingsUpdate(
                instanceID: instanceID,
                pauseUpdate: .resumed
            )
        )
    }

    public func authorize(provider: String) async {
        guard !isBusy(provider: provider) else { return }
        guard let context = beginAction() else {
            errorMessage = SettingsHubClientError.notPaired.errorDescription
            return
        }
        let owner = context.generation
        providerBusyOwners[provider] = owner
        busyProviders.insert(provider)
        errorMessage = nil
        noticeMessage = nil
        defer { finishProviderBusy(provider, owner: owner) }
        do {
            let response = try await wearableClient.authorize(provider: provider)
            guard isCurrentAction(context) else { return }
            authorizationURL = response.authorizationURL
            noticeMessage = "Complete provider authorization, then refresh Settings Hub."
        } catch {
            guard isCurrentAction(context) else { return }
            errorMessage = describe(error)
        }
    }

    public func disconnect(provider: String) async {
        await wearableMutation(provider: provider) {
            try await self.wearableClient.disconnect(provider: provider)
        }
    }

    public func sync(provider: String) async {
        await wearableMutation(provider: provider) {
            try await self.wearableClient.sync(provider: provider)
        }
    }

    public func syncHistorical(provider: String, days: Int) async {
        let bounded = safeHistoricalDays(for: provider, requested: days)
        guard days == bounded else {
            errorMessage = "\(provider) historical sync supports at most \(bounded) days."
            return
        }
        await wearableMutation(provider: provider) {
            try await self.wearableClient.syncHistorical(
                provider: provider,
                days: days
            )
        }
    }

    private func mutateInput(
        sourceID: String,
        update: InputControlPlaneSettingsUpdate
    ) async {
        guard
            !isBusy(sourceID: sourceID),
            let source = source(sourceID),
            let etag = source.strongETag
        else { return }
        guard let context = beginAction() else {
            errorMessage = InputControlPlaneClientError.notPaired
                .errorDescription
            return
        }
        let owner = context.generation
        sourceBusyOwners[sourceID] = owner
        busySourceIDs.insert(sourceID)
        sourceMessages[sourceID] = nil
        errorMessage = nil
        defer { finishSourceBusy(sourceID, owner: owner) }
        do {
            _ = try await inputClient.update(
                sourceID,
                settings: update,
                ifMatch: etag
            )
            guard isCurrentAction(context) else { return }
            await load()
        } catch let error as InputControlPlaneClientError {
            guard isCurrentAction(context) else { return }
            if case .revisionConflict = error {
                sourceMessages[sourceID] =
                    "Settings changed on another device. Latest values were loaded."
                await load()
            } else {
                sourceMessages[sourceID] = describe(error)
            }
        } catch {
            guard isCurrentAction(context) else { return }
            sourceMessages[sourceID] = describe(error)
        }
    }

    private func wearableMutation(
        provider: String,
        operation: @escaping () async throws -> WearableMutationResponse
    ) async {
        guard !provider.isEmpty, !isBusy(provider: provider) else { return }
        guard let context = beginAction() else {
            errorMessage = WearableManagementClientError.notPaired
                .errorDescription
            return
        }
        let owner = context.generation
        providerBusyOwners[provider] = owner
        busyProviders.insert(provider)
        errorMessage = nil
        noticeMessage = nil
        defer { finishProviderBusy(provider, owner: owner) }
        do {
            let response = try await operation()
            guard isCurrentAction(context) else { return }
            noticeMessage = Self.mutationMessage(response)
            await load()
        } catch {
            guard isCurrentAction(context) else { return }
            errorMessage = describe(error)
        }
    }

    private static func mutationMessage(
        _ response: WearableMutationResponse
    ) -> String {
        var message = response.message ?? "Wearable sync requested."
        if let taskID = response.taskID, !taskID.isEmpty {
            message += " Task queued."
        }
        if let days = response.days {
            message += " Range: \(days) days."
        }
        return message
    }

    private func beginOperation() -> OperationContext? {
        operationGeneration &+= 1
        guard
            let pairing = pairingStore.load(),
            let identity = pairingStore.cacheIdentity(for: pairing)
        else {
            return nil
        }
        return OperationContext(
            generation: operationGeneration,
            pairing: pairing,
            identity: identity
        )
    }

    private func beginLoad() -> OperationContext? {
        latestActionGeneration = nil
        let context = beginOperation()
        latestLoadGeneration = context?.generation
        isLoading = context != nil
        return context
    }

    private func beginAction() -> OperationContext? {
        let context = beginOperation()
        guard let context else {
            return nil
        }
        latestActionGeneration = context.generation
        // A mutation owns the next server refresh. An older refresh must not
        // overwrite the result of that mutation when it eventually returns.
        latestLoadGeneration = nil
        isLoading = false
        return context
    }

    private func invalidateOperations() {
        operationGeneration &+= 1
        invalidatedThroughGeneration = operationGeneration
        latestLoadGeneration = nil
        latestActionGeneration = nil
    }

    private func isCurrent(_ context: OperationContext) -> Bool {
        context.generation > invalidatedThroughGeneration
            && pairingStore.load() == context.pairing
            && pairingStore.cacheIdentity(for: context.pairing)
                == context.identity
    }

    private func isCurrentLoad(_ context: OperationContext) -> Bool {
        isCurrent(context) && latestLoadGeneration == context.generation
    }

    private func isCurrentAction(_ context: OperationContext) -> Bool {
        isCurrent(context) && latestActionGeneration == context.generation
    }

    private func finishSourceBusy(_ sourceID: String, owner: UInt) {
        guard sourceBusyOwners[sourceID] == owner else { return }
        sourceBusyOwners.removeValue(forKey: sourceID)
        busySourceIDs.remove(sourceID)
    }

    private func finishProviderBusy(_ provider: String, owner: UInt) {
        guard providerBusyOwners[provider] == owner else { return }
        providerBusyOwners.removeValue(forKey: provider)
        busyProviders.remove(provider)
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
