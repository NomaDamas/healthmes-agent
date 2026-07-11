import SwiftUI

/// One pending proposal with the §8.5 button row wired to the REAL
/// endpoints: ✅ Apply → accept, ✏️ Adjust → detail sheet, ❌ Keep as is →
/// decline.
struct ProposalRowView: View {
    let proposal: ProposalItem
    let busy: Bool
    let onApply: () -> Void
    let onKeep: () -> Void
    let onAdjust: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(verbatim: ProposalFormat.windowLine(proposal))
                .font(.body)
            HStack(spacing: 12) {
                Button(action: onApply) {
                    Label("Apply", systemImage: "checkmark.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy)
                .accessibilityHint(Text("Accepts this schedule proposal"))

                Button(action: onAdjust) {
                    Label("Adjust", systemImage: "pencil.circle")
                }
                .buttonStyle(.bordered)
                .disabled(busy)
                .accessibilityHint(Text("Opens the proposal details"))

                Button(role: .destructive, action: onKeep) {
                    Label("Keep as is", systemImage: "xmark.circle")
                }
                .buttonStyle(.bordered)
                .disabled(busy)
                .accessibilityHint(Text("Declines this schedule proposal"))
            }
            .font(.footnote)
            .labelStyle(.titleOnly)
            if busy {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .padding(.vertical, 2)
    }
}

/// Detail sheet behind ✏️ Adjust: the proposal's full window + status, with
/// accept/decline still available. "Adjusting" the times themselves stays a
/// conversation with the agent (Telegram/chat) — this app never edits plans
/// silently; propose-then-confirm is the product's trust gate.
struct ProposalDetailView: View {
    let proposalID: UUID

    @Environment(\.dismiss) private var dismiss
    @State private var proposal: ProposalItem?
    @State private var message: String?
    @State private var busy = false

    private let api = HealthMesAPI()

    var body: some View {
        List {
            if let proposal {
                Section {
                    LabeledContent {
                        Text(proposal.proposedStart, style: .date)
                    } label: {
                        Text("Date")
                    }
                    LabeledContent {
                        Text(verbatim: ProposalFormat.timeRange(proposal))
                    } label: {
                        Text("Time")
                    }
                    LabeledContent {
                        Text(verbatim: proposal.status.rawValue)
                    } label: {
                        Text("Status")
                    }
                } header: {
                    Text("Proposed block")
                }

                if proposal.status == .proposed {
                    Section {
                        Button {
                            Task { await resolve(.accept) }
                        } label: {
                            Label("Apply", systemImage: "checkmark.circle.fill")
                        }
                        .disabled(busy)
                        Button(role: .destructive) {
                            Task { await resolve(.decline) }
                        } label: {
                            Label("Keep as is", systemImage: "xmark.circle")
                        }
                        .disabled(busy)
                    } footer: {
                        Text(
                            "To change the times instead, reply to the alert in chat — the agent re-proposes and this list updates."
                        )
                    }
                }
            } else if let message {
                Text(verbatim: message)
                    .foregroundStyle(.secondary)
            } else {
                HStack {
                    ProgressView()
                    Text("Loading proposal…")
                        .foregroundStyle(.secondary)
                }
            }

            if let message, proposal != nil {
                Section {
                    Text(verbatim: message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle(Text("Proposal"))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button {
                    dismiss()
                } label: {
                    Text("Done")
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        do {
            // No GET-by-id endpoint; the list is tiny (single user).
            let page = try await api.listProposals()
            if let found = page.data.first(where: { $0.id == proposalID }) {
                proposal = found
            } else {
                message = String(localized: "This proposal no longer exists.")
            }
        } catch {
            message = BriefingHomeModel.describe(error)
        }
    }

    private func resolve(_ action: ProposalAction) async {
        busy = true
        defer { busy = false }
        do {
            proposal = try await api.resolveProposal(id: proposalID, action: action)
            message =
                action == .accept
                ? String(localized: "Proposal applied.")
                : String(localized: "Kept as is — proposal declined.")
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            message = String(
                localized: "Already resolved (\(error.alreadyResolvedStatus ?? "resolved"))."
            )
            await load()
        } catch {
            message = BriefingHomeModel.describe(error)
        }
    }
}

enum ProposalFormat {
    /// "Jul 12, 14:00–15:00" in the device locale/timezone (proposal times
    /// are instants; unlike glance blocks there is no server-timezone string
    /// on this payload).
    static func windowLine(_ proposal: ProposalItem) -> String {
        let day = DateFormatter()
        day.dateStyle = .medium
        day.timeStyle = .none
        return "\(day.string(from: proposal.proposedStart)) · \(timeRange(proposal))"
    }

    static func timeRange(_ proposal: ProposalItem) -> String {
        let time = DateFormatter()
        time.dateStyle = .none
        time.timeStyle = .short
        return "\(time.string(from: proposal.proposedStart))–\(time.string(from: proposal.proposedEnd))"
    }
}
