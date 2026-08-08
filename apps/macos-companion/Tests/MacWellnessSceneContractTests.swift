import Foundation
import XCTest

final class MacWellnessSceneContractTests: XCTestCase {
    func testCommandParserRequiresExplicitWritePrefixes() {
        XCTAssertEqual(
            WellnessCommandParser.parse("할 일: 라이브 QA 확인"),
            .createTask("라이브 QA 확인")
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("weekly goal: protect recovery"),
            .createGoal("protect recovery")
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("Move deep work to four"),
            .clarify("Move deep work to four")
        )
    }

    func testLensCommandsChangePerspectiveWithoutCreatingWrites() {
        XCTAssertEqual(WellnessCommandParser.parse("지금 상태"), .show(.now))
        XCTAssertEqual(WellnessCommandParser.parse("조율 제안 보여줘"), .show(.coordinate))
        XCTAssertEqual(WellnessCommandParser.parse("변화 결과"), .show(.change))
    }

    func testMutationActionFailsClosedWithoutProposalCorrelation() {
        let scene = WellnessScene(
            id: "unsafe-decision",
            lens: .coordinate,
            title: "Decision",
            summary: "Missing proposal correlation",
            severity: .action,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "decision",
                    kind: .decision,
                    title: "Decision",
                    summary: "No proposal ID"
                )
            ],
            actions: [
                WellnessSceneAction(
                    id: "accept",
                    kind: .acceptProposal,
                    label: "Yes"
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: URL(string: "https://healthmes.example")
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .mutationWithoutProposal)
        }
    }

    func testWebDetailMustStayOnPairedOrigin() {
        let scene = WellnessScene(
            id: "unsafe-link",
            lens: .change,
            title: "Outcome",
            summary: "External link",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "outcome",
                    kind: .outcomeCurve,
                    title: "Outcome",
                    summary: "Detail"
                )
            ],
            actions: [
                WellnessSceneAction(
                    id: "open",
                    kind: .openWebDetail,
                    label: "Open",
                    url: URL(string: "https://attacker.example/decision")
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: URL(string: "https://healthmes.example")
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .unsafeWebURL)
        }
    }
}
