import SwiftUI

/// HealthMes menu bar app (issue #11): the briefing lives in the status bar
/// where deep-work hours happen. Local-first — the paired base URL is the
/// only network destination in the whole target.
@main
@MainActor
struct HealthMesMacApp: App {
    @StateObject private var store: GlanceStore
    @StateObject private var notifications: MacNotificationManager

    init() {
        let store = GlanceStore()
        let notifications = MacNotificationManager.shared
        notifications.bootstrap()
        store.onAlertsRefreshed = { alerts, proposals in
            notifications.process(alerts: alerts, pendingProposals: proposals)
        }
        store.start()
        _store = StateObject(wrappedValue: store)
        _notifications = StateObject(wrappedValue: notifications)
    }

    var body: some Scene {
        MenuBarExtra {
            BriefingPopoverView(store: store)
        } label: {
            MenuBarLabel(store: store)
        }
        .menuBarExtraStyle(.window)

        Settings {
            PairingSettingsView(store: store, notifications: notifications)
        }
    }
}

/// Status-item content: SF-symbol + the score text (see StatusItemText for
/// the honest --/stale/alert-dot rules; final vocabulary is the domain
/// expert's — docs/design/WATCH-NOTIFICATIONS.ko.md Q3).
struct MenuBarLabel: View {
    @ObservedObject var store: GlanceStore

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: "bolt.heart.fill")
            Text(verbatim: StatusItemText.title(
                payload: store.payload, stale: store.isStale, isPaired: store.isPaired
            ))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: Text {
        guard store.isPaired, let payload = store.payload else {
            return Text("menubar.a11y.notPaired")
        }
        let score = GlanceFormat.scoreText(payload.energy.score)
        let confidence = confidenceText(payload.energy.confidence)
        if payload.alerts.unresolvedCount > 0 {
            return Text("menubar.a11y.energyAlerts \(score) \(confidence) \(payload.alerts.unresolvedCount)")
        }
        return Text("menubar.a11y.energy \(score) \(confidence)")
    }
}

/// Shared confidence wording (VoiceOver + badges).
func confidenceText(_ confidence: GlanceConfidence) -> String {
    switch confidence {
    case .high: return String(localized: "confidence.high")
    case .medium: return String(localized: "confidence.medium")
    case .low: return String(localized: "confidence.low")
    }
}
