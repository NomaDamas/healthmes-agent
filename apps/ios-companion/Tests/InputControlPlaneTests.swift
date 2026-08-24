import XCTest

final class InputControlPlaneContractTests: XCTestCase {
    func testDecodesFullDescriptorAndPresentsWearableAggregateHonestly() throws {
        let descriptor = try InputControlPlaneJSON.decoder().decode(
            InputSourceDescriptor.self,
            from: Self.openWearablesDescriptor()
        )

        XCTAssertEqual(descriptor.sourceID, "wearable.open-wearables")
        XCTAssertEqual(descriptor.connectionState, .configured)
        XCTAssertEqual(descriptor.collectionState, .notApplicable)
        XCTAssertTrue(descriptor.decisionAccessEnabled)
        XCTAssertEqual(descriptor.retention.first?.dataClass, "open_wearables_snapshot")
        XCTAssertEqual(descriptor.retentionAllowedValues, Self.presets)
        XCTAssertTrue(descriptor.instances.isEmpty)
        XCTAssertEqual(
            InputControlPlanePresentation.instanceSummary(descriptor),
            "Reported instances: 0"
        )
        XCTAssertEqual(
            InputControlPlanePresentation.limitation(
                "wearable_provider_device_crud_not_available"
            ),
            "Provider and device connections cannot be added or removed here because the server has no CRUD contract yet."
        )
        XCTAssertFalse(
            String(data: Self.openWearablesDescriptor(), encoding: .utf8)!
                .contains("secret-server-key")
        )
    }

    func testRevisionAndStrongETagValidation() {
        let revision = Self.revision("a")
        XCTAssertTrue(InputControlPlaneContract.isValidRevision(revision))
        XCTAssertEqual(
            InputControlPlaneContract.strongETag(for: revision),
            "\"\(revision)\""
        )
        XCTAssertEqual(
            InputControlPlaneContract.revision(fromStrongETag: "\"\(revision)\""),
            revision
        )
        XCTAssertNil(InputControlPlaneContract.revision(fromStrongETag: revision))
        XCTAssertNil(
            InputControlPlaneContract.revision(
                fromStrongETag: "W/\"\(revision)\""
            )
        )
        XCTAssertFalse(
            InputControlPlaneContract.isValidRevision(
                "sha256:\(String(repeating: "A", count: 64))"
            )
        )
    }

    static let presets = ["1d", "7d", "14d", "30d", "90d", "forever"]

    static func revision(_ character: Character) -> String {
        "sha256:" + String(repeating: String(character), count: 64)
    }

    static func openWearablesDescriptor(
        revision: String = revision("a"),
        decisionAccessEnabled: Bool = true
    ) -> Data {
        Data(
            """
            {
              "source_id": "wearable.open-wearables",
              "domain": "wearable",
              "display_name": "Open Wearables",
              "platforms": ["server"],
              "capabilities": ["sleep", "recovery", "hrv", "stress", "workouts"],
              "connection_state": "configured",
              "collection_state": "not_applicable",
              "decision_access_enabled": \(decisionAccessEnabled),
              "instances": [],
              "retention": [
                {
                  "data_class": "open_wearables_snapshot",
                  "preset": "30d",
                  "retention_days": 30,
                  "enabled": true,
                  "effective_preset": "30d",
                  "shared_across_source_instances": true
                }
              ],
              "settings": [
                {
                  "key": "decision_access_enabled",
                  "value_type": "boolean",
                  "scope": "domain",
                  "allowed_values": [],
                  "description": "Allow decision access."
                },
                {
                  "key": "retention",
                  "value_type": "retention_map",
                  "scope": "data_class",
                  "allowed_values": ["1d", "7d", "14d", "30d", "90d", "forever"],
                  "description": "Set retention."
                }
              ],
              "actions": [
                {
                  "action": "connect",
                  "execution": "external",
                  "method": null,
                  "endpoint": null,
                  "requires_instance": false,
                  "description": "Connect providers on the server."
                }
              ],
              "privacy": {
                "local_first": true,
                "raw_content_collected": false,
                "source_side_exclusions": false,
                "default_llm_exposure": "aggregate_only",
                "notes": ["Credentials stay on the server."]
              },
              "limitations": [
                "open_wearables_configuration_is_server_managed",
                "wearable_provider_device_inventory_not_available",
                "wearable_provider_device_crud_not_available"
              ],
              "revision": "\(revision)"
            }
            """.utf8
        )
    }

