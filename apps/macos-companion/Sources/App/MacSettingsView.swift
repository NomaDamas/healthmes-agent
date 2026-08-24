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
    @StateObject private var inputControl = InputControlPlaneModel()

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

                inputSourcesPanel

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
        .task {
            await loadReadiness()
            await inputControl.load()
        }
        .onChange(of: glanceStore.pairingRevision) { _, _ in
            inputControl.reset()
            Task {
                await loadReadiness()
                await inputControl.load()
            }
        }
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
                        ? connectionDescription
                        : "Set up this Mac, then connect iPhone with the Tailscale QR."
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
                    Text("The Setup card keeps URL, port and API token out of the normal flow.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var connectionDescription: String {
        guard let pairing = dashboardStore.pairing else {
            return "Not connected"
        }
        switch TailscalePairingPresentation.transport(for: pairing) {
        case .tailscaleDNS, .tailscaleIP:
            return "Connected through Tailscale. iPhone and Watch can reach this Mac outside the local network."
        case .sameDevice:
            return "This Mac is connected locally. Use Connect iPhone to prepare the Tailscale QR."
        case .remoteHTTPS:
            return "Connected through HTTPS. iPhone and Watch follow the same paired instance."
        case .disconnected:
            return "Not connected"
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

    private var inputSourcesPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("HealthMes data sources", systemImage: "square.stack.3d.up.fill")
                        .font(.headline)
                    Text("Server-owned settings shared by this Mac and every paired iPhone.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await inputControl.load() }
                } label: {
                    if inputControl.isLoading {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .buttonStyle(.bordered)
                .disabled(inputControl.isLoading)
            }

            if inputControl.isLoading, inputControl.sources.isEmpty {
                ProgressView("Loading server data sources…")
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else if inputControl.sources.isEmpty {
                Text("No server data sources are available.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 90)
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 330), spacing: 14)],
                    alignment: .leading,
                    spacing: 14
                ) {
                    ForEach(inputControl.sources) { source in
                        macInputSourceCard(source)
                    }
                }
            }

            if let error = inputControl.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }

            Text(
                "Open Wearables credentials stay on the HealthMes server. These apps receive only configured/not configured state, never the API key."
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
        .overlay {
            RoundedRectangle(cornerRadius: 20)
                .stroke(MacHealthMesStyle.line)
        }
    }

    private func macInputSourceCard(
        _ source: InputSourceDescriptor
    ) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 12) {
                MacMetadataRow(
                    label: "Server status",
                    value: InputControlPlanePresentation.sourceSummary(source)
                )
                MacMetadataRow(
                    label: "Platforms",
                    value: source.platforms.map(platformTitle).joined(separator: ", ")
                )
                MacMetadataRow(
                    label: "Capabilities",
                    value: source.capabilities
                        .map(InputControlPlanePresentation.humanize)
                        .joined(separator: ", ")
                )

                if source.isWearable {
                    MacMetadataRow(
                        label: "Provider/device inventory",
                        value: InputControlPlanePresentation.instanceSummary(source)
                    )
                    if source.sourceID == "wearable.open-wearables" {
                        MacMetadataRow(
                            label: "Server credential",
                            value: source.connectionState == .notConfigured
                                ? "Not configured on server"
                                : "Configured on server"
                        )
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
                    .toggleStyle(.switch)
                    .disabled(inputControl.isBusy(source.sourceID))
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
                        .disabled(inputControl.isBusy(source.sourceID))
                    }
                }

                if !source.instances.isEmpty {
                    Divider()
                    ForEach(source.instances) { instance in
                        macInstanceControl(instance, source: source)
                    }
                } else if source.isWearable {
                    Text(
                        "Aggregate status only. Main does not yet expose individual provider/device inventory or CRUD."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if !source.limitations.isEmpty {
                    Divider()
                    ForEach(source.limitations, id: \.self) { limitation in
                        Label(
                            InputControlPlanePresentation.limitation(limitation),
                            systemImage: "info.circle"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                }

                if let message = inputControl.sourceMessages[source.sourceID] {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                if inputControl.isBusy(source.sourceID) {
                    ProgressView("Saving to HealthMes…")
                        .controlSize(.small)
                }
            }
            .padding(.top, 10)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: sourceIcon(source))
                    .foregroundStyle(MacHealthMesStyle.brand)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: source.displayName)
                        .font(.headline)
                        .foregroundStyle(MacHealthMesStyle.graphite)
                    Text(verbatim: InputControlPlanePresentation.sourceSummary(source))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.5), in: RoundedRectangle(cornerRadius: 16))
        .overlay {
            RoundedRectangle(cornerRadius: 16)
                .stroke(MacHealthMesStyle.line)
        }
    }

    private func macInstanceControl(
        _ instance: InputInstance,
        source: InputSourceDescriptor
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
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
            .toggleStyle(.switch)
            .disabled(
                !source.supports(setting: "enabled", scope: "instance")
                    || inputControl.isBusy(source.sourceID)
            )
            Text(verbatim: InputControlPlanePresentation.instanceStatus(instance))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func decisionAccessBinding(
        _ source: InputSourceDescriptor
    ) -> Binding<Bool> {
        Binding(
            get: { source.decisionAccessEnabled },
            set: { enabled in
                Task {
                    await inputControl.setDecisionAccess(
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
                    await inputControl.setRetention(
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
                    await inputControl.setInstanceEnabled(
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
