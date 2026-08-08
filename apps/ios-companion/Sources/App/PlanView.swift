import SwiftUI

@MainActor
final class PlanModel: ObservableObject {
    @Published var goals: [WeeklyGoalItem] = []
    @Published var tasks: [TaskItem] = []
    @Published var events: [CalendarEventItem] = []
    @Published var proposals: [ProposalItem] = []
    @Published var alerts: [AlertItem] = []
    @Published var busyProposalIDs: Set<UUID> = []
    @Published var message: String?
    @Published var isLoading = false

    private let api = HealthMesAPI()
    private var refreshGate = LatestRefreshGate()

    func refresh(now: Date = Date()) async {
        let refreshID = refreshGate.begin()
        guard PairingStore.shared.load() != nil else {
            message = String(localized: "Not paired — open Settings.")
            isLoading = false
            return
        }

        isLoading = true
        defer {
            if refreshGate.isCurrent(refreshID) {
                isLoading = false
            }
        }
        let calendar = Calendar.autoupdatingCurrent
        let start = calendar.startOfDay(for: now)
        let end = calendar.date(byAdding: .day, value: 7, to: start) ?? start.addingTimeInterval(604_800)
        let weekStart = ProductDateFormat.weekStart(containing: now, calendar: calendar)

        async let goalsResult = productRefreshResult {
            try await api.listGoals(weekStart: weekStart)
        }
        async let tasksResult = productRefreshResult {
            try await api.listTasks()
        }
        async let eventsResult = productRefreshResult {
            try await api.listScheduleEvents(start: start, end: end)
        }
        async let proposalsResult = productRefreshResult {
            try await api.listProposals(status: .proposed)
        }
        async let alertsResult = productRefreshResult {
            try await api.listAlerts(hours: 168)
        }

        let results = await (
            goalsResult,
            tasksResult,
            eventsResult,
            proposalsResult,
            alertsResult
        )
        guard refreshGate.isCurrent(refreshID) else { return }

        var errors: [String] = []
        switch results.0 {
        case .success(let page):
            goals = page.data.filter { $0.status == "active" }
        case .failure(let error):
            errors.append("Goals: \(BriefingHomeModel.describe(error))")
        }
        switch results.1 {
        case .success(let page):
            tasks = page.data.filter(\.isOpen)
        case .failure(let error):
            errors.append("Tasks: \(BriefingHomeModel.describe(error))")
        }
        switch results.2 {
        case .success(let page):
            events = page.data
        case .failure(let error):
            errors.append("Calendar: \(BriefingHomeModel.describe(error))")
        }
        switch results.3 {
        case .success(let page):
            proposals = page.data
        case .failure(let error):
            errors.append("Proposals: \(BriefingHomeModel.describe(error))")
        }
        switch results.4 {
        case .success(let page):
            alerts = page.data
        case .failure(let error):
            errors.append("Alerts: \(BriefingHomeModel.describe(error))")
        }
        message = errors.isEmpty ? nil : errors.joined(separator: "\n")
    }

    func resolve(_ proposal: ProposalItem, action: ProposalAction) async {
        busyProposalIDs.insert(proposal.id)
        defer { busyProposalIDs.remove(proposal.id) }
        do {
            let resolved = try await api.resolveProposal(proposal, action: action)
            proposals.removeAll { $0.id == proposal.id }
            message = ProposalStatusPresentation.label(for: resolved.status)
            await refresh()
        } catch let error as HealthMesAPIError where error.isAlreadyResolved {
            proposals.removeAll { $0.id == proposal.id }
            message = String(
                format: String(localized: "Already resolved (%@)."),
                error.alreadyResolvedStatus ?? "resolved"
            )
            await refresh()
        } catch let error as HealthMesAPIError where error.isProposalExpired {
            proposals.removeAll { $0.id == proposal.id }
            message = String(localized: "Expired · calendar unchanged")
        } catch {
            message = BriefingHomeModel.describe(error)
        }
    }
}

