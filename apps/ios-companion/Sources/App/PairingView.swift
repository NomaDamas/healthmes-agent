import SwiftUI
import WidgetKit

/// The whole iPhone UI: one pairing screen (mirrors apps/android-usage).
///
/// Local-first: the base URL entered here is the only network destination of
/// the app, its widgets and the synced watch app. The token lands in the
/// Keychain (App Group access group); the URL in App Group defaults.
struct PairingView: View {
    @State private var baseURL: String = ""
    @State private var token: String = ""
    @State private var status: String = "Not paired"
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.1.20:8100", text: $baseURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API token (empty only for loopback)", text: $token)
                } header: {
                    Text("Your HealthMes instance")
                } footer: {
                    Text(
                        "The URL of your own healthmes service "
                            + "(HEALTHMES_API_TOKEN from its .env). "
                            + "This is the only server this app ever contacts."
                    )
                }

                Section {
                    Button("Save pairing") { save() }
                        .disabled(busy)
                    Button("Test connection") {
                        Task { await test() }
                    }
                    .disabled(busy)
                    Button("Unpair", role: .destructive) { unpair() }
                        .disabled(busy)
                }

                Section("Status") {
                    Text(status)
                        .font(.footnote)
                }

                Section("Widgets & watch") {
                    Text(
                        "After saving, add the HealthMes widget from the home or "
                            + "lock screen gallery. The pairing syncs to the watch "
                            + "app automatically when a watch is paired; watch "
                            + "complications read it from there."
                    )
                    .font(.footnote)
                }
            }
            .navigationTitle("HealthMes")
        }
        .onAppear(perform: loadExisting)
    }

    private func loadExisting() {
        guard let pairing = PairingStore.shared.load() else { return }
        baseURL = pairing.baseURL.absoluteString
        token = pairing.token ?? ""
        status = "Paired with \(pairing.baseURL.absoluteString)"
    }

    private func save() {
        do {
            let pairing = try PairingStore.shared.save(baseURLString: baseURL, token: token)
            PhoneWatchSync.shared.pushPairing(
                baseURL: pairing.baseURL.absoluteString,
                token: pairing.token ?? ""
            )
            WidgetCenter.shared.reloadAllTimelines()
            status = "Paired with \(pairing.baseURL.absoluteString). Widgets will refresh."
        } catch {
            status = error.localizedDescription
        }
    }

    private func test() async {
        guard let pairing = PairingStore.shared.load() else {
            status = "Save the pairing first."
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
            status = "Connected, but the token was rejected (401). Check HEALTHMES_API_TOKEN."
        } catch GlanceClientError.httpStatus(let code) {
            status = "Server answered HTTP \(code) — is this a healthmes instance?"
        } catch {
            status = "Could not reach the instance: \(error.localizedDescription)"
        }
    }

    private func unpair() {
        PairingStore.shared.clear()
        GlanceSnapshotCache.shared.clear()
        PhoneWatchSync.shared.pushUnpair()
        WidgetCenter.shared.reloadAllTimelines()
        token = ""
        status = "Not paired"
    }
}
