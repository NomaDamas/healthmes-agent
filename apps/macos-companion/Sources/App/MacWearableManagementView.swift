import SwiftUI

struct MacWearableManagementView: View {
    @ObservedObject var model: SettingsHubModel
    @Environment(\.openURL) private var openURL
    @State private var historicalDays = 90

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Wearables Settings Hub", systemImage: "watch.analog")
                        .font(.headline)
                    Text("Connect every supported provider and see the data sources HealthMes can use.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task { await model.load() }
                } label: {
                    if model.isLoading {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .buttonStyle(.bordered)
                .disabled(model.isLoading)
            }

            if model.isLoading, model.wearables == nil {
                ProgressView("Loading wearable connections…")
                    .frame(maxWidth: .infinity, minHeight: 100)
            } else if let snapshot = model.wearables {
                Label(
                    snapshot.degradedComponents.isEmpty
                        && snapshot.apiConfigured
                        && snapshot.userConfigured
                        ? "HealthMes wearable service ready"
                        : snapshot.degradedComponents.isEmpty
                        ? "Configure Open Wearables on the HealthMes server first"
                        : "Some wearable status is unavailable",
                    systemImage: snapshot.apiConfigured
                        && snapshot.userConfigured
                        && snapshot.degradedComponents.isEmpty
                        ? "checkmark.shield.fill"
                        : "exclamationmark.triangle.fill"
                )
                .foregroundStyle(
                    snapshot.apiConfigured
                        && snapshot.userConfigured
                        && snapshot.degradedComponents.isEmpty
                        ? MacHealthMesStyle.brand
                        : .orange
                )

                if !snapshot.degradedComponents.isEmpty {
                    Label(
                        "Provider connections are available, but some optional status details could not be loaded.",
                        systemImage: "info.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }

                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 300), spacing: 14)],
                    alignment: .leading,
                    spacing: 14
                ) {
                    ForEach(snapshot.providers) { provider in
                        providerCard(provider, snapshot: snapshot)
                    }
                }

