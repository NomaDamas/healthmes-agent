import Foundation

extension ScreenTimeActivitySyncService {
    static func live(
        deviceID: String? = nil,
        transport: any ScreenTimeActivityTransport =
            URLSessionScreenTimeActivityTransport(),
        stateStore: ScreenTimeSyncStateStore = .shared,
        pseudonymKeyLoader: () throws -> Data = {
            try ScreenTimePseudonymKeyStore().loadOrCreate()
        },
        fallbackIdentityStore: ScreenTimeFallbackDeviceIdentityStore =
            ScreenTimeFallbackDeviceIdentityStore()
    ) -> ScreenTimeActivitySyncService {
        let identity = ScreenTimeActivityIdentityResolver.resolve(
            explicitDeviceID: deviceID,
            pseudonymKeyLoader: pseudonymKeyLoader,
            fallbackIdentityStore: fallbackIdentityStore
        )
        return ScreenTimeActivitySyncService(
            deviceID: identity.deviceID,
            collector: ScreenTimeActivityCollectorFactory.make(
                pseudonymKeyData: identity.pseudonymKeyData
            ),
            transport: transport,
            stateStore: stateStore
        )
    }
}
