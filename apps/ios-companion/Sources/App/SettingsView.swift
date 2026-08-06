import SwiftUI
import UserNotifications

/// Settings tab: the pairing form plus notification status and the honest
/// delivery story (OS-throttled background polling; Telegram remains the
/// guaranteed channel).
struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var notificationStatus: UNAuthorizationStatus = .notDetermined
    @State private var showAdvanced = false

    var body: some View {
        Form {
            Section {
                if let pairing = PairingStore.shared.load() {
                    LabeledContent("Connected") {
                        Text(verbatim: pairing.baseURL.host ?? pairing.baseURL.absoluteString)
                    }
                    Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["dashboard"])) {
                        Label("Open web dashboard", systemImage: "safari")
                    }
                } else {
                    Text("Not connected")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Account")
            }

            Section {
                Label("Apple Health data stays on your devices and paired instance.", systemImage: "heart.text.square")
                Text("Health permission changes are managed in iOS Settings.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Health")
            }

            Section {
                if let pairing = PairingStore.shared.load() {
                    Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["connect"])) {
                        Label("Manage Google and iCloud calendars", systemImage: "calendar.badge.clock")
                    }
                }
                Text("Approved proposals are applied by calendar sync; No leaves the calendar unchanged.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Calendar")
            }

            Section {
                Label("Pairing syncs automatically from this iPhone.", systemImage: "applewatch")
                Text("The Watch keeps only the compact Yes/No decision remote.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Apple Watch")
            }

            Section {
                Label("Storage and retention require owner authentication on the HealthMes host.", systemImage: "externaldrive")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Storage")
            }

            Section {
                NavigationLink {
                    WeeklyReportView()
                } label: {
                    Label("Weekly report", systemImage: "chart.bar.doc.horizontal")
                }
                NavigationLink {
                    CaptureView()
                } label: {
                    Label("Capture", systemImage: "camera")
                }
            } header: {
                Text("More")
            } footer: {
                Text("Daily essentials stay on Today, Plan, and Decisions.")
            }

            Section {
                DisclosureGroup(isExpanded: $showAdvanced) {
                    NavigationLink {
                        PairingView()
                            .navigationTitle(Text("Pairing"))
                    } label: {
                        Label("Self-host pairing and API token", systemImage: "link")
                    }
                } label: {
                    Label("Advanced", systemImage: "slider.horizontal.3")
                }
            }

            Section {
                LabeledContent {
                    Text(verbatim: statusText)
                } label: {
                    Text("Notifications")
                }
                if notificationStatus == .notDetermined {
                    Button {
                        Task {
                            _ = await NotificationManager.shared.requestAuthorization()
                            notificationStatus =
                                await NotificationManager.shared.authorizationStatus()
                        }
                    } label: {
                        Text("Enable native alerts")
                    }
                } else if notificationStatus == .denied {
                    Text("Notifications are off — enable them in iOS Settings > HealthMes.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Native alerts")
            } footer: {
                Text(
                    "Native notifications come from background polling, which iOS throttles (typically a few checks per hour at best). For guaranteed, immediate delivery keep the Telegram channel — it stays the reliable path until a push relay exists."
                )
            }

            Section {
                LabeledContent {
                    Text(verbatim: appVersion)
                } label: {
                    Text("Version")
                }
                Text(
                    "Local-first: this app talks only to your paired instance. No analytics, no third-party services, no cloud relay."
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            } header: {
                Text("About")
            }
        }
        .navigationTitle(Text("Settings"))
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }
            }
        }
        .task {
            notificationStatus = await NotificationManager.shared.authorizationStatus()
        }
    }

    private var statusText: String {
        switch notificationStatus {
        case .authorized, .provisional, .ephemeral:
            return String(localized: "Enabled")
        case .denied:
            return String(localized: "Denied")
        default:
            return String(localized: "Not requested")
        }
    }

    private var appVersion: String {
        let version =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return version ?? "—"
    }
}
