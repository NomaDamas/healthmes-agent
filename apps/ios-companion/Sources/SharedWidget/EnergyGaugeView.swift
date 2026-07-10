import SwiftUI

/// Circular energy gauge used by the iOS lock-screen widget and the watch
/// complication. Deliberately minimal placeholder rendering — the final
/// glance design belongs to the healthcare domain expert
/// (docs/design/WATCH-NOTIFICATIONS.ko.md; docs/PLAN.md §8.5).
public struct EnergyGaugeView: View {
    public let score: Int?

    public init(score: Int?) {
        self.score = score
    }

    public var body: some View {
        Gauge(value: Double(score ?? 0), in: 0...100) {
            Text("HM")
        } currentValueLabel: {
            Text(GlanceFormat.scoreText(score))
        }
        .gaugeStyle(.accessoryCircular)
    }
}
