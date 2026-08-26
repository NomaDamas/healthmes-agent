import SwiftUI
import UserNotifications

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var notificationStatus: UNAuthorizationStatus = .notDetermined
    @State private var showAdvanced = false
    @State private var showHealthKitQueueDeletion = false
    @StateObject private var calendarPermission = DeviceCalendarPermissionModel()
    @StateObject private var healthKit = HealthKitSyncManager.shared
    @StateObject private var settingsHub = SettingsHubModel()

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
                    if let error = settingsHub.errorMessage {
                        Text(verbatim: error)
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
                            .background(HealthMesVisualStyle.brand, in: Circle())
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
                } else if healthKit.isPaused {
                    Button {
                        Task { await healthKit.resumeSync() }
                    } label: {
                        Label(
                            "Resume Apple Health sync",
                            systemImage: "play.circle"
                        )
                    }
                } else {
                    Button {
                        Task { await healthKit.sync() }
                    } label: {
                        Label("Sync Apple Health now", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(
                        healthKit.state == .syncing
                            || healthKit.isPaused
                    )
                }

                if healthKit.pendingUploadCount > 0 {
                    LabeledContent("Pending uploads") {
                        Text(
                            verbatim:
                                "\(healthKit.pendingUploadCount)"
                        )
                    }
                    if healthKit.terminalFailureCount > 0 {
                        Label(
                            healthKit.latestTerminalFailure
                                ?? "Some uploads need attention.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.footnote)
                        .foregroundStyle(.orange)
                    }
                    if let nextRetryAt = healthKit.nextRetryAt {
                        LabeledContent("Next automatic retry") {
                            Text(
                                nextRetryAt,
                                style: .relative
                            )
                        }
                    }
                    Button {
                        Task { await healthKit.retryPendingUploads() }
                    } label: {
                        Label(
                            "Retry queued uploads now",
                            systemImage: "arrow.clockwise.circle"
                        )
                    }
                    .disabled(
                        healthKit.state == .syncing
                            || healthKit.isPaused
                    )
                }

                if healthKit.state != .notRequested,
                    healthKit.state != .unavailable,
                    !healthKit.isPaused
                {
                    Button {
                        Task { await healthKit.pauseSync() }
                    } label: {
                        Label(
                            "Pause Apple Health sync",
                            systemImage: "pause.circle"
                        )
                    }
                    .disabled(healthKit.state == .syncing)
                }

                if healthKit.pendingUploadCount > 0
                    || healthKit.queueStatusError != nil
                {
                    Button(role: .destructive) {
                        showHealthKitQueueDeletion = true
                    } label: {
                        Label(
                            "Delete queued health data",
                            systemImage: "trash"
                        )
                    }
                    .disabled(healthKit.state == .syncing)
                }

                if case .failed(let message) = healthKit.state {
                    Text(verbatim: message)
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
                Text(
                    "To revoke HealthKit access, use Settings > Health > Data Access & Devices > HealthMes."
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            } header: {
                Text("Apple Health")
            } footer: {
                Text(
                    "Apple Watch data is collected once through iPhone HealthKit. Pending batches are encrypted on this iPhone and sent only to the currently paired HealthMes instance."
                )
            }

            Section {
                WearableManagementView(model: settingsHub)
                if let pairing = PairingStore.shared.load() {
                    Link(
                        destination: ViewerURL.make(
                            pairing: pairing,
                            pathComponents: ["connect"],
                            fragment: "wearables"
                        )
                    ) {
                        Label(
                            "Open server Settings Hub",
                            systemImage: "arrow.up.right.square"
                        )
                    }
                }
            } header: {
                Text("Wearables Settings Hub")
            } footer: {
                Text(
                    "This page reads the HealthMes server's provider catalog and device inventory. OAuth credentials and Open Wearables API keys remain on the server; this iPhone stores only its HealthMes pairing."
                )
            }

            Section {
                if settingsHub.isLoading, settingsHub.inputs.isEmpty {
                    ProgressView("Loading server data sources…")
                } else if settingsHub.inputs.isEmpty {
                    Text("No server data sources are available.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(settingsHub.inputs) { source in
                        inputSourceDisclosure(source)
                    }
                }

                if let error = settingsHub.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }

                Button {
                    Task { await settingsHub.load() }
                } label: {
                    if settingsHub.isLoading {
                        Label("Refreshing server settings…", systemImage: "arrow.triangle.2.circlepath")
                    } else {
                        Label("Refresh server settings", systemImage: "arrow.clockwise")
                    }
                }
                .disabled(settingsHub.isLoading)
            } header: {
                Text("HealthMes data sources")
            } footer: {
                Text(
                    "These controls are stored on the paired HealthMes server. Mac and iPhone stay in sync by reading this same server source of truth; secrets such as the Open Wearables API key never enter either app."
                )
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
            await settingsHub.load()
            await healthKit.refreshStatus()
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            calendarPermission.refresh()
            Task {
                notificationStatus = await NotificationManager.shared.authorizationStatus()
                await settingsHub.load()
                await healthKit.refreshStatus()
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(
                for: .healthmesPairingChanged
            )
        ) { _ in
            settingsHub.reset()
            Task {
                await healthKit.pairingDidChange()
                await settingsHub.load()
            }
        }
        .refreshable {
            await settingsHub.load()
            await healthKit.refreshStatus()
        }
        .confirmationDialog(
            "Delete queued health data?",
            isPresented: $showHealthKitQueueDeletion,
            titleVisibility: .visible
        ) {
            Button("Delete queue and pause sync", role: .destructive) {
                Task { await healthKit.deletePendingUploads() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "Encrypted batches waiting on this iPhone for the current HealthMes pairing are deleted. If the encrypted queue is unreadable, HealthMes may erase the entire local Apple Health queue so corrupted data cannot remain. Server data is unchanged."
            )
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
        guard let check = settingsHub.readiness?.check(key) else {
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

    private func inputSourceDisclosure(
        _ source: InputSourceDescriptor
    ) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 14) {
                LabeledContent("Server status") {
                    Text(verbatim: InputControlPlanePresentation.sourceSummary(source))
                        .foregroundStyle(.secondary)
                }
                LabeledContent("Platforms") {
                    Text(verbatim: source.platforms.map(platformTitle).joined(separator: ", "))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                }
                LabeledContent("Capabilities") {
                    Text(
                        verbatim: source.capabilities
                            .map(InputControlPlanePresentation.humanize)
                            .joined(separator: ", ")
                    )
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
                }

                if source.isWearable {
                    LabeledContent("Provider/device inventory") {
                        Text(verbatim: InputControlPlanePresentation.instanceSummary(source))
                            .foregroundStyle(.secondary)
                    }
                    if source.sourceID == "wearable.open-wearables" {
                        LabeledContent("Server credential") {
                            Text(
                                source.connectionState == .notConfigured
                                    ? "Not configured on server"
                                    : "Configured on server"
                            )
                            .foregroundStyle(.secondary)
                        }
                    }
                }

                if source.supports(
                    setting: "decision_access_enabled",
                    scope: "domain"
                ) {
                    Toggle(
                        "Allow Decision Agent access",
                        isOn: decisionAccessBinding(source)
                    )
                    .disabled(settingsHub.isBusy(sourceID: source.sourceID))
                }

                if source.supports(
                    setting: "source_enabled",
                    scope: "source"
                ) {
                    Toggle(
                        "Use this source in HealthMes",
                        isOn: sourceEnabledBinding(source)
                    )
                    .disabled(settingsHub.isBusy(sourceID: source.sourceID))
                }

                if source.supports(setting: "retention", scope: "data_class") {
                    ForEach(source.retention) { policy in
                        Picker(
                            retentionTitle(policy.dataClass),
                            selection: retentionBinding(
                                source: source,
                                policy: policy
                            )
                        ) {
                            ForEach(source.retentionAllowedValues, id: \.self) { preset in
                                Text(retentionPresetTitle(preset))
                                    .tag(preset)
                            }
                        }
                        .disabled(settingsHub.isBusy(sourceID: source.sourceID))
                    }
                }

                if !source.instances.isEmpty {
                    Divider()
                    ForEach(source.instances) { instance in
                        instanceControl(instance, source: source)
                    }
                } else if source.isWearable {
                    Text(
                        "Provider connections and device inventory are managed in the Wearables Settings Hub above."
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }

                if !source.limitations.isEmpty {
                    Divider()
                    ForEach(source.limitations, id: \.self) { limitation in
                        Label(
                            InputControlPlanePresentation.limitation(limitation),
                            systemImage: "info.circle"
                        )
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    }
                }

                if let message = settingsHub.sourceMessages[source.sourceID] {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }

                if settingsHub.isBusy(sourceID: source.sourceID) {
                    ProgressView("Saving to HealthMes…")
                        .font(.footnote)
                }
            }
            .padding(.top, 8)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: sourceIcon(source))
                    .foregroundStyle(HealthMesVisualStyle.brand)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: source.displayName)
                        .font(.body.weight(.semibold))
                    Text(verbatim: InputControlPlanePresentation.sourceSummary(source))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func instanceControl(
        _ instance: InputInstance,
        source: InputSourceDescriptor
    ) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Toggle(
                isOn: instanceEnabledBinding(
                    source: source,
                    instance: instance
                )
            ) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: platformTitle(instance.platform))
                        .font(.subheadline.weight(.semibold))
                    Text(verbatim: instance.instanceID)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
            .disabled(
                !source.supports(setting: "enabled", scope: "instance")
                    || settingsHub.isBusy(sourceID: source.sourceID)
            )
            Text(verbatim: InputControlPlanePresentation.instanceStatus(instance))
                .font(.caption)
                .foregroundStyle(.secondary)
            if source.supports(setting: "paused_until", scope: "instance") {
                HStack(spacing: 8) {
                    Button("Pause 1 hour") {
                        Task {
                            await settingsHub.pauseInstance(
                                until: Date().addingTimeInterval(3600),
                                instanceID: instance.instanceID,
                                for: source.sourceID
                            )
                        }
                    }
                    .buttonStyle(.bordered)
                    .disabled(settingsHub.isBusy(sourceID: source.sourceID))
                    if instance.pausedUntil != nil {
                        Button("Resume") {
                            Task {
                                await settingsHub.resumeInstance(
                                    instanceID: instance.instanceID,
                                    for: source.sourceID
                                )
                            }
                        }
                        .buttonStyle(.bordered)
                        .disabled(settingsHub.isBusy(sourceID: source.sourceID))
                    }
                }
            }
        }
    }

    private func sourceEnabledBinding(
        _ source: InputSourceDescriptor
    ) -> Binding<Bool> {
        Binding(
            get: { source.sourceEnabled ?? true },
            set: { enabled in
                Task {
                    await settingsHub.setSourceEnabled(
                        enabled,
                        for: source.sourceID
                    )
                }
            }
        )
    }

    private func decisionAccessBinding(
        _ source: InputSourceDescriptor
    ) -> Binding<Bool> {
        Binding(
            get: { source.decisionAccessEnabled },
            set: { enabled in
                Task {
                    await settingsHub.setDecisionAccess(
                        enabled,
                        for: source.sourceID
                    )
                }
            }
        )
    }

    private func retentionBinding(
        source: InputSourceDescriptor,
        policy: InputRetentionPolicy
    ) -> Binding<String> {
        Binding(
            get: { policy.preset },
            set: { preset in
                Task {
                    await settingsHub.setRetention(
                        preset,
                        dataClass: policy.dataClass,
                        for: source.sourceID
                    )
                }
            }
        )
    }

    private func instanceEnabledBinding(
        source: InputSourceDescriptor,
        instance: InputInstance
    ) -> Binding<Bool> {
        Binding(
            get: { instance.enabled },
            set: { enabled in
                Task {
                    await settingsHub.setInstanceEnabled(
                        enabled,
                        instanceID: instance.instanceID,
                        for: source.sourceID
                    )
                }
            }
        )
    }

    private func sourceIcon(_ source: InputSourceDescriptor) -> String {
        switch source.domain {
        case "activity":
            return "figure.walk.motion"
        case "nutrition":
            return "fork.knife"
        case "wearable":
            return source.sourceID == "wearable.healthkit-bridge"
                ? "heart.text.square"
                : "watch.analog"
        case "calendar":
            return "calendar"
        default:
            return "square.stack.3d.up"
        }
    }

    private func platformTitle(_ platform: String) -> String {
        switch platform {
        case "ios":
            return "iPhone"
        case "watchos":
            return "Apple Watch"
        case "macos":
            return "Mac"
        default:
            return InputControlPlanePresentation.humanize(platform)
        }
    }

    private func retentionTitle(_ dataClass: String) -> String {
        "\(InputControlPlanePresentation.humanize(dataClass)) retention"
    }

    private func retentionPresetTitle(_ preset: String) -> String {
        preset == "forever" ? "Keep forever" : preset
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
