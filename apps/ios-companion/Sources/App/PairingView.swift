import SwiftUI
import WidgetKit

/// Pairing screen (also embedded in Settings): base URL + bearer token of
/// the user's own healthmes instance.
///
/// Local-first: the base URL entered here is the only network destination of
/// the app, its widgets and the synced watch app. The token lands in the
/// Keychain (App Group access group); the URL in App Group defaults.
struct PairingView: View {
    @State private var baseURL: String = ""
    @State private var token: String = ""
    @State private var status: String = ""
    @State private var busy = false
    @State private var showUnpairConfirmation = false

    var body: some View {
        Form {
            Section {
                TextField(text: $baseURL, prompt: Text(verbatim: "https://healthmes.example.com")) {
                    Text("Base URL")
                }
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityLabel(Text("Instance base URL"))
                SecureField(text: $token, prompt: Text("API token (empty only for loopback)")) {
                    Text("API token")
                }
                .accessibilityLabel(Text("API token"))
            } header: {
                Text("Manual connection")
            } footer: {
                Text(
                    "Advanced fallback only. The normal setup uses Tailscale and a one-time QR, so no URL or API token is typed."
                )
            }

            Section {
                Button {
                    save()
                } label: {
                    Text("Save pairing")
                }
                .disabled(busy)
                Button {
                    Task { await test() }
                } label: {
                    Text("Test connection")
                }
                .disabled(busy)
                Button(role: .destructive) {
                    showUnpairConfirmation = true
                } label: {
                    Text("Unpair")
                }
                .disabled(busy)
            }

            Section {
                Text(verbatim: status.isEmpty ? statusPlaceholder : status)
                    .font(.footnote)
            } header: {
                Text("Status")
            }

            Section {
                Text(
                    "After saving, add the HealthMes widget from the home or lock screen gallery. The pairing syncs to the watch app automatically when a watch is paired."
                )
                .font(.footnote)
            } header: {
                Text("Widgets & watch")
            }
        }
        .onAppear(perform: loadExisting)
        .confirmationDialog(
            "Disconnect HealthMes?",
            isPresented: $showUnpairConfirmation,
            titleVisibility: .visible
        ) {
            Button("Unpair and delete queued uploads", role: .destructive) {
                unpair()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "Unsent Apple Health batches and sync cursors for this pairing are deleted from this iPhone. If the encrypted queue is unreadable, HealthMes may delete the entire local Apple Health upload queue, including queued batches for other pairings, so corrupted encrypted data cannot remain. Data already stored on the server is unchanged."
            )
        }
    }

    private var statusPlaceholder: String {
        String(localized: "Not paired")
    }

    private func loadExisting() {
        guard let pairing = PairingStore.shared.load() else { return }
        baseURL = pairing.baseURL.absoluteString
        token = pairing.token ?? ""
        status = String(localized: "Paired with \(pairing.baseURL.absoluteString)")
    }

    private func save() {
        busy = true
        Task { @MainActor in
            do {
                let candidate = try PairingStore.validatedPairing(
                    baseURLString: baseURL,
                    token: token
                )
                let pairing = try await HealthKitSyncManager.shared.replacePairing(
                    with: candidate
                )
                PhoneWatchSync.shared.pushPairing(
                    baseURL: pairing.baseURL.absoluteString,
                    token: pairing.token ?? ""
                )
                WidgetCenter.shared.reloadAllTimelines()
                status = String(
                    localized: "Paired with \(pairing.baseURL.absoluteString). Widgets will refresh."
                )
                NotificationCenter.default.post(
                    name: .healthmesPairingChanged,
                    object: nil
                )
                // Ask for notification permission now that alerts can exist,
                // and mark the current history as seen so enabling
                // notifications never replays old alerts as new ones.
                _ = await NotificationManager.shared.requestAuthorization()
                let page = try? await HealthMesAPI().listAlerts(
                    pairing: pairing,
                    hours: 24
                )
                SeenAlertsStore.shared.applyPairingScopedBaseline(
                    page?.data,
                    for: pairing
                )
                BackgroundRefreshManager.shared.schedule()
                await HealthKitSyncManager.shared.requestAuthorizationAndSync()
                busy = false
            } catch {
                await HealthKitSyncManager.shared.pairingDidChange()
                busy = false
                status = error.localizedDescription
            }
        }
    }

    private func test() async {
        guard let pairing = PairingStore.shared.load() else {
            status = String(localized: "Save the pairing first.")
            return
        }
        busy = true
        defer { busy = false }
        do {
            let snapshot = try await GlanceClient().fetch(pairing: pairing)
            var line = "Connected · \(GlanceFormat.energyLine(snapshot.payload))"
            line += " · \(GlanceFormat.alertsLine(snapshot.payload))"
            if snapshot.revalidated { line += " (304 revalidated)" }
            status = line
        } catch GlanceClientError.unauthorized {
            status = String(
                localized: "Connected, but the token was rejected (401). Check HEALTHMES_API_TOKEN."
            )
        } catch GlanceClientError.httpStatus(let code) {
            status = String(
                localized: "Server answered HTTP \(code) — is this a healthmes instance?"
            )
        } catch {
            status = String(
                localized: "Could not reach the instance: \(error.localizedDescription)"
            )
        }
    }

    private func unpair() {
        busy = true
        Task { @MainActor in
            do {
                try await HealthKitSyncManager.shared.unpair()
                finishUnpair()
            } catch {
                busy = false
                status = String(
                    localized:
                        "Could not safely unpair because the encrypted Apple Health queue could not be removed: \(error.localizedDescription)"
                )
            }
        }
    }

    private func finishUnpair() {
        GlanceSnapshotCache.shared.clear()
        SeenAlertsStore.shared.clear()
        PhoneWatchSync.shared.pushUnpair()
        WidgetCenter.shared.reloadAllTimelines()
        token = ""
        baseURL = ""
        status = String(localized: "Not paired")
        busy = false
        NotificationCenter.default.post(name: .healthmesPairingChanged, object: nil)
    }
}
