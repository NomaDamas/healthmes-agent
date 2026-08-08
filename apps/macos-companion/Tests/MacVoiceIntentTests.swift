import XCTest

final class MacVoiceIntentTests: XCTestCase {
    func testNavigationCommandsStayLocal() {
        XCTAssertEqual(MacVoiceIntentParser.parse("내 계획 보여줘"), .showPlan)
        XCTAssertEqual(MacVoiceIntentParser.parse("Open decisions"), .showDecisions)
        XCTAssertEqual(MacVoiceIntentParser.parse("설정 열어줘"), .showSettings)
        XCTAssertEqual(MacVoiceIntentParser.parse("오늘 상태"), .showToday)
        XCTAssertEqual(MacVoiceIntentParser.parse("refresh everything"), .refresh)
    }

    func testTaskPrefixIsRemovedBeforeConfirmation() {
        XCTAssertEqual(
            MacVoiceIntentParser.parse("할 일 라이브 QA 체크리스트 만들기"),
            .taskDraft("라이브 QA 체크리스트 만들기")
        )
        XCTAssertEqual(
            MacVoiceIntentParser.parse("Task prepare the release notes"),
            .taskDraft("prepare the release notes")
        )
        XCTAssertEqual(
            MacVoiceIntentParser.parse("Task review the calendar setup"),
            .taskDraft("review the calendar setup")
        )
    }

    func testWeeklyGoalRequiresConfirmationLikeTasks() {
        XCTAssertEqual(
            MacVoiceIntentParser.parse("주간 목표 집중 블록 세 개 보호하기"),
            .goalDraft("집중 블록 세 개 보호하기")
        )
        XCTAssertEqual(
            MacVoiceIntentParser.parse("Weekly goal protect recovery"),
            .goalDraft("protect recovery")
        )
    }

    func testUnknownSpeechFailsClosedInsteadOfBecomingTask() {
        XCTAssertNil(MacVoiceIntentParser.parse("Move deep work to four"))
        XCTAssertNil(MacVoiceIntentParser.parse("   "))
    }
}
