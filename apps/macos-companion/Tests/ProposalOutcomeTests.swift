import XCTest

/// The 409 invalid_transition story for the §8.5 buttons, pinned as data:
/// popover buttons and notification actions share this mapping.
final class ProposalOutcomeTests: XCTestCase {
    func testSuccessMapsPerAction() {
        XCTAssertEqual(ProposalOutcome.from(action: .accept, error: nil), .applied)
        XCTAssertEqual(ProposalOutcome.from(action: .decline, error: nil), .kept)
    }

    func testInvalidTransitionBecomesAlreadyResolvedWithServerStatus() {
        let error = HealthMesAPIError.server(
            statusCode: 409,
            code: "invalid_transition",
            message: "proposal already resolved",
            detail: .object([
                "current": .string("accepted"),
                "requested": .string("declined"),
            ])
        )
        XCTAssertEqual(
            ProposalOutcome.from(action: .decline, error: error),
            .alreadyResolved(status: "accepted")
        )
    }

    func testInvalidTransitionWithoutDetailStillReadsResolved() {
        let error = HealthMesAPIError.server(
            statusCode: 409,
            code: "invalid_transition",
            message: "proposal already resolved",
            detail: nil
        )
        XCTAssertEqual(
            ProposalOutcome.from(action: .accept, error: error),
            .alreadyResolved(status: "resolved")
        )
    }

    func testOtherErrorsFail() {
        XCTAssertEqual(
            ProposalOutcome.from(action: .accept, error: .httpStatus(500)),
            .failed
        )
        XCTAssertEqual(
            ProposalOutcome.from(
                action: .accept,
                error: .server(statusCode: 404, code: "not_found", message: "no", detail: nil)
            ),
            .failed
        )
    }
}
