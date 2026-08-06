import XCTest

final class ProductContractTests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "https://healthmes.example")!,
        token: "token"
    )

    func testPlanPagesDecodeServerContracts() throws {
        let json = """
            {
              "goals": {
                "data": [{
                  "id": "10000000-0000-0000-0000-000000000001",
                  "week_start": "2026-08-03",
                  "title": "Protect recovery",
                  "priority": 8,
                  "status": "active"
                }],
                "pagination": {"total_count": 1, "limit": 50, "offset": 0, "has_more": false}
              },
              "tasks": {
                "data": [{
                  "id": "20000000-0000-0000-0000-000000000002",
                  "title": "Prepare review",
                  "goal_id": null,
                  "est_minutes": 45,
                  "deadline": "2026-08-07T09:00:00Z",
                  "energy_demand": "high",
                  "status": "todo",
                  "source": "user"
                }],
                "pagination": {"total_count": 1, "limit": 100, "offset": 0, "has_more": false}
              },
              "events": {
                "data": [{
                  "id": "30000000-0000-0000-0000-000000000003",
                  "external_id": "icloud-caldav-1",
                  "calendar_source": "caldav",
                  "summary": "Deep work",
                  "start_at": "2026-08-07T09:00:00Z",
                  "end_at": "2026-08-07T10:30:00Z",
                  "is_agent_created": true,
                  "agent_task_id": null
                }],
                "pagination": {"total_count": 1, "limit": 100, "offset": 0, "has_more": false}
              }
            }
            """
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
        )
        let decoder = GlanceJSON.decoder()
        let goals = try decoder.decode(
            WeeklyGoalsPage.self,
            from: JSONSerialization.data(withJSONObject: object["goals"]!)
        )
        let tasks = try decoder.decode(
            TasksPage.self,
            from: JSONSerialization.data(withJSONObject: object["tasks"]!)
        )
        let events = try decoder.decode(
            CalendarEventsPage.self,
            from: JSONSerialization.data(withJSONObject: object["events"]!)
        )

        XCTAssertEqual(goals.data.first?.weekStart, "2026-08-03")
        XCTAssertEqual(tasks.data.first?.estimatedMinutes, 45)
        XCTAssertTrue(tasks.data.first?.isOpen == true)
        XCTAssertEqual(events.data.first?.calendarSource, "caldav")
        XCTAssertTrue(events.data.first?.isAgentCreated == true)
    }

    func testPlanRequestBuildersMatchRESTRoutes() throws {
        XCTAssertEqual(
            HealthMesAPI.goalsRequest(
                pairing: pairing,
                weekStart: "2026-08-03",
                status: "active"
            ).url?.absoluteString,
            "https://healthmes.example/v1/goals?limit=50&week_start=2026-08-03&status=active"
        )
        XCTAssertEqual(
            HealthMesAPI.tasksRequest(pairing: pairing).url?.absoluteString,
            "https://healthmes.example/v1/tasks?limit=100"
        )
        XCTAssertEqual(
            HealthMesAPI.decisionsRequest(pairing: pairing).url?.absoluteString,
            "https://healthmes.example/v1/decisions?limit=100&offset=0"
        )

        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-06T00:00:00Z"))
        let end = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-13T00:00:00Z"))
        XCTAssertEqual(
            HealthMesAPI.scheduleEventsRequest(
                pairing: pairing,
                start: start,
                end: end
            ).url?.absoluteString,
            "https://healthmes.example/v1/schedule/events"
                + "?start=2026-08-06T00:00:00Z&end=2026-08-13T00:00:00Z&limit=100"
        )
    }

    func testViewerURLUsesDerivedReadOnlyTokenAndStrictOrigin() {
        let url = ViewerURL.make(
            pairing: pairing,
            pathComponents: ["dashboard"],
            fragment: "plan"
        )
        XCTAssertEqual(
            url.absoluteString,
            "https://healthmes.example/dashboard"
                + "?token=05966fe90076e7c2a0ecbc89048e9ecc#plan"
        )
        XCTAssertTrue(
            ViewerURL.hasSameOrigin(
                URL(string: "https://healthmes.example/dashboard")!,
                as: pairing.baseURL
            )
        )
        XCTAssertFalse(
            ViewerURL.hasSameOrigin(
                URL(string: "http://healthmes.example/dashboard")!,
                as: pairing.baseURL
            )
        )
        XCTAssertFalse(
            ViewerURL.hasSameOrigin(
                URL(string: "https://healthmes.example:8443/dashboard")!,
                as: pairing.baseURL
            )
        )
    }

    func testVoiceTaskRequestUsesISO8601AndExpectedDefaults() throws {
        let deadline = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-07T09:00:00Z"))
        let request = try HealthMesAPI.createTaskRequest(
            pairing: pairing,
            body: TaskCreateBody(title: "Prepare review", deadline: deadline)
        )
        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any]
        )

        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(body["title"] as? String, "Prepare review")
        XCTAssertEqual(body["energy_demand"] as? String, "med")
        XCTAssertEqual(body["source"] as? String, "user")
        XCTAssertEqual(body["deadline"] as? String, "2026-08-07T09:00:00Z")
    }

    func testDecisionCorrelationAndExactURLPreferDecisionCard() throws {
        let proposalID = UUID(uuidString: "40000000-0000-0000-0000-000000000004")!
        let decisionID = UUID(uuidString: "50000000-0000-0000-0000-000000000005")!
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-07T09:00:00Z"))
        let proposal = ProposalItem(
            id: proposalID,
            taskId: UUID(),
            proposedStart: start,
            proposedEnd: start.addingTimeInterval(3_600),
            status: .proposed,
            decisionRecordId: decisionID,
            healthmesKind: "planned_sleep",
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )
        let card = DecisionCard(
            decisionId: decisionID,
            proposalId: proposalID,
            kind: "planned_sleep",
            severity: "coaching",
            title: "Wind down",
            observationShort: "Sleep debt is rising",
            evidenceShort: nil,
            proposedAction: "Schedule wind down?",
            before: nil,
            after: start,
            endsAt: start.addingTimeInterval(3_600),
            expiresAt: start,
            decisionUrl: "https://healthmes.example/decisions/card"
        )
        let alert = AlertItem(
            id: UUID(),
            ruleId: "sleep",
            firedAt: start,
            summary: "summary",
            proposal: nil,
            evidence: nil,
            decisionUrl: "https://healthmes.example/decisions/legacy",
            proposalId: proposalID,
            decisionCard: card
        )

        let decisions = PendingDecision.correlate(
            alerts: [alert],
            proposals: [proposal]
        )
        XCTAssertEqual(decisions.map(\.id), [proposalID])
        XCTAssertEqual(
            decisions.first?.exactWebURL?.absoluteString,
            "https://healthmes.example/decisions/card"
        )
        XCTAssertEqual(alert.exactDecisionRecordID, decisionID)
        XCTAssertEqual(decisions.first?.prompt, "Schedule wind down?")
        XCTAssertEqual(
            ProposalStatusPresentation.label(for: .accepted),
            "Approved · calendar sync pending"
        )
        XCTAssertEqual(ProposalStatusPresentation.label(for: .pushed), "Applied to calendar")
        XCTAssertEqual(
            ProposalStatusPresentation.detail(for: .accepted),
            "Calendar sync will apply the change."
        )
        XCTAssertEqual(
            ProposalStatusPresentation.detail(for: .pushed),
            "The approved change is in your calendar."
        )
    }

    func testPendingDecisionUsesActualActionWithoutDecisionCard() throws {
        let proposalID = UUID(uuidString: "60000000-0000-0000-0000-000000000006")!
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-07T09:00:00Z"))
        let proposal = ProposalItem(
            id: proposalID,
            taskId: UUID(),
            proposedStart: start,
            proposedEnd: start.addingTimeInterval(3_600),
            status: .proposed,
            decisionRecordId: nil,
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )
        let alert = AlertItem(
            id: UUID(),
            ruleId: "schedule",
            firedAt: start,
            summary: "Schedule pressure changed.",
            proposal: "Move Deep Work to 4 PM.",
            evidence: nil,
            decisionUrl: nil,
            proposalId: proposalID
        )

        let decision = try XCTUnwrap(
            PendingDecision.correlate(alerts: [alert], proposals: [proposal]).first
        )
        XCTAssertEqual(decision.prompt, "Move Deep Work to 4 PM?")
        XCTAssertNil(ProposalActionPresentation.exactPrompt(alert: nil))
        XCTAssertTrue(
            PendingDecision.correlate(alerts: [], proposals: [proposal]).isEmpty
        )
    }

    func testPendingDecisionFailsClosedForBlankActionPhrase() throws {
        let proposalID = UUID(uuidString: "70000000-0000-0000-0000-000000000007")!
        let decisionID = UUID(uuidString: "80000000-0000-0000-0000-000000000008")!
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-07T09:00:00Z"))
        let proposal = ProposalItem(
            id: proposalID,
            taskId: UUID(),
            proposedStart: start,
            proposedEnd: start.addingTimeInterval(3_600),
            status: .proposed,
            decisionRecordId: decisionID,
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )
        let card = DecisionCard(
            decisionId: decisionID,
            proposalId: proposalID,
            kind: "schedule_change",
            severity: "coaching",
            title: "Deep Work",
            observationShort: "Recovery is low",
            evidenceShort: nil,
            proposedAction: " \n ",
            before: nil,
            after: start,
            endsAt: start.addingTimeInterval(3_600),
            expiresAt: start.addingTimeInterval(600),
            decisionUrl: nil
        )
        let alert = AlertItem(
            id: UUID(),
            ruleId: "recovery",
            firedAt: start,
            summary: "Recovery is low.",
            proposal: nil,
            evidence: nil,
            decisionUrl: nil,
            proposalId: proposalID,
            decisionCard: card
        )

        XCTAssertNil(ProposalActionPresentation.exactPrompt(alert: alert))
        XCTAssertTrue(
            PendingDecision.correlate(alerts: [alert], proposals: [proposal]).isEmpty
        )
    }

    func testLatestRefreshGateRejectsOlderCompletion() {
        var gate = LatestRefreshGate()
        let first = gate.begin()
        let second = gate.begin()

        XCTAssertFalse(gate.isCurrent(first))
        XCTAssertTrue(gate.isCurrent(second))
    }

    func testProductRefreshResultKeepsSuccessAndFailureIndependent() async {
        struct ExpectedFailure: Error {}

        async let success: Result<Int, Error> = productRefreshResult { 42 }
        async let failure: Result<Int, Error> = productRefreshResult {
            throw ExpectedFailure()
        }
        let (successResult, failureResult) = await (success, failure)

        guard case .success(let value) = successResult else {
            return XCTFail("successful refresh leg was discarded")
        }
        XCTAssertEqual(value, 42)
        guard case .failure = failureResult else {
            return XCTFail("failed refresh leg was reported as success")
        }
    }

    func testWellnessSceneValidatorFailsClosedForUnsafeActions() throws {
        let module = WellnessSceneModule(
            id: "state",
            kind: .healthState,
            title: "회복",
            summary: "평소보다 낮습니다."
        )
        let mutation = WellnessScene(
            id: "unsafe-mutation",
            lens: .coordinate,
            title: "조율",
            summary: "제안",
            severity: .action,
            freshness: .current,
            modules: [module],
            actions: [
                WellnessSceneAction(
                    id: "accept",
                    kind: .acceptProposal,
                    label: "예"
                )
            ]
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                mutation,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .mutationWithoutProposal)
        }

        let external = WellnessScene(
            id: "unsafe-url",
            lens: .change,
            title: "변화",
            summary: "상세",
            severity: .neutral,
            freshness: .current,
            modules: [module],
            actions: [
                WellnessSceneAction(
                    id: "web",
                    kind: .openWebDetail,
                    label: "자세히",
                    url: URL(string: "https://attacker.test/decision")!
                )
            ]
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                external,
                pairedBaseURL: pairing.baseURL
            )
        )
    }

    func testWellnessCommandParserUsesLensesAndExplicitWritePrefixes() {
        XCTAssertEqual(
            WellnessCommandParser.parse("지금 내 상태 보여줘"),
            .show(.now)
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("일정을 조율해줘"),
            .show(.coordinate)
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("지난 결정 결과 패턴"),
            .show(.change)
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("할 일: 라이브 QA"),
            .createTask("라이브 QA")
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("주간 목표: 회복 블록 보호"),
            .createGoal("회복 블록 보호")
        )
        XCTAssertEqual(
            WellnessCommandParser.parse("내일 어떻게 하지?"),
            .clarify("내일 어떻게 하지?")
        )
    }

    func testAcceptedAndPushedStayDistinctInGeneratedUISemantics() {
        XCTAssertEqual(
            ProposalStatusPresentation.label(for: .accepted),
            "Approved · calendar sync pending"
        )
        XCTAssertEqual(
            ProposalStatusPresentation.label(for: .pushed),
            "Applied to calendar"
        )
        XCTAssertNotEqual(
            ProposalStatusPresentation.detail(for: .accepted),
            ProposalStatusPresentation.detail(for: .pushed)
        )
    }
}
