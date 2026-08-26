import SwiftUI

struct WearableManagementView: View {
    @ObservedObject var model: SettingsHubModel
    @Environment(\.openURL) private var openURL
    @State private var historicalDays = 90

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.isLoading, model.wearables == nil {
                ProgressView("Loading wearable connections…")
            } else if let snapshot = model.wearables {
                HStack {
                    Label(
                        snapshot.degradedComponents.isEmpty
                            && snapshot.apiConfigured
                            && snapshot.userConfigured
                            ? "HealthMes wearable service ready"
                            : snapshot.degradedComponents.isEmpty
                            ? "Wearable server setup required"
                            : "Some wearable status is unavailable",
                        systemImage: snapshot.apiConfigured && snapshot.userConfigured
                            && snapshot.degradedComponents.isEmpty
                            ? "checkmark.shield.fill"
                            : "exclamationmark.triangle.fill"
                    )
                    .foregroundStyle(
                        snapshot.apiConfigured
                            && snapshot.userConfigured
                            && snapshot.degradedComponents.isEmpty
                            ? HealthMesVisualStyle.brand
                            : .orange
                    )
                    Spacer()
                    Text(verbatim: "\(snapshot.providers.count) providers")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if !snapshot.degradedComponents.isEmpty {
                    Label(
                        "Provider connections are available, but some optional status details could not be loaded.",
                        systemImage: "info.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }

                ForEach(snapshot.providers) { provider in
                    providerRow(provider)
                }

                if !snapshot.recentSync.isEmpty {
                    DisclosureGroup("Recent sync activity") {
                        ForEach(snapshot.recentSync.prefix(5)) { run in
                            syncRow(run)
                        }
                    }
                    .font(.subheadline)
                }
            } else if model.wearablesState == .degraded {
                Label(
                    model.wearablesError
                        ?? "Wearable status is temporarily unavailable.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .foregroundStyle(.orange)
            } else {
                Text("Wearable status is unavailable until this iPhone is paired.")
                    .foregroundStyle(.secondary)
            }

            if let message = model.noticeMessage {
                Label(message, systemImage: "checkmark.circle")
                    .font(.footnote)
                    .foregroundStyle(HealthMesVisualStyle.brand)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.orange)
            }
            if let url = model.authorizationURL {
                Button {
                    openURL(url)
                    model.clearAuthorization()
                } label: {
                    Label("Continue provider authorization", systemImage: "safari")
                }
            }

            Button {
                Task { await model.load() }
            } label: {
                Label(
                    model.isLoading ? "Refreshing wearable status…" : "Refresh wearable status",
                    systemImage: "arrow.clockwise"
                )
            }
            .disabled(model.isLoading)
        }
    }

    private func providerRow(
        _ provider: WearableProviderDescriptor
    ) -> some View {
        let connection = model.connection(for: provider.provider)
        let devices = model.devices(for: provider.provider)
        let policy = model.actionPolicy(for: provider)
        let status = policy.connectionStatus
        let historicalDaysForProvider = model.safeHistoricalDays(
            for: provider.provider,
            requested: historicalDays
        )

        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: provider.name)
                        .font(.body.weight(.semibold))
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
                LabeledContent("Last sync") {
                    Text(lastSyncedAt, style: .relative)
                        .foregroundStyle(.secondary)
                }
                .font(.caption)
            }

            if devices.isEmpty {
                Text(
                    provider.hasCloudAPI
                        ? "No device or data source has been reported yet."
                        : "This provider is managed by a paired native source."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                ForEach(devices) { device in
                    Label {
                        Text(verbatim: device.title)
                    } icon: {
                        Image(
                            systemName: WearableManagementPresentation.deviceSymbol(
                                deviceType: device.deviceType,
                                deviceModel: device.deviceModel,
                                source: device.source
                            )
                        )
                    }
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
                    .disabled(model.isBusy(provider: provider.provider))
                }

                if policy.canSync {
                    Button {
                        Task { await model.sync(provider: provider.provider) }
                    } label: {
                        Label("Sync", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(model.isBusy(provider: provider.provider))

                    Button {
                        Task {
                            await model.syncHistorical(
                                provider: provider.provider,
                                days: historicalDaysForProvider
                            )
                        }
                    } label: {
                        Label(
                            "\(historicalDaysForProvider)d backfill",
                            systemImage: "clock.arrow.circlepath"
                        )
                    }
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
                        Label(
                            "\(historicalDaysForProvider)d backfill",
                            systemImage: "clock.arrow.circlepath"
                        )
                    }
                    .disabled(model.isBusy(provider: provider.provider))
                }

                if policy.canDisconnect {
                    Button(role: .destructive) {
                        Task { await model.disconnect(provider: provider.provider) }
                    } label: {
                        Label("Disconnect", systemImage: "link.badge.minus")
                    }
                    .disabled(model.isBusy(provider: provider.provider))
                } else if status == "expired" {
                    Text("Authorization expired. Reconnect to resume sync.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                } else if policy.isActive && provider.clientSDK {
                    Text("Connected through the native source.")
                        .font(.caption)
                        .foregroundStyle(HealthMesVisualStyle.brand)
                } else if policy.isActive && !devices.isEmpty {
                    Text("Active data source reported by HealthMes.")
                        .font(.caption)
                        .foregroundStyle(HealthMesVisualStyle.brand)
                } else if !provider.managementSupported {
                    Text("Visible from Open Wearables, but HealthMes management is not enabled for this provider yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.hasCloudAPI {
                    Text("Use the native source section to connect this provider.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.catalogAvailable {
                    Text("Visible from existing data; provider setup is not in the current catalog.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !provider.isEnabled {
                    Text("Provider is disabled on the server.")
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
        .padding(.vertical, 6)
    }

    private func syncRow(_ run: WearableSyncStatusDescriptor) -> some View {
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

    private func statusColor(_ raw: String) -> Color {
        switch raw.lowercased() {
        case "active", "connected", "data_available", "ready", "success", "succeeded":
            return HealthMesVisualStyle.brand
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
