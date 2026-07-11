import XCTest

// Request-builder tests for the capture/action endpoints. Everything here is
// pure (no network): the bytes and URLs must match the server contracts in
// healthmes/api/media.py, food.py, medical.py, schedule.py, alerts.py.

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

    func testFoodLogRequestBody() throws {
        let request = try HealthMesAPI.foodLogRequest(
            pairing: pairing,
            body: FoodLogCreateBody(
                description: "Bibimbap with extra vegetables",
                mediaPath: "media/2026/07/abc.jpg",
                mealType: "lunch",
                source: "ios-app"
            )
        )
        XCTAssertEqual(request.url?.absoluteString, "http://192.168.1.20:8100/v1/food-logs")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let decoded = try JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any]
        XCTAssertEqual(decoded?["description"] as? String, "Bibimbap with extra vegetables")
        XCTAssertEqual(decoded?["media_path"] as? String, "media/2026/07/abc.jpg")
        XCTAssertEqual(decoded?["meal_type"] as? String, "lunch")
        XCTAssertEqual(decoded?["source"] as? String, "ios-app")
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

    func testProposalActionURLs() {
        let id = UUID(uuidString: "1F0D3C5E-8A2B-4C47-9BE1-3D2A7C9F4E10")!
        let accept = HealthMesAPI.proposalActionRequest(
            pairing: pairing, proposalID: id, action: .accept
        )
        XCTAssertEqual(
            accept.url?.absoluteString,
            "http://192.168.1.20:8100/v1/schedule/proposals/"
                + "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10/accept"
        )
        XCTAssertEqual(accept.httpMethod, "POST")
        let decline = HealthMesAPI.proposalActionRequest(
            pairing: pairing, proposalID: id, action: .decline
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
    }

    func testProposalItemDecodes() throws {
        let json = """
            {
              "id": "1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10",
              "task_id": "7e6a1b2c-93d4-4f58-a1c0-5b8e2f7d9a34",
              "proposed_start": "2026-07-10T09:00:00Z",
              "proposed_end": "2026-07-10T10:30:00Z",
              "status": "proposed",
              "decision_record_id": null
            }
            """
        let proposal = try GlanceJSON.decoder().decode(ProposalItem.self, from: Data(json.utf8))
        XCTAssertEqual(proposal.status, .proposed)
        XCTAssertNil(proposal.decisionRecordId)
        XCTAssertEqual(
            proposal.proposedEnd.timeIntervalSince(proposal.proposedStart), 90 * 60
        )
    }
}
