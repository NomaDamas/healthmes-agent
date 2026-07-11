import SwiftUI
import WidgetKit

/// macOS desktop / Notification Center widgets (issue #11), fed by the SAME
/// shared GlanceTimelineProvider as the iOS/watchOS widgets: server
/// Cache-Control drives the timeline, polls are conditional GETs (ETag/304),
/// unreachable instances render the last cache honestly marked stale.
@main
struct HealthMesMacWidgetBundle: WidgetBundle {
    var body: some Widget {
        HealthMesMacGlanceWidget()
    }
}

struct HealthMesMacGlanceWidget: Widget {
    let kind = "HealthMesMacGlance"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GlanceTimelineProvider()) { entry in
            MacGlanceWidgetView(entry: entry)
                .containerBackground(.background, for: .widget)
        }
        .configurationDisplayName(String(localized: "widget.name"))
        .description(String(localized: "widget.description"))
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

/// PLACEHOLDER VISUALS (docs/design/WATCH-NOTIFICATIONS.ko.md — slot table
/// §2.2 is the start point, the expert owns the final mapping). Information
/// slots per family follow the worksheet's placeholder mapping:
/// small = score + confidence + next block; medium = + sparkline + alerts;
/// large = + full curve, 3 blocks, top-alert line, decision link.
struct MacGlanceWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: GlanceEntry

    var body: some View {
        switch entry.state {
        case .notPaired:
            emptyState(icon: "link.badge.plus", textKey: "widget.notPaired")
        case .unavailable(let reason):
            VStack(spacing: 4) {
                Image(systemName: "wifi.slash").foregroundStyle(.secondary)
                // Short provider-supplied reason ("Token rejected"/"No data").
                Text(verbatim: reason).font(.caption).foregroundStyle(.secondary)
            }
        case .placeholder(let payload):
            briefing(payload, stale: false)
        case .snapshot(let payload, let stale):
            briefing(payload, stale: stale)
        }
    }

    private func emptyState(icon: String, textKey: LocalizedStringKey) -> some View {
        VStack(spacing: 4) {
            Image(systemName: icon).foregroundStyle(.secondary)
            Text(textKey)
                .font(.caption)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func briefing(_ payload: GlancePayload, stale: Bool) -> some View {
        switch family {
        case .systemSmall:
            smallView(payload, stale: stale)
        case .systemLarge:
            largeView(payload, stale: stale)
        default:
            mediumView(payload, stale: stale)
        }
    }

    // MARK: - Families

    private func smallView(_ payload: GlancePayload, stale: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            header(payload, stale: stale)
            Spacer(minLength: 0)
            if let blockLine = GlanceFormat.nextBlockLine(payload) {
                Text(verbatim: blockLine)
                    .font(.caption)
                    .lineLimit(2)
                    .foregroundStyle(.secondary)
            } else {
                Text("blocks.empty")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func mediumView(_ payload: GlancePayload, stale: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                header(payload, stale: stale)
                Spacer(minLength: 0)
                Text(verbatim: GlanceFormat.alertsLine(payload))
                    .font(.caption)
                    .lineLimit(2)
                    .foregroundStyle(payload.alerts.unresolvedCount > 0 ? .primary : .secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                MacEnergyCurveView(
                    curve: payload.energy.curve24h,
                    currentHour: currentHour(in: payload.timezone)
                )
                .frame(height: 44)
                if let blockLine = GlanceFormat.nextBlockLine(payload) {
                    Text(verbatim: blockLine)
                        .font(.caption)
                        .lineLimit(1)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func largeView(_ payload: GlancePayload, stale: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            header(payload, stale: stale)
            MacEnergyCurveView(
                curve: payload.energy.curve24h,
                currentHour: currentHour(in: payload.timezone)
            )
            .frame(height: 70)
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(payload.nextBlocks.prefix(3).enumerated()), id: \.offset) { _, block in
                    Text(verbatim: GlanceFormat.blockLine(block, timezone: payload.timezone))
                        .font(.caption)
                        .lineLimit(1)
                }
                if payload.nextBlocks.isEmpty {
                    Text("blocks.empty").font(.caption).foregroundStyle(.tertiary)
                }
            }
            Divider()
            alertFooter(payload)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    private func alertFooter(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(verbatim: GlanceFormat.alertsLine(payload))
                .font(.caption)
                .lineLimit(2)
            // "Why this?" never disappears from a tappable surface
            // (WATCH-NOTIFICATIONS.ko.md §1.1) — widget Links open the browser.
            if let top = payload.alerts.top,
                let urlString = top.decisionUrl,
                let url = URL(string: urlString)
            {
                Link("alert.why", destination: url)
                    .font(.caption2)
            }
        }
    }

    private func header(_ payload: GlancePayload, stale: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(verbatim: GlanceFormat.scoreText(payload.energy.score))
                .font(.system(size: 30, weight: .bold, design: .rounded))
            VStack(alignment: .leading, spacing: 0) {
                Text(verbatim: confidenceShort(payload.energy.confidence))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                if stale {
                    Text("widget.stale")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
            Spacer(minLength: 0)
            if payload.alerts.unresolvedCount > 0 {
                Text(verbatim: "\(payload.alerts.unresolvedCount)")
                    .font(.caption2.bold())
                    .padding(4)
                    .background(.red.opacity(0.2), in: Circle())
                    .accessibilityLabel(Text("alerts.count \(payload.alerts.unresolvedCount)"))
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            Text("menubar.a11y.energy \(GlanceFormat.scoreText(payload.energy.score)) \(confidenceShort(payload.energy.confidence))")
        )
    }

    private func confidenceShort(_ confidence: GlanceConfidence) -> String {
        switch confidence {
        case .high: return String(localized: "confidence.high")
        case .medium: return String(localized: "confidence.medium")
        case .low: return String(localized: "confidence.low")
        }
    }

    private func currentHour(in timezone: String) -> Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }
}
