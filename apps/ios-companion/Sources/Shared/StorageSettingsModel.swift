import Foundation

@MainActor
public final class StorageSettingsModel: ObservableObject {
    @Published public private(set) var snapshot: StorageSettingsSnapshot?
    @Published public private(set) var maintenance: StorageMaintenanceReport?
    @Published public private(set) var isLoading = false
    @Published public private(set) var errorMessage: String?

    private let api: HealthMesAPI

    public init(api: HealthMesAPI = HealthMesAPI()) {
        self.api = api
    }

    public func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            snapshot = try await api.storageSettings()
            errorMessage = nil
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func update(dataClass: String, preset: String) async {
        isLoading = true
        maintenance = nil
        defer { isLoading = false }
        do {
            _ = try await api.updateStorageRetention(
                dataClass: dataClass,
                preset: preset
            )
            snapshot = try await api.storageSettings()
            errorMessage = nil
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func previewCleanup() async {
        await maintain(dryRun: true)
    }

    public func confirmCleanup() async {
        guard maintenance?.dryRun == true else { return }
        maintenance = nil
        await maintain(dryRun: false)
    }

    public static func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }

    private func maintain(dryRun: Bool) async {
        isLoading = true
        maintenance = nil
        defer { isLoading = false }
        do {
            maintenance = try await api.maintainStorage(dryRun: dryRun)
            if !dryRun {
                snapshot = try await api.storageSettings()
            }
            errorMessage = nil
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    private static func describe(_ error: Error) -> String {
        if case HealthMesAPIError.server(_, _, let message, _) = error {
            return message
        }
        if case HealthMesAPIError.unauthorized = error {
            return "The paired instance rejected this device."
        }
        if case HealthMesAPIError.notPaired = error {
            return "Connect HealthMes first."
        }
        return error.localizedDescription
    }
}
