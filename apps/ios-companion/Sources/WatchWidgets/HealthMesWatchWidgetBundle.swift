import SwiftUI
import WidgetKit

// watchOS complications (WidgetKit accessory families) over
// GET /v1/briefing/glance — energy score first, per issue #7.
//
// NOTE (issue #7): this is deliberately minimal PLACEHOLDER rendering.
// The WATCH NOTIFICATION / complication UX itself — what the wrist should
// surface, when, and how loudly — is reserved for the healthcare domain
// expert: docs/design/WATCH-NOTIFICATIONS.ko.md (design system:
// docs/PLAN.md §8.5 notification grammar). Change wording/urgency there
// first, then reflect it here.

@main
struct HealthMesWatchWidgetBundle: WidgetBundle {
    var body: some Widget {
        EnergyComplicationWidget()
    }
}

struct EnergyComplicationWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: "HealthMesEnergyComplication",
            provider: GlanceTimelineProvider()
        ) { entry in
            EnergyComplicationView(entry: entry)
                .containerBackground(for: .widget) { Color.clear }
        }
        .configurationDisplayName("HealthMes energy")
        .description("Cognitive-energy score from your HealthMes instance.")
        .supportedFamilies([
            .accessoryCircular,
            .accessoryCorner,
            .accessoryRectangular,
            .accessoryInline,
        ])
    }
}

struct EnergyComplicationView: View {
    @Environment(\.widgetFamily) private var family
    let entry: GlanceEntry

    var body: some View {
        content
            .redacted(reason: entry.isPlaceholder ? .placeholder : [])
    }

    @ViewBuilder
    private var content: some View {
        switch family {
        case .accessoryCorner:
            Text(GlanceFormat.scoreText(entry.payload?.energy.score))
                .font(.title3.bold())
                .widgetLabel { Text(cornerLabel) }
        case .accessoryInline:
            Text(inlineLabel)
        case .accessoryRectangular:
            rectangular
        default:  // .accessoryCircular
            EnergyGaugeView(score: entry.payload?.energy.score)
        }
    }

    @ViewBuilder
    private var rectangular: some View {
        if let payload = entry.payload {
            VStack(alignment: .leading, spacing: 1) {
                Text(GlanceFormat.energyLine(payload)).font(.headline)
                Text(GlanceFormat.nextBlockLine(payload) ?? "No upcoming blocks")
                    .font(.caption2)
                Text(GlanceFormat.alertsLine(payload)).font(.caption2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            Text(fallbackLabel).font(.caption)
        }
    }

    private var cornerLabel: String {
        switch entry.state {
        case .notPaired: return "pair on iPhone"
        case .unavailable: return "no data"
        default: return "energy"
        }
    }

    private var inlineLabel: String {
        if let payload = entry.payload { return GlanceFormat.inlineLine(payload) }
        return fallbackLabel
    }

    private var fallbackLabel: String {
        switch entry.state {
        case .notPaired: return "HM: not paired"
        case .unavailable(let reason): return "HM: \(reason)"
        default: return "HM --"
        }
    }
}
