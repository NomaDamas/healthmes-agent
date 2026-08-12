import SwiftUI

struct MacWorkspaceThreadInspector: View {
    @ObservedObject var store: MacWorkspaceViewModel

    var body: some View {
        VStack(spacing: 0) {
            inspectorHeader

            if let thread = store.selectedThread {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 14) {
                            anchorSummary(thread)

                            if thread.messages.isEmpty {
                                emptyThread
                            } else {
                                ForEach(thread.messages) { message in
                                    messageBubble(message)
                                        .id(message.id)
                                }
                            }
                        }
                        .padding(18)
                    }
                    .onChange(of: thread.messages.count) { _, _ in
                        if let id = thread.messages.last?.id {
                            withAnimation {
                                proxy.scrollTo(id, anchor: .bottom)
                            }
                        }
                    }
                }

                composer(thread)
            } else {
                MacEmptyState(
                    systemImage: "text.bubble",
                    title: "No thread selected",
                    message: "Open the thread button on a card, event, insight or decision."
                )
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var inspectorHeader: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Thread")
                    .font(.headline)
                Text("Local to this Mac")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                store.closeThread()
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel(Text("Close thread"))
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 13)
        .background(.regularMaterial)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(MacHealthMesStyle.line)
                .frame(height: 1)
        }
    }

    private func anchorSummary(_ thread: WorkspaceThread) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(anchorLabel(thread.anchor.kind), systemImage: anchorSymbol(thread.anchor.kind))
                .font(.caption.weight(.bold))
                .textCase(.uppercase)
                .tracking(0.8)
                .foregroundStyle(MacHealthMesStyle.moss)
            Text(thread.anchor.title)
                .font(.title3.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            Text("Discussion notes are stored on this Mac only. They do not change HealthMes data.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MacHealthMesStyle.moss.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
    }

    private var emptyThread: some View {
        VStack(spacing: 8) {
            Image(systemName: "text.bubble")
                .font(.title2)
                .foregroundStyle(MacHealthMesStyle.moss)
            Text("Start a local thread")
                .font(.headline)
            Text("Use this space for context, corrections or follow-up notes.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
    }

    private func messageBubble(_ message: WorkspaceThreadMessage) -> some View {
        HStack(alignment: .top, spacing: 10) {
            ZStack {
                Circle()
                    .fill(authorColor(message.author).opacity(0.13))
                    .frame(width: 32, height: 32)
                Image(systemName: authorSymbol(message.author))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(authorColor(message.author))
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 7) {
                    Text(authorName(message.author))
                        .font(.caption.weight(.semibold))
                    Text(message.createdAt, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Text(message.body)
                    .font(.callout)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }

    private func composer(_ thread: WorkspaceThread) -> some View {
        VStack(spacing: 8) {
            TextField(
                "Reply locally…",
                text: Binding(
                    get: { store.selectedThread?.draft ?? "" },
                    set: store.updateSelectedThreadDraft
                ),
                axis: .vertical
            )
            .textFieldStyle(.plain)
            .lineLimit(1...5)
            .onSubmit {
                store.sendSelectedThreadMessage()
            }

            HStack {
                Label("Not synced or sent to the agent", systemImage: "internaldrive")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(thread.draft.count)/4000")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                Button {
                    store.sendSelectedThreadMessage()
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white)
                        .frame(width: 26, height: 26)
                        .background(MacHealthMesStyle.moss, in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(thread.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel(Text("Send local reply"))
            }
        }
        .padding(13)
        .background(.regularMaterial)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(MacHealthMesStyle.line)
                .frame(height: 1)
        }
    }

    private func anchorLabel(_ kind: WorkspaceThreadAnchorKind) -> String {
        switch kind {
        case .post: return "Post"
        case .card: return "Dashboard card"
        case .calendarEvent: return "Calendar event"
        case .visualization: return "Insight"
        case .decision: return "Decision"
        case .nutrition: return "Nutrition"
        }
    }

    private func anchorSymbol(_ kind: WorkspaceThreadAnchorKind) -> String {
        switch kind {
        case .post: return "text.bubble"
        case .card: return "rectangle"
        case .calendarEvent: return "calendar"
        case .visualization: return "chart.xyaxis.line"
        case .decision: return "checkmark.bubble"
        case .nutrition: return "fork.knife"
        }
    }

    private func authorName(_ author: WorkspaceMessageAuthor) -> String {
        switch author {
        case .user: return "You"
        case .healthmes: return "HealthMes context"
        case .system: return "System"
        }
    }

    private func authorSymbol(_ author: WorkspaceMessageAuthor) -> String {
        switch author {
        case .user: return "person.fill"
        case .healthmes: return "bolt.heart.fill"
        case .system: return "gearshape.fill"
        }
    }

    private func authorColor(_ author: WorkspaceMessageAuthor) -> Color {
        switch author {
        case .user: return MacHealthMesStyle.graphite
        case .healthmes: return MacHealthMesStyle.moss
        case .system: return .secondary
        }
    }
}
