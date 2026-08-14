import SwiftUI
import WatchConnectivity
import WatchKit

enum WatchDecisionResult: Equatable {
    case approved
    case applied
    case declined
    case alreadyApproved
    case alreadyDeclined
    case expired
    case offline

    var title: String {
        switch self {
        case .approved:
            return String(localized: "Yes recorded")
        case .applied:
            return ProposalStatusPresentation.label(for: .pushed)
        case .declined:
            return String(localized: "No recorded")
        case .alreadyApproved:
            return String(localized: "Already approved")
        case .alreadyDeclined:
            return String(localized: "Already declined")
        case .expired:
            return String(localized: "Decision expired")
        case .offline:
            return String(localized: "Not sent")
        }
    }

    var detail: String {
        switch self {
        case .approved:
            return ProposalStatusPresentation.detail(for: .accepted)
        case .applied:
            return ProposalStatusPresentation.detail(for: .pushed)
        case .declined:
            return String(localized: "Your calendar stays unchanged.")
        case .alreadyApproved:
            return String(localized: "Another device already approved it.")
        case .alreadyDeclined:
            return String(localized: "Another device already declined it.")
        case .expired:
            return String(localized: "The decision window closed without a change.")
        case .offline:
            return String(localized: "Reconnect, then try again.")
        }
    }

    var systemImage: String {
        switch self {
        case .approved, .applied, .alreadyApproved:
            return "checkmark.circle.fill"
        case .declined, .alreadyDeclined:
            return "xmark.circle.fill"
        case .expired:
            return "clock.badge.xmark"
        case .offline:
            return "wifi.slash"
        }
    }

    var tint: Color {
        switch self {
        case .approved, .applied, .alreadyApproved:
            return .green
        case .declined, .alreadyDeclined:
            return .secondary
        case .expired, .offline:
            return .orange
        }
    }
}

enum WatchWellnessAvailability: Equatable {
    case current
    case stale
    case unpaired
    case unauthorized
    case offline
    case contractError

    var label: String {
        switch self {
        case .current: return String(localized: "Current")
        case .stale: return String(localized: "Cached · may be old")
        case .unpaired: return String(localized: "Pair with iPhone")
        case .unauthorized: return String(localized: "Connection needs attention")
        case .offline: return String(localized: "Offline")
        case .contractError: return String(localized: "App update needed")
        }
    }

    var systemImage: String {
        switch self {
        case .current: return "checkmark.circle"
        case .stale: return "clock.arrow.circlepath"
        case .unpaired: return "iphone.gen2"
        case .unauthorized: return "key.slash"
        case .offline: return "wifi.slash"
        case .contractError: return "exclamationmark.arrow.triangle.2.circlepath"
        }
    }
}

@MainActor
final class WatchDecisionRemoteModel: ObservableObject {
    @Published var decision: PendingDecision?
    @Published var isLoading = false
    @Published var applyingAction: ProposalAction?
    @Published var result: WatchDecisionResult?
    @Published var glanceLine: String?
    @Published var energyScore: Int?
    @Published var wellnessImpact: String?
    @Published var upcomingEvents: [CalendarEventItem] = []
    @Published var timezone = TimeZone.current.identifier
    @Published var isDecisionContextReady = false
    @Published var availability: WatchWellnessAvailability = .offline

    private let api = HealthMesAPI()
    private let glanceClient = GlanceClient()
    private var refreshOperationGate = PairingOperationGate()
    private var resolutionOperationGate = PairingOperationGate()

