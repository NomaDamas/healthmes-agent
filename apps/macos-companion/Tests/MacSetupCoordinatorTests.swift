import XCTest

final class MacSetupSupportTests: XCTestCase {
    func testDecodesMachineReadableSetupEvents() {
        let output = """
        {"schema":"healthmes.setup.v1","action":"install","step":"environment","state":"ready","message":"Configured.","detail":null}
        not-json
        {"schema":"healthmes.setup.v1","action":"install","step":"pair_phone","state":"ready","message":"Scan.","detail":"healthmes://pair?url=x&code=y"}
        """

        let events = MacSetupSupport.decodeEvents(output)

        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events.last?.step, "pair_phone")
        XCTAssertFalse(events[0].isFailure)
    }

    func testFindsRepositoryFromExplicitEnvironment() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let scripts = root.appendingPathComponent("scripts", isDirectory: true)
        try FileManager.default.createDirectory(
            at: scripts,
            withIntermediateDirectories: true
        )
        try Data().write(to: scripts.appendingPathComponent("healthmes_setup.py"))
        defer { try? FileManager.default.removeItem(at: root) }

        XCTAssertEqual(
            MacSetupSupport.repositoryRoot(
                environment: ["HEALTHMES_REPO_ROOT": root.path],
                currentDirectory: URL(fileURLWithPath: "/")
            )?.standardizedFileURL,
            root.standardizedFileURL
        )
    }

    func testManagedRepositoryLivesUnderApplicationSupport() {
        let applicationSupport = URL(
            fileURLWithPath: "/tmp/HealthMesTests/Application Support",
            isDirectory: true
        )

        XCTAssertEqual(
            MacSetupSupport.managedRepositoryRoot(
                applicationSupportDirectory: applicationSupport
            ).path,
            "/tmp/HealthMesTests/Application Support/HealthMes/runtime-source"
        )
    }

    func testRuntimeRevisionIsPinnedAndSupportsReleaseOverride() {
        XCTAssertEqual(
            MacSetupSupport.defaultRuntimeRevision,
            "beda2d7cc88536979a0aa24f490629f9d88d6a25"
        )
        XCTAssertEqual(
            MacSetupSupport.runtimeRevision(
                environment: [:],
                bundleValue: nil
            ),
            MacSetupSupport.defaultRuntimeRevision
        )
        XCTAssertEqual(
            MacSetupSupport.runtimeRevision(
                environment: ["HEALTHMES_RUNTIME_REVISION": "release-commit"],
                bundleValue: "bundle-commit"
            ),
            "release-commit"
        )
        XCTAssertEqual(
            MacSetupSupport.runtimeRevision(
                environment: [:],
                bundleValue: "bundle-commit"
            ),
            "bundle-commit"
        )
    }

    func testPairingGrantExpiryMatchesServerBoundary() {
        let expiry = Date(timeIntervalSince1970: 1_786_000_300)

        XCTAssertFalse(
            MacSetupSupport.isPairingGrantExpired(
                expiresAt: expiry,
                now: expiry
            )
        )
        XCTAssertTrue(
            MacSetupSupport.isPairingGrantExpired(
                expiresAt: expiry,
                now: expiry.addingTimeInterval(0.001)
            )
        )
        XCTAssertFalse(
            MacSetupSupport.isPairingGrantExpired(
                expiresAt: nil,
                now: expiry.addingTimeInterval(10)
            )
        )
    }

    func testTailscaleSetupContractIsSharedWithAppleClients() {
        XCTAssertEqual(TailscalePairingPresentation.steps.count, 5)
        XCTAssertEqual(
            TailscalePairingPresentation.steps.last?.title,
            "Done"
        )
        XCTAssertEqual(
            TailscalePairingPresentation.transport(
                for: Pairing(
                    baseURL: URL(string: "https://healthmes.example.ts.net")!,
                    token: "token"
                )
            ),
            .tailscaleDNS
        )
    }

    func testReadinessMapsEveryComponentWithoutCollapsingFailures() {
        let readiness = SetupReadiness(
            overall: .actionRequired,
            checks: [
                SetupReadinessCheck(
                    key: "instance",
                    label: "HealthMes instance",
                    state: .ready,
                    detail: "Authenticated API is reachable."
                ),
                SetupReadinessCheck(
                    key: "calendar_icloud",
                    label: "Apple Calendar",
                    state: .actionRequired,
                    detail: "Connect iCloud CalDAV."
                ),
            ]
        )

        let events = MacSetupSupport.readinessEvents(readiness)

        XCTAssertEqual(events.map(\.step), [
            "readiness_instance",
            "readiness_calendar_icloud",
        ])
        XCTAssertEqual(events.map(\.state), ["ready", "action_required"])
        XCTAssertEqual(events.last?.detail, "Connect iCloud CalDAV.")
    }
}

