import XCTest

@MainActor
final class StorageSettingsModelTests: XCTestCase {
    func testFailedPreviewInvalidatesPreviousDestructivePreview() async throws {
        let suiteName = "healthmes-storage-model-tests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let credentials = FakePairingTokenStore()
        let pairingStore = PairingStore(
            defaults: defaults,
            keychain: credentials
        )
        _ = try pairingStore.save(
            baseURLString: "https://healthmes.example.com",
            token: "secret-token"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        let api = HealthMesAPI(
            session: URLSession(configuration: configuration),
            pairingStore: pairingStore
        )
        let model = StorageSettingsModel(api: api)
        let successfulPreview = Data(
            """
            {
              "job_id": "31370b3a-d731-4bee-b538-69a02e31e8bc",
              "dry_run": true,
              "candidates": 4,
              "deleted": 0,
              "bytes_reclaimed": 0,
              "errors": []
            }
            """.utf8
        )
        var requestCount = 0
        StubURLProtocol.handler = { _ in
            requestCount += 1
            if requestCount == 1 {
                return (200, [:], successfulPreview)
            }
            let failure = Data(
                """
                {
                  "error": {
                    "code": "maintenance_failed",
                    "message": "Preview unavailable",
                    "detail": null
                  }
                }
                """.utf8
            )
            return (503, [:], failure)
        }
        defer { StubURLProtocol.handler = nil }

        await model.previewCleanup()
        XCTAssertEqual(model.maintenance?.candidates, 4)
        XCTAssertTrue(model.maintenance?.dryRun == true)

        await model.previewCleanup()
        XCTAssertNil(model.maintenance)
        XCTAssertEqual(model.errorMessage, "Preview unavailable")
    }
}
