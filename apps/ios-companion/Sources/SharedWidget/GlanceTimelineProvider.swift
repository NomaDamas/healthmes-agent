import Foundation
import WidgetKit

/// Timeline entry rendered by every HealthMes widget/complication.
public struct GlanceEntry: TimelineEntry {
    public let date: Date
    public let state: GlanceEntryState

    public init(date: Date, state: GlanceEntryState) {
        self.date = date
        self.state = state
    }

    public var isPlaceholder: Bool {
        if case .placeholder = state { return true }
        return false
    }

    /// Payload to render, when any exists (placeholder sample included).
    public var payload: GlancePayload? {
        switch state {
        case .placeholder(let payload): return payload
        case .snapshot(let payload, _): return payload
        case .notPaired, .unavailable: return nil
        }
    }
}

public enum GlanceEntryState {
    /// Widget-gallery/redaction sample — never real data.
    case placeholder(GlancePayload)
    /// Honest empty state: no instance paired yet (open the iPhone app).
    case notPaired
    /// Honest empty state: paired but no payload and no cache (reason is a
    /// short human string, e.g. "Token rejected" / "No data").
    case unavailable(reason: String)
    /// Real data; `stale` means it came from the on-device cache because the
    /// instance was unreachable on the last poll.
    case snapshot(GlancePayload, stale: Bool)
}

/// One provider for every family on both platforms.
///
/// Refresh policy = the endpoint's caching contract, adapted to WidgetKit's
/// refresh budget (roughly 40-70 timeline reloads/day):
/// - never poll before the server's `Cache-Control: max-age` expires,
/// - never ask WidgetKit for more than one reload per 15 minutes even
///   though the server would allow 5 (budget-friendliness),
/// - every poll sends `If-None-Match`, so an unchanged briefing costs a
///   bodyless 304 (see GlanceClient).
public struct GlanceTimelineProvider: TimelineProvider {
    public typealias Entry = GlanceEntry

    static let minimumRefreshInterval: TimeInterval = 15 * 60
    static let staleRetryInterval: TimeInterval = 15 * 60
    static let failureRetryInterval: TimeInterval = 30 * 60
    static let notPairedRetryInterval: TimeInterval = 60 * 60

    public init() {}

    public func placeholder(in context: Context) -> GlanceEntry {
        GlanceEntry(date: Date(), state: .placeholder(.sample()))
    }

    public func getSnapshot(in context: Context, completion: @escaping (GlanceEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task {
            let (entry, _) = await Self.loadEntry()
            completion(entry)
        }
    }

    public func getTimeline(
        in context: Context,
        completion: @escaping (Timeline<GlanceEntry>) -> Void
    ) {
        Task {
            let (entry, refresh) = await Self.loadEntry()
            completion(Timeline(entries: [entry], policy: .after(refresh)))
        }
    }

    static func loadEntry(now: Date = Date()) async -> (GlanceEntry, Date) {
        guard let pairing = PairingStore.shared.load() else {
            return (
                GlanceEntry(date: now, state: .notPaired),
                now.addingTimeInterval(notPairedRetryInterval)
            )
        }
        let client = GlanceClient()
        do {
            let snapshot = try await client.fetch(pairing: pairing, now: now)
            let refresh = max(
                snapshot.nextRefresh,
                now.addingTimeInterval(minimumRefreshInterval)
            )
            return (
                GlanceEntry(date: now, state: .snapshot(snapshot.payload, stale: false)),
                refresh
            )
        } catch {
            // Unreachable instance: honest stale rendering beats a blank.
            if let cached = client.cache.decodedPayload() {
                return (
                    GlanceEntry(date: now, state: .snapshot(cached, stale: true)),
                    now.addingTimeInterval(staleRetryInterval)
                )
            }
            let reason: String
            if case GlanceClientError.unauthorized = error {
                reason = "Token rejected"
            } else {
                reason = "No data"
            }
            return (
                GlanceEntry(date: now, state: .unavailable(reason: reason)),
                now.addingTimeInterval(failureRetryInterval)
            )
        }
    }
}

extension GlancePayload {
    /// Static sample for widget-gallery previews and redacted placeholders.
    public static func sample(now: Date = Date()) -> GlancePayload {
        GlancePayload(
            generatedAt: now,
            timezone: TimeZone.current.identifier,
            energy: GlanceEnergy(
                score: 62,
                confidence: .medium,
                curve24h: (0..<24).map { hour in
                    GlanceCurvePoint(
                        hour: hour,
                        score: (7...14).contains(hour) ? 55 + (hour % 3) * 5 : nil
                    )
                }
            ),
            nextBlocks: [
                GlanceBlock(
                    start: now.addingTimeInterval(30 * 60),
                    end: now.addingTimeInterval(90 * 60),
                    title: "Deep work",
                    energyDemand: .high,
                    source: .calendar
                )
            ],
            alerts: GlanceAlerts(
                unresolvedCount: 1,
                top: GlanceTopAlert(
                    ruleId: "sample_rule",
                    summary: "Sample alert",
                    decisionUrl: nil
                )
            ),
            latestDecision: nil
        )
    }
}
