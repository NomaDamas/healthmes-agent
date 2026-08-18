import Foundation
import XCTest

final class MacWellnessSceneContractTests: XCTestCase {
    private func fixtureData(_ name: String) throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: Self.self).url(forResource: name, withExtension: "json")
        )
        return try Data(contentsOf: url)
    }

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

    func testProjectedActionableScenePassesWithItsExactProposalIdentity() {
        let proposalID = UUID()
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "token"
        )
        let scene = WellnessScene(
            id: "projected-decision",
            lens: .coordinate,
            title: "Move focus block?",
            summary: "Current recovery is below the planned workload.",
            severity: .action,
            freshness: .current,
            confidence: WellnessConfidence(
                level: .medium,
                coverage: "Current health snapshot and exact proposal."
            ),
            modules: [
                WellnessSceneModule(
                    id: "proposal-preview",
                    kind: .proposalPreview,
                    title: "One reversible intervention",
                    summary: "No change occurs until Yes.",
                    items: [
                        WellnessSceneItem(
                            id: "proposal-id",
                            label: "proposal_id",
                            value: proposalID.uuidString
                        ),
                        WellnessSceneItem(
                            id: "proposal-task",
                            label: "Exact action",
                            value: "Move focus block"
                        ),
                        WellnessSceneItem(
                            id: "proposal-window",
                            label: "Proposed time",
                            value: "2026-08-10T16:00:00Z/2026-08-10T17:00:00Z"
                        ),
                    ]
                )
            ],
            actions: [
                WellnessSceneAction(
                    id: "decline",
                    kind: .declineProposal,
                    label: "No",
                    proposalID: proposalID
                ),
                WellnessSceneAction(
                    id: "accept",
                    kind: .acceptProposal,
                    label: "Yes",
                    proposalID: proposalID
                ),
            ]
        )

        XCTAssertNoThrow(
            try WellnessSceneValidator.validateLocalProjection(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        )
    }

    func testReportOnlySceneUsesServerReportTimezone() throws {
        let report = try GlanceJSON.decoder().decode(
            WeeklyReport.self,
            from: fixtureData("weekly_report")
        )

        let resolved = MacSceneTimeZone.resolve(
            payloadTimezone: nil,
            reportTimezone: report.timezone,
            fallback: try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))
        )

        let expected = try XCTUnwrap(TimeZone(identifier: report.timezone))
        XCTAssertEqual(
            resolved.secondsFromGMT(for: report.generatedAt),
            expected.secondsFromGMT(for: report.generatedAt)
        )
    }
}
