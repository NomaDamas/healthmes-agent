import SwiftUI
import UserNotifications

/// Settings tab: the pairing form plus notification status and the honest
/// delivery story (OS-throttled background polling; Telegram remains the
/// guaranteed channel).
struct SettingsView: View {
    @State private var notificationStatus: UNAuthorizationStatus = .notDetermined

    var body: some View {
        Form {
            Section {
                NavigationLink {
                    PairingView()
                        .navigationTitle(Text("Pairing"))
                } label: {
                    Label("Instance pairing", systemImage: "link")
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