    static func listResponse(
        revision: String = revision("a"),
        decisionAccessEnabled: Bool = true
    ) -> Data {
        let descriptor = String(
            data: openWearablesDescriptor(
                revision: revision,
                decisionAccessEnabled: decisionAccessEnabled
            ),
            encoding: .utf8
        )!
        return Data("{\"sources\":[\(descriptor)]}".utf8)
    }
}

final class InputControlPlaneClientTests: XCTestCase {
    private var defaults: UserDefaults!
    private var pairingStore: PairingStore!
    private var suiteName: String!

    override func setUpWithError() throws {
        try super.setUpWithError()
        suiteName = "healthmes-input-control-tests-\(UUID().uuidString)"
        defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        pairingStore = PairingStore(
            defaults: defaults,
            keychain: InputControlPlaneFakeTokenStore()
        )
        _ = try pairingStore.save(
            baseURLString: "https://healthmes.example/base",
            token: "secret-token"
        )
    }

    override func tearDownWithError() throws {
        InputControlPlaneStubURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        pairingStore = nil
        suiteName = nil
        try super.tearDownWithError()
    }

    func testListRequestUsesPairedServerAndBearer() async throws {
        let client = makeClient()
        InputControlPlaneStubURLProtocol.handler = { request in
            XCTAssertEqual(
                request.url?.absoluteString,
                "https://healthmes.example/base/v1/inputs"
            )
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"),
                "Bearer secret-token"
            )
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Accept"),
                "application/json"
            )
            return (
                200,
                [:],
                InputControlPlaneContractTests.listResponse()
            )
        }

        let sources = try await client.listSources()

        XCTAssertEqual(sources.map(\.sourceID), ["wearable.open-wearables"])
    }

    func testSourceRequiresMatchingStrongETag() async throws {
        let client = makeClient()
        let revision = InputControlPlaneContractTests.revision("b")
        InputControlPlaneStubURLProtocol.handler = { request in
            XCTAssertEqual(
                request.url?.absoluteString,
                "https://healthmes.example/base/v1/inputs/wearable.open-wearables"
            )
            return (
                200,
                ["ETag": "\"\(revision)\""],
                InputControlPlaneContractTests.openWearablesDescriptor(
                    revision: revision
                )
            )
        }

        let versioned = try await client.source("wearable.open-wearables")

        XCTAssertEqual(versioned.descriptor.revision, revision)
        XCTAssertEqual(versioned.etag, "\"\(revision)\"")
    }

    func testSourceRejectsMissingOrMismatchedETag() async throws {
        let revision = InputControlPlaneContractTests.revision("b")
        let client = makeClient()
        for headers in [
            [:],
            ["ETag": "\"\(InputControlPlaneContractTests.revision("c"))\""],
            ["ETag": "W/\"\(revision)\""],
        ] {
            InputControlPlaneStubURLProtocol.handler = { _ in
                (
                    200,
                    headers,
                    InputControlPlaneContractTests.openWearablesDescriptor(
                        revision: revision
                    )
                )
            }
            do {
                _ = try await client.source("wearable.open-wearables")
                XCTFail("Expected an invalid ETag contract")
            } catch InputControlPlaneClientError.invalidContract {
                // Expected.
            }
        }
    }

    func testUpdateSendsQuotedIfMatchAndExactBody() async throws {
        let client = makeClient()
        let before = InputControlPlaneContractTests.revision("a")
        let after = InputControlPlaneContractTests.revision("b")
        InputControlPlaneStubURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            XCTAssertEqual(
                request.url?.absoluteString,
                "https://healthmes.example/base/v1/inputs/wearable.open-wearables/settings"
            )
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "If-Match"),
                "\"\(before)\""
            )
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Content-Type"),
                "application/json"
            )
            let object = try! XCTUnwrap(
                JSONSerialization.jsonObject(
                    with: inputControlRequestBody(request)
                ) as? [String: Any]
            )
            XCTAssertEqual(object.count, 2)
            XCTAssertEqual(object["decision_access_enabled"] as? Bool, false)
            XCTAssertEqual(
                (object["retention"] as? [String: String])?[
                    "open_wearables_snapshot"
                ],
                "90d"
            )
            XCTAssertNil(object["instance_id"])
            return (
                200,
                ["ETag": "\"\(after)\""],
                InputControlPlaneContractTests.openWearablesDescriptor(
                    revision: after,
                    decisionAccessEnabled: false
                )
            )
        }

        let updated = try await client.update(
            "wearable.open-wearables",
            settings: InputControlPlaneSettingsUpdate(
                decisionAccessEnabled: false,
                retention: ["open_wearables_snapshot": "90d"]
            ),
            ifMatch: "\"\(before)\""
        )

        XCTAssertFalse(updated.descriptor.decisionAccessEnabled)
        XCTAssertEqual(updated.descriptor.revision, after)
    }

    func testMapsRevisionConflictDetail() async throws {
        let before = InputControlPlaneContractTests.revision("a")
        let current = InputControlPlaneContractTests.revision("b")
        InputControlPlaneStubURLProtocol.handler = { _ in
            (
                409,
                [:],
                Data(
                    """
                    {
                      "error": {
                        "code": "input_settings_revision_conflict",
                        "message": "The input settings changed after the caller read them.",
                        "detail": {
                          "expected_revision": "\(before)",
                          "current_revision": "\(current)"
                        }
                      }
                    }
                    """.utf8
                )
            )
        }

        do {
            _ = try await makeClient().update(
                "wearable.open-wearables",
                settings: InputControlPlaneSettingsUpdate(
                    decisionAccessEnabled: false
                ),
                ifMatch: "\"\(before)\""
            )
            XCTFail("Expected revision conflict")
        } catch InputControlPlaneClientError.revisionConflict(
            let expected,
            let actual,
            _
        ) {
            XCTAssertEqual(expected, before)
            XCTAssertEqual(actual, current)
        }
    }

    private func makeClient() -> InputControlPlaneClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InputControlPlaneStubURLProtocol.self]
        return InputControlPlaneClient(
            session: URLSession(configuration: configuration),
            pairingStore: pairingStore
        )
    }
}

