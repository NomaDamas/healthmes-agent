import XCTest

// Request-builder tests for the capture/action endpoints. Everything here is
// pure (no network): the bytes and URLs must match the server contracts in
// healthmes/api/media.py, intake_interactions.py, medical.py, schedule.py,
// alerts.py.

final class CaptureRequestTests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "http://192.168.1.20:8100")!,
        token: "secret-token"
    )

    func testMediaUploadRequestIsWellFormedMultipart() throws {
        let payload = Data("fake-jpeg-bytes".utf8)
        let request = HealthMesAPI.mediaUploadRequest(
            pairing: pairing,
            data: payload,
            mediaType: .jpeg,
            boundary: "healthmes-test-boundary"
        )

        XCTAssertEqual(request.url?.absoluteString, "http://192.168.1.20:8100/v1/media")
        XCTAssertEqual(request.httpMethod, "POST")
        // Bearer-only endpoint: the upload must carry the token header.
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Content-Type"),
            "multipart/form-data; boundary=healthmes-test-boundary"
        )

        let body = try XCTUnwrap(request.httpBody)
        let text = try XCTUnwrap(String(data: body, encoding: .utf8))
        // Field name MUST be `file` (server contract), part content type is
        // the canonical allowlist value, bytes are enclosed verbatim.
        XCTAssertTrue(text.hasPrefix("--healthmes-test-boundary\r\n"))
        XCTAssertTrue(
            text.contains(
                "Content-Disposition: form-data; name=\"file\"; filename=\"capture.jpg\"\r\n"
            )
        )
        XCTAssertTrue(text.contains("Content-Type: image/jpeg\r\n\r\nfake-jpeg-bytes\r\n"))
        XCTAssertTrue(text.hasSuffix("--healthmes-test-boundary--\r\n"))
    }

    func testVoiceUploadUsesCanonicalAudioType() throws {
        let request = HealthMesAPI.mediaUploadRequest(
            pairing: pairing,
            data: Data([0x00, 0x01]),
            mediaType: .m4a,
            boundary: "b"
        )
        let text = try XCTUnwrap(String(data: request.httpBody!, encoding: .utf8))
        XCTAssertTrue(text.contains("filename=\"capture.m4a\""))
        XCTAssertTrue(text.contains("Content-Type: audio/mp4\r\n"))
    }

    func testMediaUploadResponseDecodes() throws {
        let json = """
            {
              "media_path": "media/2026/07/0f3a2b1c4d5e6f708192a3b4c5d6e7f8.jpg",
              "content_type": "image/jpeg",
              "bytes": 15
            }
            """
        let upload = try GlanceJSON.decoder().decode(MediaUpload.self, from: Data(json.utf8))
        XCTAssertEqual(upload.mediaPath, "media/2026/07/0f3a2b1c4d5e6f708192a3b4c5d6e7f8.jpg")
        XCTAssertEqual(upload.contentType, "image/jpeg")
        XCTAssertEqual(upload.bytes, 15)
        // Serve-back URL passes the token verbatim after /v1/media/.
        XCTAssertEqual(
            HealthMesAPI.mediaURL(pairing: pairing, mediaPath: upload.mediaPath).absoluteString,
            "http://192.168.1.20:8100/v1/media/media/2026/07/"
                + "0f3a2b1c4d5e6f708192a3b4c5d6e7f8.jpg"
        )
    }

    func testTextNutritionAnalysisUsesAwareStableCaptureInputs() throws {
        let operationID = UUID(
            uuidString: "11111111-2222-3333-4444-555555555555"
        )!
        let observedAt = try XCTUnwrap(
            ISO8601DateFormatter().date(
                from: "2026-08-05T23:30:00Z"
            )
        )
        let request = try HealthMesAPI.intakeAnalysisRequest(
            pairing: pairing,
            body: IntakeInteractionAnalysisBody(
                operationID: operationID,
                modality: "text",
                observedAt: observedAt,
                timezone: "Asia/Seoul",
                source: "ios-app-text",
                sourceText: "Bibimbap with extra vegetables",
                mediaPath: nil,
                allowRemoteAnalysis: false
            )
        )
        XCTAssertEqual(
            request.url?.absoluteString,
            "http://192.168.1.20:8100/v1/intake-interactions/analyze"
        )
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let decoded = try JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any]
        XCTAssertEqual(decoded?["operation_id"] as? String, operationID.uuidString)
        XCTAssertEqual(decoded?["intent"] as? String, "log_consumed")
        XCTAssertEqual(decoded?["modality"] as? String, "text")
        XCTAssertEqual(decoded?["observed_at"] as? String, "2026-08-06T08:30:00.000+09:00")
        XCTAssertEqual(decoded?["timezone"] as? String, "Asia/Seoul")
        XCTAssertEqual(decoded?["source_text"] as? String, "Bibimbap with extra vegetables")
        XCTAssertNil(decoded?["media_path"])
    }

    func testVoiceNutritionUsesMediaAndOmitsSourceText() throws {
        let request = try HealthMesAPI.intakeAnalysisRequest(
            pairing: pairing,
            body: IntakeInteractionAnalysisBody(
                operationID: UUID(
                    uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
                )!,
                modality: "voice",
                observedAt: Date(timeIntervalSince1970: 1_786_000_000),
                timezone: "Asia/Seoul",
                source: "mac-app-voice",
                sourceText: nil,
                mediaPath: "media/2026/08/voice.wav",
                allowRemoteAnalysis: false
            )
        )
        let decoded = try JSONSerialization.jsonObject(
            with: request.httpBody!
        ) as? [String: Any]
        XCTAssertEqual(decoded?["modality"] as? String, "voice")
        XCTAssertEqual(
            decoded?["media_path"] as? String,
            "media/2026/08/voice.wav"
        )
        XCTAssertNil(decoded?["source_text"])
    }

    func testPhotoInteractionOmitsManualItems() throws {
        let request = try HealthMesAPI.photoIntakeRequest(
            pairing: pairing,
            body: PhotoIntakeInteractionBody(
                operationID: UUID(
                    uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
                )!,
                source: "ios-app-photo",
                sourceText: "half portion",
                nutritionObservationID: UUID(
                    uuidString: "01234567-89AB-CDEF-0123-456789ABCDEF"
                )!
            )
        )
        let decoded = try JSONSerialization.jsonObject(
            with: request.httpBody!
        ) as? [String: Any]
        XCTAssertEqual(decoded?["modality"] as? String, "photo")
        XCTAssertEqual(decoded?["source_text"] as? String, "half portion")
        XCTAssertNil(decoded?["items"])
    }

    func testPhotoReviewPrecedesInteractionWithExplicitProvenance() throws {
        let observationID = UUID(
            uuidString: "01234567-89AB-CDEF-0123-456789ABCDEF"
        )!
        let operationID = UUID(
            uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        )!
        let request = try HealthMesAPI.nutritionObservationReviewRequest(
            pairing: pairing,
            observationID: observationID,
            body: NutritionObservationReviewBody(
                operationID: operationID,
                status: .confirmed,
                source: "ios-app",
                items: []
            )
        )

        XCTAssertEqual(
            request.url?.absoluteString,
            "http://192.168.1.20:8100/v1/nutrition-observations/\(observationID.uuidString.lowercased())/review"
        )
        let decoded = try JSONSerialization.jsonObject(
            with: request.httpBody!
        ) as? [String: Any]
        XCTAssertEqual(decoded?["operation_id"] as? String, operationID.uuidString)
        XCTAssertEqual(decoded?["status"] as? String, "confirmed")
        XCTAssertEqual(decoded?["source"] as? String, "ios-app")
        XCTAssertEqual((decoded?["items"] as? [Any])?.count, 0)
    }

    func testOutcomeStatusIsExplicitAndUnchangedItemsAreOmitted() throws {
        let interactionID = UUID(
            uuidString: "01234567-89AB-CDEF-0123-456789ABCDEF"
        )!
        let request = try HealthMesAPI.intakeOutcomeRequest(
            pairing: pairing,
            interactionID: interactionID,
            body: IntakeOutcomeBody(
                operationID: UUID(
                    uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
                )!,
                status: .notConsumed,
                source: "ios-app",
                consumedAt: nil,
                note: nil
            )
        )
        let decoded = try JSONSerialization.jsonObject(
            with: request.httpBody!
        ) as? [String: Any]
        XCTAssertEqual(decoded?["status"] as? String, "not_consumed")
        XCTAssertNil(decoded?["consumed_at"])
        XCTAssertNil(decoded?["corrected_items"])
        XCTAssertNil(decoded?["note"])
    }

    func testCorrectedItemsUseStructuredOutcomeContract() throws {
        let item = IntakeItemResult(
            name: "bibimbap",
            intakeType: "food",
            serving: IntakeServingResult(
                kind: "exact",
                unit: "g",
                exact: 320,
                minimum: nil,
                maximum: nil,
                evidenceText: "user corrected portion",
                estimationBasis: "user"
            ),
            nutrients: [
                IntakeNutrientFactResult(
                    nutrient: "energy",
                    amount: IntakeServingResult(
                        kind: "range",
                        unit: "kcal",
                        exact: nil,
                        minimum: 420,
                        maximum: 520,
                        evidenceText: nil,
                        estimationBasis: "database"
                    ),
                    confidence: "medium",
                    origin: "user",
                    evidenceText: "portion corrected during review"
                )
            ],
            confidence: "high",
            warnings: []
        )
        let request = try HealthMesAPI.intakeOutcomeRequest(
            pairing: pairing,
            interactionID: UUID(),
            body: IntakeOutcomeBody(
                operationID: UUID(),
                status: .consumed,
                source: "ios-app",
                consumedAt: Date(timeIntervalSince1970: 1_786_000_000),
                correctedItems: [item],
                note: nil
            )
        )

        let decoded = try JSONSerialization.jsonObject(
            with: request.httpBody!
        ) as? [String: Any]
        let correctedItems = decoded?["corrected_items"] as? [[String: Any]]
        let corrected = try XCTUnwrap(correctedItems?.first)
        XCTAssertEqual(corrected["name"] as? String, "bibimbap")
        XCTAssertEqual(corrected["intake_type"] as? String, "food")
        XCTAssertNotNil(corrected["serving"] as? [String: Any])
        let nutrients = corrected["nutrients"] as? [[String: Any]]
        XCTAssertEqual(nutrients?.first?["nutrient"] as? String, "energy")
    }

    func testNutritionCorrectionRenamesItemWithoutDiscardingNutrients() {
        var correction = NutritionItemCorrectionDraft(
            item: nutritionCorrectionFixture()
        )
        correction.name = "beef bibimbap"

        XCTAssertTrue(correction.isChanged)
        XCTAssertEqual(correction.correctedItem?.name, "beef bibimbap")
        XCTAssertEqual(correction.correctedItem?.nutrients.count, 1)
    }

    func testNutritionCorrectionClearsNutrientsWhenServingChanges() {
        var correction = NutritionItemCorrectionDraft(
            item: nutritionCorrectionFixture()
        )
        correction.exactAmount = "420"
        correction.unit = "g"

        let corrected = correction.correctedItem
        XCTAssertTrue(correction.isChanged)
        XCTAssertEqual(corrected?.serving.exact, 420)
        XCTAssertEqual(corrected?.serving.unit, "g")
        XCTAssertEqual(corrected?.nutrients, [])
        XCTAssertTrue(
            corrected?.warnings.contains(
                "Serving corrected by user; nutrients require recalculation."
            ) == true
        )
    }

    func testNutritionCorrectionExcludesMisidentifiedItem() {
        var correction = NutritionItemCorrectionDraft(
            item: nutritionCorrectionFixture()
        )
        correction.isExcluded = true

        XCTAssertTrue(correction.isChanged)
        XCTAssertNil(correction.correctedItem)
    }

    func testNutritionCorrectionRejectsInvalidServingInsteadOfFallingBack() {
        var correction = NutritionItemCorrectionDraft(
            item: nutritionCorrectionFixture()
        )
        correction.exactAmount = "zero"

        XCTAssertTrue(correction.isChanged)
        XCTAssertFalse(correction.isValid)
        XCTAssertNotNil(correction.validationMessage)
        XCTAssertNil(correction.correctedItem)
    }

    func testNutritionCorrectionAcceptsCommaDecimal() {
        var correction = NutritionItemCorrectionDraft(
            item: nutritionCorrectionFixture()
        )
        correction.exactAmount = "420,5"

        XCTAssertTrue(correction.isValid)
        XCTAssertEqual(correction.correctedItem?.serving.exact, 420.5)
    }

    func testUnchangedNutritionCorrectionKeepsOriginalItem() {
        let original = nutritionCorrectionFixture()
        let correction = NutritionItemCorrectionDraft(item: original)

        XCTAssertFalse(correction.isChanged)
        XCTAssertEqual(correction.correctedItem, original)
    }

    func testNutritionDraftReusesRetryIdentity() {
        let interactionID = UUID(
            uuidString: "11111111-2222-3333-4444-555555555555"
        )!
        let observedAt = Date(timeIntervalSince1970: 1_786_000_000)
        var draft = NutritionCaptureDraft(
            interactionOperationID: interactionID,
            modality: .voice,
            observedAt: observedAt,
            timezone: "Asia/Seoul",
            source: "ios-app-voice"
        )
        draft.uploadedMediaPath = "media/2026/08/voice.m4a"

        let first = draft.outcome(
            for: .consumed,
            now: Date(timeIntervalSince1970: 1_786_000_100)
        )
        let retry = draft.outcome(
            for: .consumed,
            now: Date(timeIntervalSince1970: 1_786_000_200)
        )
        let changed = draft.outcome(
            for: .cancelled,
            now: Date(timeIntervalSince1970: 1_786_000_300)
        )

        XCTAssertEqual(draft.interactionOperationID, interactionID)
        XCTAssertEqual(draft.observedAt, observedAt)
        XCTAssertEqual(draft.timezone, "Asia/Seoul")
        XCTAssertEqual(
            draft.uploadedMediaPath,
            "media/2026/08/voice.m4a"
        )
        XCTAssertEqual(first, retry)
        XCTAssertNotEqual(changed.operationID, first.operationID)
        XCTAssertEqual(changed.status, .cancelled)
    }

    func testNutritionDraftChangesOutcomeIdentityWhenCorrectionsChange() {
        let original = nutritionCorrectionFixture()
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
            source: "ios-app-text"
        )

        let first = draft.outcome(
            for: .consumed,
            correctedItems: [original],
            note: "meal_type=lunch",
            now: Date(timeIntervalSince1970: 1_786_000_100)
        )
        let retry = draft.outcome(
            for: .consumed,
            correctedItems: [original],
            note: "meal_type=lunch",
            now: Date(timeIntervalSince1970: 1_786_000_200)
        )
        let corrected = draft.outcome(
            for: .consumed,
            correctedItems: [renamed],
            note: "meal_type=lunch",
            now: Date(timeIntervalSince1970: 1_786_000_300)
        )

        XCTAssertEqual(first, retry)
        XCTAssertNotEqual(first.operationID, corrected.operationID)
        XCTAssertEqual(corrected.correctedItems, [renamed])
        XCTAssertEqual(corrected.actedAt, Date(timeIntervalSince1970: 1_786_000_300))
    }

    func testNutritionDraftReusesReviewIdentityUntilReviewChanges() {
        var draft = NutritionCaptureDraft(
            modality: .photo,
            source: "ios-app-photo"
        )
        let first = draft.nutritionReview(status: .confirmed, items: [])
        let retry = draft.nutritionReview(status: .confirmed, items: [])
        let rejected = draft.nutritionReview(status: .rejected, items: [])

        XCTAssertEqual(first, retry)
        XCTAssertNotEqual(first.operationID, rejected.operationID)
        XCTAssertEqual(rejected.status, .rejected)
    }

    private func nutritionCorrectionFixture() -> IntakeItemResult {
        IntakeItemResult(
            name: "bibimbap",
            intakeType: "food",
            serving: IntakeServingResult(
                kind: "exact",
                unit: "g",
                exact: 320,
                minimum: nil,
                maximum: nil,
                evidenceText: "visual estimate",
                estimationBasis: "model"
            ),
            nutrients: [
                IntakeNutrientFactResult(
                    nutrient: "energy",
                    amount: IntakeServingResult(
                        kind: "range",
                        unit: "kcal",
                        exact: nil,
                        minimum: 420,
                        maximum: 520,
                        evidenceText: nil,
                        estimationBasis: "database"
                    ),
                    confidence: "medium",
                    origin: "vlm",
                    evidenceText: nil
                )
            ],
            confidence: "medium",
            warnings: ["portion estimated"]
        )
    }

    func testPhotoObservationAndInteractionExposeReviewDetails() throws {
        let observationJSON = """
            {
              "observation_id": "01234567-89ab-cdef-0123-456789abcdef",
              "status": "usable",
              "confidence": "medium",
              "warnings": ["portion estimated"],
              "items": [{
                "intake_type": "food",
                "name_candidates": ["bibimbap"],
                "category": "meal",
                "serving": {
                  "kind": "range",
                  "unit": "g",
                  "exact": null,
                  "minimum": 350,
                  "maximum": 450,
                  "evidence_text": null,
                  "estimation_basis": "visual"
                },
                "caffeine": {
                  "kind": "unknown",
                  "unit": "mg",
                  "exact": null,
                  "minimum": null,
                  "maximum": null,
                  "evidence_text": null,
                  "estimation_basis": null
                },
                "nutrients": [],
                "confidence": "medium",
                "warnings": []
              }],
              "confirmation_status": "unconfirmed"
            }
            """
        let observation = try GlanceJSON.decoder().decode(
            NutritionObservationResult.self,
            from: Data(observationJSON.utf8)
        )
        XCTAssertEqual(observation.items.first?.nameCandidates, ["bibimbap"])
        XCTAssertEqual(observation.items.first?.serving.summary, "350–450 g")

        let interactionJSON = """
            {
              "interaction_id": "11111111-2222-3333-4444-555555555555",
              "modality": "photo",
              "source_text": "half portion",
              "resolved_items": [{
                "name": "bibimbap",
                "intake_type": "food",
                "serving": {
                  "kind": "range",
                  "unit": "g",
                  "exact": null,
                  "minimum": 175,
                  "maximum": 225,
                  "evidence_text": "half portion",
                  "estimation_basis": "user_context"
                },
                "nutrients": [{
                  "nutrient": "energy",
                  "amount": {
                    "kind": "range",
                    "unit": "kcal",
                    "exact": null,
                    "minimum": 280,
                    "maximum": 380,
                    "evidence_text": null,
                    "estimation_basis": "model"
                  },
                  "confidence": "medium",
                  "origin": "vlm",
                  "evidence_text": null
                }],
                "confidence": "medium",
                "warnings": ["portion estimated"]
              }],
              "warnings": [],
              "is_confirmed_intake": false
            }
            """
        let interaction = try GlanceJSON.decoder().decode(
            IntakeInteractionResult.self,
            from: Data(interactionJSON.utf8)
        )
        XCTAssertFalse(interaction.isConfirmedIntake)
        XCTAssertEqual(interaction.resolvedItems.first?.name, "bibimbap")
        XCTAssertEqual(
            interaction.resolvedItems.first?.nutrients.first?.amount.summary,
            "280–380 kcal"
        )
    }

    func testMedicalRecordRequestBodyKeepsContextCaptureOnly() throws {
        let request = try HealthMesAPI.medicalRecordRequest(
            pairing: pairing,
            body: MedicalRecordCreateBody(
                kind: .medication,
                description: "White round pill, label reads 5mg",
                mediaPath: "media/2026/07/pill.jpg",
                transcript: nil,
                context: ["source": .string("ios-app-photo")]
            )
        )
        XCTAssertEqual(
            request.url?.absoluteString, "http://192.168.1.20:8100/v1/medical-records"
        )
        let decoded = try JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any]
        XCTAssertEqual(decoded?["kind"] as? String, "medication")
        XCTAssertEqual(decoded?["description"] as? String, "White round pill, label reads 5mg")
        XCTAssertEqual(decoded?["media_path"] as? String, "media/2026/07/pill.jpg")
        let context = decoded?["context"] as? [String: Any]
        // Capture metadata only — the server owns context.health.
        XCTAssertEqual(context?.count, 1)
        XCTAssertEqual(context?["source"] as? String, "ios-app-photo")
    }

    func testProposalActionURLsAndResolutionBody() throws {
        let id = UUID(uuidString: "1F0D3C5E-8A2B-4C47-9BE1-3D2A7C9F4E10")!
        let accept = try HealthMesAPI.proposalActionRequest(
            pairing: pairing,
            proposalID: id,
            action: .accept,
            resolutionToken: "scoped-token"
        )
        XCTAssertEqual(
            accept.url?.absoluteString,
            "http://192.168.1.20:8100/v1/schedule/proposals/"
                + "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10/accept"
        )
        XCTAssertEqual(accept.httpMethod, "POST")
        XCTAssertEqual(
            try JSONSerialization.jsonObject(with: accept.httpBody!) as? [String: String],
            [
                "resolution_token": "scoped-token",
                "surface": "ios_app",
            ]
        )
        let decline = try HealthMesAPI.proposalActionRequest(
            pairing: pairing,
            proposalID: id,
            action: .decline,
            resolutionToken: "scoped-token"
        )
        XCTAssertTrue(decline.url!.absoluteString.hasSuffix("/decline"))
    }

    func testAlertsAndReportRequests() {
        let alerts = HealthMesAPI.alertsRequest(pairing: pairing, hours: 24, limit: 50, offset: 0)
        XCTAssertEqual(
            alerts.url?.absoluteString,
            "http://192.168.1.20:8100/v1/alerts?hours=24&limit=50&offset=0"
        )
        XCTAssertEqual(alerts.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")

        let report = HealthMesAPI.weeklyReportRequest(pairing: pairing)
        XCTAssertEqual(
            report.url?.absoluteString, "http://192.168.1.20:8100/reports/weekly.json"
        )

        let proposals = HealthMesAPI.proposalsRequest(pairing: pairing, status: .proposed)
        XCTAssertEqual(
            proposals.url?.absoluteString,
            "http://192.168.1.20:8100/v1/schedule/proposals?limit=50&status=proposed"
        )

        let proposal = HealthMesAPI.proposalRequest(
            pairing: pairing,
            proposalID: UUID(uuidString: "1F0D3C5E-8A2B-4C47-9BE1-3D2A7C9F4E10")!
        )
        XCTAssertEqual(
            proposal.url?.absoluteString,
            "http://192.168.1.20:8100/v1/schedule/proposals/"
                + "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10"
        )
    }

    func testErrorEnvelopeMapping() throws {
        let envelope = """
            {
              "error": {
                "code": "invalid_transition",
                "message": "schedule_proposal cannot transition from 'accepted' to 'declined'",
                "detail": {"current": "accepted", "requested": "declined"}
              }
            }
            """
        let decoded = try JSONDecoder().decode(APIErrorEnvelope.self, from: Data(envelope.utf8))
        let error = HealthMesAPIError.server(
            statusCode: 409,
            code: decoded.error.code,
            message: decoded.error.message,
            detail: decoded.error.detail
        )
        XCTAssertTrue(error.isAlreadyResolved)
        XCTAssertEqual(error.alreadyResolvedStatus, "accepted")

        let other = HealthMesAPIError.server(
            statusCode: 422, code: "validation_error", message: "bad", detail: nil
        )
        XCTAssertFalse(other.isAlreadyResolved)
        XCTAssertNil(other.alreadyResolvedStatus)

        let expired = HealthMesAPIError.server(
            statusCode: 409, code: "proposal_expired", message: "expired", detail: nil
        )
        XCTAssertTrue(expired.isProposalExpired)
        XCTAssertFalse(other.isProposalExpired)
    }

    func testForbiddenResolutionTokenPreservesServerError() {
        let envelope = """
            {
              "error": {
                "code": "invalid_resolution_token",
                "message": "The schedule proposal resolution token is invalid",
                "detail": null
              }
            }
            """
        let error = HealthMesAPI.responseError(
            statusCode: 403,
            data: Data(envelope.utf8)
        )

        guard case .server(403, "invalid_resolution_token", let message, _) = error else {
            return XCTFail("expected structured forbidden server error")
        }
        XCTAssertEqual(
            message,
            "The schedule proposal resolution token is invalid"
        )
    }

    func testProposalItemDecodes() throws {
        let json = """
            {
              "id": "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10",
              "task_id": "7e6a1b2c-93d4-4f58-a1c0-5b8e2f7d9a34",
              "proposed_start": "2026-07-10T09:00:00Z",
              "proposed_end": "2026-07-10T10:30:00Z",
              "status": "proposed",
              "decision_record_id": null,
              "healthmes_kind": "planned_sleep",
              "accept_resolution_token": "accept-token",
              "decline_resolution_token": "decline-token"
            }
            """
        let proposal = try GlanceJSON.decoder().decode(ProposalItem.self, from: Data(json.utf8))
        XCTAssertEqual(proposal.status, .proposed)
        XCTAssertTrue(proposal.isActionable)
        XCTAssertNil(proposal.decisionRecordId)
        XCTAssertEqual(proposal.healthmesKind, "planned_sleep")
        XCTAssertEqual(proposal.resolutionToken(for: .accept), "accept-token")
        XCTAssertEqual(proposal.resolutionToken(for: .decline), "decline-token")
        XCTAssertEqual(
            proposal.proposedEnd.timeIntervalSince(proposal.proposedStart), 90 * 60
        )

        let expiredJSON = json
            .replacingOccurrences(of: "\"accept-token\"", with: "null")
            .replacingOccurrences(of: "\"decline-token\"", with: "null")
        let expired = try GlanceJSON.decoder().decode(
            ProposalItem.self,
            from: Data(expiredJSON.utf8)
        )
        XCTAssertEqual(expired.status, .proposed)
        XCTAssertFalse(expired.isActionable)
    }
}
