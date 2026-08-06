import SwiftUI

struct MacDashboardRootView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var notifications: MacNotificationManager
    @EnvironmentObject private var router: MacAppRouter
    @StateObject private var dashboardStore = MacDashboardStore()
    @State private var detail: MacDetailContext?

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            ZStack {
                MacHealthMesStyle.canvas
                    .ignoresSafeArea()
                content
            }
            .toolbar {
                ToolbarItemGroup(placement: .primaryAction) {
                    MacPrivacyPill(
                        isPaired: glanceStore.isPaired,
                        isStale: glanceStore.isStale
                    )
                    Button {
                        Task { await refreshAll(force: true) }
                    } label: {
                        if glanceStore.isRefreshing || dashboardStore.isRefreshing {
                            ProgressView()
                                .controlSize(.small)
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .help("Refresh")
                    .disabled(glanceStore.isRefreshing || dashboardStore.isRefreshing)
                }
            }
        }
        .navigationSplitViewStyle(.balanced)
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
        .frame(minWidth: 980, minHeight: 680)
        .task {
            await refreshAll(force: glanceStore.payload == nil)
        }
        .onChange(of: glanceStore.isPaired) { _, _ in
            Task { await dashboardStore.refresh() }
        }
    }

    private var sidebar: some View {
        List(selection: $router.section) {
            Section {
                ForEach(MacAppSection.allCases) { section in
                    Label(section.title, systemImage: section.systemImage)
                        .tag(section)
                }
            }

            Section {
                VStack(alignment: .leading, spacing: 6) {
                    MacPrivacyPill(
                        isPaired: glanceStore.isPaired,
                        isStale: glanceStore.isStale
                    )
                    Text("Health and schedule data stay between this Mac and your paired instance.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 6)
            }
        }
        .navigationTitle(Text("HealthMes"))
        .listStyle(.sidebar)
    }

    @ViewBuilder
    private var content: some View {
        if !glanceStore.isPaired, router.section != .settings {
            MacNeedsPairingView {
                router.section = .settings
            }
        } else {
            switch router.section {
            case .today:
                MacTodayView(
                    glanceStore: glanceStore,
                    dashboardStore: dashboardStore,
                    onSelect: { detail = $0 },
                    onSpeak: { router.requestSpeak() }
                )
            case .plan:
                MacPlanView(
                    glanceStore: glanceStore,
                    dashboardStore: dashboardStore,
                    onSelect: { detail = $0 }
                )
            case .decisions:
                MacDecisionsView(
                    glanceStore: glanceStore,
                    dashboardStore: dashboardStore,
                    onSelect: { detail = $0 }
                )
            case .speak:
                MacSpeakView(
                    dashboardStore: dashboardStore,
                    onNavigate: { router.section = $0 },
                    onRefresh: { Task { await refreshAll(force: true) } }
                )
            case .settings:
                MacSettingsView(
                    glanceStore: glanceStore,
                    notifications: notifications,
                    dashboardStore: dashboardStore
                )
            }
        }
    }

    private func refreshAll(force: Bool) async {
        await glanceStore.refresh(force: force)
        await dashboardStore.refresh()
    }
}

private struct MacNeedsPairingView: View {
    let openSettings: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "bolt.heart.fill")
                .font(.system(size: 48))
                .foregroundStyle(MacHealthMesStyle.moss)
            Text("Connect HealthMes")
                .font(.system(size: 28, weight: .semibold, design: .rounded))
            Text("One private connection unlocks Today, Plan, Decisions and Speak on this Mac.")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
            Button("Open Settings", action: openSettings)
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .controlSize(.large)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(48)
    }
}
