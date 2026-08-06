import SwiftUI

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
    @Published var section: MacAppSection = .today
    @Published private(set) var speakRequest = 0

    func requestSpeak() {
        section = .speak
        Task { @MainActor in
            await Task.yield()
            speakRequest += 1
        }
    }
}
