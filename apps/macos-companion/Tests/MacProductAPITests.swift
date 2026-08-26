import XCTest

final class MacProductAPITests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "https://example.test/healthmes")!,
        token: "secret"
    )

    func testSharedProductRequestsAndMacDecisionRequestUsePairing() {
        let requests = [
            HealthMesAPI.goalsRequest(pairing: pairing),
            HealthMesAPI.tasksRequest(pairing: pairing),
            MacDecisionAPI.decisionsRequest(pairing: pairing),
        ]

        XCTAssertEqual(requests.map(\.valueForAuthorization), Array(repeating: "Bearer secret", count: 3))
        XCTAssertTrue(requests.allSatisfy { $0.url?.path.hasPrefix("/healthmes/v1/") == true })
        XCTAssertTrue(requests.allSatisfy { $0.url?.query?.contains("limit=") == true })
    }

    func testDecisionURLPreservesReverseProxySubpath() {
        let id = UUID(uuidString: "8ad1b599-cc9e-4b3b-99dc-183bf87ce91a")!
        XCTAssertEqual(
            MacWebLinks.decision(id: id, pairing: pairing).absoluteString,
            "https://example.test/healthmes/decisions/8ad1b599-cc9e-4b3b-99dc-183bf87ce91a"
                + "?token=\(ViewerURL.token(from: "secret"))"
        )
    }

    func testPlanLinkTargetsDashboardPlanAnchor() {
        XCTAssertEqual(
            MacWebLinks.plan(pairing: pairing).absoluteString,
            "https://example.test/healthmes/dashboard"
                + "?token=\(ViewerURL.token(from: "secret"))#plan"
        )
    }

    func testServerProvidedDecisionURLWinsAndKeepsViewerToken() {
        let exact = "https://example.test/healthmes/decisions/abc?token=viewer-token"
        let alert = AlertItem(
            id: UUID(),
            ruleId: "sleep-low",
            firedAt: Date(),
            summary: "Recovery is low.",
            proposal: nil,
            evidence: nil,
            decisionUrl: exact,
            proposalId: nil
        )

        XCTAssertEqual(
            MacWebLinks.decision(for: alert, pairing: pairing)?.absoluteString,
            "https://example.test/healthmes/decisions/abc"
                + "?token=\(ViewerURL.token(from: "secret"))"
        )
    }

    func testWeeklyReportURLRequiresSameOriginAndKeepsViewerToken() {
        XCTAssertEqual(
            MacWebLinks.weeklyReport(
                "https://example.test/healthmes/reports/weekly?token=stale",
                pairing: pairing
            )?.absoluteString,
            "https://example.test/healthmes/reports/weekly"
                + "?token=\(ViewerURL.token(from: "secret"))"
        )
        XCTAssertNil(
            MacWebLinks.weeklyReport(
                "https://attacker.test/reports/weekly",
                pairing: pairing
            )
        )
        XCTAssertNil(
            MacWebLinks.weeklyReport(
                "https://example.test/outside/reports/weekly",
                pairing: pairing
            )
        )
        XCTAssertNil(
            MacWebLinks.weeklyReport(
                "http://example.test/healthmes/reports/weekly",
                pairing: pairing
            )
        )
        XCTAssertNil(
            MacWebLinks.weeklyReport(
                "https://example.test:8443/healthmes/reports/weekly",
                pairing: pairing
            )
        )
    }
}

private extension URLRequest {
    var valueForAuthorization: String? {
        value(forHTTPHeaderField: "Authorization")
    }
}
