import SwiftUI

/// One fixed HealthMes control shell. Current health impact is the default;
/// deeper calendar, goal and outcome views stay behind the Explore menu.
struct RootView: View {
    @EnvironmentObject private var router: AppRouter
    @State private var isPaired = PairingStore.shared.load() != nil

    var body: some View {
        Group {
            if isPaired {
                NavigationStack {
                    WellnessControlView()
                }
            } else {
                HealthMesOnboardingView()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPairingChanged)) { _ in
            isPaired = PairingStore.shared.load() != nil
        }
        .sheet(item: $router.decisionSheet) { target in
            SafariView(url: target.url)
                .ignoresSafeArea()
        }
        .sheet(item: $router.modal) { modal in
            switch modal {
            case .settings:
                NavigationStack {
                    SettingsView()
                }
            case .report:
                NavigationStack {
                    WeeklyReportView()
                }
            case .capture:
                NavigationStack {
                    CaptureView()
                }
            }
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

}

struct ProposalSheetTarget: Identifiable {
    let id: UUID
}
