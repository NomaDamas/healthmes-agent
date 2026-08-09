import SwiftUI

struct MacDashboardRootView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var notifications: MacNotificationManager
    @EnvironmentObject private var router: MacAppRouter
    @StateObject private var dashboardStore = MacDashboardStore()
    @State private var detail: MacDetailContext?

    var body: some View {
        ZStack {
            MacHealthMesStyle.canvas
                .ignoresSafeArea()
            MacWellnessControlView(
                glanceStore: glanceStore,
                dashboardStore: dashboardStore,
                onSelect: { detail = $0 },
                onRefresh: { force in
                    await refreshAll(force: force)
                },
                onSettings: {
                    router.presentSettings()
                }
            )
        }
        .preferredColorScheme(.light)
        .inspector(
            isPresented: Binding(
                get: { detail != nil },
                set: { if !$0 { detail = nil } }
            )
        ) {
            if let detail {
                MacDetailInspector(
                    detail: detail,
                    pairing: dashboardStore.pairing,
                    onClose: { self.detail = nil }
                )
                .inspectorColumnWidth(min: 300, ideal: 360, max: 460)
            }
        }
        .sheet(
            isPresented: Binding(
                get: { router.isSettingsPresented },
                set: { if !$0 { router.dismissSettings() } }
            )
        ) {
            MacSettingsView(
                glanceStore: glanceStore,
                notifications: notifications,
                dashboardStore: dashboardStore
            )
            .frame(minWidth: 760, minHeight: 620)
        }
        .frame(minWidth: 760, minHeight: 620)
        .task {
            await refreshAll(force: glanceStore.payload == nil)
        }
        .onChange(of: glanceStore.pairingRevision) { _, _ in
            dashboardStore.resetForPairingChange()
            Task { await dashboardStore.refresh() }
        }
    }

    private func refreshAll(force: Bool) async {
        await glanceStore.refresh(force: force)
        await dashboardStore.refresh()
    }
}
