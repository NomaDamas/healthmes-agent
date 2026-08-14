import AppKit
import CoreImage
import CoreImage.CIFilterBuiltins
import SwiftUI

struct MacSetupView: View {
    @ObservedObject var coordinator: MacSetupCoordinator
    @ObservedObject var glanceStore: GlanceStore

    var body: some View {
        MacSurfaceCard("Setup", systemImage: "wand.and.stars") {
            VStack(alignment: .leading, spacing: 12) {
                Text(glanceStore.isPaired ? "This Mac is ready" : "One clean setup")
                    .font(.title2.weight(.semibold))
                Text(
                    glanceStore.isPaired
                        ? "HealthMes is ready on this Mac. Connect iPhone through Tailscale next."
                        : "Installs the local runtime, protects it with a token, and pairs this app."
                )
                .font(.callout)
                .foregroundStyle(.secondary)

                Button {
                    Task {
                        if glanceStore.isPaired {
                            await coordinator.verifyPairing()
                        } else {
                            await coordinator.install(glanceStore: glanceStore)
                        }
                    }
                } label: {
                    if coordinator.isRunning {
                        ProgressView()
                    } else {
                        Label(
                            glanceStore.isPaired ? "Verify setup" : "Set up this Mac",
                            systemImage: "arrow.down.circle.fill"
                        )
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .controlSize(.large)
                .disabled(coordinator.isRunning)

                if let failure = coordinator.failure {
                    Label(failure, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.orange)
                }

                if coordinator.requiresDeveloperTools {
                    Button {
                        Task {
                            await coordinator.requestDeveloperToolsInstallation()
                        }
                    } label: {
                        Label(
                            "Install Apple Developer Tools",
                            systemImage: "hammer.fill"
                        )
                    }
                    .buttonStyle(.bordered)
                    .disabled(coordinator.isRunning)
                    Text("Apple shows one system confirmation. After it finishes, select Set up this Mac again to continue.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if coordinator.requiresHomebrew {
                    Button {
                        if let url = URL(string: "https://brew.sh") {
                            NSWorkspace.shared.open(url)
                        }
                    } label: {
                        Label(
                            "Install Homebrew",
                            systemImage: "shippingbox.fill"
                        )
                    }
                    .buttonStyle(.bordered)
                    .disabled(coordinator.isRunning)
                    Text("The official Homebrew installer requires your visible approval. Install it, then select Set up this Mac again.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if !coordinator.events.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(coordinator.events.suffix(8)) { event in
                            VStack(alignment: .leading, spacing: 2) {
                                Label(
                                    event.message,
                                    systemImage: event.isFailure
                                        ? "exclamationmark.circle"
                                        : "checkmark.circle.fill"
                                )
                                .font(.caption)
                                .foregroundStyle(event.isFailure ? .orange : .secondary)
                                if let detail = event.detail, !detail.isEmpty {
                                    Text(verbatim: detail)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .padding(.leading, 20)
                                }
                            }
                        }
                    }
                }

                if let phoneURL = coordinator.phonePairingURL {
                    Divider()
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        if MacSetupSupport.isPairingGrantExpired(
                            expiresAt: coordinator.phonePairingExpiresAt,
                            now: context.date
                        ) {
                            VStack(alignment: .leading, spacing: 8) {
                                Label(
                                    "This pairing QR has expired.",
                                    systemImage: "clock.badge.exclamationmark"
                                )
                                .font(.headline)
                                Text("Generate a new one-time QR before scanning with your iPhone.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                pairingRefreshButton
                            }
                        } else {
                            HStack(alignment: .center, spacing: 16) {
                                Image(nsImage: QRCodeRenderer.image(for: phoneURL.absoluteString))
                                    .interpolation(.none)
                                    .resizable()
                                    .frame(width: 132, height: 132)
                                    .accessibilityLabel("iPhone pairing QR code")
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("4. Scan the QR")
                                        .font(.headline)
                                    Text("Scan with the iPhone Camera within five minutes. HealthMes opens, verifies the Tailnet path, and pairs automatically.")
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                    if let expiresAt = coordinator.phonePairingExpiresAt {
                                        Text(
                                            timerInterval: min(context.date, expiresAt)...expiresAt,
                                            countsDown: true
                                        )
                                        .font(.caption.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                    }
                                    Text("The QR contains a one-time code, not your API token.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    pairingRefreshButton
                                }
                            }
                        }
                    }
                } else if glanceStore.isPaired {
                    Divider()
                    tailscalePairingGuide
                }
            }
        }
    }

    private var tailscalePairingGuide: some View {
        VStack(alignment: .leading, spacing: 11) {
            Text("Connect iPhone")
                .font(.headline)
            ForEach(TailscalePairingPresentation.steps.prefix(3)) { step in
                HStack(alignment: .top, spacing: 9) {
                    Text(verbatim: "\(step.number)")
                        .font(.caption.bold().monospacedDigit())
                        .foregroundStyle(.white)
                        .frame(width: 22, height: 22)
                        .background(MacHealthMesStyle.moss, in: Circle())
                    VStack(alignment: .leading, spacing: 2) {
                        Text(verbatim: step.title)
                            .font(.subheadline.weight(.semibold))
                        Text(verbatim: step.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            HStack {
                Link(destination: TailscalePairingPresentation.downloadURL) {
                    Label("Install Tailscale", systemImage: "arrow.down.circle")
                }
                .buttonStyle(.bordered)
                Button {
                    Task { await coordinator.refreshPhonePairing() }
                } label: {
                    Label("3. Connect iPhone", systemImage: "qrcode")
                }
                .buttonStyle(.borderedProminent)
                .tint(MacHealthMesStyle.moss)
                .disabled(coordinator.isRunning)
            }
            Text("HealthMes selects the Tailnet address and generates a short-lived QR. Do not enter an IP, port, domain, or API token on iPhone.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var pairingRefreshButton: some View {
        Button("Generate a new QR") {
            Task { await coordinator.refreshPhonePairing() }
        }
        .buttonStyle(.link)
        .disabled(coordinator.isRunning)
    }
}

struct MacSetupAdvancedView: View {
    @ObservedObject var coordinator: MacSetupCoordinator

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Local runtime")
                .font(.headline)
            HStack {
                advancedButton("Repair", systemImage: "wrench", action: .repair)
                advancedButton("Update", systemImage: "arrow.triangle.2.circlepath", action: .update)
                advancedButton("Diagnostics", systemImage: "doc.text.magnifyingglass", action: .diagnostics)
                advancedButton("Uninstall", systemImage: "trash", action: .uninstall)
            }
            Text("Uninstall removes the runtime but keeps local health data.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func advancedButton(
        _ title: LocalizedStringKey,
        systemImage: String,
        action: MacSetupCoordinator.Action
    ) -> some View {
        Button {
            Task { await coordinator.runAdvanced(action) }
        } label: {
            Label(title, systemImage: systemImage)
        }
        .buttonStyle(.bordered)
        .disabled(coordinator.isRunning)
    }
}

private enum QRCodeRenderer {
    static func image(for value: String) -> NSImage {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(value.utf8)
        filter.correctionLevel = "M"
        let context = CIContext()
        guard
            let output = filter.outputImage?.transformed(
                by: CGAffineTransform(scaleX: 8, y: 8)
            ),
            let cgImage = context.createCGImage(output, from: output.extent)
        else {
            return NSImage(size: NSSize(width: 132, height: 132))
        }
        return NSImage(cgImage: cgImage, size: NSSize(width: 132, height: 132))
    }
}
