import SwiftUI

struct MacSpeakView: View {
    @ObservedObject var dashboardStore: MacDashboardStore
    let onNavigate: (MacAppSection) -> Void
    let onRefresh: () -> Void

    @EnvironmentObject private var router: MacAppRouter
    @StateObject private var speech = MacSpeechController()
    @State private var lastHandledRequest = 0

    var body: some View {
        VStack(spacing: 28) {
            MacPageHeader(
                eyebrow: "Speak",
                title: "Say what should change.",
                subtitle: "HealthMes listens locally when macOS supports it. Nothing mutates until you confirm."
            )
            .frame(maxWidth: 720, alignment: .leading)

            VStack(spacing: 22) {
                Button {
                    Task { await speech.toggle() }
                } label: {
                    ZStack {
                        Circle()
                            .fill(
                                speech.isListening
                                    ? MacHealthMesStyle.amber
                                    : MacHealthMesStyle.graphite
                            )
                            .frame(width: 116, height: 116)
                            .shadow(color: .black.opacity(0.12), radius: 24, y: 12)
                        Image(systemName: speech.isListening ? "stop.fill" : "waveform")
                            .font(.system(size: 34, weight: .semibold))
                            .foregroundStyle(.white)
                            .symbolEffect(.variableColor.iterative, isActive: speech.isListening)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel(Text(speech.isListening ? "Stop listening" : "Start speaking"))
                .keyboardShortcut(" ", modifiers: [.command, .shift])

                Text(verbatim: phaseTitle)
                    .font(.title3.weight(.semibold))
                Text("Keyboard: ⇧⌘Space")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if !speech.transcript.isEmpty {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("I heard")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .textCase(.uppercase)
                        Text(verbatim: speech.transcript)
                            .font(.system(size: 23, weight: .medium, design: .rounded))
                            .frame(maxWidth: .infinity, alignment: .leading)

                        if let intent = MacVoiceIntentParser.parse(speech.transcript) {
                            intentAction(intent)
                        }

                        Button("Try again") {
                            dashboardStore.clearPlanSaveMessage()
                            speech.reset()
                        }
                        .buttonStyle(.link)
                    }
                    .padding(22)
                    .frame(maxWidth: 680)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
                    .overlay {
                        RoundedRectangle(cornerRadius: 20)
                            .stroke(MacHealthMesStyle.line)
                    }
                } else {
                    examplePrompts
                }

                if let message = dashboardStore.planSaveMessage {
                    Label {
                        Text(verbatim: message)
                    } icon: {
                        Image(
                            systemName: dashboardStore.planSaveSucceeded
                                ? "checkmark.circle.fill"
                                : "exclamationmark.triangle.fill"
                        )
                    }
                    .font(.callout)
                    .foregroundStyle(
                        dashboardStore.planSaveSucceeded ? MacHealthMesStyle.moss : .red
                    )
                }
            }
            .frame(maxWidth: .infinity)

            Spacer(minLength: 0)
        }
        .padding(32)
        .onReceive(router.$speakRequest) { request in
            guard request > lastHandledRequest else { return }
            lastHandledRequest = request
            Task { await speech.toggle() }
        }
    }

    private var phaseTitle: String {
        switch speech.phase {
        case .idle: return String(localized: "Press and speak")
        case .requestingPermission: return String(localized: "Checking microphone access…")
        case .listening: return String(localized: "Listening…")
        case .ready: return String(localized: "Review before acting")
        case .denied: return String(localized: "Microphone or speech access is off")
        case .failed(let message): return message
        }
    }

    private var examplePrompts: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Try")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            Text("“Show my plan”")
            Text("“What decisions are waiting?”")
            Text("“Task: prepare the live QA checklist”")
            Text("“Weekly goal: protect three focus blocks”")
        }
        .font(.callout)
        .foregroundStyle(.secondary)
        .padding(18)
        .frame(maxWidth: 420, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    private func intentAction(_ intent: MacVoiceIntent) -> some View {
        switch intent {
        case .showToday:
            confirmationButton("Open Today", systemImage: "sun.max") {
                onNavigate(.today)
            }
        case .showPlan:
            confirmationButton("Open Plan", systemImage: "calendar") {
                onNavigate(.plan)
            }
        case .showDecisions:
            confirmationButton("Open Decisions", systemImage: "checkmark.bubble") {
                onNavigate(.decisions)
            }
        case .showSettings:
            confirmationButton("Open Settings", systemImage: "gearshape") {
                onNavigate(.settings)
            }
        case .refresh:
            confirmationButton("Refresh HealthMes", systemImage: "arrow.clockwise") {
                onRefresh()
            }
        case .taskDraft(let title):
            saveButton("Add task to Plan", systemImage: "checklist") {
                await dashboardStore.createTask(title: title)
            }
        case .goalDraft(let title):
            saveButton("Add weekly goal", systemImage: "scope") {
                await dashboardStore.createGoal(title: title)
            }
        }
    }

    private func saveButton(
        _ title: LocalizedStringKey,
        systemImage: String,
        save: @escaping () async -> Bool
    ) -> some View {
        Button {
            Task {
                if await save() {
                    speech.reset()
                    onNavigate(.plan)
                }
            }
        } label: {
            if dashboardStore.isSavingPlanItem {
                ProgressView()
            } else {
                Label(title, systemImage: systemImage)
            }
        }
        .buttonStyle(.borderedProminent)
        .tint(MacHealthMesStyle.moss)
        .controlSize(.large)
        .disabled(dashboardStore.isSavingPlanItem)
    }

    private func confirmationButton(
        _ title: LocalizedStringKey,
        systemImage: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
        }
        .buttonStyle(.borderedProminent)
        .tint(MacHealthMesStyle.moss)
        .controlSize(.large)
    }
}
