import Foundation

// Codable contract for `GET /reports/weekly.json` (healthmes/api/reports.py
// WeeklyReportOut). Field names pinned verbatim; Tests/Fixtures/
// weekly_report.json holds a reference payload validated against the
// server's pydantic model. Plain-date fields (`week_start`, day `date`) stay
// `String` ("YYYY-MM-DD") — they are local calendar dates, not instants, and
// must never be shifted through a timezone conversion.

public struct WeeklyReport: Codable, Equatable {
    public let generatedAt: Date
    public let timezone: String
    public let weekStart: String
    public let weekEnd: String
    /// Browser-tappable report page (read-only viewer token embedded
    /// server-side when the instance is token-gated).
    public let reportUrl: String
    public let energy: ReportEnergy
    public let insights: ReportInsights
    public let schedule: ReportScheduleAdherence
    public let alerts: ReportAlertDigest
    public let decisions: ReportDecisions

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case timezone
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case reportUrl = "report_url"
        case energy
        case insights
        case schedule
        case alerts
        case decisions
    }
}

/// Per-day energy aggregates; `null` scores are honestly-missing days.
public struct ReportEnergy: Codable, Equatable {
    public let days: [ReportEnergyDay]
    public let overallAvg: Int?
    public let samples: Int

    enum CodingKeys: String, CodingKey {
        case days
        case overallAvg = "overall_avg"
        case samples
    }
}

public struct ReportEnergyDay: Codable, Equatable {
    public let date: String
    public let avgScore: Int?
    public let minScore: Int?
    public let maxScore: Int?
    public let samples: Int

    enum CodingKeys: String, CodingKey {
        case date
        case avgScore = "avg_score"
        case minScore = "min_score"
        case maxScore = "max_score"
        case samples
    }
}

public struct ReportInsights: Codable, Equatable {
    /// Full-week total; `items` is capped server-side.
    public let count: Int
    public let items: [ReportInsight]
}

public enum ReportConfidenceLevel: String, Codable {
    case high
    case medium
    case low
    case none
}

public struct ReportInsight: Codable, Equatable, Identifiable {
    public let id: UUID
    public let period: String
    public let kind: String
    public let statement: String
    public let confidence: Double?
    public let confidenceLevel: ReportConfidenceLevel
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id
        case period
        case kind
        case statement
        case confidence
        case confidenceLevel = "confidence_level"
        case createdAt = "created_at"
    }
}

public struct ReportScheduleAdherence: Codable, Equatable {
    public let proposed: Int
    public let accepted: Int
    public let pushed: Int
    public let declined: Int
    /// accepted + pushed + declined (`proposed` is still pending).
    public let decided: Int
    /// round(100 * (accepted + pushed) / decided); null when nothing decided.
    public let acceptancePct: Int?

    enum CodingKeys: String, CodingKey {
        case proposed
        case accepted
        case pushed
        case declined
        case decided
        case acceptancePct = "acceptance_pct"
    }
}

public struct ReportAlertDigest: Codable, Equatable {
    public let fired: Int
    public let delivered: Int
    public let dailyBudget: Int
    public let weeklyBudget: Int
    public let byRule: [ReportAlertRuleCount]

    enum CodingKeys: String, CodingKey {
        case fired
        case delivered
        case dailyBudget = "daily_budget"
        case weeklyBudget = "weekly_budget"
        case byRule = "by_rule"
    }
}

public struct ReportAlertRuleCount: Codable, Equatable {
    public let ruleId: String
    public let fired: Int
    public let delivered: Int

    enum CodingKeys: String, CodingKey {
        case ruleId = "rule_id"
        case fired
        case delivered
    }
}

/// Mirror of healthmes.store.enums.DecisionKind.
public enum ReportDecisionKind: String, Codable {
    case scheduleChange = "schedule_change"
    case alert
    case insight
    case capture
}

public struct ReportDecisions: Codable, Equatable {
    public let count: Int
    public let kindCounts: [String: Int]
    public let items: [ReportDecision]

    enum CodingKeys: String, CodingKey {
        case count
        case kindCounts = "kind_counts"
        case items
    }
}

public struct ReportDecision: Codable, Equatable, Identifiable {
    public let id: UUID
    public let kind: ReportDecisionKind
    public let summary: String
    public let createdAt: Date
    public let url: String

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case summary
        case createdAt = "created_at"
        case url
    }
}
