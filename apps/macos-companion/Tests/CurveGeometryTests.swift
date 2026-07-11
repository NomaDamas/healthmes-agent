import XCTest

final class CurveGeometryTests: XCTestCase {
    private func curve(_ scores: [Int: Int]) -> [GlanceCurvePoint] {
        (0..<24).map { GlanceCurvePoint(hour: $0, score: scores[$0]) }
    }

    func testNullHoursSplitTheCurveIntoHonestRuns() {
        // Fixture shape: hour 8 alone, hours 13-14 consecutive.
        let points = curve([8: 71, 13: 64, 14: 58])

        let segments = CurveGeometry.segments(points)
        XCTAssertEqual(segments.count, 1)
        XCTAssertEqual(segments[0].map(\.hour), [13, 14])

        let isolated = CurveGeometry.isolatedPoints(points)
        XCTAssertEqual(isolated.map(\.hour), [8])

        XCTAssertEqual(CurveGeometry.dataHourCount(points), 3)
    }

    func testAllNullCurveRendersNothing() {
        let points = curve([:])
        XCTAssertTrue(CurveGeometry.segments(points).isEmpty)
        XCTAssertTrue(CurveGeometry.isolatedPoints(points).isEmpty)
        XCTAssertEqual(CurveGeometry.dataHourCount(points), 0)
    }

    func testFullDayIsOneSegment() {
        let points = curve(Dictionary(uniqueKeysWithValues: (0..<24).map { ($0, 50) }))
        let segments = CurveGeometry.segments(points)
        XCTAssertEqual(segments.count, 1)
        XCTAssertEqual(segments[0].count, 24)
        XCTAssertTrue(CurveGeometry.isolatedPoints(points).isEmpty)
    }

    func testUnitSpaceMapping() {
        let points = curve([8: 71])
        let dot = CurveGeometry.isolatedPoints(points)[0]
        XCTAssertEqual(dot.x, 8.0 / 23.0, accuracy: 0.0001)
        XCTAssertEqual(dot.y, 0.71, accuracy: 0.0001)

        XCTAssertEqual(CurveGeometry.xPosition(forHour: 0), 0)
        XCTAssertEqual(CurveGeometry.xPosition(forHour: 23), 1)
        // Out-of-range hours clamp instead of drawing off-canvas.
        XCTAssertEqual(CurveGeometry.xPosition(forHour: 99), 1)
    }

    func testScoresClampToTheGaugeRange() {
        let points = [GlanceCurvePoint(hour: 3, score: 140), GlanceCurvePoint(hour: 4, score: -5)]
        let segment = CurveGeometry.segments(points)[0]
        XCTAssertEqual(segment[0].y, 1.0)
        XCTAssertEqual(segment[1].y, 0.0)
    }
}
