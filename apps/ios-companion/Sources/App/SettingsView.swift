import EventKit
import SwiftUI
import UserNotifications

@MainActor
private final class DeviceCalendarPermissionModel: ObservableObject {
    @Published var status = EKEventStore.authorizationStatus(for: .event)
    @Published var message: String?

    private let store = EKEventStore()

    func request() async {
        do {
            _ = try await store.requestFullAccessToEvents()
            status = EKEventStore.authorizationStatus(for: .event)
            message = nil
        } catch {
            status = EKEventStore.authorizationStatus(for: .event)
            message = error.localizedDescription
        }
    }

    var label: String {
        switch status {
        case .fullAccess, .authorized:
            return String(localized: "Device access granted")
        case .writeOnly:
            return String(localized: "Write-only access")
        case .denied, .restricted:
            return String(localized: "Permission denied")
        case .notDetermined:
            return String(localized: "Not requested")
        @unknown default:
            return String(localized: "Unknown")
        }
    }
}

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var notificationStatus: UNAuthorizationStatus = .notDetermined
    @State private var showAdvanced = false
    @State private var serverReadiness: SetupReadiness?
    @State private var readinessError: String?
    @StateObject private var calendarPermission = DeviceCalendarPermissionModel()
    @StateObject private var healthKit = HealthKitSyncManager.shared

    var body: some View {
        Form {
            Section {
                if let pairing = PairingStore.shared.load() {
                    readinessRow(
                        "HealthMes instance",
                        value: instanceMode(pairing),
                        systemImage: "network"
                    )
                    readinessRow(
                        "Apple Health",
                        value: healthKit.statusText,
                        systemImage: "heart.text.square"
                    )
                    readinessRow(
                        "Google Calendar",
                        value: readinessValue("calendar_google"),
                        systemImage: "g.circle"
                    )
                    readinessRow(
                        "Apple Calendar sync",
                        value: readinessValue("calendar_icloud"),
                        systemImage: "calendar"
                    )
                    readinessRow(
                        "Device calendar access",
                        value: calendarPermission.label,
                        systemImage: "iphone"
                    )
                    readinessRow(
                        "Decision notifications",
                        value: statusText,
                        systemImage: "bell.badge"
                    )
                    readinessRow(
                        "Apple Watch",
                        value: "Pairing follows iPhone",
                        systemImage: "applewatch"
                    )
                    LabeledContent("Instance host") {
                        Text(verbatim: pairing.baseURL.host ?? pairing.baseURL.absoluteString)
                    }
                    if let readinessError {
                        Text(verbatim: readinessError)
                            .font(.footnote)
                            .foregroundStyle(.orange)
                    }
                } else {
                    Text("Not connected")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Ready check")
            } footer: {
                Text("Device calendar permission and HealthMes server synchronization are separate. A proposal is on the external calendar only after it reaches Applied.")
            }

            Section {
                if healthKit.state == .notRequested {
                    Button {
                        Task { await healthKit.requestAuthorizationAndSync() }
                    } label: {
                        Label("Connect Apple Health", systemImage: "heart.badge.plus")
                    }
                } else {
                    Button {
                        Task { await healthKit.sync() }
                    } label: {
                        Label("Sync Apple Health now", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(healthKit.state == .syncing)
                }
                if case .failed(let message) = healthKit.state {
                    Text(verbatim: message)
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
            } header: {
                Text("Apple Health")
            } footer: {
                Text("Apple Watch data is collected once through iPhone HealthKit and uploaded only to your paired HealthMes instance.")
            }

            Section {
                if let pairing = PairingStore.shared.load() {
                    Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["dashboard"])) {
                        Label("Open detailed web dashboard", systemImage: "safari")
                    }
                }
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
                Text("Details")
            }

            Section {
                DisclosureGroup(isExpanded: $showAdvanced) {
                    if calendarPermission.status == .notDetermined {
                        Button {
                            Task { await calendarPermission.request() }
                        } label: {
                            Label("Request Apple Calendar access", systemImage: "calendar.badge.plus")
                        }
                    }
                    if let message = calendarPermission.message {
                        Text(verbatim: message)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    if let pairing = PairingStore.shared.load() {
                        Link(destination: ViewerURL.make(pairing: pairing, pathComponents: ["connect"])) {
                            Label("Server calendar connections", systemImage: "calendar.badge.clock")
                        }
                        Text("Google OAuth and iCloud CalDAV configure the paired server. EventKit permission above only grants this app access to calendars already configured on this iPhone.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    NavigationLink {
                        PairingView()
                            .navigationTitle(Text("Pairing"))
                    } label: {
                        Label("Self-host pairing and API token", systemImage: "link")
                    }
                    NavigationLink {
                        StorageAdvancedView()
                    } label: {
                        Label("Storage and retention", systemImage: "externaldrive")
                    }
                    LabeledContent("Version") {
                        Text(verbatim: appVersion)
                    }
                } label: {
                    Label("Advanced", systemImage: "slider.horizontal.3")
                }
            }

            Section {
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
        }
        .navigationTitle(Text("Settings"))
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }
            }
        }
        .task {
            notificationStatus = await NotificationManager.shared.authorizationStatus()
            await loadReadiness()
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

    private func readinessRow(
        _ title: LocalizedStringKey,
        value: String,
        systemImage: String
    ) -> some View {
        LabeledContent {
            Text(verbatim: value)
                .foregroundStyle(.secondary)
        } label: {
            Label(title, systemImage: systemImage)
        }
    }

    private func readinessValue(_ key: String) -> String {
        guard let check = serverReadiness?.check(key) else {
            return String(localized: "Checking…")
        }
        switch check.state {
        case .ready:
            return String(localized: "Ready")
        case .actionRequired:
            return check.detail
        case .blocked:
            return String(localized: "Blocked · \(check.detail)")
        }
    }

    private func loadReadiness() async {
        do {
            serverReadiness = try await HealthMesAPI().setupReadiness()
            readinessError = nil
        } catch {
            readinessError = String(localized: "Could not verify server readiness.")
        }
    }

    private func instanceMode(_ pairing: Pairing) -> String {
        guard let host = pairing.baseURL.host?.lowercased() else {
            return String(localized: "Self-host")
        }
        if host == "localhost" || host == "127.0.0.1" || host == "::1" {
            return String(localized: "Local demo")
        }
        if pairing.baseURL.scheme?.lowercased() == "https" {
            return String(localized: "HTTPS instance")
        }
        return String(localized: "LAN self-host")
    }
}
