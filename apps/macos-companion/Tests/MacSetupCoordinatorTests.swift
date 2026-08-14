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

    func testFindsRepositoryFromExplicitEnvironment() {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()

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
