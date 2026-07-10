import SwiftUI

/// Minimal watch screen: pairing state + the current glance line, plus a
/// manual refresh. Deliberate placeholder — the real watch UX (and whether
/// this app should show anything beyond complications at all) is the domain
/// expert's call: docs/design/WATCH-NOTIFICATIONS.ko.md (docs/PLAN.md §8.5).
struct WatchHomeView: View {
    @State private var headline = "Loading..."
    @State private var detail = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                Text("HealthMes")
                    .font(.headline)
                Text(headline)
                    .font(.footnote)
                if !detail.isEmpty {
                    Text(detail)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Button("Refresh") {
                    Task { await refresh() }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await refresh() }
    }

    @MainActor
    private func refresh() async {
        guard let pairing = PairingStore.shared.load() else {
            headline = "Not paired"
            detail = "Save the pairing in the iPhone app; it syncs here automatically."
            return
        }
        do {
            let snapshot = try await GlanceClient().fetch(pairing: pairing)
            headline = GlanceFormat.energyLine(snapshot.payload)
            var lines = [GlanceFormat.alertsLine(snapshot.payload)]
            if let block = GlanceFormat.nextBlockLine(snapshot.payload) {
                lines.append("Next: \(block)")
            }
            detail = lines.joined(separator: "\n")
        } catch GlanceClientError.unauthorized {
            headline = "Token rejected"
            detail = "Re-save the pairing on the iPhone."
        } catch {
            if let cached = GlanceSnapshotCache.shared.decodedPayload() {
                headline = GlanceFormat.energyLine(cached) + " (cached)"
                detail = "Instance unreachable; showing the last snapshot."
            } else {
                headline = "No data"
                detail = "Could not reach \(pairing.baseURL.absoluteString)."
            }
        }
    }
}