    func refresh() async {
        // Refresh leaves the transient action/result screen and recomputes the
        // best available proposal or wellness glance from current data.
        resolutionOperationGate.invalidate()
        applyingAction = nil
        result = nil
        decision = nil
        isDecisionContextReady = false
        guard let pairingSnapshot = PairingStore.shared.load() else {
            clearForUnpaired()
            return
        }
        let refreshOperation = refreshOperationGate.begin(pairing: pairingSnapshot)
        isLoading = true
        defer {
            if refreshOperationGate.isCurrent(
                refreshOperation,
                pairing: PairingStore.shared.load()
            ) {
                isLoading = false
            }
        }
        async let alertsResult: Result<AlertsPage, Error> = productRefreshResult {
            try await api.listAlerts(pairing: pairingSnapshot, hours: 24)
        }
        async let proposalsResult: Result<ProposalsPage, Error> = productRefreshResult {
            try await api.listProposals(pairing: pairingSnapshot, status: .proposed)
        }
        async let eventsResult: Result<CalendarEventsPage, Error> = productRefreshResult {
            try await api.listScheduleEvents(
                pairing: pairingSnapshot,
                start: Date(),
                end: Date().addingTimeInterval(24 * 60 * 60)
            )
        }
        let controlResults = await (
            alertsResult,
            proposalsResult,
            eventsResult
        )
        guard
            refreshOperationGate.isCurrent(
                refreshOperation,
                pairing: PairingStore.shared.load()
            )
        else {
            if PairingStore.shared.load() == nil {
                clearForUnpaired()
            }
            return
        }
        var controlAvailability: WatchWellnessAvailability?

        switch (controlResults.0, controlResults.1) {
        case (.success(let alerts), .success(let proposals)):
            decision = PendingDecision.correlate(
                alerts: alerts.data,
                proposals: proposals.data
            ).first
        case (.failure(let error), _), (_, .failure(let error)):
            decision = nil
            controlAvailability = Self.availability(for: error)
        }

        switch controlResults.2 {
        case .success(let events):
            upcomingEvents = events.data
        case .failure:
            // Calendar detail is supplementary. A valid proposal remains
            // actionable even when the event list cannot be refreshed.
            upcomingEvents = []
        }
        result = nil

        do {
            let glance = try await glanceClient.fetch(pairing: pairingSnapshot)
            guard
                refreshOperationGate.isCurrent(
                    refreshOperation,
                    pairing: PairingStore.shared.load()
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            energyScore = glance.payload.energy.score
            wellnessImpact = Self.impact(for: glance.payload.energy.score)
            glanceLine = GlanceFormat.nextBlockLine(glance.payload)
            timezone = glance.payload.timezone
            isDecisionContextReady = glance.payload.energy.score != nil
            availability = controlAvailability ?? .current
        } catch {
            guard
                refreshOperationGate.isCurrent(
                    refreshOperation,
                    pairing: PairingStore.shared.load()
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            if let cached = GlanceSnapshotCache.shared.decodedPayload(for: pairingSnapshot) {
                energyScore = cached.energy.score
                wellnessImpact = Self.impact(for: cached.energy.score)
                glanceLine = GlanceFormat.nextBlockLine(cached)
                timezone = cached.timezone
                isDecisionContextReady = false
                availability = controlAvailability ?? .stale
            } else {
                energyScore = nil
                wellnessImpact = String(localized: "Not enough health data to adjust the plan.")
                availability = controlAvailability ?? Self.availability(for: error)
            }
        }
    }

    func resolve(_ action: ProposalAction) async {
        guard
            applyingAction == nil,
            WatchDecisionLayoutPolicy.canResolve(
                isDecisionContextReady: isDecisionContextReady,
                hasCurrentWellnessContext: availability == .current
            ),
            energyScore != nil,
            let decision,
            let pairingSnapshot = PairingStore.shared.load()
        else {
            if PairingStore.shared.load() == nil {
                clearForUnpaired()
            }
            return
        }
        refreshOperationGate.invalidate()
        let resolutionOperation = resolutionOperationGate.begin(
            pairing: pairingSnapshot,
            proposalID: decision.id
        )
        applyingAction = action
        result = nil
        defer {
            if resolutionOperationGate.isCurrent(
                resolutionOperation,
                pairing: PairingStore.shared.load(),
                proposalID: decision.id
            ) {
                applyingAction = nil
            }
        }
        do {
            let current = try await api.getProposal(
                decision.id,
                pairing: pairingSnapshot
            )
            guard
                resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: decision.id
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            guard current.isActionable else {
                result = Self.result(for: current)
                self.decision = nil
                return
            }
            let resolved = try await api.resolveProposal(
                current,
                action: action,
                surface: "apple_watch_app",
                pairing: pairingSnapshot
            )
            guard
                resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: decision.id
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            result = Self.result(for: resolved.status)
            self.decision = nil
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            guard
                resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: decision.id
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            result = Self.result(for: error.alreadyResolvedStatus)
            self.decision = nil
        } catch let error as HealthMesAPIError where error.isProposalExpired {
            guard
                resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: decision.id
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            result = .expired
            self.decision = nil
        } catch {
            guard
                resolutionOperationGate.isCurrent(
                    resolutionOperation,
                    pairing: PairingStore.shared.load(),
                    proposalID: decision.id
                )
            else {
                if PairingStore.shared.load() == nil {
                    clearForUnpaired()
                }
                return
            }
            result = .offline
        }
    }

    func pairingDidChange() {
        refreshOperationGate.invalidate()
        resolutionOperationGate.invalidate()
        decision = nil
        isLoading = false
        applyingAction = nil
        result = nil
        glanceLine = nil
        energyScore = nil
        wellnessImpact = nil
        upcomingEvents = []
        timezone = TimeZone.current.identifier
        isDecisionContextReady = false
        availability = PairingStore.shared.load() == nil ? .unpaired : .offline
    }

    private func clearForUnpaired() {
        refreshOperationGate.invalidate()
        resolutionOperationGate.invalidate()
        decision = nil
        isLoading = false
        applyingAction = nil
        result = nil
        glanceLine = nil
        energyScore = nil
        upcomingEvents = []
        timezone = TimeZone.current.identifier
        isDecisionContextReady = false
        availability = .unpaired
        wellnessImpact = String(localized: "Connect HealthMes on iPhone first.")
    }

    private static func result(for proposal: ProposalItem) -> WatchDecisionResult {
        if proposal.status == .proposed {
            return .expired
        }
        return result(for: proposal.status.rawValue)
    }

    private static func result(for rawStatus: String?) -> WatchDecisionResult {
        guard let rawStatus, let status = ProposalStatus(rawValue: rawStatus) else {
            return .expired
        }
        return result(for: status, alreadyResolved: true)
    }

    private static func result(
        for status: ProposalStatus,
        alreadyResolved: Bool = false
    ) -> WatchDecisionResult {
        switch status {
        case .accepted:
            return alreadyResolved ? .alreadyApproved : .approved
        case .pushed:
            return .applied
        case .declined:
            return alreadyResolved ? .alreadyDeclined : .declined
        case .proposed, .invalidated:
            return .expired
        }
    }

    private static func availability(for error: Error) -> WatchWellnessAvailability {
        switch error {
        case GlanceClientError.notPaired, HealthMesAPIError.notPaired:
            return .unpaired
        case GlanceClientError.unauthorized, HealthMesAPIError.unauthorized:
            return .unauthorized
        case GlanceClientError.decoding, HealthMesAPIError.decoding:
            return .contractError
        default:
            return .offline
        }
    }

    private static func impact(for score: Int?) -> String {
        guard let score else {
            return String(localized: "Not enough data to change today's plan.")
        }
        if score < 45 {
            return String(localized: "Protect recovery before high-energy work.")
        }
        if score < 70 {
            return String(localized: "Save capacity for one important block.")
        }
        return String(localized: "Use this capacity on the highest-priority goal.")
    }
}

struct WatchDecisionRemoteView: View {
    @ObservedObject var model: WatchDecisionRemoteModel
    @State private var detail: WatchDecisionDetail?
    @State private var isSpeaking = false
    @State private var spokenDraft: String?
    @State private var speakStatus: String?

    var body: some View {
        Group {
            if let result = model.result {
                resultView(result)
            } else if let decision = model.decision {
                pending(decision)
            } else if model.isLoading {
                ProgressView()
            } else {
                glance
            }
        }
        .sheet(item: $detail) { detail in
            WatchDecisionDetailView(detail: detail)
        }
        .environment(\.timeZone, TimeZone(identifier: model.timezone) ?? .current)
    }

    private func pending(_ decision: PendingDecision) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Label("CHANGE", systemImage: "calendar.badge.clock")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color(red: 0.42, green: 0.88, blue: 0.76))
                    Spacer()
                    if let score = model.energyScore {
                        Text(verbatim: "\(score)%")
                            .font(.caption2.bold().monospacedDigit())
                            .foregroundStyle(energyTint(score))
                            .accessibilityLabel(Text("Cognitive energy"))
                            .accessibilityValue(Text(verbatim: "\(score) percent"))
                    }
                }