@MainActor
final class InputControlPlaneModelTests: XCTestCase {
    func testStaleLoadCannotOverwriteSuccessfulMutation() async throws {
        let context = try makeContext()
        defer { context.cleanup() }
        let before = InputControlPlaneContractTests.revision("a")
        let updated = InputControlPlaneContractTests.revision("b")
        let refreshed = InputControlPlaneContractTests.revision("c")
        InputControlPlaneStubURLProtocol.handler = { _ in
            (
                200,
                [:],
                InputControlPlaneContractTests.listResponse(revision: before)
            )
        }

        let model = InputControlPlaneModel(client: context.client)
        await model.load()

        let staleRequestStarted = expectation(
            description: "stale GET started"
        )
        let responses = InputControlPlanePendingResponses()
        var getCount = 0
        let requestLock = NSLock()
        InputControlPlaneStubURLProtocol.handler = nil
        InputControlPlaneStubURLProtocol.asynchronousHandler = {
            request,
            completion in
            let currentGet: Int?
            requestLock.lock()
            if request.httpMethod == "GET" {
                getCount += 1
                currentGet = getCount
            } else {
                currentGet = nil
            }
            requestLock.unlock()

            switch (request.httpMethod, currentGet) {
            case ("GET", 1):
                responses.store(completion, for: "stale")
                staleRequestStarted.fulfill()
            case ("PUT", _):
                completion(
                    (
                        200,
                        ["ETag": "\"\(updated)\""],
                        InputControlPlaneContractTests.openWearablesDescriptor(
                            revision: updated,
                            decisionAccessEnabled: false
                        )
                    )
                )
            case ("GET", 2):
                completion(
                    (
                        200,
                        [:],
                        InputControlPlaneContractTests.listResponse(
                            revision: refreshed,
                            decisionAccessEnabled: false
                        )
                    )
                )
            default:
                XCTFail("Unexpected request \(request.httpMethod ?? "nil")")
                completion((500, [:], Data()))
            }
        }

        let staleLoad = Task { await model.load() }
        await fulfillment(of: [staleRequestStarted], timeout: 2)

        await model.setDecisionAccess(
            false,
            for: "wearable.open-wearables"
        )

        XCTAssertEqual(
            model.source("wearable.open-wearables")?.revision,
            refreshed
        )
        XCTAssertFalse(
            try XCTUnwrap(model.source("wearable.open-wearables"))
                .decisionAccessEnabled
        )
        XCTAssertTrue(model.isLoading)

        responses.complete(
            "stale",
            with: (
                200,
                [:],
                InputControlPlaneContractTests.listResponse(
                    revision: before,
                    decisionAccessEnabled: true
                )
            )
        )
        await staleLoad.value

        XCTAssertEqual(
            model.source("wearable.open-wearables")?.revision,
            refreshed
        )
        XCTAssertFalse(
            try XCTUnwrap(model.source("wearable.open-wearables"))
                .decisionAccessEnabled
        )
        XCTAssertFalse(model.isLoading)
    }

