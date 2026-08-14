import SwiftUI
import UserNotifications

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
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
                        "Connection",
                        value: connectionLabel(pairing),
                        systemImage: connectionSymbol(pairing)
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
                        value: "Connected through iPhone",
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
                Text("Connection")
            } footer: {
                Text("iPhone reaches your Mac or Linux HealthMes through Tailscale. Apple Watch uses iPhone as its secure connection hub.")
            }

            Section {
                ForEach(TailscalePairingPresentation.steps) { step in
                    HStack(alignment: .top, spacing: 10) {
                        Text(verbatim: "\(step.number)")
                            .font(.caption.bold().monospacedDigit())
                            .foregroundStyle(.white)
                            .frame(width: 22, height: 22)
                            .background(HealthMesVisualStyle.capacity, in: Circle())
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: step.title)
                                .font(.subheadline.weight(.semibold))
                            Text(verbatim: step.detail)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Link(destination: TailscalePairingPresentation.downloadURL) {
                    Label("Open Tailscale setup", systemImage: "network.badge.shield.half.filled")
                }
            } header: {
                Text("Connect another iPhone")
            } footer: {
                Text("Generate the QR on the Mac or Linux host. The QR uses a one-time pairing code, not the long-lived API token.")
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
            calendarPermission.refresh()
            await loadReadiness()
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            calendarPermission.refresh()
            Task {
                notificationStatus = await NotificationManager.shared.authorizationStatus()
            }
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

    private func connectionLabel(_ pairing: Pairing) -> String {
        switch TailscalePairingPresentation.transport(for: pairing) {
        case .disconnected:
            return String(localized: "Not connected")
        case .sameDevice:
            return String(localized: "Local demo")
        case .tailscaleDNS, .tailscaleIP:
            return String(localized: "Connected · Tailscale")
        case .remoteHTTPS:
            return String(localized: "Connected · HTTPS")
        }
    }

    private func connectionSymbol(_ pairing: Pairing) -> String {
        TailscalePairingPresentation.transport(for: pairing).isTailscale
            ? "network.badge.shield.half.filled"
            : "network"
    }
}