                Text(verbatim: decision.watchActionTitle)
                    .font(.headline.weight(.bold))
                    .fixedSize(horizontal: false, vertical: true)

                if let reason = decision.watchReason {
                    Text(verbatim: reason)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !model.isDecisionContextReady {
                    Label("Confirming calendar time", systemImage: "clock")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                } else if let before = decision.card?.before {
                    HStack(spacing: 5) {
                        watchTime(before)
                            .foregroundStyle(.secondary)
                        Image(systemName: "arrow.right")
                            .font(.caption2.bold())
                            .foregroundStyle(.orange)
                        watchTime(decision.card?.after ?? decision.proposal.proposedStart)
                            .foregroundStyle(.primary)
                    }
                    .font(.system(.caption, design: .rounded).weight(.bold))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(Text("Schedule moves"))
                } else {
                    Text(
                        verbatim: ProposalFormat.watchWindowLine(
                            decision.proposal,
                            timeZone: TimeZone(identifier: model.timezone)
                                ?? .autoupdatingCurrent
                        )
                    )
                        .font(.caption.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                }

                Button {
                    detail = WatchDecisionDetail(
                        decision: decision,
                        timezone: model.timezone
                    )
                } label: {
                    Label("Why?", systemImage: "info.circle")
                        .font(.caption2.weight(.semibold))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityHint(Text("Shows the reason and supporting evidence"))

                if let speakStatus {
                    Label(speakStatus, systemImage: "checkmark.circle.fill")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color(red: 0.42, green: 0.88, blue: 0.76))
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let spokenDraft {
                    spokenConfirmation(spokenDraft, decision: decision)
                } else {
                    VStack(spacing: 5) {
                        HStack(spacing: 7) {
                            decisionButton(
                                title: "No",
                                image: "xmark",
                                action: .decline,
                                prominent: false,
                                accessibilityLabel: "Reject: \(decision.primaryActionText)"
                            )
                            decisionButton(
                                title: "Yes",
                                image: "checkmark",
                                action: .accept,
                                prominent: true,
                                accessibilityLabel: "Approve: \(decision.primaryActionText)"
                            )
                        }

                        Button {
                            presentSpeakInput(for: decision)
                        } label: {
                            Group {
                                if isSpeaking {
                                    ProgressView()
                                } else {
                                    Label("Speak", systemImage: "microphone.fill")
                                        .font(.caption.weight(.bold))
                                }
                            }
                            .frame(maxWidth: .infinity, minHeight: 28)
                        }
                        .buttonStyle(.bordered)
                        .tint(Color(red: 0.42, green: 0.88, blue: 0.76))
                        .disabled(model.applyingAction != nil || isSpeaking)
                        .accessibilityHint(
                            Text("Dictate a different instruction to HealthMes")
                        )
                    }
                    .padding(.top, 2)
                }
            }
            .padding(.bottom, 6)
        }
        .scrollIndicators(.visible)
    }

