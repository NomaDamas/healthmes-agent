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
                  "agent_task_id": null,
                  "is_all_day": false,
                  "is_locked": true
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
        XCTAssertFalse(events.data.first?.isAllDay == true)
        XCTAssertTrue(events.data.first?.isLocked == true)
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
        XCTAssertEqual(
            HealthMesAPI.setupReadinessRequest(pairing: pairing).url?.absoluteString,
            "https://healthmes.example/v1/setup/readiness"
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

        let proposalID = UUID(uuidString: "91000000-0000-0000-0000-000000000091")!
        let decisionID = UUID(uuidString: "92000000-0000-0000-0000-000000000092")!
        let scene = try HealthMesAPI.wellnessSceneRequest(
            pairing: pairing,
            query: "언제 집중 업무를 해야 해?",
            source: .proactive,
            proposalID: proposalID,
            decisionRecordID: decisionID
        )
        XCTAssertEqual(
            scene.url?.absoluteString,
            "https://healthmes.example/v1/wellness/scenes"
        )
        XCTAssertEqual(scene.httpMethod, "POST")
        let sceneBody = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: try XCTUnwrap(scene.httpBody)
            ) as? [String: String]
        )
        XCTAssertEqual(sceneBody["query"], "언제 집중 업무를 해야 해?")
        XCTAssertEqual(sceneBody["source"], "proactive")
        XCTAssertEqual(sceneBody["proposal_id"], proposalID.uuidString.uppercased())
        XCTAssertEqual(sceneBody["decision_record_id"], decisionID.uuidString.uppercased())
    }

    func testSetupReadinessDecodesIndependentComponents() throws {
        let json = """
            {
              "overall": "action_required",
              "checks": [
                {
                  "key": "calendar_google",
                  "label": "Google Calendar",
                  "state": "ready",
                  "detail": "connected"
                },
                {
                  "key": "calendar_icloud",
                  "label": "iCloud Calendar",
                  "state": "action_required",
                  "detail": "connect once"
                }
              ]
            }
            """
        let readiness = try GlanceJSON.decoder().decode(
            SetupReadiness.self,
            from: Data(json.utf8)
        )
        XCTAssertEqual(readiness.overall, .actionRequired)
        XCTAssertEqual(readiness.check("calendar_google")?.state, .ready)
        XCTAssertEqual(
            readiness.check("calendar_icloud")?.state,
            .actionRequired
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
        XCTAssertEqual(decisions.first?.watchActionTitle, "Wind down")
        XCTAssertEqual(decisions.first?.watchReason, "Sleep debt is rising")
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

    func testPendingDecisionFailsClosedWithoutDecisionCard() throws {
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

        XCTAssertNil(ProposalActionPresentation.exactPrompt(alert: nil))
        XCTAssertTrue(PendingDecision.correlate(alerts: [alert], proposals: [proposal]).isEmpty)
        XCTAssertTrue(PendingDecision.correlate(alerts: [], proposals: [proposal]).isEmpty)
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

    func testCompactProposalWindowUsesSceneTimezoneInsteadOfDeviceTimezone() throws {
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-09T21:37:00Z"))
        let now = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-09T14:00:00Z"))
        let proposal = ProposalItem(
            id: UUID(),
            taskId: UUID(),
            proposedStart: start,
            proposedEnd: start.addingTimeInterval(5_400),
            status: .proposed,
            decisionRecordId: UUID(),
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )

        let utc = ProposalFormat.compactWindowLine(
            proposal,
            now: now,
            timeZone: try XCTUnwrap(TimeZone(identifier: "UTC"))
        )
        let seoul = ProposalFormat.compactWindowLine(
            proposal,
            now: now,
            timeZone: try XCTUnwrap(TimeZone(identifier: "Asia/Seoul"))
        )

        XCTAssertTrue(utc.hasPrefix("Today"))
        XCTAssertTrue(seoul.hasPrefix("Tomorrow"))
        XCTAssertNotEqual(utc, seoul)
        XCTAssertFalse(
            ProposalFormat.watchWindowLine(
                proposal,
                now: now,
                timeZone: try XCTUnwrap(TimeZone(identifier: "UTC"))
            ).contains("Today")
        )
        XCTAssertTrue(
            ProposalFormat.watchWindowLine(
                proposal,
                now: now,
                timeZone: try XCTUnwrap(TimeZone(identifier: "Asia/Seoul"))
            ).hasPrefix("Tomorrow")
        )
    }

    func testLatestRefreshGateRejectsOlderCompletion() {
        var gate = LatestRefreshGate()
        let first = gate.begin()
        let second = gate.begin()

        XCTAssertFalse(gate.isCurrent(first))
        XCTAssertTrue(gate.isCurrent(second))
    }

    func testPairingOperationGateRejectsOldGenerationAndCredentialChange() {
        let firstPairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "first-token"
        )
        let changedCredential = Pairing(
            baseURL: firstPairing.baseURL,
            token: "second-token"
        )
        let proposalID = UUID()
        var gate = PairingOperationGate()
        let first = gate.begin(pairing: firstPairing, proposalID: proposalID)

        XCTAssertTrue(
            gate.isCurrent(first, pairing: firstPairing, proposalID: proposalID)
        )
        XCTAssertFalse(
            gate.isCurrent(first, pairing: changedCredential, proposalID: proposalID)
        )
        XCTAssertFalse(
            gate.isCurrent(first, pairing: firstPairing, proposalID: UUID())
        )

        _ = gate.begin(pairing: firstPairing, proposalID: proposalID)
        XCTAssertFalse(
            gate.isCurrent(first, pairing: firstPairing, proposalID: proposalID)
        )
    }

    func testResolutionAwareRefreshGateRejectsPollsAroundResolution() {
        var gate = ResolutionAwareRefreshGate()
        let beforeResolution = gate.beginRefresh()
        let resolution = gate.beginResolution()
        let duringResolution = gate.beginRefresh()

        XCTAssertFalse(gate.canApplyRefresh(beforeResolution))
        XCTAssertFalse(gate.canApplyRefresh(duringResolution))
        XCTAssertTrue(gate.finishResolution(resolution))
        XCTAssertFalse(gate.canApplyRefresh(duringResolution))

        let afterResolution = gate.beginRefresh()
        XCTAssertTrue(gate.canApplyRefresh(afterResolution))
    }

    func testResolutionAwareRefreshGateAllowsConcurrentResolutionsToFinish() {
        var gate = ResolutionAwareRefreshGate()
        let first = gate.beginResolution()
        let second = gate.beginResolution()

        XCTAssertTrue(gate.finishResolution(second))
        XCTAssertTrue(gate.finishResolution(first))
        XCTAssertFalse(gate.finishResolution(first))

        let refresh = gate.beginRefresh()
        XCTAssertTrue(gate.canApplyRefresh(refresh))
    }

    func testStaleResolutionCannotInvalidateCurrentAccountResolution() {
        var gate = ResolutionAwareRefreshGate()
        let previousAccount = gate.beginResolution()
        gate.invalidate()
        let currentAccount = gate.beginResolution()

        XCTAssertFalse(gate.finishResolution(previousAccount))
        XCTAssertTrue(gate.finishResolution(currentAccount))
    }

    func testProactiveProposalSelectionRequiresDecisionRecordCorrelation() {
        let now = Date()
        let uncorrelated = ProposalItem(
            id: UUID(),
            taskId: UUID(),
            proposedStart: now,
            proposedEnd: now.addingTimeInterval(1_800),
            status: .proposed,
            decisionRecordId: nil,
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )
        let correlated = ProposalItem(
            id: UUID(),
            taskId: UUID(),
            proposedStart: now.addingTimeInterval(3_600),
            proposedEnd: now.addingTimeInterval(5_400),
            status: .proposed,
            decisionRecordId: UUID(),
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )

        XCTAssertFalse(uncorrelated.canComposeProactiveScene)
        XCTAssertEqual(
            ProactiveProposalSelection.firstEligible(in: [uncorrelated, correlated])?.id,
            correlated.id
        )
        XCTAssertFalse(
            ProactiveProposalSelection.containsEligibleProposal(
                id: uncorrelated.id,
                in: [uncorrelated, correlated]
            )
        )
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

    func testWellnessSceneValidatorRejectsDuplicateItemIdentity() {
        let duplicated = WellnessSceneItem(
            id: "weekly-report",
            label: "Decisions",
            value: "4"
        )
        let scene = WellnessScene(
            id: "duplicate-items",
            lens: .change,
            title: "Weekly outcome",
            summary: "Metrics",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "weekly",
                    kind: .outcomeCurve,
                    title: "Weekly outcome",
                    summary: "Metrics",
                    items: [duplicated, duplicated]
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .duplicateItemID)
        }
    }

    func testWellnessSceneDecodesTrustedVisualizationContract() throws {
        let json = """
            {
              "schema_version": "1",
              "id": "focus:1",
              "intent": "find_focus_window",
              "lens": "coordinate",
              "title": "집중 업무를 보호할 시간",
              "summary": "오전 가용량이 높습니다.",
              "severity": "supportive",
              "freshness": "current",
              "confidence": {
                "level": "medium",
                "coverage": "에너지 12/24시간 · 캘린더 1건",
                "limitations": ["오후 표본이 적습니다."]
              },
              "modules": [{
                "id": "calendar",
                "kind": "calendar_canvas",
                "title": "Apple · Google Calendar",
                "summary": "실제 일정과 제안을 함께 표시합니다.",
                "items": [],
                "visualization": {
                  "kind": "calendar_canvas",
                  "unit": null,
                  "minimum": null,
                  "maximum": null,
                  "series": [],
                  "events": [{
                    "id": "google:1",
                    "title": "핵심 문서 작성",
                    "starts_at": "2026-08-09T09:00:00Z",
                    "ends_at": "2026-08-09T10:30:00Z",
                    "provider": "google",
                    "is_healthmes_managed": false,
                    "energy_demand": "high",
                    "status": "current"
                  }]
                },
                "accessibility_summary": "캘린더 일정 1건"
              }],
              "actions": [],
              "generated_at": "2026-08-09T00:00:00Z",
              "timezone": "Asia/Seoul"
            }
            """

        let scene = try GlanceJSON.decoder().decode(
            WellnessScene.self,
            from: Data(json.utf8)
        )

        XCTAssertEqual(scene.schemaVersion, "1")
        XCTAssertEqual(scene.intent, "find_focus_window")
        XCTAssertEqual(scene.timezone, "Asia/Seoul")
        XCTAssertEqual(scene.confidence.level, .medium)
        XCTAssertEqual(scene.modules.first?.visualization?.kind, .calendarCanvas)
        XCTAssertEqual(
            scene.modules.first?.visualization?.events.first?.provider,
            "google"
        )
        XCTAssertEqual(
            scene.modules.first?.visualization?.events.first?.energyDemand,
            "high"
        )
        XCTAssertNoThrow(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        )
    }

    func testWellnessSceneDecodesProactiveProposalPreview() throws {
        let proposalID = UUID(uuidString: "803aee59-99e0-478f-88eb-bcb6ae28acde")!
        let json = """
            {
              "schema_version": "1",
              "id": "proactive:1",
              "intent": "proactive_intervention",
              "lens": "coordinate",
              "title": "HealthMes가 먼저 찾은 조정",
              "summary": "승인 전 제안이 있습니다.",
              "severity": "action",
              "freshness": "current",
              "confidence": {
                "level": "low",
                "coverage": "정확히 연결된 제안 1건",
                "limitations": []
              },
              "modules": [{
                "id": "proposal-preview",
                "kind": "proposal_preview",
                "title": "승인 전 일정 블록",
                "summary": "제안된 블록을 승인 전 미리보기로 표시합니다.",
                "items": [{
                  "id": "proposal-id",
                  "label": "proposal_id",
                  "value": "\(proposalID.uuidString.lowercased())",
                  "detail": null
                }, {
                  "id": "proposal-task",
                  "label": "일정",
                  "value": "핵심 문서 작성",
                  "detail": null
                }, {
                  "id": "proposal-window",
                  "label": "제안 시간",
                  "value": "2026-08-09T09:00:00Z/2026-08-09T10:00:00Z",
                  "detail": null
                }],
                "visualization": null,
                "accessibility_summary": "승인 전 일정 블록"
              }],
              "actions": [{
                "id": "accept:\(proposalID.uuidString.lowercased())",
                "kind": "accept_proposal",
                "label": "적용",
                "proposal_id": "\(proposalID.uuidString.lowercased())",
                "url": null
              }, {
                "id": "decline:\(proposalID.uuidString.lowercased())",
                "kind": "decline_proposal",
                "label": "유지",
                "proposal_id": "\(proposalID.uuidString.lowercased())",
                "url": null
              }],
              "generated_at": "2026-08-09T00:00:00Z",
              "timezone": "Asia/Seoul"
            }
            """

        let scene = try GlanceJSON.decoder().decode(
            WellnessScene.self,
            from: Data(json.utf8)
        )

        XCTAssertEqual(scene.modules.first?.kind, .proposalPreview)
        XCTAssertEqual(scene.exactMutationPreview?.task, "핵심 문서 작성")
        XCTAssertEqual(
            scene.exactMutationPreview?.dateInterval?.duration,
            3_600
        )
        let localizedWindow = try XCTUnwrap(
            scene.exactMutationPreview?.localizedWindow(timezone: scene.timezone)
        )
        XCTAssertFalse(localizedWindow.contains("T09:00:00"))
        XCTAssertFalse(localizedWindow.contains(proposalID.uuidString.lowercased()))
        XCTAssertTrue(scene.allowsProposalActions(for: proposalID))
        XCTAssertNoThrow(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL,
                expectedProposalID: proposalID
            )
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .unexpectedMutation
            )
        }
    }

    func testWellnessSceneValidatorRejectsMutationWithoutExactActionPreview() {
        let proposalID = UUID()
        let scene = WellnessScene(
            id: "missing-exact-action",
            lens: .coordinate,
            title: "Schedule change",
            summary: "Approval requested",
            severity: .action,
            freshness: .current,
            confidence: WellnessConfidence(level: .high, coverage: "proposal"),
            modules: [
                WellnessSceneModule(
                    id: "proposal-preview",
                    kind: .proposalPreview,
                    title: "Proposal",
                    summary: "Missing task and window",
                    items: [
                        WellnessSceneItem(
                            id: "proposal-id",
                            label: "proposal_id",
                            value: proposalID.uuidString
                        )
                    ]
                )
            ],
            actions: [
                WellnessSceneAction(
                    id: "accept",
                    kind: .acceptProposal,
                    label: "Apply",
                    proposalID: proposalID
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL,
                expectedProposalID: proposalID
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .missingExactProposalPreview
            )
        }
    }

    func testWellnessSceneDecodesCalendarOwnershipAndProviderStatus() throws {
        let json = """
            {
              "schema_version": "1",
              "id": "calendar:metadata",
              "intent": "review_schedule",
              "lens": "coordinate",
              "title": "캘린더",
              "summary": "실제 provider 상태를 보존합니다.",
              "severity": "neutral",
              "freshness": "current",
              "confidence": {
                "level": "medium",
                "coverage": "캘린더 1건",
                "limitations": []
              },
              "modules": [{
                "id": "calendar",
                "kind": "calendar_canvas",
                "title": "Calendar",
                "summary": "Metadata",
                "items": [],
                "visualization": {
                  "kind": "calendar_canvas",
                  "unit": null,
                  "minimum": null,
                  "maximum": null,
                  "series": [],
                  "events": [{
                    "id": "google:owned",
                    "title": "Team sync",
                    "starts_at": "2026-08-09T09:00:00Z",
                    "ends_at": "2026-08-09T10:00:00Z",
                    "provider": "google",
                    "is_healthmes_managed": false,
                    "energy_demand": "medium",
                    "is_all_day": false,
                    "is_recurring": true,
                    "is_locked": true,
                    "has_attendees": true,
                    "organizer_self": false,
                    "provider_status": "confirmed",
                    "status": "current"
                  }]
                },
                "accessibility_summary": "캘린더 일정 1건"
              }],
              "actions": [],
              "generated_at": "2026-08-09T00:00:00Z",
              "timezone": "America/Los_Angeles"
            }
            """

        let scene = try GlanceJSON.decoder().decode(
            WellnessScene.self,
            from: Data(json.utf8)
        )
        let event = try XCTUnwrap(
            scene.modules.first?.visualization?.events.first
        )

        XCTAssertEqual(scene.timezone, "America/Los_Angeles")
        XCTAssertEqual(event.energyDemand, "medium")
        XCTAssertFalse(event.organizerSelf)
        XCTAssertEqual(event.providerStatus, "confirmed")
        XCTAssertTrue(event.isRecurring)
        XCTAssertTrue(event.isLocked)
        XCTAssertTrue(event.hasAttendees)
        XCTAssertNoThrow(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        )
    }

    func testWellnessSceneValidatorRejectsModuleVisualizationMismatch() {
        let scene = WellnessScene(
            id: "mismatch",
            lens: .now,
            title: "Mismatch",
            summary: "Mismatch",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "capacity",
                    kind: .capacityBar,
                    title: "Capacity",
                    summary: "Capacity",
                    visualization: WellnessVisualization(
                        kind: .comparisonBar,
                        series: [
                            WellnessSeries(
                                id: "capacity",
                                label: "Capacity",
                                points: [WellnessPoint(label: "Now", value: 62)]
                            )
                        ]
                    )
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .moduleVisualizationMismatch
            )
        }
    }

    func testWellnessSceneValidatorRejectsDuplicateVisualizationIdentity() {
        let duplicated = WellnessSeries(
            id: "energy",
            label: "Energy",
            points: [WellnessPoint(label: "09", value: 60)]
        )
        let scene = WellnessScene(
            id: "duplicate-series",
            lens: .now,
            title: "Energy",
            summary: "Energy",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "energy",
                    kind: .timeSeries,
                    title: "Energy",
                    summary: "Energy",
                    visualization: WellnessVisualization(
                        kind: .timeSeries,
                        series: [duplicated, duplicated]
                    )
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .duplicateVisualizationID
            )
        }
    }

    func testWellnessSceneValidatorRejectsInvalidCalendarEventRange() {
        let start = Date()
        let scene = WellnessScene(
            id: "invalid-event-range",
            lens: .coordinate,
            title: "Calendar",
            summary: "Invalid event",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "calendar",
                    kind: .calendarCanvas,
                    title: "Calendar",
                    summary: "Invalid event",
                    visualization: WellnessVisualization(
                        kind: .calendarCanvas,
                        events: [
                            WellnessCalendarEvent(
                                id: "google:invalid",
                                title: "Invalid",
                                startsAt: start,
                                endsAt: start,
                                provider: "google",
                                isHealthMesManaged: false
                            )
                        ]
                    )
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .invalidCalendarEventRange
            )
        }
    }

    func testWellnessSceneValidatorFailsClosedForNonCurrentProposalActions() {
        let proposalID = UUID()
        for freshness in [
            WellnessFreshness.stale,
            .insufficientData,
            .offline,
        ] {
            let scene = WellnessScene(
                id: "non-current-\(freshness.rawValue)",
                lens: .coordinate,
                title: "Schedule",
                summary: "No stale action",
                severity: .action,
                freshness: freshness,
                confidence: WellnessConfidence(
                    level: freshness == .insufficientData ? .insufficientData : .low,
                    coverage: "Incomplete"
                ),
                modules: [
                    WellnessSceneModule(
                        id: "decision",
                        kind: .decision,
                        title: "Decision",
                        summary: "Blocked"
                    )
                ],
                actions: [
                    WellnessSceneAction(
                        id: "accept",
                        kind: .acceptProposal,
                        label: "Yes",
                        proposalID: proposalID
                    )
                ]
            )

            XCTAssertFalse(scene.allowsProposalActions)
            XCTAssertThrowsError(
                try WellnessSceneValidator.validate(
                    scene,
                    pairedBaseURL: pairing.baseURL,
                    expectedProposalID: proposalID
                )
            ) { error in
                XCTAssertEqual(
                    error as? WellnessSceneValidationError,
                    .nonCurrentMutation
                )
            }
        }
    }

    func testWellnessSceneValidatorRejectsUnexpectedProposalIdentity() {
        let shownProposalID = UUID()
        let differentProposalID = UUID()
        let scene = WellnessScene(
            id: "proposal-mismatch",
            lens: .coordinate,
            title: "Schedule",
            summary: "Review this exact proposal",
            severity: .action,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "decision",
                    kind: .decision,
                    title: "Decision",
                    summary: "Exact proposal"
                )
            ],
            actions: [
                WellnessSceneAction(
                    id: "accept",
                    kind: .acceptProposal,
                    label: "Yes",
                    proposalID: differentProposalID
                )
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL,
                expectedProposalID: shownProposalID
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .proposalMismatch)
        }
    }

    func testWellnessSceneValidatorRejectsDuplicateCalendarIdentityAcrossModules() {
        let start = Date()
        let duplicatedEvent = WellnessCalendarEvent(
            id: "google:same-event",
            title: "Deep Work",
            startsAt: start,
            endsAt: start.addingTimeInterval(3_600),
            provider: "google",
            isHealthMesManaged: false
        )
        let scene = WellnessScene(
            id: "duplicate-calendar-event",
            lens: .coordinate,
            title: "Calendar",
            summary: "Duplicate event",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "calendar",
                    kind: .calendarCanvas,
                    title: "Calendar",
                    summary: "Current",
                    visualization: WellnessVisualization(
                        kind: .calendarCanvas,
                        events: [duplicatedEvent]
                    )
                ),
                WellnessSceneModule(
                    id: "comparison",
                    kind: .scheduleComparison,
                    title: "Comparison",
                    summary: "Proposed",
                    visualization: WellnessVisualization(
                        kind: .scheduleComparison,
                        events: [duplicatedEvent]
                    )
                ),
            ]
        )

        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .duplicateVisualizationID
            )
        }
    }

    func testWellnessSceneValidatorRejectsEmptyOutOfRangeAndMisalignedSeries() {
        func scene(_ visualization: WellnessVisualization) -> WellnessScene {
            WellnessScene(
                id: UUID().uuidString,
                lens: .now,
                title: "Energy",
                summary: "Series",
                severity: .neutral,
                freshness: .current,
                modules: [
                    WellnessSceneModule(
                        id: "energy",
                        kind: .timeSeries,
                        title: "Energy",
                        summary: "Series",
                        visualization: visualization
                    )
                ]
            )
        }

        let empty = scene(
            WellnessVisualization(
                kind: .timeSeries,
                series: [
                    WellnessSeries(
                        id: "empty",
                        label: "Energy",
                        points: [WellnessPoint(label: "09", value: nil)]
                    )
                ]
            )
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(empty, pairedBaseURL: pairing.baseURL)
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .emptyVisualization)
        }

        let outOfRange = scene(
            WellnessVisualization(
                kind: .timeSeries,
                minimum: 0,
                maximum: 100,
                series: [
                    WellnessSeries(
                        id: "overflow",
                        label: "Energy",
                        points: [WellnessPoint(label: "09", value: 150)]
                    )
                ]
            )
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                outOfRange,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .visualizationValueOutOfRange
            )
        }

        let misaligned = scene(
            WellnessVisualization(
                kind: .timeSeries,
                series: [
                    WellnessSeries(
                        id: "primary",
                        label: "Energy",
                        points: [WellnessPoint(label: "09", value: 60)]
                    ),
                    WellnessSeries(
                        id: "secondary",
                        label: "Stress",
                        points: [WellnessPoint(label: "10", value: 40)]
                    ),
                ]
            )
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                misaligned,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .inconsistentXAxis)
        }
    }

    func testWellnessSceneValidatorRejectsEmptyAndInvalidVisualizations() {
        let emptyCalendar = WellnessScene(
            id: "empty-calendar",
            lens: .coordinate,
            title: "Calendar",
            summary: "No data",
            severity: .neutral,
            freshness: .insufficientData,
            modules: [
                WellnessSceneModule(
                    id: "calendar",
                    kind: .calendarCanvas,
                    title: "Calendar",
                    summary: "No data",
                    visualization: WellnessVisualization(kind: .calendarCanvas)
                )
            ]
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                emptyCalendar,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(error as? WellnessSceneValidationError, .emptyVisualization)
        }

        let invalidRange = WellnessScene(
            id: "invalid-range",
            lens: .now,
            title: "Energy",
            summary: "Invalid",
            severity: .neutral,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "energy",
                    kind: .energyCurve,
                    title: "Energy",
                    summary: "Invalid",
                    visualization: WellnessVisualization(
                        kind: .energyCurve,
                        minimum: 100,
                        maximum: 0,
                        series: [
                            WellnessSeries(
                                id: "energy",
                                label: "Energy",
                                points: [WellnessPoint(label: "09", value: 70)]
                            )
                        ]
                    )
                )
            ]
        )
        XCTAssertThrowsError(
            try WellnessSceneValidator.validate(
                invalidRange,
                pairedBaseURL: pairing.baseURL
            )
        ) { error in
            XCTAssertEqual(
                error as? WellnessSceneValidationError,
                .invalidVisualizationRange
            )
        }
    }

    func testScheduleComparisonAcceptsEventOnlyVisualization() throws {
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-09T09:00:00Z"))
        let comparison = WellnessVisualization(
            kind: .scheduleComparison,
            events: [
                WellnessCalendarEvent(
                    id: "proposal:1",
                    title: "Deep Work",
                    startsAt: start,
                    endsAt: start.addingTimeInterval(3_600),
                    provider: "healthmes",
                    isHealthMesManaged: true,
                    status: .proposed
                )
            ]
        )
        let scene = WellnessScene(
            id: "schedule-comparison",
            lens: .coordinate,
            title: "Schedule change",
            summary: "New block",
            severity: .action,
            freshness: .current,
            modules: [
                WellnessSceneModule(
                    id: "comparison",
                    kind: .scheduleComparison,
                    title: "Before and after",
                    summary: "New block",
                    visualization: comparison
                )
            ]
        )

        XCTAssertNoThrow(
            try WellnessSceneValidator.validate(
                scene,
                pairedBaseURL: pairing.baseURL
            )
        )
    }

    func testDisplayPolicyKeepsCalendarWhenItIsThirdVisualization() {
        func module(_ id: String, _ kind: WellnessVisualizationKind) -> WellnessSceneModule {
            let visualization: WellnessVisualization
            if kind == .calendarCanvas {
                visualization = WellnessVisualization(
                    kind: kind,
                    events: [
                        WellnessCalendarEvent(
                            id: "google:1",
                            title: "Meeting",
                            startsAt: Date(),
                            endsAt: Date().addingTimeInterval(1_800),
                            provider: "google",
                            isHealthMesManaged: false
                        )
                    ]
                )
            } else {
                visualization = WellnessVisualization(
                    kind: kind,
                    series: [
                        WellnessSeries(
                            id: id,
                            label: id,
                            points: [WellnessPoint(label: "now", value: 50)]
                        )
                    ]
                )
            }
            return WellnessSceneModule(
                id: id,
                kind: WellnessModuleKind(rawValue: kind.rawValue) ?? .fallback,
                title: id,
                summary: id,
                visualization: visualization
            )
        }
        let scene = WellnessScene(
            id: "priority",
            lens: .coordinate,
            title: "Priority",
            summary: "Priority",
            severity: .supportive,
            freshness: .current,
            modules: [
                module("energy", .energyCurve),
                module("goals", .comparisonBar),
                module("calendar", .calendarCanvas),
            ]
        )

        let visible = WellnessSceneDisplayPolicy.visibleModules(
            in: scene,
            maximumVisualizations: 2
        )

        XCTAssertEqual(visible.map(\.id), ["energy", "calendar"])
    }

    func testPhoneInsightPolicyExcludesCalendarAndSparseOrStaleCharts() {
        func module(
            _ id: String,
            _ kind: WellnessVisualizationKind,
            values: [Double?]
        ) -> WellnessSceneModule {
            let visualization: WellnessVisualization
            if kind == .calendarCanvas {
                visualization = WellnessVisualization(
                    kind: kind,
                    events: [
                        WellnessCalendarEvent(
                            id: "google:1",
                            title: "Meeting",
                            startsAt: Date(),
                            endsAt: Date().addingTimeInterval(1_800),
                            provider: "google",
                            isHealthMesManaged: false
                        )
                    ]
                )
            } else {
                visualization = WellnessVisualization(
                    kind: kind,
                    series: [
                        WellnessSeries(
                            id: id,
                            label: id,
                            points: values.enumerated().map {
                                WellnessPoint(label: "\($0.offset)", value: $0.element)
                            }
                        )
                    ]
                )
            }
            return WellnessSceneModule(
                id: id,
                kind: WellnessModuleKind(rawValue: kind.rawValue) ?? .fallback,
                title: id,
                summary: id,
                visualization: visualization
            )
        }

        let current = WellnessScene(
            id: "phone-current",
            lens: .now,
            title: "Current",
            summary: "Current",
            severity: .supportive,
            freshness: .current,
            confidence: WellnessConfidence(level: .medium, coverage: "current"),
            modules: [
                module("calendar", .calendarCanvas, values: []),
                module("sparse", .energyCurve, values: [50]),
                module("capacity", .capacityBar, values: [62]),
                module("trend", .timeSeries, values: [52, 61]),
            ]
        )

        XCTAssertEqual(
            WellnessSceneDisplayPolicy.primaryInsightModules(
                in: current,
                maximumInsights: 1
            ).map(\.id),
            ["capacity"]
        )

        let stale = WellnessScene(
            id: "phone-stale",
            lens: .now,
            title: "Stale",
            summary: "Stale",
            severity: .neutral,
            freshness: .stale,
            confidence: WellnessConfidence(level: .low, coverage: "stale"),
            modules: [module("capacity", .capacityBar, values: [62])]
        )
        XCTAssertTrue(
            WellnessSceneDisplayPolicy.primaryInsightModules(
                in: stale,
                maximumInsights: 1
            ).isEmpty
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
        XCTAssertEqual(WellnessCommandParser.parse("오늘 왜 피곤해?"), .show(.now))
        XCTAssertEqual(WellnessCommandParser.parse("현재 영향"), .show(.now))
        XCTAssertEqual(WellnessCommandParser.parse("일정과 목표"), .show(.coordinate))
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

    func testWatchDecisionLayoutKeepsThreeSecondActionsPinned() {
        XCTAssertTrue(WatchDecisionLayoutPolicy.keepsActionsOutsideScrollContent)
        XCTAssertGreaterThanOrEqual(
            WatchDecisionLayoutPolicy.minimumButtonHeight,
            42
        )
        XCTAssertTrue(
            WatchDecisionLayoutPolicy.canResolve(
                isDecisionContextReady: true,
                hasCurrentWellnessContext: true
            )
        )
        XCTAssertFalse(
            WatchDecisionLayoutPolicy.canResolve(
                isDecisionContextReady: true,
                hasCurrentWellnessContext: false
            )
        )
        XCTAssertFalse(
            WatchDecisionLayoutPolicy.canResolve(
                isDecisionContextReady: false,
                hasCurrentWellnessContext: true
            )
        )
    }

    func testDecisionCorrelationRequiresDecisionIDAndExactTimeWindow() throws {
        let proposalID = UUID()
        let decisionID = UUID()
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T09:00:00Z"))
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

        func alert(decisionId: UUID, after: Date, endsAt: Date) -> AlertItem {
            AlertItem(
                id: UUID(),
                ruleId: "schedule",
                firedAt: start,
                summary: "Recovery changed",
                proposal: "Move Deep Work",
                evidence: nil,
                decisionUrl: nil,
                proposalId: proposalID,
                decisionCard: DecisionCard(
                    decisionId: decisionId,
                    proposalId: proposalID,
                    kind: "schedule_change",
                    severity: "coaching",
                    title: "Deep Work",
                    observationShort: "Recovery changed",
                    evidenceShort: nil,
                    proposedAction: "Move Deep Work?",
                    before: nil,
                    after: after,
                    endsAt: endsAt,
                    expiresAt: start.addingTimeInterval(600),
                    decisionUrl: nil
                )
            )
        }

        XCTAssertTrue(
            PendingDecision.correlate(
                alerts: [alert(
                    decisionId: UUID(),
                    after: start,
                    endsAt: start.addingTimeInterval(3_600)
                )],
                proposals: [proposal]
            ).isEmpty
        )
        XCTAssertTrue(
            PendingDecision.correlate(
                alerts: [alert(
                    decisionId: decisionID,
                    after: start.addingTimeInterval(60),
                    endsAt: start.addingTimeInterval(3_600)
                )],
                proposals: [proposal]
            ).isEmpty
        )
        XCTAssertEqual(
            PendingDecision.correlate(
                alerts: [alert(
                    decisionId: decisionID,
                    after: start,
                    endsAt: start.addingTimeInterval(3_600)
                )],
                proposals: [proposal]
            ).map(\.id),
            [proposalID]
        )
    }

    func testMismatchedDecisionCardCannotCorrelateOrExposeYesNo() throws {
        let proposalID = UUID()
        let otherProposalID = UUID()
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T09:00:00Z"))
        let proposal = ProposalItem(
            id: proposalID,
            taskId: UUID(),
            proposedStart: start,
            proposedEnd: start.addingTimeInterval(3_600),
            status: .proposed,
            decisionRecordId: UUID(),
            acceptResolutionToken: "accept",
            declineResolutionToken: "decline"
        )
        let card = DecisionCard(
            decisionId: UUID(),
            proposalId: otherProposalID,
            kind: "schedule_change",
            severity: "coaching",
            title: "Wrong proposal",
            observationShort: "Low recovery",
            evidenceShort: nil,
            proposedAction: "Move Deep Work?",
            before: nil,
            after: start,
            endsAt: start.addingTimeInterval(3_600),
            expiresAt: start.addingTimeInterval(600),
            decisionUrl: nil
        )
        let alert = AlertItem(
            id: UUID(),
            ruleId: "schedule",
            firedAt: start,
            summary: "Schedule changed",
            proposal: "Move Deep Work",
            evidence: nil,
            decisionUrl: nil,
            proposalId: proposalID,
            decisionCard: card
        )

        XCTAssertFalse(alert.hasConsistentProposalIdentity)
        XCTAssertNil(alert.correlatedDecisionCard)
        XCTAssertTrue(
            PendingDecision.correlate(alerts: [alert], proposals: [proposal]).isEmpty
        )
        let notification = AlertNotificationContent.from(alert: alert)
        XCTAssertEqual(notification.categoryID, AlertNotificationContent.infoCategoryID)
        XCTAssertNil(notification.userInfo[AlertNotificationContent.userInfoProposalID])
    }

    func testTimelineUsesHealthMesTimezoneAndKeepsMidnightEndVisible() throws {
        let start = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T12:30:00Z"))
        let end = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T15:00:00Z"))
        let seoul = try XCTUnwrap(TimeZone(identifier: "Asia/Seoul"))
        let losAngeles = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))

        let seoulDay = try XCTUnwrap(
            WellnessTimelinePolicy.dayInterval(containing: start, timeZone: seoul)
        )
        let losAngelesDay = try XCTUnwrap(
            WellnessTimelinePolicy.dayInterval(containing: start, timeZone: losAngeles)
        )
        XCTAssertNotEqual(seoulDay.start, losAngelesDay.start)

        let bounds = WellnessTimelinePolicy.hourBounds(
            for: [DateInterval(start: start, end: end)],
            timeZone: seoul
        )
        XCTAssertEqual(bounds.upperBound, 24)
    }

    func testCalendarFetchWindowUsesHealthMesTimezone() throws {
        let instant = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T18:30:00Z"))
        let seoul = try XCTUnwrap(TimeZone(identifier: "Asia/Seoul"))
        let losAngeles = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))

        let seoulWindow = try XCTUnwrap(
            WellnessTimelinePolicy.sevenDayInterval(
                containing: instant,
                timeZone: seoul
            )
        )
        let losAngelesWindow = try XCTUnwrap(
            WellnessTimelinePolicy.sevenDayInterval(
                containing: instant,
                timeZone: losAngeles
            )
        )

        XCTAssertNotEqual(seoulWindow.start, losAngelesWindow.start)
        XCTAssertEqual(seoulWindow.duration, 7 * 24 * 60 * 60, accuracy: 1)
        XCTAssertEqual(losAngelesWindow.duration, 7 * 24 * 60 * 60, accuracy: 1)
    }

    func testDecisionSafetyRejectsStaleOrMissingHealthContext() {
        XCTAssertTrue(
            WellnessDecisionSafety.canResolve(
                hasHealthSnapshot: true,
                isBriefingStale: false,
                sceneAllowsActions: true
            )
        )
        XCTAssertFalse(
            WellnessDecisionSafety.canResolve(
                hasHealthSnapshot: true,
                isBriefingStale: true,
                sceneAllowsActions: true
            )
        )
        XCTAssertFalse(
            WellnessDecisionSafety.canResolve(
                hasHealthSnapshot: false,
                isBriefingStale: false,
                sceneAllowsActions: true
            )
        )
        XCTAssertFalse(
            WellnessDecisionSafety.canResolve(
                hasHealthSnapshot: true,
                isBriefingStale: false,
                sceneAllowsActions: false
            )
        )
    }

    func testTimelineLaneCountsResetAfterOverlapCluster() throws {
        let base = try XCTUnwrap(GlanceJSON.parseISO8601("2026-08-10T09:00:00Z"))
        let intervals = [
            DateInterval(start: base, duration: 3_600),
            DateInterval(start: base.addingTimeInterval(900), duration: 3_600),
            DateInterval(start: base.addingTimeInterval(7_200), duration: 1_800),
        ]

        XCTAssertEqual(
            WellnessTimelinePolicy.laneAssignments(for: intervals),
            [
                TimelineLaneAssignment(lane: 0, laneCount: 2),
                TimelineLaneAssignment(lane: 1, laneCount: 2),
                TimelineLaneAssignment(lane: 0, laneCount: 1),
            ]
        )
        XCTAssertFalse(
            WellnessTimelinePolicy.shouldUseReadableList(maxLaneCount: 3)
        )
        XCTAssertTrue(
            WellnessTimelinePolicy.shouldUseReadableList(maxLaneCount: 4)
        )
    }

}
