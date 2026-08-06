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
        guard pairing != nil else {
            clear()
            return
        }
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        var errors: [String] = []
        do {
            goals = try await api.listGoals(
                weekStart: ProductDateFormat.weekStart(containing: Date()),
                status: "active"
            ).data
        } catch {
            errors.append(describe(error, surface: "Goals"))
        }
        do {
            tasks = try await api.listTasks().data
        } catch {
            errors.append(describe(error, surface: "Tasks"))
        }
        do {
            let range = Self.visibleCalendarRange()
            events = try await api.listScheduleEvents(
                start: range.start, end: range.end
            ).data
        } catch {
            errors.append(describe(error, surface: "Calendar"))
        }
        do {
            decisions = try await decisionAPI.listDecisions().data
        } catch {
            errors.append(describe(error, surface: "Decisions"))
        }
        do {
            weeklyReport = try await api.weeklyReport()
        } catch {
            errors.append(describe(error, surface: "Weekly report"))
        }

        errorMessages = errors
        lastUpdated = Date()
    }

    @discardableResult
    func createTask(title: String) async -> Bool {
        await savePlanItem(
            title: title,
            successMessage: String(localized: "Task added to Plan")
        ) { title in
            _ = try await api.createTask(TaskCreateBody(title: title))
        }
    }

    @discardableResult
    func createGoal(title: String) async -> Bool {
        await savePlanItem(
            title: title,
            successMessage: String(localized: "Weekly goal added to Plan")
        ) { title in
            _ = try await api.createGoal(
                WeeklyGoalCreateBody(
                    weekStart: ProductDateFormat.weekStart(containing: Date()),
                    title: title
                )
            )
        }
    }

    func clearPlanSaveMessage() {
        planSaveMessage = nil
        planSaveSucceeded = false
    }

    private func savePlanItem(
        title: String,
        successMessage: String,
        operation: (String) async throws -> Void
    ) async -> Bool {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isSavingPlanItem else { return false }
        isSavingPlanItem = true
        planSaveMessage = nil
        planSaveSucceeded = false
        defer { isSavingPlanItem = false }
        do {
            try await operation(trimmed)
            planSaveMessage = successMessage
            planSaveSucceeded = true
            await refresh()
            return true
        } catch {
            planSaveMessage = describe(error, surface: "Plan")
            return false
        }
    }

    private func clear() {
        goals = []
        tasks = []
        events = []
        decisions = []
        weeklyReport = nil
        errorMessages = []
        lastUpdated = nil
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