    func testResetInvalidatesInFlightLoad() async throws {
        let context = try makeContext()
        defer { context.cleanup() }
        let requestStarted = expectation(description: "GET started")
        let responses = InputControlPlanePendingResponses()
        InputControlPlaneStubURLProtocol.handler = nil
        InputControlPlaneStubURLProtocol.asynchronousHandler = {
            request,
            completion in
            XCTAssertEqual(request.httpMethod, "GET")
            responses.store(completion, for: "load")
            requestStarted.fulfill()
        }

        let model = InputControlPlaneModel(client: context.client)
        let load = Task { await model.load() }
        await fulfillment(of: [requestStarted], timeout: 2)
        XCTAssertTrue(model.isLoading)

        model.reset()
        XCTAssertFalse(model.isLoading)

        responses.complete(
            "load",
            with: (
                200,
                [:],
                InputControlPlaneContractTests.listResponse()
            )
        )
        await load.value

        XCTAssertTrue(model.sources.isEmpty)
        XCTAssertNil(model.lastUpdated)
        XCTAssertFalse(model.isLoading)
    }

    func testSuccessfulMutationReloadsSharedServerTruth() async throws {
        let context = try makeContext()
        defer { context.cleanup() }
        let before = InputControlPlaneContractTests.revision("a")
        let updated = InputControlPlaneContractTests.revision("b")
        let related = InputControlPlaneContractTests.revision("c")
        var requestCount = 0
        InputControlPlaneStubURLProtocol.handler = { request in
            requestCount += 1
            switch requestCount {
            case 1:
                return (
                    200,
                    [:],
                    InputControlPlaneContractTests.listResponse(
                        revision: before
                    )
                )
            case 2:
                XCTAssertEqual(request.httpMethod, "PUT")
                return (
                    200,
                    ["ETag": "\"\(updated)\""],
                    InputControlPlaneContractTests.openWearablesDescriptor(
                        revision: updated,
                        decisionAccessEnabled: false
                    )
                )
            default:
                XCTAssertEqual(request.httpMethod, "GET")
                return (
                    200,
                    [:],
                    InputControlPlaneContractTests.listResponse(
                        revision: related,
                        decisionAccessEnabled: false
                    )
                )
            }
        }

        let model = InputControlPlaneModel(client: context.client)
        await model.load()
        await model.setDecisionAccess(
            false,
            for: "wearable.open-wearables"
        )

        XCTAssertEqual(requestCount, 3)
        XCTAssertFalse(
            try XCTUnwrap(model.source("wearable.open-wearables"))
                .decisionAccessEnabled
        )
        XCTAssertEqual(
            model.source("wearable.open-wearables")?.revision,
            related
        )
        XCTAssertNil(model.errorMessage)
    }

