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
        .sheet(item: $router.modal) { modal in
            switch modal {
            case .speak:
                NavigationStack {
                    SpeakView()
                }
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

    private var tabs: some View {
        VStack(spacing: 0) {
            NavigationStack {
                Group {
                    switch router.tab {
                    case .today:
                        BriefingHomeView()
                    case .plan:
                        PlanView()
                    case .decisions:
                        DecisionsView()
                    }
                }
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            router.modal = .settings
                        } label: {
                            Image(systemName: "person.crop.circle")
                        }
                        .accessibilityLabel(Text("Profile"))
                    }
                }
            }
            UnifiedProductDock(selection: $router.tab) {
                router.modal = .speak
            }
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }
}

struct ProposalSheetTarget: Identifiable {
    let id: UUID
}

private struct UnifiedProductDock: View {
    @Binding var selection: AppTab
    let onSpeak: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            tabButton("Today", systemImage: "sun.max.fill", tab: .today)
            tabButton("Plan", systemImage: "calendar", tab: .plan)

            Button(action: onSpeak) {
                VStack(spacing: 3) {
                    Image(systemName: "waveform")
                        .font(.title3.weight(.semibold))
                    Text("Speak")
                        .font(.caption2.weight(.semibold))
                }
                .foregroundStyle(.white)
                .frame(width: 64, height: 54)
                .background(Color(red: 0.02, green: 0.34, blue: 0.25), in: Capsule())
                .shadow(color: .black.opacity(0.14), radius: 8, y: 3)
            }
            .buttonStyle(.plain)
            .accessibilityHint(Text("Opens voice-only input"))

            tabButton("Decisions", systemImage: "checkmark.circle", tab: .decisions)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
        .overlay(alignment: .top) {
            Divider()
        }
    }

    private func tabButton(_ title: LocalizedStringKey, systemImage: String, tab: AppTab)
        -> some View
    {
        Button {
            selection = tab
        } label: {
            VStack(spacing: 3) {
                Image(systemName: systemImage)
                    .font(.body.weight(selection == tab ? .semibold : .regular))
                Text(title)
                    .font(.caption2)
                    .lineLimit(1)
            }
            .foregroundStyle(selection == tab ? Color.accentColor : Color.secondary)
            .frame(maxWidth: .infinity, minHeight: 48)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selection == tab ? .isSelected : [])
    }
}
