import SwiftUI
import WidgetKit

// iOS home-screen (systemSmall/systemMedium/systemLarge) and lock-screen
// (accessoryCircular/accessoryRectangular/accessoryInline) widgets over
// GET /v1/briefing/glance.
//
// NOTE (issue #7): rendering here is deliberately minimal PLACEHOLDER
// plumbing over the stable glance contract. The actual glanceable UX —
// what deserves lock-screen space, wording, urgency — is healthcare-domain
// design reserved for the domain expert: docs/design/WATCH-NOTIFICATIONS.ko.md
// (design system: docs/PLAN.md §8.5 notification grammar).

@main
struct HealthMesWidgetBundle: WidgetBundle {
    var body: some Widget {
        GlanceWidget()
        #if canImport(ActivityKit)
            FocusBlockLiveActivity()
        #endif
    }
}

struct GlanceWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: "HealthMesGlance",
            provider: GlanceTimelineProvider()
        ) { entry in
            GlanceWidgetEntryView(entry: entry)
        }
        .configurationDisplayName("HealthMes glance")
        .description("Energy, next block and alerts from your own HealthMes instance.")
        .supportedFamilies([
            .systemSmall,
            .systemMedium,
            .systemLarge,
            .accessoryCircular,
            .accessoryRectangular,
            .accessoryInline,
        ])
    }
}

struct GlanceWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: GlanceEntry

    var body: some View {
        switch family {
        case .accessoryCircular, .accessoryRectangular, .accessoryInline:
            accessoryBody
                .redacted(reason: entry.isPlaceholder ? .placeholder : [])
                .containerBackground(for: .widget) { Color.clear }
        default:
            homeBody
                .redacted(reason: entry.isPlaceholder ? .placeholder : [])
                .containerBackground(.fill.tertiary, for: .widget)
        }
    }

    // MARK: Lock-screen accessory families

    @ViewBuilder
    private var accessoryBody: some View {
        switch entry.state {
        case .notPaired:
            accessoryFallback(short: "HM", long: "HealthMes: not paired")
        case .unavailable(let reason):
            accessoryFallback(short: "--", long: "HealthMes: \(reason)")
        case .placeholder(let payload), .snapshot(let payload, _):
            accessoryContent(payload)
        }
    }

    @ViewBuilder
    private func accessoryContent(_ payload: GlancePayload) -> some View {
        switch family {
        case .accessoryCircular:
            EnergyGaugeView(score: payload.energy.score)
        case .accessoryInline:
            Text(GlanceFormat.inlineLine(payload))
        default:  // .accessoryRectangular
            VStack(alignment: .leading, spacing: 1) {
                Text(GlanceFormat.energyLine(payload) + staleSuffix)
                    .font(.headline)
                Text(GlanceFormat.nextBlockLine(payload) ?? "No upcoming blocks")
                    .font(.caption2)
                Text(GlanceFormat.alertsLine(payload))
                    .font(.caption2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func accessoryFallback(short: String, long: String) -> some View {
        switch family {
        case .accessoryCircular:
            VStack(spacing: 0) {
                Text(short).font(.headline)
                Text("pair").font(.caption2)
            }
        case .accessoryInline:
            Text(long)
        default:
            Text(long).font(.caption)
        }
    }

    // MARK: Home-screen families

    @ViewBuilder
    private var homeBody: some View {
        switch entry.state {
        case .notPaired:
            emptyHome(
                title: "Not paired",
                detail: "Open HealthMes and save your instance URL + token."
            )
        case .unavailable(let reason):
            emptyHome(title: reason, detail: "Last poll failed and no cached briefing exists.")
        case .placeholder(let payload), .snapshot(let payload, _):
            switch family {
            case .systemLarge:
                largeHome(payload)
            case .systemMedium:
                mediumHome(payload)
            default:
                smallHome(payload)
            }
        }
    }

    private func emptyHome(title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("HEALTHMES").font(.caption2).foregroundStyle(.secondary)
            Text(title).font(.headline)
            Text(detail).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func smallHome(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("HEALTHMES").font(.caption2).foregroundStyle(.secondary)
            Text(GlanceFormat.scoreText(payload.energy.score))
                .font(.system(size: 40, weight: .bold, design: .rounded))
            Text("energy · \(payload.energy.confidence.rawValue)\(staleSuffix)")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            Text(GlanceFormat.alertsLine(payload))
                .font(.caption2)
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func mediumHome(_ payload: GlancePayload) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("HEALTHMES").font(.caption2).foregroundStyle(.secondary)
                Text(GlanceFormat.scoreText(payload.energy.score))
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                Text("energy · \(payload.energy.confidence.rawValue)\(staleSuffix)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(GlanceFormat.nextBlockLine(payload) ?? "No upcoming blocks")
                    .font(.footnote)
                    .lineLimit(2)
                Text(GlanceFormat.alertsLine(payload))
                    .font(.caption2)
                    .lineLimit(2)
                Spacer(minLength: 0)
                Text("updated \(entry.date, style: .time)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// systemLarge: the medium header plus *all* payload blocks (≤3) and the
    /// alert digest — still placeholder rendering over the same contract.
    private func largeHome(_ payload: GlancePayload) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("HEALTHMES").font(.caption2).foregroundStyle(.secondary)
                    Text(GlanceFormat.scoreText(payload.energy.score))
                        .font(.system(size: 44, weight: .bold, design: .rounded))
                    Text("energy · \(payload.energy.confidence.rawValue)\(staleSuffix)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Text("updated \(entry.date, style: .time)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Divider()
            Text("NEXT BLOCKS").font(.caption2).foregroundStyle(.secondary)
            if payload.nextBlocks.isEmpty {
                Text("No upcoming blocks").font(.footnote).foregroundStyle(.secondary)
            } else {
                ForEach(Array(payload.nextBlocks.enumerated()), id: \.offset) { _, block in
                    Text(GlanceFormat.blockLine(block, timezone: payload.timezone))
                        .font(.footnote)
                        .lineLimit(1)
                }
            }
            Divider()
            Text("ALERTS").font(.caption2).foregroundStyle(.secondary)
            Text(GlanceFormat.alertsLine(payload))
                .font(.footnote)
                .lineLimit(3)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var staleSuffix: String {
        if case .snapshot(_, let stale) = entry.state, stale { return " · cached" }
        return ""
    }
}
