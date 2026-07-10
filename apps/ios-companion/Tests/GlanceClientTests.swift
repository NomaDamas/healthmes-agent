import XCTest

// The Shared sources are compiled directly into this host-less test bundle
// (see project.yml), so everything is same-module — no app import needed.
//
// Tests/Fixtures/glance.json mirrors the server's own seeded exact-payload
// test (tests/api/test_briefing.py::test_seeded_glance_payload...): if the
// contract drifts on either side, one of the two suites goes red.

final class GlanceContractDecodingTests: XCTestCase {
    private func fixtureData() throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: GlanceContractDecodingTests.self)
                .url(forResource: "glance", withExtension: "json"),
            "glance.json fixture missing from the test bundle resources"
        )
        return try Data(contentsOf: url)
    }

    func testDecodesContractFixtureExactly() throws {
        let payload = try GlanceJSON.decodePayload(fixtureData())

        // generated_at: 2026-07-09T14:23:00Z
        XCTAssertEqual(payload.generatedAt.timeIntervalSince1970, 1_783_606_980, accuracy: 0.001)
        XCTAssertEqual(payload.timezone, "UTC")

        // energy
        XCTAssertEqual(payload.energy.score, 58)
        XCTAssertEqual(payload.energy.confidence, .high)
        XCTAssertEqual(payload.energy.curve24h.count, 24)
        XCTAssertEqual(payload.energy.curve24h.map(\.hour), Array(0..<24))
        let expectedScores: [Int: Int] = [8: 71, 13: 64, 14: 58]
        for point in payload.energy.curve24h {
            XCTAssertEqual(point.score, expectedScores[point.hour], "hour \(point.hour)")
        }

        // next_blocks: soonest first, calendar/proposal merged
        XCTAssertEqual(payload.nextBlocks.count, 3)
        let first = payload.nextBlocks[0]
        XCTAssertEqual(first.title, "Deep work block")
        XCTAssertEqual(first.energyDemand, .high)
        XCTAssertEqual(first.source, .calendar)
        XCTAssertEqual(first.start.timeIntervalSince1970, 1_783_605_600, accuracy: 0.001)  // 14:00Z
        XCTAssertEqual(first.end.timeIntervalSince1970, 1_783_609_200, accuracy: 0.001)  // 15:00Z
        let second = payload.nextBlocks[1]
        XCTAssertEqual(second.title, "Write weekly report")
        XCTAssertEqual(second.energyDemand, .med)
        XCTAssertEqual(second.source, .proposal)
        let third = payload.nextBlocks[2]
        XCTAssertNil(third.title)
        XCTAssertNil(third.energyDemand)
        XCTAssertEqual(third.source, .calendar)

        // alerts
        XCTAssertEqual(payload.alerts.unresolvedCount, 2)
        let top = try XCTUnwrap(payload.alerts.top)
        XCTAssertEqual(top.ruleId, "stress_spike_vs_baseline")
        XCTAssertEqual(top.summary, "Stress 82 vs baseline 55")
        XCTAssertEqual(
            top.decisionUrl,
            "http://192.168.1.20:8100/decisions/1f0d3c5e-8a2b-4c47-9be1-3d2a7c9f4e10"
                + "?token=hm-ro-3q2b8d1f7c6e5a4"
        )

        // latest_decision
        let decision = try XCTUnwrap(payload.latestDecision)
        XCTAssertEqual(decision.id, UUID(uuidString: "7e6a1b2c-93d4-4f58-a1c0-5b8e2f7d9a34"))
        XCTAssertEqual(
            decision.url,
            "http://192.168.1.20:8100/decisions/7e6a1b2c-93d4-4f58-a1c0-5b8e2f7d9a34"
                + "?token=hm-ro-3q2b8d1f7c6e5a4"
        )
    }

    func testDecodesEmptyDatabaseAllNullShape() throws {
        let curve = (0..<24).map { "{\"hour\": \($0), \"score\": null}" }
            .joined(separator: ",")
        let json = """
            {
              "generated_at": "2026-07-09T14:23:00Z",
              "timezone": "UTC",
              "energy": {"score": null, "confidence": "low", "curve_24h": [\(curve)]},
              "next_blocks": [],
              "alerts": {"unresolved_count": 0, "top": null},
              "latest_decision": null
            }
            """
        let payload = try GlanceJSON.decodePayload(Data(json.utf8))
        XCTAssertNil(payload.energy.score)
        XCTAssertEqual(payload.energy.confidence, .low)
        XCTAssertEqual(payload.energy.curve24h.count, 24)
        XCTAssertTrue(payload.energy.curve24h.allSatisfy { $0.score == nil })
        XCTAssertTrue(payload.nextBlocks.isEmpty)
        XCTAssertEqual(payload.alerts.unresolvedCount, 0)
        XCTAssertNil(payload.alerts.top)
        XCTAssertNil(payload.latestDecision)
    }

    func testAcceptsFractionalSecondAndOffsetTimestamps() {
        // pydantic emits "Z" for whole seconds, but stay tolerant of
        // fractional seconds and numeric offsets.
        let base = GlanceJSON.parseISO8601("2026-07-09T14:23:00Z")
        XCTAssertNotNil(base)
        let fractional = GlanceJSON.parseISO8601("2026-07-09T14:23:00.123456Z")
        XCTAssertNotNil(fractional)
        XCTAssertEqual(
            fractional!.timeIntervalSince1970, base!.timeIntervalSince1970 + 0.123,
            accuracy: 0.001
        )
        let offset = GlanceJSON.parseISO8601("2026-07-09T23:23:00+09:00")
        XCTAssertEqual(offset, base)
        XCTAssertNil(GlanceJSON.parseISO8601("not a datetime"))
    }

    func testNextBlockLineRendersServerTimezone() throws {
        let payload = try GlanceJSON.decodePayload(fixtureData())
        XCTAssertEqual(GlanceFormat.nextBlockLine(payload), "14:00 Deep work block [high]")
        XCTAssertEqual(GlanceFormat.alertsLine(payload), "2 alerts · Stress 82 vs baseline 55")
    }
}