    func testConflictReloadsLatestServerTruthWithoutRetryingWrite() async throws {
        let context = try makeContext()
        defer { context.cleanup() }
        let before = InputControlPlaneContractTests.revision("a")
        let latest = InputControlPlaneContractTests.revision("b")
        var requestCount = 0
        InputControlPlaneStubURLProtocol.handler = { request in
            requestCount += 1
            switch requestCount {
            case 1:
                return (
                    200,
                    [:],
                    InputControlPlaneContractTests.listResponse(
                        revision: before
                    )
                )
            case 2:
                XCTAssertEqual(request.httpMethod, "PUT")
                return (
                    409,
                    [:],
                    Data(
                        """
                        {
                          "error": {
                            "code": "input_settings_revision_conflict",
                            "message": "The input settings changed after the caller read them.",
                            "detail": {
                              "expected_revision": "\(before)",
                              "current_revision": "\(latest)"
                            }
                          }
                        }
                        """.utf8
                    )
                )
            default:
                XCTAssertEqual(request.httpMethod, "GET")
                return (
                    200,
                    [:],
                    InputControlPlaneContractTests.listResponse(
                        revision: latest,
                        decisionAccessEnabled: false
                    )
                )
            }
        }

        let model = InputControlPlaneModel(client: context.client)
        await model.load()
        await model.setDecisionAccess(
            false,
            for: "wearable.open-wearables"
        )

        XCTAssertEqual(requestCount, 3)
        XCTAssertEqual(
            model.source("wearable.open-wearables")?.revision,
            latest
        )
        XCTAssertFalse(
            try XCTUnwrap(model.source("wearable.open-wearables"))
                .decisionAccessEnabled
        )
        XCTAssertNotNil(
            model.sourceMessages["wearable.open-wearables"]
        )
    }

    private func makeContext() throws -> InputControlPlaneTestContext {
        let suiteName = "healthmes-input-model-tests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let pairingStore = PairingStore(
            defaults: defaults,
            keychain: InputControlPlaneFakeTokenStore()
        )
        _ = try pairingStore.save(
            baseURLString: "https://healthmes.example",
            token: "secret-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InputControlPlaneStubURLProtocol.self]
        return InputControlPlaneTestContext(
            client: InputControlPlaneClient(
                session: URLSession(configuration: configuration),
                pairingStore: pairingStore
            ),
            defaults: defaults,
            suiteName: suiteName
        )
    }
}

private struct InputControlPlaneTestContext {
    let client: InputControlPlaneClient
    let defaults: UserDefaults
    let suiteName: String

    func cleanup() {
        InputControlPlaneStubURLProtocol.handler = nil
        InputControlPlaneStubURLProtocol.asynchronousHandler = nil
        defaults.removePersistentDomain(forName: suiteName)
    }
}

private final class InputControlPlaneFakeTokenStore: PairingTokenStoring {
    private var token: String?

    func readToken() -> String? { token }

    func writeToken(_ token: String) throws {
        self.token = token
    }

    func deleteToken() {
        token = nil
    }
}

private final class InputControlPlaneStubURLProtocol: URLProtocol {
    typealias Response = (Int, [String: String], Data)
    typealias Completion = (Response) -> Void

    static var handler: ((URLRequest) -> Response)?
    static var asynchronousHandler:
        ((URLRequest, @escaping Completion) -> Void)?

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(
        for request: URLRequest
    ) -> URLRequest {
        request
    }

    override func startLoading() {
        if let asynchronousHandler = Self.asynchronousHandler {
            asynchronousHandler(request) { [weak self] response in
                self?.complete(response)
            }
            return
        }
        guard let handler = Self.handler else {
            client?.urlProtocol(
                self,
                didFailWithError: URLError(.unsupportedURL)
            )
            return
        }
        complete(handler(request))
    }

    override func stopLoading() {}

    private func complete(_ responsePayload: Response) {
        let (status, headers, body) = responsePayload
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
        client?.urlProtocol(
            self,
            didReceive: response,
            cacheStoragePolicy: .notAllowed
        )
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

private final class InputControlPlanePendingResponses {
    private let lock = NSLock()
    private var completions:
        [String: InputControlPlaneStubURLProtocol.Completion] = [:]

    func store(
        _ completion: @escaping InputControlPlaneStubURLProtocol.Completion,
        for key: String
    ) {
        lock.lock()
        completions[key] = completion
        lock.unlock()
    }

    func complete(
        _ key: String,
        with response: InputControlPlaneStubURLProtocol.Response
    ) {
        lock.lock()
        let completion = completions.removeValue(forKey: key)
        lock.unlock()
        completion?(response)
    }
}

private func inputControlRequestBody(_ request: URLRequest) -> Data {
    if let body = request.httpBody {
        return body
    }
    guard let stream = request.httpBodyStream else {
        return Data()
    }
    stream.open()
    defer { stream.close() }

    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4_096)
    while true {
        let count = stream.read(&buffer, maxLength: buffer.count)
        guard count > 0 else { break }
        data.append(buffer, count: count)
    }
    return data
}
