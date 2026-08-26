import SwiftUI

struct StorageAdvancedView: View {
    @StateObject private var model = StorageSettingsModel()
    @State private var confirmCleanup = false
    private let presets = ["1d", "7d", "14d", "30d", "90d", "forever"]

    var body: some View {
        Form {
            if let snapshot = model.snapshot {
                Section {
                    LabeledContent("Available") {
                        Text(StorageSettingsModel.bytes(snapshot.diskFreeBytes))
                    }
                    LabeledContent("Location") {
                        Text(verbatim: snapshot.dataDir)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    ForEach(snapshot.usage.keys.sorted(), id: \.self) { dataClass in
                        LabeledContent(policyTitle(dataClass)) {
                            Text(StorageSettingsModel.bytes(usageBytes(snapshot, for: dataClass)))
                        }
                    }
                } header: {
                    Text("Paired HealthMes storage")
                } footer: {
                    Text("These values and cleanup actions apply to the paired HealthMes instance, not this iPhone.")
                }

                Section {
                    ForEach(snapshot.policies) { policy in
                        Picker(policyTitle(policy.dataClass), selection: binding(for: policy)) {
                            ForEach(presets, id: \.self) { preset in
                                Text(presetTitle(preset)).tag(preset)
                            }
                        }
                        .disabled(model.isLoading)
                    }
                } header: {
                    Text("Retention")
                } footer: {
                    Text("HealthMes only cleans data covered by these retention policies. Calendar records are not deleted here.")
                }

                Section("Backup") {
                    LabeledContent("Provider", value: snapshot.backup.provider)
                    LabeledContent("Snapshots", value: String(snapshot.backup.snapshotCount))
                    LabeledContent(
                        "Encryption",
                        value: snapshot.backup.encryptionConfigured ? "Ready" : "Not configured"
                    )
                }
            } else if model.isLoading {
                ProgressView("Loading storage…")
            }

            Section("Maintenance") {
                Button("Preview cleanup") {
                    Task { await model.previewCleanup() }
                }
                .disabled(model.isLoading)

                if let report = model.maintenance {
                    Text(
                        report.dryRun
                            ? "\(report.candidates) items are eligible for cleanup."
                            : "\(report.deleted) items removed · \(StorageSettingsModel.bytes(report.bytesReclaimed)) reclaimed."
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    if !report.errors.isEmpty {
                        Label(
                            "\(report.errors.count) items could not be removed. Preview again before retrying.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.footnote)
                        .foregroundStyle(.orange)
                    }

                    if report.dryRun, report.candidates > 0 {
                        Button("Review and run cleanup", role: .destructive) {
                            confirmCleanup = true
                        }
                        .disabled(model.isLoading)
                    }
                }
            }

            if let error = model.errorMessage {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                }
            }

            if let pairing = PairingStore.shared.load() {
                Section {
                    Link(
                        destination: ViewerURL.make(
                            pairing: pairing,
                            pathComponents: ["storage"]
                        )
                    ) {
                        Label("Open full storage details", systemImage: "arrow.up.right.square")
                    }
                }
            }
        }
        .navigationTitle("Storage")
        .task { await model.load() }
        .refreshable { await model.load() }
        .confirmationDialog(
            "Remove \(model.maintenance?.candidates ?? 0) eligible items?",
            isPresented: $confirmCleanup,
            titleVisibility: .visible
        ) {
            Button("Run cleanup", role: .destructive) {
                Task { await model.confirmCleanup() }
            }
            Button("Cancel", role: .cancel) {}
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

    private func policyTitle(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func presetTitle(_ value: String) -> String {
        value == "forever" ? "Keep forever" : value
    }

    private func usageBytes(_ snapshot: StorageSettingsSnapshot, for dataClass: String) -> Int64 {
        snapshot.usage[dataClass]?["bytes"] ?? 0
    }
}
