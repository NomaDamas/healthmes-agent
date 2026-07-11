import SwiftUI

/// Hand-drawn 24-hour energy curve (no external dependencies). The honesty
/// semantics — null hours are GAPS (never interpolated), a single-hour run
/// renders as a dot, only runs >= 2 become a polyline — come from the shared
/// `CurveGeometry` (Sources/Shared), the same unit-tested geometry the macOS
/// popover, widgets and screensaver draw; this view only maps its unit space
/// to pixels and adds the current-hour marker.
///
/// PLACEHOLDER VISUALS: colors/stroke styling are engineering placeholders;
/// the glance design language (state words vs numbers, low-confidence
/// blurring, color grading) is the domain expert's deliverable —
/// docs/design/WATCH-NOTIFICATIONS.ko.md Q3/Q5.
struct EnergyCurveView: View {
    let curve: [GlanceCurvePoint]
    let timezone: String

    private var currentHour: Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: timezone) ?? .current
        return calendar.component(.hour, from: Date())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            GeometryReader { geometry in
                let size = geometry.size
                ZStack {
                    gridLines(in: size)
                    curveSegments(in: size)
                    currentHourMarker(in: size)
                }
            }
            .frame(height: 96)
            hourAxis
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("Energy curve for today"))
        .accessibilityValue(Text(verbatim: accessibilitySummary))
    }

    // MARK: Drawing

    /// Unit-space point (CurveGeometry) -> pixel position (y grows downward).
    private func position(of point: CurveGeometry.Point, in size: CGSize) -> CGPoint {
        CGPoint(x: size.width * CGFloat(point.x), y: size.height * (1 - CGFloat(point.y)))
    }

    private func x(for hour: Int, in size: CGSize) -> CGFloat {
        size.width * CGFloat(CurveGeometry.xPosition(forHour: hour))
    }

    private func y(for score: Int, in size: CGSize) -> CGFloat {
        size.height * (1 - CGFloat(min(max(score, 0), 100)) / 100.0)
    }

    private func gridLines(in size: CGSize) -> some View {
        Path { path in
            for score in [0, 50, 100] {
                let lineY = y(for: score, in: size)
                path.move(to: CGPoint(x: 0, y: lineY))
                path.addLine(to: CGPoint(x: size.width, y: lineY))
            }
        }
        .stroke(Color.secondary.opacity(0.25), style: StrokeStyle(lineWidth: 0.5, dash: [3, 3]))
    }

    /// Shared-geometry rendering: polylines for runs >= 2, dots for isolated
    /// hours, nothing at all across null gaps.
    private func curveSegments(in size: CGSize) -> some View {
        ZStack {
            ForEach(Array(CurveGeometry.segments(curve).enumerated()), id: \.offset) { _, run in
                Path { path in
                    for (index, point) in run.enumerated() {
                        let position = position(of: point, in: size)
                        if index == 0 {
                            path.move(to: position)
                        } else {
                            path.addLine(to: position)
                        }
                    }
                }
                .stroke(
                    Color.accentColor,
                    style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round)
                )
            }
            ForEach(
                Array(CurveGeometry.isolatedPoints(curve).enumerated()), id: \.offset
            ) { _, point in
                Circle()
                    .fill(Color.accentColor)
                    .frame(width: 4, height: 4)
                    .position(position(of: point, in: size))
            }
        }
    }

    @ViewBuilder
    private func currentHourMarker(in size: CGSize) -> some View {
        let hour = currentHour
        Path { path in
            path.move(to: CGPoint(x: x(for: hour, in: size), y: 0))
            path.addLine(to: CGPoint(x: x(for: hour, in: size), y: size.height))
        }
        .stroke(Color.secondary.opacity(0.6), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
        if let score = curve.first(where: { $0.hour == hour })?.score {
            Circle()
                .fill(Color.accentColor)
                .frame(width: 8, height: 8)
                .position(x: x(for: hour, in: size), y: y(for: score, in: size))
        }
    }

    private var hourAxis: some View {
        HStack {
            Text(verbatim: "0")
            Spacer()
            Text(verbatim: "6")
            Spacer()
            Text(verbatim: "12")
            Spacer()
            Text(verbatim: "18")
            Spacer()
            Text(verbatim: "23")
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    /// VoiceOver reads real numbers, not shapes: "8h 71 … 14h 58; no data
    /// for the other hours."
    private var accessibilitySummary: String {
        let known = curve.compactMap { point in
            point.score.map { "\(point.hour)h \($0)" }
        }
        if known.isEmpty {
            return String(localized: "No energy data recorded today")
        }
        return known.joined(separator: ", ")
    }
}
