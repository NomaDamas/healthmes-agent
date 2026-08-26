import XCTest

final class HealthKitIngestContractTests: XCTestCase {
    func testPayloadUsesNativeSchemaAndAllSdkArrays() throws {
        let start = Date(timeIntervalSince1970: 1_786_320_000)
        let payload = HealthKitIngestPayload(
            syncTimestamp: start,
            data: .init(
                records: [
                    .init(
                        id: "metric-1",
                        type: "HKQuantityTypeIdentifierHeartRate",
                        startDate: start,
                        endDate: start.addingTimeInterval(60),
                        value: 62,
                        unit: "count/min",
                        zoneOffset: "+09:00",
                        source: .init(
                            bundleIdentifier: "com.apple.health",
                            productType: "Watch7,5",
                            deviceType: "watch"
                        )
                    )
                ],
                sleep: [
                    .init(
                        id: "sleep-1",
                        stage: "deep",
                        startDate: start,
                        endDate: start.addingTimeInterval(3600)
                    )
                ],
                workouts: [],
                deletions: [
                    .init(id: "deleted-1", type: "HKQuantityTypeIdentifierHeartRate")
                ]
            )
        )
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        let encoded = try encoder.encode(payload)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        XCTAssertEqual(object["schema"] as? String, "healthmes.healthkit.v1")
        XCTAssertEqual(object["sdkVersion"] as? String, "healthmes-ios/1")
        let rows = try XCTUnwrap(object["data"] as? [String: Any])
        XCTAssertEqual((rows["records"] as? [[String: Any]])?.count, 1)
        XCTAssertEqual((rows["sleep"] as? [[String: Any]])?.count, 1)
        XCTAssertEqual((rows["workouts"] as? [[String: Any]])?.count, 0)
        XCTAssertEqual((rows["deletions"] as? [[String: Any]])?.count, 1)
        let metric = try XCTUnwrap((rows["records"] as? [[String: Any]])?.first)
        XCTAssertEqual(metric["zoneOffset"] as? String, "+09:00")
        let source = try XCTUnwrap(metric["source"] as? [String: Any])
        XCTAssertEqual(source["deviceType"] as? String, "watch")
    }

    func testWireFormatConvertsHealthKitFractionsAndOffsets() {
        XCTAssertEqual(HealthKitWireFormat.percentage(fromFraction: 0.97), 97)
        let timeZone = TimeZone(secondsFromGMT: -(5 * 3_600 + 30 * 60))!
        XCTAssertEqual(
            HealthKitWireFormat.zoneOffset(for: Date(timeIntervalSince1970: 0), timeZone: timeZone),
            "-05:30"
        )
    }

