import XCTest

final class NutritionCaptureContractTests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "https://healthmes.example")!,
        token: "test-token"
    )

    func testVoiceAnalysisUsesStableMediaCaptureInputs() throws {
        let operationID = UUID(
            uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        )!
        let observedAt = Date(timeIntervalSince1970: 1_786_000_000)
        let request = try HealthMesAPI.intakeAnalysisRequest(
            pairing: pairing,
            body: IntakeInteractionAnalysisBody(
                operationID: operationID,
                modality: "voice",
                observedAt: observedAt,
                timezone: "Asia/Seoul",
                source: "mac-app-voice",
                sourceText: nil,
                mediaPath: "media/2026/08/voice.wav",
                allowRemoteAnalysis: false
            )
        )

        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: request.httpBody!)
                as? [String: Any]
        )
        XCTAssertEqual(body["operation_id"] as? String, operationID.uuidString)
        XCTAssertEqual(body["modality"] as? String, "voice")
        XCTAssertEqual(body["observed_at"] as? String, "2026-08-06T16:06:40.000+09:00")
        XCTAssertEqual(body["timezone"] as? String, "Asia/Seoul")
        XCTAssertEqual(
            body["media_path"] as? String,
            "media/2026/08/voice.wav"
        )
        XCTAssertNil(body["source_text"])
    }

    func testDraftReusesInteractionAndOutcomeRetryIdentity() {
        let operationID = UUID(
            uuidString: "11111111-2222-3333-4444-555555555555"
        )!
        let observedAt = Date(timeIntervalSince1970: 1_786_000_000)
        var draft = NutritionCaptureDraft(
            interactionOperationID: operationID,
            modality: .photo,
            observedAt: observedAt,
            timezone: "Asia/Seoul",
            source: "mac-app-photo"
        )
        draft.uploadedMediaPath = "media/2026/08/meal.jpg"

        let first = draft.outcome(
            for: .consumed,
            now: Date(timeIntervalSince1970: 1_786_000_100)
        )
        let retry = draft.outcome(
            for: .consumed,
            now: Date(timeIntervalSince1970: 1_786_000_200)
        )

        XCTAssertEqual(draft.interactionOperationID, operationID)
        XCTAssertEqual(draft.observedAt, observedAt)
        XCTAssertEqual(draft.timezone, "Asia/Seoul")
        XCTAssertEqual(draft.uploadedMediaPath, "media/2026/08/meal.jpg")
        XCTAssertEqual(first, retry)
    }

    func testDraftChangesOutcomeIdentityWhenCorrectionsChange() {
        let original = IntakeItemResult(
            name: "bibimbap",
            intakeType: "food",
            serving: IntakeServingResult(
                kind: "exact",
                unit: "g",
                exact: 320,
                minimum: nil,
                maximum: nil,
                evidenceText: nil,
                estimationBasis: "user"
            ),
            nutrients: [],
            confidence: "high",
            warnings: []
        )
        let renamed = IntakeItemResult(
            name: "beef bibimbap",
            intakeType: original.intakeType,
            serving: original.serving,
            nutrients: original.nutrients,
            confidence: original.confidence,
            warnings: original.warnings
        )
        var draft = NutritionCaptureDraft(
            modality: .text,
            source: "mac-app-text"
        )

        let first = draft.outcome(
            for: .consumed,
            correctedItems: [original],
            now: Date(timeIntervalSince1970: 1_786_000_100)
        )
        let corrected = draft.outcome(
            for: .consumed,
            correctedItems: [renamed],
            now: Date(timeIntervalSince1970: 1_786_000_200)
        )

        XCTAssertNotEqual(first.operationID, corrected.operationID)
        XCTAssertEqual(corrected.correctedItems, [renamed])
    }

    func testOutcomeOmitsUnchangedItemsAndEncodesStructuredCorrections() throws {
        let interactionID = UUID()
        let unchanged = try HealthMesAPI.intakeOutcomeRequest(
            pairing: pairing,
            interactionID: interactionID,
            body: IntakeOutcomeBody(
                operationID: UUID(),
                status: .notConsumed,
                source: "mac-app",
                consumedAt: nil,
                note: nil
            )
        )
        let unchangedBody = try XCTUnwrap(
            JSONSerialization.jsonObject(with: unchanged.httpBody!)
                as? [String: Any]
        )
        XCTAssertNil(unchangedBody["corrected_items"])

        let corrected = try HealthMesAPI.intakeOutcomeRequest(
            pairing: pairing,
            interactionID: interactionID,
            body: IntakeOutcomeBody(
                operationID: UUID(),
                status: .consumed,
                source: "mac-app",
                consumedAt: Date(timeIntervalSince1970: 1_786_000_000),
                correctedItems: [
                    IntakeItemResult(
                        name: "bibimbap",
                        intakeType: "food",
                        serving: IntakeServingResult(
                            kind: "exact",
                            unit: "g",
                            exact: 320,
                            minimum: nil,
                            maximum: nil,
                            evidenceText: "user correction",
                            estimationBasis: "user"
                        ),
                        nutrients: [],
                        confidence: "high",
                        warnings: []
                    )
                ],
                note: nil
            )
        )
        let correctedBody = try XCTUnwrap(
            JSONSerialization.jsonObject(with: corrected.httpBody!)
                as? [String: Any]
        )
        let items = try XCTUnwrap(
            correctedBody["corrected_items"] as? [[String: Any]]
        )
        XCTAssertEqual(items.first?["name"] as? String, "bibimbap")
        XCTAssertNotNil(items.first?["serving"] as? [String: Any])
    }

    func testInvalidServingCorrectionCannotProduceAnOutcomeItem() {
        let item = IntakeItemResult(
            name: "latte",
            intakeType: "beverage",
            serving: IntakeServingResult(
                kind: "exact",
                unit: "ml",
                exact: 355,
                minimum: nil,
                maximum: nil,
                evidenceText: nil,
                estimationBasis: "label"
            ),
            nutrients: [],
            confidence: "high",
            warnings: []
        )
        var correction = NutritionItemCorrectionDraft(item: item)
        correction.exactAmount = "-1"

        XCTAssertFalse(correction.isValid)
        XCTAssertNotNil(correction.validationMessage)
        XCTAssertNil(correction.correctedItem)
    }
}
