import SwiftUI

/// 24h energy curve for the menu bar popover and the desktop widgets.
///
/// PLACEHOLDER VISUAL (docs/design/WATCH-NOTIFICATIONS.ko.md; grammar
/// docs/PLAN.md §8.5): stroke widths, colors and dot radius are engineering
/// placeholders. The *information architecture* is real and pinned by
/// CurveGeometry: null hours are gaps (never interpolated), single-hour data
/// renders as a dot, and the current local hour carries a marker.
public struct MacEnergyCurveView: View {
    public let curve: [GlanceCurvePoint]
    /// Current hour in the server's user timezone; nil hides the marker.
    public let currentHour: Int?

    public init(curve: [GlanceCurvePoint], currentHour: Int?) {
        self.curve = curve
        self.currentHour = currentHour
    }

    public var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let height = geo.size.height
            let segments = CurveGeometry.segments(curve)
            let dots = CurveGeometry.isolatedPoints(curve)

            ZStack(alignment: .topLeading) {
                // Baseline (score 0) — orientation cue, not data.
                Path { path in
                    path.move(to: CGPoint(x: 0, y: height - 0.5))
                    path.addLine(to: CGPoint(x: width, y: height - 0.5))
                }
                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)

                // Current-hour marker.
                if let currentHour {
                    let x = CurveGeometry.xPosition(forHour: currentHour) * width
                    Path { path in
                        path.move(to: CGPoint(x: x, y: 0))
                        path.addLine(to: CGPoint(x: x, y: height))
                    }
                    .stroke(Color.secondary.opacity(0.45), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                }

                // Polyline runs — only across consecutive hours with data.
                ForEach(segments.indices, id: \.self) { index in
                    Path { path in
                        let points = segments[index]
                        guard let first = points.first else { return }
                        path.move(to: cgPoint(first, width: width, height: height))
                        for point in points.dropFirst() {
                            path.addLine(to: cgPoint(point, width: width, height: height))
                        }
                    }
                    .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                }

                // Isolated hours: honest dots instead of invented lines.
                ForEach(dots.indices, id: \.self) { index in
                    let point = cgPoint(dots[index], width: width, height: height)
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 5, height: 5)
                        .position(point)
                }

                if segments.isEmpty && dots.isEmpty {
                    Text("energy.noScore")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: width, height: height)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("a11y.curve \(CurveGeometry.dataHourCount(curve))"))
    }

    private func cgPoint(_ point: CurveGeometry.Point, width: CGFloat, height: CGFloat) -> CGPoint {
        CGPoint(x: point.x * width, y: (1 - point.y) * height)
    }
}
