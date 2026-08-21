import Foundation

extension ScreenTimeActivitySyncService {
    static func live(
        deviceID: String? = nil,
        transport: any ScreenTimeActivityTransport =
            URLSessionScreenTimeActivityTransport(),
        stateStore: ScreenTimeSyncStateStore = .shared,
        outbox: ScreenTimeActivityOutbox = .shared,
        pseudonymKeyLoader: () throws -> Data = {
            try ScreenTimePseudonymKeyStore().loadOrCreate()
        },
        authorizationIntentStore:
            ScreenTimeAuthorizationIntentStore =
                ScreenTimeAuthorizationIntentStore()
    ) -> ScreenTimeActivitySyncService {
        let normalizedDeviceID = deviceID?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let knownDeviceID =
            normalizedDeviceID.flatMap {
                $0.isEmpty ? nil : $0
            }
            ?? authorizationIntentStore.activeDeviceID
            ?? authorizationIntentStore.legacyFallbackDeviceID
        guard
            authorizationIntentStore.isOptedIn,
            !authorizationIntentStore.isPrivacyCleanupPending
        else {
            return unavailableService(
                deviceID: knownDeviceID,
                reason:
                    authorizationIntentStore.isPrivacyCleanupPending
                    ? "ios_screen_time_privacy_cleanup_pending"
                    : "ios_screen_time_not_opted_in",
                transport: transport,
                stateStore: stateStore,
                outbox: outbox,
                cleanupDeviceIDs:
                    authorizationIntentStore.privacyCleanupDeviceIDs
            )
        }

        #if HEALTHMES_APP_WEBSITE_USAGE_SDK_AVAILABLE
        if #available(iOS 26.4, *) {
            let identity = ScreenTimeActivityIdentityResolver.resolve(
                explicitDeviceID: deviceID,
                pseudonymKeyLoader: pseudonymKeyLoader,
                rememberedDeviceID:
                    authorizationIntentStore.activeDeviceID
            )
            if identity.pseudonymKeyData != nil {
                authorizationIntentStore.rememberActiveDeviceID(
                    identity.deviceID
                )
            }
            return ScreenTimeActivitySyncService(
                deviceID: identity.deviceID,
                collector: ScreenTimeActivityCollectorFactory.make(
                    pseudonymKeyData: identity.pseudonymKeyData
                ),
                transport: transport,
                stateStore: stateStore,
                outbox: outbox,
                cleanupDeviceIDs:
                    authorizationIntentStore.privacyCleanupDeviceIDs
            )
        }
        return unavailableService(
            deviceID: knownDeviceID,
            reason: "ios_screen_time_export_requires_ios_26_4",
            transport: transport,
            stateStore: stateStore,
            outbox: outbox,
            cleanupDeviceIDs:
                authorizationIntentStore.privacyCleanupDeviceIDs
        )
        #elseif HEALTHMES_SCREENTIME_OPT_IN_REQUESTED
        return unavailableService(
            deviceID: knownDeviceID,
            reason: "ios_screen_time_export_sdk_unavailable",
            transport: transport,
            stateStore: stateStore,
            outbox: outbox,
            cleanupDeviceIDs:
                authorizationIntentStore.privacyCleanupDeviceIDs
        )
        #else
        return unavailableService(
            deviceID: knownDeviceID,
            reason: "ios_screen_time_normal_build_unavailable",
            transport: transport,
            stateStore: stateStore,
            outbox: outbox,
            cleanupDeviceIDs:
                authorizationIntentStore.privacyCleanupDeviceIDs
        )
        #endif
    }

    private static func unavailableService(
        deviceID: String?,
        reason: String,
        transport: any ScreenTimeActivityTransport,
        stateStore: ScreenTimeSyncStateStore,
        outbox: ScreenTimeActivityOutbox,
        cleanupDeviceIDs: Set<String>
    ) -> ScreenTimeActivitySyncService {
        let normalizedDeviceID = deviceID?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedDeviceID =
            normalizedDeviceID.flatMap {
                $0.isEmpty ? nil : $0
            }
            ?? ScreenTimeActivityIdentityResolver.unavailableDeviceID
        return ScreenTimeActivitySyncService(
            deviceID: resolvedDeviceID,
            collector: UnavailableScreenTimeActivityCollector(
                reason: reason
            ),
            transport: transport,
            stateStore: stateStore,
            outbox: outbox,
            cleanupDeviceIDs: cleanupDeviceIDs
        )
    }
}