@MainActor
final class MacSetupReadinessTests: XCTestCase {
    func testSettingsReadinessDropsLateResponseAfterPairingChanges()
        async throws
    {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        _ = try fixture.store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )

        let requestStarted = expectation(description: "readiness request started")
        let gate = ReadinessGate()
        let model = MacSetupReadinessModel(
            pairingStore: fixture.store,
            loader: { _ in
                requestStarted.fulfill()
                await gate.wait()
                return Self.readyFixture
            }
        )

        let refresh = Task {
            await model.load()
        }
        await fulfillment(of: [requestStarted], timeout: 2)

        _ = try fixture.store.save(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        model.reset()
        await gate.release()
        await refresh.value

        XCTAssertNil(model.readiness)
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.isLoading)
    }

    func testVerifyPairingDoesNotPublishStaleReadinessAfterPairingChanges()
        async throws
    {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        _ = try fixture.store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )

        let requestStarted = expectation(description: "readiness request started")
        let gate = ReadinessGate()
        let coordinator = MacSetupCoordinator(
            pairingStore: fixture.store,
            readinessLoader: { _ in
                requestStarted.fulfill()
                await gate.wait()
                return Self.readyFixture
            }
        )

        let verification = Task {
            await coordinator.verifyPairing()
        }
        await fulfillment(of: [requestStarted], timeout: 2)

        _ = try fixture.store.save(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        coordinator.resetForPairingChange()
        await gate.release()
        await verification.value

        XCTAssertTrue(coordinator.events.isEmpty)
        XCTAssertNil(coordinator.failure)
    }

    private static let readyFixture = SetupReadiness(
        overall: .ready,
        checks: [
            SetupReadinessCheck(
                key: "instance",
                label: "HealthMes instance",
                state: .ready,
                detail: "Authenticated API is reachable."
            ),
        ]
    )

    private func makeFixture() throws -> SetupFixture {
        let suite = "healthmes-mac-readiness-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        let lockURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-mac-readiness-\(UUID().uuidString)")
        let tokenStore = SetupTestPairingTokenStore()
        return SetupFixture(
            suite: suite,
            defaults: defaults,
            lockURL: lockURL,
            store: PairingStore(
                defaults: defaults,
                keychain: tokenStore,
                lockURL: lockURL
            )
        )
    }
}

private actor ReadinessGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var isReleased = false

    func wait() async {
        guard !isReleased else { return }
        await withCheckedContinuation { continuation in
            self.continuation = continuation
        }
    }

    func release() {
        isReleased = true
        continuation?.resume()
        continuation = nil
    }
}

private final class SetupTestPairingTokenStore: PairingTokenStoring {
    private var values: [String: String] = [:]

    func readToken(identifier: String) -> String? {
        values[identifier]
    }

    func writeToken(_ token: String, identifier: String) throws {
        values[identifier] = token
    }

    func deleteToken(identifier: String) {
        values.removeValue(forKey: identifier)
    }

    func deleteTokenForCleanup(identifier: String) throws {
        values.removeValue(forKey: identifier)
    }
}

private struct SetupFixture {
    let suite: String
    let defaults: UserDefaults
    let lockURL: URL
    let store: PairingStore

    func cleanup() {
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: lockURL)
    }
}
