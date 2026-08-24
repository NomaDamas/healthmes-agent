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

    private func makeAck(
        durable: Bool,
        sha256: String,
        sizeBytes: Int
    ) throws -> HealthKitIngestAck {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "raw_id": "00000000-0000-0000-0000-000000000001",
                "durable": durable,
                "sha256": sha256,
                "size_bytes": sizeBytes,
                "parse_status": "parsed",
                "forward_status": "queued",
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
}
