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
    }
}
