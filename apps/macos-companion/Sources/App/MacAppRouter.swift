import SwiftUI

/// Compatibility destinations used by menu-bar commands and older views.
/// The full-window product maps them onto one fixed wellness canvas instead
/// of rendering five independent pages.
enum MacAppSection: String, CaseIterable, Identifiable {
    case today
    case plan
    case decisions
    case speak
    case settings

    var id: String { rawValue }

    var title: LocalizedStringKey {
        switch self {
        case .today: return "Today"
        case .plan: return "Plan"
        case .decisions: return "Decisions"
        case .speak: return "Speak"
        case .settings: return "Settings"
        }
    }

    var systemImage: String {
        switch self {
        case .today: return "sun.max"
        case .plan: return "calendar"
        case .decisions: return "checkmark.bubble"
        case .speak: return "waveform"
        case .settings: return "gearshape"
        }
    }
}

@MainActor
final class MacAppRouter: ObservableObject {
    @Published var section: MacAppSection = .today {
        didSet {
            routeLegacySection(section)
        }
    }
    @Published var lens: WellnessLens = .now
    @Published private(set) var isSettingsPresented = false
    @Published private(set) var speakRequest = 0

    func requestSpeak() {
        section = .speak
        Task { @MainActor in
            await Task.yield()
            speakRequest += 1
        }
    }

    func selectLens(_ lens: WellnessLens) {
        isSettingsPresented = false
        self.lens = lens
    }

    func presentSettings() {
        isSettingsPresented = true
    }

    func dismissSettings() {
        isSettingsPresented = false
    }

    private func routeLegacySection(_ section: MacAppSection) {
        switch section {
        case .today:
            selectLens(.now)
        case .plan:
            selectLens(.coordinate)
        case .decisions:
            selectLens(.change)
        case .speak:
            isSettingsPresented = false
        case .settings:
            presentSettings()
        }
    }
}
