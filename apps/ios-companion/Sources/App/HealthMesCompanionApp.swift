import SwiftUI

@main
struct HealthMesCompanionApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var router = AppRouter.shared

    init() {
        // Order matters at launch:
        // 1. BGTaskScheduler handlers must be registered before the app
        //    finishes launching.
        BackgroundRefreshManager.shared.register()
        // 2. The notification delegate must exist before a notification tap
        //    can deliver its response.
        NotificationManager.shared.configure()
        // 3. WatchConnectivity early so a saved pairing reaches a freshly
        //    installed watch app without reopening the pairing screen.
        PhoneWatchSync.shared.activate()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(router)
                .onOpenURL { url in
                    router.handle(url)
                }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                // Foreground sync drives notifications + the Live Activity
                // even when iOS grants no background budget (README: the OS
                // throttles BGAppRefreshTask; Telegram stays the guaranteed
                // channel).
                Task { await RefreshCoordinator.shared.sync(isForeground: true) }
            case .background:
                BackgroundRefreshManager.shared.schedule()
            default:
                break
            }
        }
    }
}
