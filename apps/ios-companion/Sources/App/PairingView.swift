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

    var body: some View {
        Form {
            Section {
                TextField(text: $baseURL, prompt: Text(verbatim: "http://192.168.1.20:8100")) {
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
                Text("Your HealthMes instance")
            } footer: {
                Text(
                    "The URL of your own healthmes service (HEALTHMES_API_TOKEN from its .env). This is the only server this app ever contacts."
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
                    unpair()
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
        do {
            let pairing = try PairingStore.shared.save(baseURLString: baseURL, token: token)
            PhoneWatchSync.shared.pushPairing(
                baseURL: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
            WidgetCenter.shared.reloadAllTimelines()
            status = String(
                localized: "Paired with \(pairing.baseURL.absoluteString). Widgets will refresh."
            )
            NotificationCenter.default.post(name: .healthmesPairingChanged, object: nil)
            Task {
                // Ask for notification permission now that alerts can exist,
                // and mark the current history as seen so enabling
                // notifications never replays old alerts as new ones.
                _ = await NotificationManager.shared.requestAuthorization()
                if let page = try? await HealthMesAPI().listAlerts(hours: 24) {
                    SeenAlertsStore.shared.primeWithoutNotifying(page.data)
                }
                BackgroundRefreshManager.shared.schedule()
            }
        } catch {
            status = error.localizedDescription
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
        PairingStore.shared.clear()
        GlanceSnapshotCache.shared.clear()
        SeenAlertsStore.shared.clear()
        PhoneWatchSync.shared.pushUnpair()
        WidgetCenter.shared.reloadAllTimelines()
        token = ""
        baseURL = ""
        status = String(localized: "Not paired")
        NotificationCenter.default.post(name: .healthmesPairingChanged, object: nil)
    }
}
