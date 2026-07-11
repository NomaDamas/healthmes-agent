import SwiftUI

/// Native rendering of `GET /reports/weekly.json` (issue #10): energy trend,
/// insights with confidence badges, schedule adherence, alert digest and the
/// week's decisions. The HTML page stays one tap away (toolbar) for parity
/// with the Telegram Sunday-briefing link.
struct WeeklyReportView: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model = WeeklyReportModel()

    var body: some View {
        List {
            if let report = model.report {
                energySection(report)
                insightsSection(report)
                adherenceSection(report)
                alertsSection(report)
                decisionsSection(report)
            } else if let error = model.error {
                Label {
                    Text(verbatim: error)
                } icon: {
                    Image(systemName: "wifi.exclamationmark")
                }
                .foregroundStyle(.secondary)
            } else {
                HStack {
                    ProgressView()
                    Text("Loading report…")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(Text("Weekly report"))
        .toolbar {
            if let report = model.report, let url = URL(string: report.reportUrl) {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        router.openDecision(url)
                    } label: {
                        Label("Open web report", systemImage: "safari")
                    }
                    .accessibilityHint(Text("Opens the report page in the in-app viewer"))
                }
            }
        }
        .refreshable { await model.refresh() }
        .task {
            if model.report == nil {
                await model.refresh()
            }
        }
    }

    // MARK: Energy trend

    private func energySection(_ report: WeeklyReport) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text(verbatim: report.energy.overallAvg.map(String.init) ?? "--")
                        .font(.system(.largeTitle, design: .rounded).weight(.bold))
                    Text("week average")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(verbatim: "\(report.weekStart) – \(report.weekEnd)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                WeeklyEnergyBars(days: report.energy.days)
                Text("\(report.energy.samples) hourly samples this week")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        } header: {
            Text("Energy trend")
        }
    }

    // MARK: Insights

    private func insightsSection(_ report: WeeklyReport) -> some View {
        Section {
            if report.insights.items.isEmpty {
                Text("No insights this week")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(report.insights.items) { insight in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(verbatim: insight.statement)
                            .font(.body)
                        HStack {
                            ConfidenceBadge(rawLevel: insight.confidenceLevel.rawValue)
                            if let confidence = insight.confidence {
                                Text(verbatim: String(format: "%.2f", confidence))
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(verbatim: insight.kind)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 2)
                    .accessibilityElement(children: .combine)
                }
            }
        } header: {
            HStack {
                Text("Insights")
                Spacer()
                Text(verbatim: "\(report.insights.count)")
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Schedule adherence

    private func adherenceSection(_ report: WeeklyReport) -> some View {
        Section {
            HStack(spacing: 0) {
                statCell(
                    value: report.schedule.acceptancePct.map { "\($0)%" } ?? "--",
                    label: String(localized: "acceptance")
                )
                statCell(
                    value: String(report.schedule.accepted + report.schedule.pushed),
                    label: String(localized: "applied")
                )
                statCell(
                    value: String(report.schedule.declined),
                    label: String(localized: "declined")
                )
                statCell(
                    value: String(report.schedule.proposed),
                    label: String(localized: "pending")
                )
            }
            .padding(.vertical, 4)
        } header: {
            Text("Schedule adherence")
        } footer: {
            Text("Of \(report.schedule.decided) decided proposals this week.")
        }
    }

    private func statCell(value: String, label: String) -> some View {
        VStack(spacing: 2) {
            Text(verbatim: value)
                .font(.title3.weight(.semibold))
            Text(verbatim: label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    // MARK: Alert digest

    private func alertsSection(_ report: WeeklyReport) -> some View {
        Section {
            LabeledContent {
                Text(verbatim: "\(report.alerts.delivered) / \(report.alerts.fired)")
            } label: {
                Text("Delivered / fired")
            }
            LabeledContent {
                Text(verbatim: "\(report.alerts.delivered) of \(report.alerts.weeklyBudget)")
            } label: {
                Text("Weekly budget used")
            }
            ForEach(report.alerts.byRule, id: \.ruleId) { rule in
                LabeledContent {
                    Text(verbatim: "\(rule.delivered) / \(rule.fired)")
                        .font(.callout.monospacedDigit())
                } label: {
                    Text(verbatim: rule.ruleId)
                        .font(.callout)
                }
            }
        } header: {
            Text("Alert digest")
        } footer: {
            Text("Suppressed firings never notified you; the server enforces quiet hours, cooldowns and the daily budget.")
        }
    }

    // MARK: Decisions

    private func decisionsSection(_ report: WeeklyReport) -> some View {
        Section {
            if report.decisions.items.isEmpty {
                Text("No decisions recorded")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(report.decisions.items) { decision in
                    Button {
                        if let url = URL(string: decision.url) {
                            router.openDecision(url)
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: decision.summary)
                                .font(.body)
                                .foregroundStyle(.primary)
                            HStack {
                                Text(verbatim: decision.kind.rawValue)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Text(decision.createdAt, style: .date)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .accessibilityHint(Text("Opens the decision viewer"))
                }
            }
        } header: {
            HStack {
                Text("Decisions")
                Spacer()
                Text(verbatim: "\(report.decisions.count)")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Seven bars, one per local day; missing days render as hollow stubs
/// (honest nulls). PLACEHOLDER visuals — expert-owned design pending.
struct WeeklyEnergyBars: View {
    let days: [ReportEnergyDay]

    var body: some View {
        HStack(alignment: .bottom, spacing: 6) {
            ForEach(days, id: \.date) { day in
                VStack(spacing: 2) {
                    GeometryReader { geometry in
                        VStack {
                            Spacer(minLength: 0)
                            if let avg = day.avgScore {
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(Color.accentColor.opacity(0.8))
                                    .frame(
                                        height: max(
                                            4,
                                            geometry.size.height * CGFloat(avg) / 100.0
                                        )
                                    )
                            } else {
                                RoundedRectangle(cornerRadius: 3)
                                    .strokeBorder(
                                        Color.secondary.opacity(0.4),
                                        style: StrokeStyle(lineWidth: 1, dash: [3, 2])
                                    )
                                    .frame(height: 12)
                            }
                        }
                    }
                    Text(verbatim: String(day.date.suffix(2)))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(Text(verbatim: day.date))
                .accessibilityValue(
                    Text(
                        verbatim: day.avgScore.map(String.init)
                            ?? String(localized: "no data")
                    )
                )
            }
        }
        .frame(height: 80)
    }
}

@MainActor
final class WeeklyReportModel: ObservableObject {
    @Published var report: WeeklyReport?
    @Published var error: String?

    private let api = HealthMesAPI()

    func refresh() async {
        do {
            report = try await api.weeklyReport()
            error = nil
        } catch let fetchError {
            if report == nil {
                error = BriefingHomeModel.describe(fetchError)
            }
        }
    }
}
