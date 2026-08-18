import SwiftUI

/// One pending proposal with the §8.5 button row wired to the REAL
/// endpoints: ✅ Apply → accept, ✏️ Adjust → detail sheet, ❌ Keep as is →
/// decline.
struct ProposalRowView: View {
    let proposal: ProposalItem
    let actionPrompt: String?
    let busy: Bool
    let onApply: () -> Void
    let onKeep: () -> Void
    let onAdjust: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let actionPrompt {
                Text(verbatim: actionPrompt)
                    .font(.body.weight(.semibold))
            }
            Text(verbatim: ProposalFormat.windowLine(proposal))
                .font(.body)
            HStack(spacing: 12) {
                if actionPrompt != nil {
                    Button(action: onApply) {
                        Label("Apply", systemImage: "checkmark.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)
                    .accessibilityHint(Text("Accepts this schedule proposal"))

                    Button(role: .destructive, action: onKeep) {
                        Label("Keep as is", systemImage: "xmark.circle")
                    }
                    .buttonStyle(.bordered)
                    .disabled(busy)
                    .accessibilityHint(Text("Declines this schedule proposal"))
                }
                Button(action: onAdjust) {
                    Label("Adjust", systemImage: "pencil.circle")
                }
                .buttonStyle(.bordered)
                .disabled(busy)
                .accessibilityHint(Text("Opens the proposal details"))
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
    @State private var actionPrompt: String?
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

                if proposal.isActionable, let actionPrompt {
                    Section {
                        Text(verbatim: actionPrompt)
                            .font(.body.weight(.semibold))
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
                } else if proposal.isActionable {
                    Section {
                        Text(
                            "Approval controls are hidden because the exact calendar action is unavailable."
                        )
                        .foregroundStyle(.secondary)
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
        async let proposalResult: Result<ProposalItem, Error> = productRefreshResult {
            try await api.getProposal(proposalID)
        }
        async let alertsResult: Result<AlertsPage, Error> = productRefreshResult {
            try await api.listAlerts(hours: 168)
        }
        let results = await (proposalResult, alertsResult)

        switch results.0 {
        case .success(let loadedProposal):
            proposal = loadedProposal
        case .failure(let error):
            message = BriefingHomeModel.describe(error)
            return
        }

        switch results.1 {
        case .success(let page):
            let alert = page.data.first { $0.proposalId == proposalID }
            actionPrompt = ProposalActionPresentation.exactPrompt(alert: alert)
        case .failure:
            actionPrompt = nil
        }
    }

    private func resolve(_ action: ProposalAction) async {
        busy = true
        defer { busy = false }
        do {
            guard let current = proposal else { return }
            proposal = try await api.resolveProposal(current, action: action)
            if let proposal {
                message = ProposalStatusPresentation.label(for: proposal.status)
            }
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
