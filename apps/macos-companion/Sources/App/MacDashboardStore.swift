import Foundation

@MainActor
final class MacDashboardStore: ObservableObject {
    @Published private(set) var goals: [WeeklyGoalItem] = []
    @Published private(set) var tasks: [TaskItem] = []
    @Published private(set) var events: [CalendarEventItem] = []
    @Published private(set) var decisions: [MacDecisionSummary] = []
    @Published private(set) var weeklyReport: WeeklyReport?
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var errorMessages: [String] = []
    @Published private(set) var isSavingPlanItem = false
    @Published private(set) var planSaveMessage: String?
    @Published private(set) var planSaveSucceeded = false

    private let api: HealthMesAPI
    private let decisionAPI: MacDecisionAPI
    private let pairingStore: PairingStore
    private var refreshGate = LatestRefreshGate()
    private var saveGeneration: UInt = 0

    init(
        api: HealthMesAPI = HealthMesAPI(),
        decisionAPI: MacDecisionAPI = MacDecisionAPI(),
        pairingStore: PairingStore = .shared
    ) {
        self.api = api
        self.decisionAPI = decisionAPI
        self.pairingStore = pairingStore
    }

    var pairing: Pairing? {
        pairingStore.load()
    }

    func refresh() async {
        let refreshID = refreshGate.begin()
        guard let pairingSnapshot = pairing else {
            resetForPairingChange()
            return
        }
        let visibleCalendarRange = Self.visibleCalendarRange()
        isRefreshing = true
        defer {
            if refreshGate.isCurrent(refreshID) {
                isRefreshing = false
            }
        }

        async let goalsResult: Result<WeeklyGoalsPage, Error> = productRefreshResult {
            try await api.listGoals(
                pairing: pairingSnapshot,
                weekStart: ProductDateFormat.weekStart(containing: Date()),
                status: "active"
            )
        }
        async let tasksResult: Result<TasksPage, Error> = productRefreshResult {
            try await api.listTasks(pairing: pairingSnapshot)
        }
        async let eventsResult: Result<CalendarEventsPage, Error> = productRefreshResult {
            return try await api.listScheduleEvents(
                pairing: pairingSnapshot,
                start: visibleCalendarRange.start,
                end: visibleCalendarRange.end
            )
        }
        async let decisionsResult: Result<MacDecisionsPage, Error> = productRefreshResult {
            try await decisionAPI.listDecisions(pairing: pairingSnapshot)
        }
        async let reportResult: Result<WeeklyReport, Error> = productRefreshResult {
            try await api.weeklyReport(pairing: pairingSnapshot)
        }
        let results = await (
            goalsResult,
            tasksResult,
            eventsResult,
            decisionsResult,
            reportResult
        )
        guard
            refreshGate.isCurrent(refreshID),
            pairing == pairingSnapshot
        else { return }

        var errors: [String] = []
        switch results.0 {
        case .success(let page): goals = page.data
        case .failure(let error): errors.append(describe(error, surface: "Goals"))
        }
        switch results.1 {
        case .success(let page): tasks = page.data
        case .failure(let error): errors.append(describe(error, surface: "Tasks"))
        }
        switch results.2 {
        case .success(let page): events = page.data
        case .failure(let error): errors.append(describe(error, surface: "Calendar"))
        }
        switch results.3 {
        case .success(let page): decisions = page.data
        case .failure(let error): errors.append(describe(error, surface: "Decisions"))
        }
        switch results.4 {
        case .success(let report): weeklyReport = report
        case .failure(let error):
            errors.append(describe(error, surface: "Weekly report"))
        }
        errorMessages = errors
        lastUpdated = Date()
    }

    @discardableResult
    func createTask(title: String) async -> Bool {
        guard let pairingSnapshot = pairing else { return false }
        return await savePlanItem(
            title: title,
            pairing: pairingSnapshot,
            successMessage: String(localized: "Task added to Plan")
        ) { title in
            _ = try await api.createTask(
                TaskCreateBody(title: title),
                pairing: pairingSnapshot
            )
        }
    }

    @discardableResult
    func createGoal(title: String) async -> Bool {
        guard let pairingSnapshot = pairing else { return false }
        return await savePlanItem(
            title: title,
            pairing: pairingSnapshot,
            successMessage: String(localized: "Weekly goal added to Plan")
        ) { title in
            _ = try await api.createGoal(
                WeeklyGoalCreateBody(
                    weekStart: ProductDateFormat.weekStart(containing: Date()),
                    title: title
                ),
                pairing: pairingSnapshot
            )
        }
    }

    func clearPlanSaveMessage() {
        planSaveMessage = nil
        planSaveSucceeded = false
    }

    private func savePlanItem(
        title: String,
        pairing: Pairing,
        successMessage: String,
        operation: (String) async throws -> Void
    ) async -> Bool {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isSavingPlanItem else { return false }
        saveGeneration &+= 1
        let generation = saveGeneration
        isSavingPlanItem = true
        planSaveMessage = nil
        planSaveSucceeded = false
        defer {
            if generation == saveGeneration {
                isSavingPlanItem = false
            }
        }
        do {
            try await operation(trimmed)
            guard generation == saveGeneration, self.pairing == pairing else {
                return false
            }
            planSaveMessage = successMessage
            planSaveSucceeded = true
            await refresh()
            guard generation == saveGeneration, self.pairing == pairing else {
                return false
            }
            return true
        } catch {
            guard generation == saveGeneration, self.pairing == pairing else {
                return false
            }
            planSaveMessage = describe(error, surface: "Plan")
            return false
        }
    }

    func resetForPairingChange() {
        _ = refreshGate.begin()
        saveGeneration &+= 1
        goals = []
        tasks = []
        events = []
        decisions = []
        weeklyReport = nil
        errorMessages = []
        lastUpdated = nil
        isRefreshing = false
        isSavingPlanItem = false
        planSaveMessage = nil
        planSaveSucceeded = false
    }

    private func describe(_ error: Error, surface: String) -> String {
        switch error {
        case HealthMesAPIError.notPaired:
            return String(localized: "\(surface): connect HealthMes first")
        case HealthMesAPIError.unauthorized:
            return String(localized: "\(surface): connection needs attention")
        case HealthMesAPIError.server(_, _, let message, _):
            return "\(surface): \(message)"
        case HealthMesAPIError.transport:
            return String(localized: "\(surface): instance unavailable")
        case HealthMesAPIError.decoding:
            return String(localized: "\(surface): response needs an app update")
        default:
            return String(localized: "\(surface): could not load")
        }
    }

    private static func visibleCalendarRange(now: Date = Date()) -> DateInterval {
        let calendar = Calendar.autoupdatingCurrent
        let week = calendar.dateInterval(of: .weekOfYear, for: now)
        let start = week?.start ?? calendar.startOfDay(for: now)
        let end = calendar.date(byAdding: .day, value: 14, to: start)
            ?? start.addingTimeInterval(14 * 86_400)
        return DateInterval(start: start, end: end)
    }
}
