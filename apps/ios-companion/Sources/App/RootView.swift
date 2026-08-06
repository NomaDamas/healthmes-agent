import SwiftUI

/// One fixed HealthMes control shell. Lenses change the perspective of the
/// same canvas; command input and loaded state remain in place.
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
