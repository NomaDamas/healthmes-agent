import SwiftUI

struct MacStorageAdvancedView: View {
    @StateObject private var model = StorageSettingsModel()
    @State private var confirmCleanup = false
    private let presets = ["1d", "7d", "14d", "30d", "90d", "forever"]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Paired HealthMes storage", systemImage: "externaldrive")
                .font(.headline)

            if let snapshot = model.snapshot {
                LabeledContent("Available") {
                    Text(StorageSettingsModel.bytes(snapshot.diskFreeBytes))
                }
                LabeledContent("Backup") {
                    Text(
                        snapshot.backup.encryptionConfigured
                            ? "\(snapshot.backup.snapshotCount) encrypted snapshots"
                            : "Not configured"
                    )
                }
                Text("Retention and cleanup apply to the paired HealthMes instance.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 170), spacing: 10)],
                    spacing: 10
                ) {
                    ForEach(snapshot.usage.keys.sorted(), id: \.self) { dataClass in
                        LabeledContent(
                            dataClass.replacingOccurrences(of: "_", with: " ").capitalized
                        ) {
                            Text(
                                StorageSettingsModel.bytes(
                                    snapshot.usage[dataClass]?["bytes"] ?? 0
                                )
                            )
                        }
                    }
                }

                ForEach(snapshot.policies) { policy in
                    Picker(
                        policy.dataClass.replacingOccurrences(of: "_", with: " ").capitalized,
                        selection: binding(for: policy)
                    ) {
                        ForEach(presets, id: \.self) { preset in
                            Text(preset == "forever" ? "Keep forever" : preset)
                                .tag(preset)
                        }
                    }
                    .disabled(model.isLoading)
                }

                HStack {
                    Button("Preview cleanup") {
                        Task { await model.previewCleanup() }
                    }
                    if let report = model.maintenance,
                        report.dryRun,
                        report.candidates > 0
                    {
                        Button("Review cleanup", role: .destructive) {
                            confirmCleanup = true
                        }
                    }
                    if let pairing = PairingStore.shared.load() {
                        Link(
                            destination: ViewerURL.make(
                                pairing: pairing,
                                pathComponents: ["storage"]
                            )
                        ) {
                            Label("Full details", systemImage: "arrow.up.right.square")
                        }
                    }
                }
                .disabled(model.isLoading)
            } else if model.isLoading {
                ProgressView()
            }

            if let report = model.maintenance {
                Text(
                    report.dryRun
                        ? "\(report.candidates) items are eligible for cleanup."
                        : "\(report.deleted) items removed · \(StorageSettingsModel.bytes(report.bytesReclaimed)) reclaimed."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                if !report.errors.isEmpty {
                    Label(
                        "\(report.errors.count) items could not be removed. Preview again before retrying.",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .task { await model.load() }
        .alert(
            "Remove \(model.maintenance?.candidates ?? 0) eligible items?",
            isPresented: $confirmCleanup
        ) {
            Button("Cancel", role: .cancel) {}
            Button("Run cleanup", role: .destructive) {
                Task { await model.confirmCleanup() }
            }
        } message: {
            Text("Only items already expired under the selected retention policy are removed.")
        }
    }

    private func binding(for policy: StorageRetentionPolicy) -> Binding<String> {
        Binding(
            get: { policy.preset },
            set: { preset in
                Task {
                    await model.update(dataClass: policy.dataClass, preset: preset)
                }
            }
        )
    }
}
