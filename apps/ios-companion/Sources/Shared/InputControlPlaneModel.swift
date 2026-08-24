import Combine
import Foundation

@MainActor
public final class InputControlPlaneModel: ObservableObject {
    @Published public private(set) var sources: [InputSourceDescriptor] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var busySourceIDs: Set<String> = []
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var sourceMessages: [String: String] = [:]
    @Published public private(set) var lastUpdated: Date?

    private let client: InputControlPlaneClient
    private var readGeneration: UInt = 0
    private var activeReadTokens: Set<UInt> = []

    public init(client: InputControlPlaneClient = InputControlPlaneClient()) {
        self.client = client
    }

    public func load() async {
        let readToken = beginRead()
        defer { finishRead(readToken) }
        do {
            let loadedSources = try await client.listSources()
            guard isCurrentRead(readToken) else { return }
            sources = loadedSources
            errorMessage = nil
            sourceMessages = [:]
            lastUpdated = Date()
        } catch {
            guard isCurrentRead(readToken) else { return }
            errorMessage = describe(error)
            if case InputControlPlaneClientError.notPaired = error {
                sources = []
            }
        }
    }

    public func reset() {
        invalidateReads(clearLoadingState: true)
        sources = []
        busySourceIDs = []
        errorMessage = nil
        sourceMessages = [:]
        lastUpdated = nil
    }

    public func source(_ sourceID: String) -> InputSourceDescriptor? {
        sources.first(where: { $0.sourceID == sourceID })
    }

    public func isBusy(_ sourceID: String) -> Bool {
        busySourceIDs.contains(sourceID)
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
        await mutate(
            sourceID,
            settings: InputControlPlaneSettingsUpdate(
                decisionAccessEnabled: enabled
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
            source.retention.first(
                where: { $0.dataClass == dataClass }
            )?.preset != preset
        else { return }
        await mutate(
            sourceID,
            settings: InputControlPlaneSettingsUpdate(
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
            source.instances.first(
                where: { $0.instanceID == instanceID }
            )?.enabled != enabled
        else { return }
        await mutate(
            sourceID,
            settings: InputControlPlaneSettingsUpdate(
                instanceID: instanceID,
                enabled: enabled
            )
        )
    }

    private func mutate(
        _ sourceID: String,
        settings: InputControlPlaneSettingsUpdate
    ) async {
        guard
            !busySourceIDs.contains(sourceID),
            let descriptor = source(sourceID),
            let etag = descriptor.strongETag
        else { return }

        busySourceIDs.insert(sourceID)
        sourceMessages[sourceID] = nil
        defer { busySourceIDs.remove(sourceID) }

        do {
            let updated = try await client.update(
                sourceID,
                settings: settings,
                ifMatch: etag
            )
            invalidateReads()
            replace(updated.descriptor)
            lastUpdated = Date()
            let readToken = beginRead()
            defer { finishRead(readToken) }
            do {
                let loadedSources = try await client.listSources()
                guard isCurrentRead(readToken) else { return }
                sources = loadedSources
                errorMessage = nil
                lastUpdated = Date()
            } catch {
                guard isCurrentRead(readToken) else { return }
                sourceMessages[sourceID] =
                    "Saved on the server, but related source summaries could not refresh."
            }
        } catch let error as InputControlPlaneClientError {
            switch error {
            case .revisionConflict:
                await reloadAfterConflict(sourceID)
                sourceMessages[sourceID] =
                    "Settings changed on another device. Latest server values were loaded; try again."
            default:
                sourceMessages[sourceID] = describe(error)
            }
        } catch {
            sourceMessages[sourceID] = describe(error)
        }
    }

    private func reloadAfterConflict(_ sourceID: String) async {
        let readToken = beginRead()
        defer { finishRead(readToken) }
        do {
            let loadedSources = try await client.listSources()
            guard isCurrentRead(readToken) else { return }
            sources = loadedSources
            errorMessage = nil
            lastUpdated = Date()
            return
        } catch {
            guard isCurrentRead(readToken) else { return }
            // Fall back to the one descriptor so stale values are never kept
            // for the source that actually conflicted.
        }
        do {
            let descriptor = try await client.source(sourceID).descriptor
            guard isCurrentRead(readToken) else { return }
            replace(descriptor)
            lastUpdated = Date()
        } catch {
            guard isCurrentRead(readToken) else { return }
            errorMessage = describe(error)
        }
    }

    private func beginRead() -> UInt {
        readGeneration &+= 1
        let token = readGeneration
        activeReadTokens.insert(token)
        isLoading = true
        return token
    }

    private func finishRead(_ token: UInt) {
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

    private func replace(_ descriptor: InputSourceDescriptor) {
        guard let index = sources.firstIndex(
            where: { $0.sourceID == descriptor.sourceID }
        ) else {
            sources.append(descriptor)
            return
        }
        sources[index] = descriptor
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
