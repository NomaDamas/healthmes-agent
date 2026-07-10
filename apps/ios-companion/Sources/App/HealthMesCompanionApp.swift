import SwiftUI

@main
struct HealthMesCompanionApp: App {
    init() {
        // Activate WatchConnectivity early so a previously saved pairing can
        // reach a freshly installed watch app without reopening this screen.
        PhoneWatchSync.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            PairingView()
        }
    }
}