                if !snapshot.recentSync.isEmpty {
                    DisclosureGroup("Recent sync activity") {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(snapshot.recentSync.prefix(8)) { run in
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: syncSymbol(run.status))
                                        .foregroundStyle(statusColor(run.status))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(verbatim: "\(run.provider) · \(run.stage)")
                                            .font(.caption.weight(.semibold))
                                        Text(verbatim: run.message ?? WearableManagementPresentation.status(run.status))
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                        .padding(.top, 8)
                    }
                }
            } else if model.wearablesState == .degraded {
                Label(
                    model.wearablesError
                        ?? "Wearable status is temporarily unavailable.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .foregroundStyle(.orange)
                .frame(maxWidth: .infinity, minHeight: 90)
            } else {
                Text("Wearable status is unavailable until this Mac is paired.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 90)
            }

            if let message = model.noticeMessage {
                Label(message, systemImage: "checkmark.circle")
                    .font(.callout)
                    .foregroundStyle(MacHealthMesStyle.brand)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            if let url = model.authorizationURL {
                Button {
                    openURL(url)
                    model.clearAuthorization()
                } label: {
                    Label("Continue provider authorization", systemImage: "safari")
                }
                .buttonStyle(.borderedProminent)
            }

            Text(
                "Open Wearables API keys, OAuth tokens, and upstream user identifiers stay on the HealthMes server. Mac and iPhone read the same server state."
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

    private func providerCard(
        _ provider: WearableProviderDescriptor,
        snapshot: WearablesManagementSnapshot
    ) -> some View {
        let connection = model.connection(for: provider.provider)
        let devices = model.devices(for: provider.provider)
        let policy = model.actionPolicy(for: provider)
        let status = policy.connectionStatus
        let historicalDaysForProvider = model.safeHistoricalDays(
            for: provider.provider,
            requested: historicalDays
        )

        return VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: provider.name)
                        .font(.headline)
                    Text(verbatim: provider.provider)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text(verbatim: WearableManagementPresentation.status(status))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(statusColor(status))
            }

            if let lastSyncedAt = connection?.lastSyncedAt {
                Text("Last sync \(lastSyncedAt, style: .relative)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if devices.isEmpty {
                Text(
                    provider.hasCloudAPI
                        ? "No device or data source reported yet."
                        : "Managed by a paired native source."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                ForEach(devices) { device in
                    Label(
                        device.title,
                        systemImage: WearableManagementPresentation.deviceSymbol(
                            deviceType: device.deviceType,
                            deviceModel: device.deviceModel,
                            source: device.source
                        )
                    )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 8) {
                if policy.canAuthorize {
                    Button {
                        Task { await model.authorize(provider: provider.provider) }
                    } label: {
                        Label(
                            status == "expired" ? "Reconnect" : "Connect",
                            systemImage: "link.badge.plus"
                        )
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy(provider: provider.provider))
                }
                if policy.canSync {
                    Button {
                        Task { await model.sync(provider: provider.provider) }
                    } label: {
                        Label("Sync", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .buttonStyle(.bordered)
                    .disabled(model.isBusy(provider: provider.provider))

                    Button {
                        Task {
                            await model.syncHistorical(
                                provider: provider.provider,
                                days: historicalDaysForProvider
                            )
                        }
                    } label: {
                        Text("\(historicalDaysForProvider)d")
                    }
                    .buttonStyle(.bordered)
                    .help(
                        "Request a \(historicalDaysForProvider)-day historical sync"
                    )
                    .disabled(model.isBusy(provider: provider.provider))

                } else if policy.canHistoricalSync {
                    Button {
                        Task {
                            await model.syncHistorical(
                                provider: provider.provider,
                                days: historicalDaysForProvider
                            )
                        }
                    } label: {
                        Text("\(historicalDaysForProvider)d backfill")
                    }
                    .buttonStyle(.bordered)
                    .help(
                        "Request a \(historicalDaysForProvider)-day historical sync"
                    )
                    .disabled(model.isBusy(provider: provider.provider))
                }

                if policy.canDisconnect {
                    Button(role: .destructive) {
                        Task { await model.disconnect(provider: provider.provider) }
                    } label: {
                        Image(systemName: "link.badge.minus")
                    }
                    .buttonStyle(.bordered)
                    .help("Disconnect \(provider.name)")
                    .disabled(model.isBusy(provider: provider.provider))
                } else if status == "expired" {
                    Text("Authorization expired. Reconnect to resume sync.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                } else if policy.isActive && provider.clientSDK {
                    Text("Connected through the native source.")
                        .font(.caption)
                        .foregroundStyle(MacHealthMesStyle.brand)
                } else if policy.isActive && !devices.isEmpty {
                    Text("Active data source reported by HealthMes.")
                        .font(.caption)
                        .foregroundStyle(MacHealthMesStyle.brand)
                } else if !provider.managementSupported {
                    Text("Visible from Open Wearables, but HealthMes management is not enabled for this provider yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.hasCloudAPI {
                    Text("Use the native source section.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.catalogAvailable {
                    Text("Visible from existing data; setup is not in the current catalog.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.isEnabled {
                    Text("Disabled on server.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !policy.isActive {
                    Text("Connect this provider to enable sync.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if provider.webhookCallback && !provider.restPull {
                    Text("Live data arrives by webhook; historical backfill is available.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if model.isBusy(provider: provider.provider) {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
        .padding(14)
        .background(Color.white.opacity(0.5), in: RoundedRectangle(cornerRadius: 16))
        .overlay {
            RoundedRectangle(cornerRadius: 16)
                .stroke(MacHealthMesStyle.line)
        }
    }

    private func statusColor(_ raw: String) -> Color {
        switch raw.lowercased() {
        case "active", "connected", "data_available", "ready", "success", "succeeded":
            return MacHealthMesStyle.brand
        case "error", "failed", "failure", "expired", "revoked":
            return .orange
        default:
            return .secondary
        }
    }

    private func syncSymbol(_ raw: String) -> String {
        switch raw.lowercased() {
        case "success", "succeeded", "completed":
            return "checkmark.circle.fill"
        case "error", "failed", "failure":
            return "exclamationmark.triangle.fill"
        default:
            return "arrow.triangle.2.circlepath"
        }
    }
}
