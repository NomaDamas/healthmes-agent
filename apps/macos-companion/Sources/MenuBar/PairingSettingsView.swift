import SwiftUI

/// Settings window: pairing (base URL + bearer token → shared PairingStore,
/// token in the login Keychain) and the optional notification toggle with
/// the delivery-honesty note. "Save & test" proves the pairing with a real
/// glance fetch before reporting success.
struct PairingSettingsView: View {
    @ObservedObject var store: GlanceStore
    @ObservedObject var notifications: MacNotificationManager

    @State private var baseURLText = ""
    @State private var tokenText = ""
    @State private var testResultKey: String?
    @State private var testSucceeded = false
    @State private var isTesting = false
    @State private var notificationsEnabled = MacNotificationManager.shared.isEnabled

    var body: some View {
        Form {
            Section {
                TextField(text: $baseURLText, prompt: Text(verbatim: "http://192.168.1.20:8100")) {
                    Text("settings.baseURL")
                }
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel(Text("settings.baseURL"))
                SecureField(text: $tokenText, prompt: Text("settings.token.prompt")) {
                    Text("settings.token")
                }
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel(Text("settings.token"))

                HStack(spacing: 8) {
                    Button {
                        Task { await saveAndTest() }
                    } label: {
                        if isTesting {
                            ProgressView().controlSize(.small)
                        } else {
                            Text("settings.save")
                        }
                    }
                    .disabled(baseURLText.isEmpty || isTesting)

                    if store.isPaired {
                        Button(role: .destructive) {
                            store.unpair()
                            testResultKey = nil
                        } label: {
                            Text("settings.unpair")
                        }
                    }
                }

                if let testResultKey {
                    Label {
                        Text(LocalizedStringKey(testResultKey))
                    } icon: {
                        Image(
                            systemName: testSucceeded
                                ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                        )
                    }
                    .font(.callout)
                    .foregroundStyle(testSucceeded ? .green : .orange)
                }

                Text("settings.localFirst")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("settings.pairing.title")
            }

            Section {
                Toggle(isOn: $notificationsEnabled) {
                    Text("settings.notifications.toggle")
                }
                .onChange(of: notificationsEnabled) { _, enabled in
                    Task {
                        await notifications.setEnabled(enabled, currentAlerts: store.alerts)
                    }
                }
                if notifications.authorizationDenied {
                    Text("settings.notifications.denied")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                // Delivery honesty (PLAN §8.5 / local-first): polling-based,
                // Telegram stays the guaranteed channel.
                Text("settings.notifications.honesty")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("settings.notifications.title")
            }
        }
        .formStyle(.grouped)
        .frame(width: 440)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear {
            if let pairing = PairingStore.shared.load() {
                baseURLText = pairing.baseURL.absoluteString
                tokenText = pairing.token ?? ""
            }
        }
    }

    private func saveAndTest() async {
        isTesting = true
        defer { isTesting = false }
        do {
            try await store.pair(baseURLString: baseURLText, token: tokenText)
            testResultKey = "settings.test.ok"
            testSucceeded = true
        } catch let error as PairingTestError {
            testResultKey = error.localizationKey
            testSucceeded = false
        } catch is PairingError {
            testResultKey = "settings.test.invalidURL"
            testSucceeded = false
        } catch {
            testResultKey = "error.unreachable"
            testSucceeded = false
        }
    }
}
