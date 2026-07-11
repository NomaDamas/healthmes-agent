import Foundation

/// Pure render model for the screensaver: everything the ambient briefing
/// shows, computed off the glance payload with the issue-#11 PRIVACY TOGGLE
/// applied *here* — so "hide health numbers" is a testable data rule, not a
/// drawing detail.
///
/// Privacy rule (placeholder policy, documented for the domain expert —
/// docs/design/WATCH-NOTIFICATIONS.ko.md): when `hideNumbers` is on, every
/// health-derived value disappears entirely (score, confidence, curve,
/// energy demand, alert count, alert summary). Schedule facts (next block
/// time/title) stay: they are calendar data, not health numbers, and keep
/// the saver useful in a shared space. Nothing is blurred or abbreviated —
/// hidden means absent.
public struct SaverBriefing: Equatable {
    public enum State: Equatable {
        /// No instance paired — the saver renders how to pair, never a blank.
        case notPaired
        /// Paired but no snapshot cached yet (menu bar app has not fetched).
        case noData
        case briefing(Content)
    }

    public struct Content: Equatable {
        /// "58" / "--"; nil when privacy hides numbers.
        public let scoreText: String?
        /// "high"|"medium"|"low"; nil when privacy hides numbers.
        public let confidenceRaw: String?
        /// The 24h curve; nil when privacy hides numbers.
        public let curve: [GlanceCurvePoint]?
        /// Current hour in the *server's* user timezone (curve marker).
        public let currentHour: Int?
        /// "14:00" in the server's user timezone (kept under privacy).
        public let nextBlockTimeText: String?
        /// Block title, nil when the block is untitled (kept under privacy).
        public let nextBlockTitle: String?
        /// True when the glance carried at least one upcoming block.
        public let hasNextBlock: Bool
        /// "low"|"med"|"high"; nil when absent or privacy-hidden.
        public let nextBlockDemandRaw: String?
        /// Recent pushed alerts; nil when privacy hides numbers.
        public let alertCount: Int?
        /// Top alert observation line; nil when privacy hides numbers.
        public let topAlertSummary: String?
        /// Whole minutes since the snapshot was fetched (staleness honesty).
        public let updatedMinutesAgo: Int?
        /// True when values were removed by the privacy toggle.
        public let numbersHidden: Bool
    }

    public let state: State

    public init(state: State) {
        self.state = state
    }

    public static func make(
        payload: GlancePayload?,
        fetchedAt: Date?,
        isPaired: Bool,
        hideNumbers: Bool,
        now: Date = Date()
    ) -> SaverBriefing {
        guard isPaired else { return SaverBriefing(state: .notPaired) }
        guard let payload else { return SaverBriefing(state: .noData) }

        let timezone = TimeZone(identifier: payload.timezone) ?? .current
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        formatter.timeZone = timezone

        let block = payload.nextBlocks.first

        let content = Content(
            scoreText: hideNumbers ? nil : GlanceFormat.scoreText(payload.energy.score),
            confidenceRaw: hideNumbers ? nil : payload.energy.confidence.rawValue,
            curve: hideNumbers ? nil : payload.energy.curve24h,
            currentHour: hideNumbers ? nil : calendar.component(.hour, from: now),
            nextBlockTimeText: block.map { formatter.string(from: $0.start) },
            nextBlockTitle: block?.title,
            hasNextBlock: block != nil,
            nextBlockDemandRaw: hideNumbers ? nil : block?.energyDemand?.rawValue,
            alertCount: hideNumbers ? nil : payload.alerts.unresolvedCount,
            topAlertSummary: hideNumbers ? nil : payload.alerts.top?.summary,
            updatedMinutesAgo: fetchedAt.map { max(0, Int(now.timeIntervalSince($0) / 60)) },
            numbersHidden: hideNumbers
        )
        return SaverBriefing(state: .briefing(content))
    }
}
