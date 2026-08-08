import AppKit
import SwiftUI

struct MacSettingsView: View {
    @ObservedObject var glanceStore: GlanceStore
    @ObservedObject var notifications: MacNotificationManager
    @ObservedObject var dashboardStore: MacDashboardStore

    @State private var notificationsEnabled = MacNotificationManager.shared.isEnabled
    @State private var showAdvanced = false
    @State private var serverReadiness: SetupReadiness?
    @State private var readinessError: String?
    @StateObject private var setup = MacSetupCoordinator()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                MacPageHeader(
                    eyebrow: "Settings",
                    title: "Simple by default.",
                    subtitle: "Connection, calendar and alerts stay visible. Tokens and self-host details stay under Advanced."
                )

                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 280), spacing: 16)],
                    spacing: 16
                ) {
                    MacSetupView(
                        coordinator: setup,
                        glanceStore: glanceStore
                    )
                    connectionCard
                    calendarCard
                    notificationsCard
                    privacyCard
                }

                DisclosureGroup(isExpanded: $showAdvanced) {
                    PairingSettingsView(
                        store: glanceStore,
                        notifications: notifications
                    )
                    .padding(.top, 12)
                    Divider()
                        .padding(.vertical, 12)
                    MacStorageAdvancedView()
                    Divider()
                        .padding(.vertical, 12)
                    MacSetupAdvancedView(coordinator: setup)
                } label: {
                    Label("Advanced · self-host and diagnostics", systemImage: "slider.horizontal.3")
                        .font(.headline)
                }
                .padding(20)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
            }
            .padding(32)
        }
        .task { await loadReadiness() }
    }

    private var calendarCard: some View {
        MacSurfaceCard("Calendar", systemImage: "calendar.badge.clock") {
            VStack(alignment: .leading, spacing: 10) {
                Text("Google and iCloud")
                    .font(.title2.weight(.semibold))
                readinessLabel("calendar_google", fallback: "Google Calendar", icon: "g.circle")
                readinessLabel("calendar_icloud", fallback: "Apple Calendar (iCloud)", icon: "calendar")
                if let pairing = dashboardStore.pairing {
                    Link(destination: MacWebLinks.connections(pairing: pairing)) {
                        Label("Manage calendars", systemImage: "arrow.up.right.square")
                    }
                    .buttonStyle(.bordered)
                } else {
                    Text("Connect HealthMes first.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var connectionCard: some View {
        MacSurfaceCard("Connection", systemImage: "link") {
            VStack(alignment: .leading, spacing: 10) {
                Text(glanceStore.isPaired ? "Connected" : "Not connected")
                    .font(.title2.weight(.semibold))
                Text(
                    glanceStore.isPaired
                        ? "Today, Plan and Decisions use your paired HealthMes instance."
                        : "Open Advanced once to connect a self-hosted or managed instance."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                if let health = serverReadiness?.check("health") {
                    Label(
                        health.state == .ready ? "Health data ready" : health.detail,
                        systemImage: health.state == .ready
                            ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                    )
                    .foregroundStyle(
                        health.state == .ready ? MacHealthMesStyle.moss : .orange
                    )
                }
                if let readinessError {
                    Text(verbatim: readinessError)
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                if let pairing = dashboardStore.pairing {
                    Link(destination: MacWebLinks.dashboard(pairing: pairing)) {
                        Label("Open web dashboard", systemImage: "safari")
                    }
                    .buttonStyle(.bordered)
                } else {
                    Button("Connect HealthMes") {
                        showAdvanced = true
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(MacHealthMesStyle.moss)
                }
            }
        }
    }

    private var notificationsCard: some View {
        MacSurfaceCard("Notifications", systemImage: "bell.badge") {
            VStack(alignment: .leading, spacing: 10) {
                Toggle("Actionable alerts", isOn: $notificationsEnabled)
                    .toggleStyle(.switch)
                    .onChange(of: notificationsEnabled) { _, enabled in
                        Task {
                            await notifications.setEnabled(
                                enabled,
                                currentAlerts: glanceStore.alerts,
                                hasLoadedAlerts: glanceStore.hasLoadedAlerts
                            )
                        }
                    }
                Text("Yes/No actions use the same proposal contract as iPhone and Watch.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                if notifications.authorizationDenied {
                    Button("Open System Settings") {
                        openNotificationSettings()
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    private var privacyCard: some View {
        MacSurfaceCard("Privacy", systemImage: "lock.shield") {
            VStack(alignment: .leading, spacing: 10) {
                Label("No analytics", systemImage: "checkmark")
                Label("Paired instance only", systemImage: "checkmark")
                Label("On-device speech when available", systemImage: "checkmark")
                Text("Voice-created tasks send only the confirmed transcript to your instance.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .foregroundStyle(MacHealthMesStyle.graphite)
        }
    }

    private func openNotificationSettings() {
        guard
            let url = URL(
                string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
            )
        else { return }
        NSWorkspace.shared.open(url)
    }

    @ViewBuilder
    private func readinessLabel(
        _ key: String,
        fallback: String,
        icon: String
    ) -> some View {
        if let check = serverReadiness?.check(key) {
            Label(
                check.state == .ready ? "\(fallback) · Ready" : check.detail,
                systemImage: check.state == .ready
                    ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
            )
            .foregroundStyle(
                check.state == .ready ? MacHealthMesStyle.moss : .orange
            )
        } else {
            Label(fallback, systemImage: icon)
                .foregroundStyle(.secondary)
        }
    }

    private func loadReadiness() async {
        guard glanceStore.isPaired else { return }
        do {
            serverReadiness = try await HealthMesAPI().setupReadiness()
            readinessError = nil
        } catch {
            readinessError = "Could not verify setup readiness."
        }
    }
}
