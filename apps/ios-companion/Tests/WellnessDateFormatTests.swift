import XCTest

final class WellnessDateFormatTests: XCTestCase {
    func testAbbreviatedDateTimeUsesServerTimezoneInsteadOfDeviceTimezone() throws {
        let instant = try XCTUnwrap(
            ISO8601DateFormatter().date(from: "2026-08-10T19:33:00Z")
        )
        let losAngeles = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))
        let seoul = try XCTUnwrap(TimeZone(identifier: "Asia/Seoul"))
        let locale = Locale(identifier: "en_US_POSIX")

        XCTAssertEqual(
            normalized(
                WellnessDateFormat.abbreviatedDateTime(
                    instant,
                    timeZone: losAngeles,
                    locale: locale
                )
            ),
            "Aug 10, 2026 at 12:33 PM"
        )
        XCTAssertEqual(
            normalized(
                WellnessDateFormat.abbreviatedDateTime(
                    instant,
                    timeZone: seoul,
                    locale: locale
                )
            ),
            "Aug 11, 2026 at 4:33 AM"
        )
    }

    private func normalized(_ value: String) -> String {
        value.replacingOccurrences(of: "\u{202F}", with: " ")
    }
}