    func testUploadRequestUsesPairedServerAndBearer() throws {
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "secret"
        )
        let request = try HealthMesAPI.healthKitUploadRequest(
            pairing: pairing,
            payload: .init(data: .init())
        )
        XCTAssertEqual(
            request.url?.absoluteString,
            "https://healthmes.example/v1/ingest/healthkit"
        )
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"),
            "Bearer secret"
        )
        let body = try XCTUnwrap(request.httpBody)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Idempotency-Key"),
            HealthKitSyncOutboxIdentity.idempotencyKey(for: body)
        )
    }

    func testExactBodyUploadRequestPreservesBytesAndStableKey() throws {
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "secret"
        )
        let body = Data(
            #"{"schema":"healthmes.healthkit.v1","data":{"records":[]}}"#.utf8
        )
        let idempotencyKey =
            HealthKitSyncOutboxIdentity.idempotencyKey(for: body)

        let request = try HealthMesAPI.healthKitUploadRequest(
            pairing: pairing,
            body: body,
            idempotencyKey: idempotencyKey
        )

        XCTAssertEqual(request.httpBody, body)
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Idempotency-Key"),
            idempotencyKey
        )
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Content-Type"),
            "application/json"
        )
    }

    func testExactBodyUploadRequestRejectsUnsafeIdempotencyKeys() {
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example")!,
            token: "secret"
        )
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)

        for key in ["", " leading", "trailing ", "line\nbreak", "한글"] {
            XCTAssertThrowsError(
                try HealthMesAPI.healthKitUploadRequest(
                    pairing: pairing,
                    body: body,
                    idempotencyKey: key
                )
            ) { error in
                XCTAssertEqual(
                    error as? HealthKitUploadRequestError,
                    .invalidIdempotencyKey
                )
            }
        }
    }

    func testAckValidationRequiresDurableExactHashAndSize() throws {
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        let matching = try makeAck(
            durable: true,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count
        )
        XCTAssertNoThrow(try matching.validate(exactBody: body))

        let notDurable = try makeAck(
            durable: false,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count
        )
        XCTAssertThrowsError(try notDurable.validate(exactBody: body)) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .notDurable
            )
        }

        let wrongSize = try makeAck(
            durable: true,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count + 1
        )
        XCTAssertThrowsError(try wrongSize.validate(exactBody: body)) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .sizeMismatch(
                    expected: body.count,
                    received: body.count + 1
                )
            )
        }

        let wrongHash = try makeAck(
            durable: true,
            sha256: String(repeating: "0", count: 64),
            sizeBytes: body.count
        )
        XCTAssertThrowsError(try wrongHash.validate(exactBody: body)) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .hashMismatch
            )
        }
    }

    func testAckRequiresAcceptedForwardingBeforeAnchorsAdvance() throws {
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        for forwardStatus in [
            "queued",
            "nothing_mapped",
        ] {
            let ack = try makeAck(
                durable: true,
                sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
                sizeBytes: body.count,
                forwardStatus: forwardStatus
            )
            XCTAssertNoThrow(
                try ack.validate(exactBody: body),
                forwardStatus
            )
        }

        let legacyDeletionAck = try makeAck(
            durable: true,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count,
            forwardStatus: "deletions_recorded"
        )
        XCTAssertThrowsError(
            try legacyDeletionAck.validate(exactBody: body)
        ) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .deletionForwardingPending
            )
            XCTAssertEqual(
                HealthKitUploadFailureDisposition.classify($0),
                .retryable
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition.permitsAnchorAdvance($0)
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .countsTowardAutomaticQuarantine($0)
            )
        }

        for forwardStatus in ["forward_failed", "skipped_no_user"] {
            let ack = try makeAck(
                durable: true,
                sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
                sizeBytes: body.count,
                forwardStatus: forwardStatus
            )
            XCTAssertThrowsError(try ack.validate(exactBody: body)) {
                XCTAssertEqual(
                    $0 as? HealthKitIngestAckValidationError,
                    .forwardingPending(status: forwardStatus)
                )
            }
        }

        let unknown = try makeAck(
            durable: true,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count,
            forwardStatus: "future_status"
        )
        XCTAssertThrowsError(try unknown.validate(exactBody: body)) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .unsupportedForwardStatus("future_status")
            )
        }

        let unparsed = try makeAck(
            durable: true,
            sha256: HealthKitSyncOutboxIdentity.sha256Hex(body),
            sizeBytes: body.count,
            parseStatus: "stored_unparsed",
            forwardStatus: "nothing_mapped"
        )
        XCTAssertThrowsError(try unparsed.validate(exactBody: body)) {
            XCTAssertEqual(
                $0 as? HealthKitIngestAckValidationError,
                .unsupportedParseStatus("stored_unparsed")
            )
        }
    }

    func testHealthKitUploadFailureDispositionSeparatesPermanentFailures() {
        assertRetryable(
            HealthMesAPIError.transport(
                underlying: URLError(.notConnectedToInternet)
            )
        )
        assertRetryable(HealthMesAPIError.httpStatus(429))
        assertRetryable(HealthMesAPIError.httpStatus(503))
        assertRetryable(
            HealthMesAPIError.server(
                statusCode: 409,
                code: "healthkit_ingest_in_progress",
                message: "still processing",
                detail: nil
            )
        )

        assertTerminal(HealthMesAPIError.unauthorized(statusCode: 401))
        assertTerminal(HealthMesAPIError.httpStatus(413))
        assertTerminal(
            HealthMesAPIError.server(
                statusCode: 422,
                code: "invalid_payload",
                message: "invalid payload",
                detail: nil
            )
        )
        assertRetryable(
            HealthKitIngestAckValidationError.forwardingPending(
                status: "forward_failed"
            )
        )
        assertRetryable(
            HealthKitIngestAckValidationError.deletionForwardingPending
        )
        assertTerminal(HealthKitIngestAckValidationError.hashMismatch)
        assertTerminal(
            HealthKitIngestAckValidationError.unsupportedForwardStatus(
                "future_status"
            )
        )
        assertTerminal(
            HealthKitIngestAckValidationError.unsupportedParseStatus(
                "stored_unparsed"
            )
        )
    }

    func testForwardingFailuresNeverAutoQuarantineOrAdvanceAnchors() {
        let policy = HealthKitSyncRetryPolicy(
            initialDelay: 1,
            maximumDelay: 60,
            maximumAutomaticAttempts: 8
        )
        for status in ["forward_failed", "skipped_no_user"] {
            let error =
                HealthKitIngestAckValidationError.forwardingPending(
                    status: status
                )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .countsTowardAutomaticQuarantine(error)
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .countsTowardAutomaticQuarantine(
                        error,
                        isManualRetry: true
                    )
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .shouldQuarantineAfterAutomaticRetries(
                        error,
                        failedAttempts: 7,
                        retryPolicy: policy
                    ),
                status
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .shouldQuarantineAfterAutomaticRetries(
                        error,
                        failedAttempts: 8,
                        retryPolicy: policy
                    ),
                status
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .shouldQuarantineAfterAutomaticRetries(
                        error,
                        failedAttempts: 8,
                        isManualRetry: true,
                        retryPolicy: policy
                    ),
                status
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .permitsAnchorAdvance(error)
            )
            XCTAssertFalse(
                HealthKitUploadFailureDisposition
                    .permitsAnchorAdvance(
                        error,
                        isManualRetry: true
                    )
            )
        }

        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .shouldQuarantineAfterAutomaticRetries(
                    HealthMesAPIError.transport(
                        underlying: URLError(.notConnectedToInternet)
                    ),
                    failedAttempts: 8,
                    retryPolicy: policy
                )
        )
        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .countsTowardAutomaticQuarantine(
                    HealthMesAPIError.transport(
                        underlying: URLError(.notConnectedToInternet)
                    )
                )
        )
        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .shouldQuarantineAfterAutomaticRetries(
                    HealthMesAPIError.httpStatus(503),
                    failedAttempts: 8,
                    retryPolicy: policy
                )
        )
        let deletionPending = HealthMesAPIError.server(
            statusCode: 503,
            code: "healthkit_deletion_pending",
            message: "canonical deletion is pending",
            detail: nil
        )
        assertRetryable(deletionPending)
        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .countsTowardAutomaticQuarantine(deletionPending)
        )
        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .shouldQuarantineAfterAutomaticRetries(
                    deletionPending,
                    failedAttempts: 8,
                    retryPolicy: policy
                )
        )
        XCTAssertFalse(
            HealthKitUploadFailureDisposition
                .permitsAnchorAdvance(deletionPending)
        )
    }

    private func makeAck(
        durable: Bool,
        sha256: String,
        sizeBytes: Int,
        parseStatus: String = "parsed",
        forwardStatus: String = "queued"
    ) throws -> HealthKitIngestAck {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "raw_id": "00000000-0000-0000-0000-000000000001",
                "durable": durable,
                "sha256": sha256,
                "size_bytes": sizeBytes,
                "parse_status": parseStatus,
                "forward_status": forwardStatus,
                "records_forwarded": 0,
                "sleep_forwarded": 0,
                "workouts_forwarded": 0,
                "deletions_received": 0,
            ],
            options: [.sortedKeys]
        )
        return try JSONDecoder().decode(
            HealthKitIngestAck.self,
            from: data
        )
    }

    private func assertRetryable(
        _ error: Error,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertEqual(
            HealthKitUploadFailureDisposition.classify(error),
            .retryable,
            file: file,
            line: line
        )
    }

    private func assertTerminal(
        _ error: Error,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .terminal =
            HealthKitUploadFailureDisposition.classify(error)
        else {
            XCTFail("Expected terminal failure", file: file, line: line)
            return
        }
    }
}
