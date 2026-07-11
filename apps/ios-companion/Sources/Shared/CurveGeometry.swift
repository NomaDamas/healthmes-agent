import Foundation

/// Pure geometry over the glance `curve_24h`: normalizes the day into unit
/// space so every Apple-platform surface draws the exact same honest shape —
/// SwiftUI on iOS (home curve), SwiftUI on macOS (menu bar popover +
/// widgets), and AppKit (`NSBezierPath`, screensaver). Lives in the shared
/// layer (Foundation-only) precisely so the gap/dot rules cannot fork per
/// platform; unit-tested by apps/macos-companion/Tests/CurveGeometryTests.swift.
///
/// Honesty rules (information architecture, not styling — the visual pass on
/// top is the domain expert's deliverable, docs/design/WATCH-NOTIFICATIONS.ko.md
/// Q3/Q5):
/// - `null` hours are GAPS — the line never interpolates across missing data.
/// - Only runs of >= 2 consecutive hours become a polyline; a single hour
///   with data renders as an isolated dot, never a fake segment.
/// - x spans the local day (hour 0 -> 0.0, hour 23 -> 1.0), y is score/100
///   with 0 at the bottom (views flip for their coordinate system).
public enum CurveGeometry {
    public struct Point: Equatable {
        public let hour: Int
        /// 0...1 across the 24 local hours.
        public let x: Double
        /// 0...1, score/100, clamped.
        public let y: Double

        public init(hour: Int, x: Double, y: Double) {
            self.hour = hour
            self.x = x
            self.y = y
        }
    }

    public static func xPosition(forHour hour: Int) -> Double {
        Double(min(max(hour, 0), 23)) / 23.0
    }

    static func point(hour: Int, score: Int) -> Point {
        Point(
            hour: hour,
            x: xPosition(forHour: hour),
            y: Double(min(max(score, 0), 100)) / 100.0
        )
    }

    /// Runs of >= 2 consecutive hours that carry a score, hour-ascending.
    public static func segments(_ curve: [GlanceCurvePoint]) -> [[Point]] {
        runs(curve).filter { $0.count >= 2 }
    }

    /// Hours with data whose neighbours are both missing (rendered as dots).
    public static func isolatedPoints(_ curve: [GlanceCurvePoint]) -> [Point] {
        runs(curve).filter { $0.count == 1 }.map { $0[0] }
    }

    /// Count of hours that carry data (accessibility summaries).
    public static func dataHourCount(_ curve: [GlanceCurvePoint]) -> Int {
        curve.filter { $0.score != nil }.count
    }

    /// Maximal runs of consecutive hours with non-null scores.
    private static func runs(_ curve: [GlanceCurvePoint]) -> [[Point]] {
        let scored =
            curve
            .compactMap { entry -> Point? in
                guard let score = entry.score else { return nil }
                return point(hour: entry.hour, score: score)
            }
            .sorted { $0.hour < $1.hour }

        var result: [[Point]] = []
        var run: [Point] = []
        for point in scored {
            if let last = run.last, point.hour == last.hour + 1 {
                run.append(point)
            } else {
                if !run.isEmpty { result.append(run) }
                run = [point]
            }
        }
        if !run.isEmpty { result.append(run) }
        return result
    }
}
