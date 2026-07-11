import SwiftUI

/// Tab root of the full companion app (issue #10). Until an instance is
/// paired, the pairing screen takes the whole window — every other surface
/// depends on the base URL + token.
struct RootView: View {
    @EnvironmentObject private var router: AppRouter
    @State private var isPaired = PairingStore.shared.load() != nil

    var body: some View {
        Group {
            if isPaired {
                tabs
            } else {
                NavigationStack {
                    PairingView()
                        .navigationTitle(Text("HealthMes"))
                }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            isPaired = PairingStore.shared.load() != nil
        }
        .sheet(item: $router.decisionSheet) { target in
            SafariView(url: target.url)
                .ignoresSafeArea()
        }
        .sheet(
            item: Binding(
                get: { router.proposalSheetID.map { ProposalSheetTarget(id: $0) } },
                set: { router.proposalSheetID = $0?.id }
            )
        ) { target in
            NavigationStack {
                ProposalDetailView(proposalID: target.id)
            }
        }
    }

    private var tabs: some View {
        TabView(selection: $router.tab) {
            NavigationStack {
                BriefingHomeView()
            }
            .tabItem { Label("Home", systemImage: "gauge.medium") }
            .tag(AppTab.home)

            NavigationStack {
                WeeklyReportView()
            }
            .tabItem { Label("Report", systemImage: "chart.bar.doc.horizontal") }
            .tag(AppTab.report)

            NavigationStack {
                CaptureView()
            }
            .tabItem { Label("Capture", systemImage: "camera") }
            .tag(AppTab.capture)

            NavigationStack {
                SettingsView()
            }
            .tabItem { Label("Settings", systemImage: "gearshape") }
            .tag(AppTab.settings)
        }
    }
}

struct ProposalSheetTarget: Identifiable {
    let id: UUID
}
