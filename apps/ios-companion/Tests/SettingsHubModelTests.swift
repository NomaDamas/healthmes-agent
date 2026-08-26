import Foundation
import XCTest

@MainActor
final class SettingsHubModelTests: XCTestCase {
    func testStaleAuthorizationResponseCannotOverwriteLatestAction() async throws {
        let context = try makeContext()
        defer { context.cleanup() }

        let firstStarted = expectation(description: "first authorization started")
        let secondStarted = expectation(description: "second authorization started")
        let pending = SettingsHubModelPendingRequests(
            onRequest: { provider in
                if provider == "whoop" {
                    firstStarted.fulfill()
                } else if provider == "oura" {
                    secondStarted.fulfill()
                }
            }
        )
        SettingsHubModelStubURLProtocol.asynchronousHandler = {
            request,
            completion in
            pending.store(completion, for: Self.provider(from: request))
        }

        let first = Task {
            await context.model.authorize(provider: "whoop")
        }
        await fulfillment(of: [firstStarted], timeout: 2)

        let second = Task {
            await context.model.authorize(provider: "oura")
        }
        await fulfillment(of: [secondStarted], timeout: 2)

        pending.complete(
            "whoop",
            with: (
                200,
                [:],
                Self.authorizationResponse(
                    provider: "whoop",
                    url: "https://provider.example/whoop"
                )
            )
        )
        await first.value
        XCTAssertNil(context.model.authorizationURL)

        pending.complete(
            "oura",
            with: (
                200,
                [:],
                Self.authorizationResponse(
                    provider: "oura",
                    url: "https://provider.example/oura"
                )
            )
        )
        await second.value
        XCTAssertEqual(
            context.model.authorizationURL?.absoluteString,
            "https://provider.example/oura"
        )
    }

    private func makeContext() throws -> SettingsHubModelTestContext {
        let suiteName = "healthmes-settings-hub-model-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let pairingStore = PairingStore(
            defaults: defaults,
            keychain: SettingsHubModelFakeTokenStore()
        )
        _ = try pairingStore.save(
            baseURLString: "https://healthmes.example",
            token: "secret-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SettingsHubModelStubURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let model = SettingsHubModel(
            client: SettingsHubClient(
                session: session,
                pairingStore: pairingStore
            ),
            wearableClient: WearableManagementClient(
                session: session,
                pairingStore: pairingStore
            ),
            pairingStore: pairingStore
        )
        return SettingsHubModelTestContext(
            model: model,
            defaults: defaults,
            suiteName: suiteName
        )
    }

    private static func provider(from request: URLRequest) -> String {
        let components = request.url?.pathComponents ?? []
        guard components.count >= 2 else { return "" }
        return components[components.count - 2]
    }

    private static func authorizationResponse(
        provider: String,
        url: String
    ) -> Data {
        Data(
            """
            {
              "provider": "\(provider)",
              "authorization_url": "\(url)"
            }
            """.utf8
        )
    }
}

private struct SettingsHubModelTestContext {
    let model: SettingsHubModel
    let defaults: UserDefaults
    let suiteName: String

    func cleanup() {
        SettingsHubModelStubURLProtocol.handler = nil
        SettingsHubModelStubURLProtocol.asynchronousHandler = nil
        defaults.removePersistentDomain(forName: suiteName)
    }
}

private final class SettingsHubModelFakeTokenStore: PairingTokenStoring {
    private var tokens: [String: String] = [:]

    func readToken(identifier: String) -> String? {
        tokens[identifier]
    }

    func writeToken(_ token: String, identifier: String) throws {
        tokens[identifier] = token
    }

    func deleteToken(identifier: String) {
        tokens.removeValue(forKey: identifier)
    }
}

private final class SettingsHubModelStubURLProtocol: URLProtocol {
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

private final class SettingsHubModelPendingRequests {
    private let lock = NSLock()
    private var completions:
        [String: SettingsHubModelStubURLProtocol.Completion] = [:]
    private let onRequest: (String) -> Void

    init(onRequest: @escaping (String) -> Void) {
        self.onRequest = onRequest
    }

    func store(
        _ completion: @escaping SettingsHubModelStubURLProtocol.Completion,
        for provider: String
    ) {
        lock.lock()
        completions[provider] = completion
        lock.unlock()
        onRequest(provider)
    }

    func complete(
        _ provider: String,
        with response: SettingsHubModelStubURLProtocol.Response
    ) {
        lock.lock()
        let completion = completions.removeValue(forKey: provider)
        lock.unlock()
        completion?(response)
    }
}