struct PlanView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model = PlanModel()
    @State private var showAllEvents = false
    @State private var showAllTasks = false

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 14) {
                goalsCard
                scheduleCard
                tasksCard
                if !model.proposals.isEmpty {
                    proposalsCard
                }
                if let message = model.message {
                    Label {
                        Text(verbatim: message)
                    } icon: {
                        Image(systemName: "info.circle")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 4)
                }
            }
            .padding(16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle(Text("Plan"))
        .refreshable { await model.refresh() }
        .task {
            if model.events.isEmpty && !model.isLoading {
                await model.refresh()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .healthmesPlanChanged)) { _ in
            Task { await model.refresh() }
        }
    }

    private var goalsCard: some View {
        ProductCard(kicker: "This week", systemImage: "scope") {
            if model.goals.isEmpty {
                Text("No active goals")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(model.goals.prefix(3)) { goal in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Circle()
                            .fill(priorityColor(goal.priority))
                            .frame(width: 8, height: 8)
                        Text(verbatim: goal.title)
                            .font(.body.weight(.medium))
                        Spacer()
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    private var scheduleCard: some View {
        ProductCard(kicker: "Schedule", systemImage: "calendar.day.timeline.left") {
            if model.events.isEmpty {
                Text(model.isLoading ? "Loading…" : "No scheduled events")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(visibleEvents) { event in
                    eventRow(event)
                    if event.id != visibleEvents.last?.id {
                        Divider()
                    }
                }
                if model.events.count > 4 {
                    Button(showAllEvents ? "Show less" : "View the next 7 days") {
                        withAnimation { showAllEvents.toggle() }
                    }
                    .font(.footnote.weight(.semibold))
                }
            }
        }
    }

    private var tasksCard: some View {
        ProductCard(kicker: "Open tasks", systemImage: "checklist") {
            if model.tasks.isEmpty {
                Text("No open tasks")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(visibleTasks) { task in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: task.status == "in_progress" ? "circle.dotted" : "circle")
                            .foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(verbatim: task.title)
                            if let deadline = task.deadline {
                                Text(deadline, style: .relative)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        if task.energyDemand == "high" {
                            Image(systemName: "bolt.fill")
                                .font(.caption)
                                .foregroundStyle(.orange)
                                .accessibilityLabel(Text("High energy"))
                        }
                    }
                }
                if model.tasks.count > 4 {
                    Button(showAllTasks ? "Show less" : "View all tasks") {
                        withAnimation { showAllTasks.toggle() }
                    }
                    .font(.footnote.weight(.semibold))
                }
            }
        }
    }

    private var proposalsCard: some View {
        ProductCard(kicker: "Proposed changes", systemImage: "sparkles") {
            ForEach(model.proposals) { proposal in
                VStack(alignment: .leading, spacing: 10) {
                    if let actionPrompt = proposalActionPrompt(proposal) {
                        Text(verbatim: actionPrompt)
                            .font(.body.weight(.semibold))
                    } else {
                        Text("Action details unavailable")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    Text(verbatim: ProposalFormat.windowLine(proposal))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if proposalActionPrompt(proposal) != nil {
                        HStack(spacing: 10) {
                            Button {
                                Task { await model.resolve(proposal, action: .decline) }
                            } label: {
                                Text("No")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.bordered)

                            Button {
                                Task { await model.resolve(proposal, action: .accept) }
                            } label: {
                                Text("Yes")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(Color(red: 0.02, green: 0.34, blue: 0.25))
                        }
                        .disabled(model.busyProposalIDs.contains(proposal.id))
                    }

                    Button {
                        router.openProposalDetail(proposal.id)
                    } label: {
                        Text("Open details")
                    }
                    .font(.footnote.weight(.semibold))
                }
                if proposal.id != model.proposals.last?.id {
                    Divider()
                }
            }
        }
    }

    private func proposalActionPrompt(_ proposal: ProposalItem) -> String? {
        let alert = model.alerts.first { $0.proposalId == proposal.id }
        return ProposalActionPresentation.exactPrompt(alert: alert)
    }

    private var visibleEvents: ArraySlice<CalendarEventItem> {
        model.events.prefix(showAllEvents ? model.events.count : 4)
    }

    private var visibleTasks: ArraySlice<TaskItem> {
        model.tasks.prefix(showAllTasks ? model.tasks.count : 4)
    }

    private func eventRow(_ event: CalendarEventItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 2) {
                Text(event.startAt, style: .time)
                    .font(.caption.weight(.semibold))
                Text(event.endAt, style: .time)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .frame(width: 58, alignment: .leading)

            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: event.summary ?? String(localized: "Untitled event"))
                    .font(.body.weight(.medium))
                if event.isAgentCreated {
                    Label("HealthMes planned", systemImage: "sparkles")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
    }

    private func priorityColor(_ priority: Int) -> Color {
        switch priority {
        case 7...: return .orange
        case 4...: return .green
        default: return .secondary
        }
    }
}