    private func spokenConfirmation(
        _ transcript: String,
        decision: PendingDecision
    ) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("Recognized")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)

            Text(verbatim: transcript)
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    Color.white.opacity(0.08),
                    in: RoundedRectangle(cornerRadius: 9)
                )

            Button {
                applySpokenDraft(transcript, decision: decision)
            } label: {
                Label("Apply", systemImage: "checkmark")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.03, green: 0.55, blue: 0.46))

            Button {
                presentSpeakInput(for: decision)
            } label: {
                Label("Record Again", systemImage: "arrow.counterclockwise")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(isSpeaking)
        }
    }

    private func applySpokenDraft(
        _ transcript: String,
        decision: PendingDecision
    ) {
        let command = SpeakCommand.compose(
            userText: transcript,
            proposalID: decision.id,
            title: decision.card?.title,
            proposedAction: decision.primaryActionText
        )
        let queued = WatchPairingReceiver.shared.sendSpokenCommand(
            command,
            requestID: UUID().uuidString.lowercased(),
            proposalID: decision.id
        )
        guard queued else {
            speakStatus = String(localized: "iPhone connection is unavailable.")
            return
        }
        spokenDraft = nil
        speakStatus = String(localized: "Instruction queued for iPhone HealthMes.")
    }

    private func presentSpeakInput(for decision: PendingDecision) {
        guard !isSpeaking else { return }
        isSpeaking = true
        speakStatus = nil
        WKExtension.shared().visibleInterfaceController?.presentTextInputController(
            withSuggestions: nil,
            allowedInputMode: .plain
        ) { results in
            Task { @MainActor in
                defer { isSpeaking = false }
                guard
                    let spoken = results?.first as? String
                else { return }
                let clean = spoken.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !clean.isEmpty else { return }
                spokenDraft = clean
            }
        }
    }

    private func decisionButton(
        title: LocalizedStringKey,
        image: String,
        action: ProposalAction,
        prominent: Bool,
        accessibilityLabel: String
    ) -> some View {
        Group {
            if prominent {
                decisionButtonContent(
                    title: title,
                    image: image,
                    action: action,
                    accessibilityLabel: accessibilityLabel
                )
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 0.03, green: 0.55, blue: 0.46))
            } else {
                decisionButtonContent(
                    title: title,
                    image: image,
                    action: action,
                    accessibilityLabel: accessibilityLabel
                )
                    .buttonStyle(.bordered)
                    .tint(.secondary)
            }
        }
    }

    private func decisionButtonContent(
        title: LocalizedStringKey,
        image: String,
        action: ProposalAction,
        accessibilityLabel: String
    ) -> some View {
        Button {
            Task { await model.resolve(action) }
        } label: {
            Group {
                if model.applyingAction == action {
                    ProgressView()
                } else {
                    Label(title, systemImage: image)
                        .font(.headline)
                }
            }
            .frame(
                maxWidth: .infinity,
                minHeight: WatchDecisionLayoutPolicy.minimumButtonHeight
            )
        }
        .disabled(
            model.applyingAction != nil
                || !WatchDecisionLayoutPolicy.canResolve(
                    isDecisionContextReady: model.isDecisionContextReady,
                    hasCurrentWellnessContext: model.availability == .current
                )
        )
        .accessibilityLabel(Text(verbatim: accessibilityLabel))
        .accessibilityHint(
            Text(
                model.isDecisionContextReady && model.availability == .current
                    ? "Acts on the exact proposal shown above"
                    : "Waits for current health and calendar context"
            )
        )
    }

    private func resultView(_ result: WatchDecisionResult) -> some View {
        VStack(spacing: 8) {
            Image(systemName: result.systemImage)
                .font(.title2)
                .foregroundStyle(result.tint)
            Text(verbatim: result.title)
                .font(.headline)
                .multilineTextAlignment(.center)
            Text(verbatim: result.detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(2)
            Button("Refresh") {
                Task { await model.refresh() }
            }
            .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity)
    }

    private var glance: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                if model.availability == .unpaired {
                    Label("CONNECT", systemImage: "iphone.gen2")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color(red: 0.42, green: 0.88, blue: 0.76))
                    Text("Open HealthMes on iPhone")
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Pair iPhone once. This Watch connects automatically.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Label("No URL or token on Watch", systemImage: "lock.shield")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Button("Check again") {
                        Task { await model.refresh() }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 0.03, green: 0.55, blue: 0.46))
                    .frame(maxWidth: .infinity)
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        Text("CAPACITY")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        if let energyScore = model.energyScore {
                            Text(verbatim: "\(energyScore)")
                                .font(.system(.title2, design: .rounded).bold())
                                .accessibilityLabel(Text("Cognitive energy"))
                                .accessibilityValue(Text(verbatim: "\(energyScore)"))
                        }
                    }

                    if let energyScore = model.energyScore {
                        GeometryReader { proxy in
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.12))
                                Capsule()
                                    .fill(energyTint(energyScore))
                                    .frame(
                                        width: proxy.size.width
                                            * CGFloat(min(max(energyScore, 0), 100)) / 100
                                    )
                            }
                        }
                        .frame(height: 7)
                        .accessibilityHidden(true)
                    }

                    Text(verbatim: model.wellnessImpact ?? String(localized: "Checking body-to-plan impact…"))
                        .font(.headline)
                        .lineLimit(3)
                        .minimumScaleFactor(0.78)
                        .fixedSize(horizontal: false, vertical: true)

                    if !model.upcomingEvents.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(model.upcomingEvents.prefix(3)) { event in
                                HStack(spacing: 5) {
                                    Text(event.startAt, format: .dateTime.hour().minute())
                                        .font(.caption2.bold().monospacedDigit())
                                        .frame(width: 38, alignment: .leading)
                                    Text(verbatim: event.summary ?? String(localized: "Scheduled block"))
                                        .font(.caption)
                                        .lineLimit(1)
                                }
                            }
                        }
                    } else if let glanceLine = model.glanceLine {
                        Label(glanceLine, systemImage: "calendar")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Label(
                        model.availability.label,
                        systemImage: model.availability.systemImage
                    )
                    .font(.caption2)
                    .foregroundStyle(model.availability == .current ? .green : .orange)

                    Button("Refresh") {
                        Task { await model.refresh() }
                    }
                    .buttonStyle(.bordered)
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }

    private func watchTime(_ date: Date) -> Text {
        Text(date, format: .dateTime.weekday(.abbreviated).hour().minute())
    }

    private func energyTint(_ score: Int) -> Color {
        if score < 45 {
            return .orange
        }
        if score < 70 {
            return .yellow
        }
        return .green
    }
}

private extension WatchDecisionDetail {
    init(decision: PendingDecision, timezone: String) {
        let timeZone = TimeZone(identifier: timezone) ?? .autoupdatingCurrent
        self.init(
            prompt: decision.prompt,
            target: ProposalFormat.windowLine(decision.proposal, timeZone: timeZone),
            observation: decision.card?.observationShort ?? decision.alert?.summary,
            evidence: decision.card?.evidenceShort,
            action: decision.card?.proposedAction,
            before: decision.card?.before,
            after: decision.card?.after ?? decision.proposal.proposedStart,
            endsAt: decision.card?.endsAt ?? decision.proposal.proposedEnd,
            expiresAt: decision.card?.expiresAt
        )
    }
}