final class GlanceClientBehaviourTests: XCTestCase {
    private let pairing = Pairing(
        baseURL: URL(string: "http://192.168.1.20:8100")!,
        token: "secret-token"
    )

    func testRequestCarriesBearerAndConditionalHeaders() {
        let request = GlanceClient.makeRequest(pairing: pairing, ifNoneMatch: "\"abc\"")
        XCTAssertEqual(
            request.url?.absoluteString,
            "http://192.168.1.20:8100/v1/briefing/glance"
        )
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "If-None-Match"), "\"abc\"")

        // Token-less loopback pairing: no Authorization header at all.
        let open = Pairing(baseURL: URL(string: "http://127.0.0.1:8100")!, token: nil)
        let openRequest = GlanceClient.makeRequest(pairing: open, ifNoneMatch: nil)
        XCTAssertNil(openRequest.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(openRequest.value(forHTTPHeaderField: "If-None-Match"))
    }

    func testBaseURLNormalizationKeepsSubpathsAndRejectsGarbage() throws {
        XCTAssertEqual(
            try PairingStore.normalizeBaseURL(" http://192.168.1.20:8100/ ").absoluteString,
            "http://192.168.1.20:8100"
        )
        XCTAssertEqual(
            try PairingStore.normalizeBaseURL("https://home.example/healthmes/").absoluteString,
            "https://home.example/healthmes"
        )
        XCTAssertThrowsError(try PairingStore.normalizeBaseURL("192.168.1.20:8100"))
        XCTAssertThrowsError(try PairingStore.normalizeBaseURL("ftp://192.168.1.20"))
        XCTAssertThrowsError(try PairingStore.normalizeBaseURL(""))
    }

    func testCacheControlMaxAgeParsing() {
        XCTAssertEqual(GlanceClient.maxAgeSeconds(fromCacheControl: "private, max-age=300"), 300)
        XCTAssertEqual(GlanceClient.maxAgeSeconds(fromCacheControl: "MAX-AGE=60"), 60)
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: "private"))
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: nil))
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: "max-age=oops"))
    }

    /// End-to-end conditional-GET flow against a stubbed transport:
    /// 200 + ETag stored, then If-None-Match sent and 304 re-serves the
    /// cached body with a refreshed validity window.
    func testFetchStoresETagThenRevalidatesWith304() async throws {
        let fixtureURL = try XCTUnwrap(
            Bundle(for: GlanceClientBehaviourTests.self)
                .url(forResource: "glance", withExtension: "json")
        )
        let body = try Data(contentsOf: fixtureURL)
        let etag = "\"" + String(repeating: "ab", count: 32) + "\""  // strong 66-char ETag
        let headers = ["ETag": etag, "Cache-Control": "private, max-age=300"]

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        let cache = GlanceSnapshotCache(
            fileURL: FileManager.default.temporaryDirectory
                .appendingPathComponent("glance-test-\(UUID().uuidString).json")
        )
        defer { cache.clear() }
        let client = GlanceClient(session: URLSession(configuration: configuration), cache: cache)

        // First poll: unconditional, answered 200.
        StubURLProtocol.handler = { request in
            XCTAssertNil(request.value(forHTTPHeaderField: "If-None-Match"))
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token"
            )
            return (200, headers, body)
        }
        let now = Date(timeIntervalSince1970: 1_783_606_980)
        let first = try await client.fetch(pairing: pairing, now: now)
        XCTAssertFalse(first.revalidated)
        XCTAssertEqual(first.payload.energy.score, 58)
        XCTAssertEqual(first.nextRefresh, now.addingTimeInterval(300))
        XCTAssertEqual(cache.load()?.etag, etag)

        // Second poll: the stored ETag goes out, 304 keeps the cached body.
        StubURLProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "If-None-Match"), etag)
            return (304, headers, Data())
        }
        let later = now.addingTimeInterval(600)
        let second = try await client.fetch(pairing: pairing, now: later)
        XCTAssertTrue(second.revalidated)
        XCTAssertEqual(second.payload, first.payload)
        XCTAssertEqual(second.nextRefresh, later.addingTimeInterval(300))
        XCTAssertEqual(cache.load()?.fetchedAt, later)

        // Token rejection surfaces as .unauthorized.
        StubURLProtocol.handler = { _ in (401, [:], Data()) }
        do {
            _ = try await client.fetch(pairing: pairing, now: later)
            XCTFail("expected unauthorized")
        } catch GlanceClientError.unauthorized(let status) {
            XCTAssertEqual(status, 401)
        }
    }
}

/// In-process transport stub — no network is ever touched in these tests.
final class StubURLProtocol: URLProtocol {
    static var handler: ((URLRequest) -> (Int, [String: String], Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = StubURLProtocol.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        let (status, headers, body) = handler(request)
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
