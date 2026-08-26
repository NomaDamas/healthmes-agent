import Security
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

    func testAcceptsNaiveUTCTimestamps() {
        // Store-backed endpoints (schedule proposals, food logs) emit
        // sqlite's naive datetimes with no zone designator; the store
        // contract says they are UTC. Proven against a live instance.
        let base = GlanceJSON.parseISO8601("2026-07-09T14:23:00Z")
        XCTAssertEqual(GlanceJSON.parseISO8601("2026-07-09T14:23:00"), base)
        let naiveFractional = GlanceJSON.parseISO8601("2026-07-09T14:23:00.355753")
        XCTAssertNotNil(naiveFractional)
        XCTAssertEqual(
            naiveFractional!.timeIntervalSince1970,
            base!.timeIntervalSince1970 + 0.355,
            accuracy: 0.01
        )
        XCTAssertNil(GlanceJSON.parseISO8601("2026-07-09"))
    }

    func testNextBlockLineRendersServerTimezone() throws {
        let payload = try GlanceJSON.decodePayload(fixtureData())
        XCTAssertEqual(GlanceFormat.nextBlockLine(payload), "14:00 Deep work block [high]")
        XCTAssertEqual(GlanceFormat.alertsLine(payload), "2 alerts · Stress 82 vs baseline 55")
    }
}

final class GlanceClientBehaviourTests: XCTestCase {
    func testAppGroupUsesPlatformAppropriateKeychainAccessGroup() {
        #if os(macOS)
            XCTAssertNil(AppGroup.keychainAccessGroupForCurrentPlatform)
        #else
            XCTAssertEqual(
                AppGroup.keychainAccessGroupForCurrentPlatform,
                AppGroup.keychainIdentifier
            )
        #endif
    }

    func testKeychainFallsBackWithoutAccessGroupEntitlement() throws {
        let accessGroup = "group.com.healthmes.tests"
        let token = "simulator-secret-\(UUID().uuidString)"
        let security = RecordingKeychainSecurity(token: token)
        let store = KeychainTokenStore(
            accessGroup: accessGroup,
            security: security.operations,
            allowsUngroupedFallback: true
        )

        try store.writeToken(token, identifier: "api-token")

        XCTAssertEqual(
            try store.readTokenForRecovery(identifier: "api-token"),
            token
        )
        XCTAssertEqual(
            security.updatedAccessGroups,
            [accessGroup, nil]
        )
        XCTAssertEqual(security.addedAccessGroups, [nil])
        XCTAssertEqual(
            security.readAccessGroups,
            [accessGroup, nil]
        )
    }

    func testSignedKeychainDoesNotFallBackToPrivateCredentials() {
        let accessGroup = "TEAM.group.com.healthmes.tests"
        let security = RecordingKeychainSecurity(token: "secret")
        let store = KeychainTokenStore(
            accessGroup: accessGroup,
            security: security.operations,
            allowsUngroupedFallback: false
        )

        XCTAssertThrowsError(
            try store.writeToken("secret", identifier: "api-token")
        ) {
            XCTAssertEqual(
                $0 as? PairingError,
                .credentialStorageFailed
            )
        }
        XCTAssertEqual(security.updatedAccessGroups, [accessGroup])
        XCTAssertTrue(security.addedAccessGroups.isEmpty)
    }

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

    func testPairingDeepLinkContainsOneTimeCodeNotToken() throws {
        let url = try XCTUnwrap(
            URL(
                string:
                    "healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com&code=one-time-code"
            )
        )

        let payload = try PairingDeepLink.parse(url)

        XCTAssertEqual(
            payload.baseURL.absoluteString,
            "https://healthmes.example.com"
        )
        XCTAssertEqual(payload.code, "one-time-code")
        let request = try PairingExchangeClient.request(for: payload)
        XCTAssertEqual(
            request.url?.absoluteString,
            "https://healthmes.example.com/v1/setup/pairing/exchange"
        )
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertFalse(String(data: request.httpBody!, encoding: .utf8)!.contains("token"))
    }

    func testPairingDeepLinkRejectsPlainHTTPAndLegacyToken() throws {
        XCTAssertThrowsError(
            try PairingDeepLink.parse(
                XCTUnwrap(
                    URL(
                        string:
                            "healthmes://pair?url=http%3A%2F%2Fexample.com&code=one-time-code"
                    )
                )
            )
        )
        XCTAssertThrowsError(
            try PairingDeepLink.parse(
                XCTUnwrap(
                    URL(
                        string:
                            "healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com&token=secret"
                    )
                )
            )
        )
        XCTAssertThrowsError(
            try PairingDeepLink.parse(
                XCTUnwrap(
                    URL(
                        string:
                            "healthmes://pair?url=http%3A%2F%2F192.168.1.20%3A8100&code=one-time-code"
                    )
                )
            )
        )
        XCTAssertNoThrow(
            try PairingDeepLink.parse(
                XCTUnwrap(
                    URL(
                        string:
                            "healthmes://pair?url=http%3A%2F%2F127.0.0.1%3A8100&code=one-time-code"
                    )
                )
            )
        )
    }

    func testLoopbackPolicyAcceptsOnlyLocalhostIPv6AndExact127Slash8() {
        for host in [
            "localhost",
            "::1",
            "[::1]",
            "127.0.0.1",
            "127.255.255.255",
        ] {
            XCTAssertTrue(PairingStore.isLoopbackHost(host), host)
        }
        for host in [
            "127",
            "127.0.0",
            "127.0.0.256",
            "127.attacker.example",
            "127.0.0.1.attacker.example",
            "126.255.255.255",
            "128.0.0.1",
        ] {
            XCTAssertFalse(PairingStore.isLoopbackHost(host), host)
        }
    }

    func testPairingStoreAllowsTokenlessLoopbackButRequiresHTTPSAndTokenRemotely() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let local = try store.save(
            baseURLString: "http://127.0.0.1:8100",
            token: " "
        )
        XCTAssertNil(local.token)
        XCTAssertNil(credentials.token)
        XCTAssertEqual(store.load(), local)

        XCTAssertThrowsError(
            try store.save(
                baseURLString: "https://healthmes.example.com",
                token: ""
            )
        ) { error in
            XCTAssertEqual(error as? PairingError, .tokenRequired)
        }
        XCTAssertThrowsError(
            try store.save(
                baseURLString: "http://192.168.1.20:8100",
                token: "secret"
            )
        ) { error in
            XCTAssertEqual(error as? PairingError, .insecureBaseURL)
        }

        let remote = try store.save(
            baseURLString: "https://healthmes.example.com",
            token: " secret "
        )
        XCTAssertEqual(remote.token, "secret")
        XCTAssertEqual(credentials.token, "secret")
    }

    func testSavingIdenticalPairingPreservesCacheGeneration() throws {
        let (store, defaults, _, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let pairing = try store.save(
            baseURLString: "https://healthmes.example.com",
            token: "secret"
        )
        let first = try XCTUnwrap(store.cacheIdentity(for: pairing))

        let repeated = try store.save(
            baseURLString: "https://healthmes.example.com/",
            token: " secret "
        )
        let second = try XCTUnwrap(store.cacheIdentity(for: repeated))

        XCTAssertEqual(repeated, pairing)
        XCTAssertEqual(second, first)
    }

    func testPairingStoreLoadRemovesLegacyInsecurePairingAndCredential() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set(
            "http://192.168.1.20:8100",
            forKey: "healthmes.pairing.baseURL"
        )
        credentials.token = "legacy-secret"

        XCTAssertNil(store.load())
        XCTAssertNil(
            defaults.string(forKey: "healthmes.pairing.baseURL")
        )
        XCTAssertNil(credentials.token)
    }

    func testPairingStoreLoadRemovesRemoteHTTPSPairingWithoutToken() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set(
            "https://healthmes.example.com",
            forKey: "healthmes.pairing.baseURL"
        )
        credentials.token = nil

        XCTAssertNil(store.load())
        XCTAssertNil(
            defaults.string(forKey: "healthmes.pairing.baseURL")
        )
    }

    func testPairingStoreRestoresPreviousCredentialWhenReplacementFails() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        credentials.failNextWrite = true

        XCTAssertThrowsError(
            try store.save(
                baseURLString: "https://new.healthmes.example",
                token: "new-secret"
            )
        ) { error in
            XCTAssertEqual(error as? PairingError, .credentialStorageFailed)
        }
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(credentials.token, "old-secret")
    }

    func testPairingStoreRestoresPreviousCredentialAfterReadBackMismatch() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        credentials.corruptReadAfterNextWrite = true

        XCTAssertThrowsError(
            try store.save(
                baseURLString: "https://new.healthmes.example",
                token: "new-secret"
            )
        ) { error in
            XCTAssertEqual(error as? PairingError, .credentialStorageFailed)
        }
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(credentials.token, "old-secret")
    }

    func testPairingReplacementStaysFailClosedUntilCommit() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )

        let transition = try store.beginReplacement(with: candidate)

        XCTAssertEqual(
            transition.previousFingerprint,
            original.cacheFingerprint
        )
        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertNil(
            PairingStore.persistedCacheIdentity(defaults: defaults)
        )
        XCTAssertFalse(
            PairingStore.hasPersistedPairing(defaults: defaults)
        )
        XCTAssertEqual(store.pendingTransition()?.candidate, candidate)
        XCTAssertEqual(
            Set(credentials.tokens.values),
            ["old-secret", "new-secret"]
        )

        XCTAssertThrowsError(try store.commitPendingTransition()) {
            XCTAssertEqual(
                $0 as? PairingError,
                .transitionInProgress
            )
        }
        try store.markPendingTransitionCleanupStarted()
        XCTAssertEqual(
            try store.commitPendingTransition(),
            candidate
        )
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(store.load(), candidate)
        XCTAssertEqual(Set(credentials.tokens.values), ["new-secret"])
    }

    func testInitialPairingUsesTheSameTransitionJournal() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )

        let transition = try store.beginReplacement(with: candidate)

        XCTAssertNil(transition.previousFingerprint)
        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertEqual(store.pendingTransition()?.candidate, candidate)
        XCTAssertEqual(Set(credentials.tokens.values), ["secret"])

        try store.markPendingTransitionCleanupStarted()
        XCTAssertEqual(
            try store.commitPendingTransition(),
            candidate
        )
        XCTAssertEqual(store.load(), candidate)
    }

    func testReplacementRejectsUnverifiedPersistedState() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let credentialIdentifier = try XCTUnwrap(
            credentials.tokens.first {
                $0.value == "old-secret"
            }?.key
        )
        let corruptedRecord: [String: Any] = [
            "version": 1,
            "baseURL": original.baseURL.absoluteString,
            "credentialIdentifier": credentialIdentifier,
            "fingerprint": String(repeating: "a", count: 64),
            "generation": 1,
        ]
        defaults.set(
            try JSONSerialization.data(withJSONObject: corruptedRecord),
            forKey: "healthmes.pairing.record.v1"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )

        XCTAssertThrowsError(
            try store.beginReplacement(with: candidate)
        ) {
            XCTAssertEqual(
                $0 as? PairingError,
                .credentialStorageFailed
            )
        }
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(Set(credentials.tokens.values), ["old-secret"])
    }

    func testPreparedInitialReplacementRequiresVerifiedUnpairedMarker()
        throws
    {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        credentials.tokens["staged-credential"] = "secret"
        let journal: [String: Any] = [
            "version": 1,
            "kind": "replacement",
            "phase": "prepared",
            "candidateBaseURL": candidate.baseURL.absoluteString,
            "candidateCredentialIdentifier": "staged-credential",
            "candidateFingerprint": candidate.cacheFingerprint,
            "generation": 1,
        ]
        defaults.set(
            try JSONSerialization.data(withJSONObject: journal),
            forKey: "healthmes.pairing.transition.v1"
        )

        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.pendingTransition())
        XCTAssertThrowsError(
            try store.markPendingTransitionCleanupStarted()
        ) {
            XCTAssertEqual(
                $0 as? PairingError,
                .credentialStorageFailed
            )
        }
    }

    func testReplacementJournalRejectsUnvalidatedCandidate() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        let insecure = Pairing(
            baseURL: try XCTUnwrap(
                URL(string: "http://192.168.1.20:8100")
            ),
            token: "exposed-secret"
        )

        XCTAssertThrowsError(
            try store.beginReplacement(with: insecure)
        ) {
            XCTAssertEqual(
                $0 as? PairingError,
                .insecureBaseURL
            )
        }
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(Set(credentials.tokens.values), ["secret"])
    }

    func testAbortingPreparedReplacementRestoresOriginalPairing() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        _ = try store.beginReplacement(with: candidate)

        store.abortPendingTransition()

        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(Set(credentials.tokens.values), ["old-secret"])
    }

    func testClearCannotBypassPreparedReplacement() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        _ = try store.beginReplacement(with: candidate)

        store.clear()

        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertEqual(store.pendingTransition()?.candidate, candidate)
        XCTAssertEqual(
            Set(credentials.tokens.values),
            ["old-secret", "new-secret"]
        )
    }

    func testWatchUnpairContextRejectsPendingReplacement() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        _ = try store.beginReplacement(with: candidate)

        XCTAssertFalse(
            PairingContextApplication.apply(
                baseURLString: "",
                token: "",
                pairingStore: store
            )
        )
        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertEqual(store.pendingTransition()?.candidate, candidate)
        XCTAssertEqual(
            Set(credentials.tokens.values),
            ["old-secret", "new-secret"]
        )
    }

    func testCleanupStartedReplacementCannotBeAborted() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        _ = try store.beginReplacement(with: candidate)
        try store.markPendingTransitionCleanupStarted()

        store.abortPendingTransition()

        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertEqual(store.pendingTransition()?.candidate, candidate)
        XCTAssertEqual(
            Set(credentials.tokens.values),
            ["old-secret", "new-secret"]
        )
    }

    func testPreparedReplacementSurvivesStoreRecreation() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        _ = try store.beginReplacement(with: candidate)
        let reloaded = PairingStore(
            defaults: defaults,
            keychain: credentials
        )

        XCTAssertNil(reloaded.load())
        XCTAssertEqual(
            reloaded.pendingTransition()?.candidate,
            candidate
        )
        try reloaded.markPendingTransitionCleanupStarted()
        XCTAssertEqual(
            try reloaded.commitPendingTransition(),
            candidate
        )
        XCTAssertEqual(reloaded.load(), candidate)
    }

    func testPreparedUnpairStaysFailClosedUntilCommit() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )

        let transition = try store.beginUnpair()

        XCTAssertEqual(transition.kind, .unpair)
        XCTAssertEqual(
            transition.previousFingerprint,
            original.cacheFingerprint
        )
        XCTAssertNil(store.load())
        XCTAssertThrowsError(try store.commitPendingTransition()) {
            XCTAssertEqual(
                $0 as? PairingError,
                .transitionInProgress
            )
        }
        try store.markPendingTransitionCleanupStarted()
        XCTAssertNil(try store.commitPendingTransition())
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertTrue(credentials.tokens.isEmpty)
    }

    func testUnpairJournalCleansUpUnreadableCredentialState() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = try store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        credentials.tokens.removeAll()

        XCTAssertNil(store.load())
        XCTAssertTrue(store.hasPersistedPairingState)

        let transition = try store.beginUnpair()

        XCTAssertNil(transition.previousFingerprint)
        XCTAssertEqual(
            HealthKitSyncRemovalPolicy.scope(
                fingerprint: transition.previousFingerprint,
                userDirectedRemoval: true
            ),
            .all
        )
        try store.markPendingTransitionCleanupStarted()
        XCTAssertNil(try store.commitPendingTransition())
        XCTAssertFalse(store.hasPersistedPairingState)
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertTrue(credentials.tokens.isEmpty)
    }

    func testUnpairDoesNotTrustUnverifiedPersistedFingerprint() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        let credentialIdentifier = try XCTUnwrap(
            credentials.tokens.first {
                $0.value == "secret"
            }?.key
        )
        let corruptedRecord: [String: Any] = [
            "version": 1,
            "baseURL": original.baseURL.absoluteString,
            "credentialIdentifier": credentialIdentifier,
            "fingerprint": String(repeating: "a", count: 64),
            "generation": 1,
        ]
        defaults.set(
            try JSONSerialization.data(withJSONObject: corruptedRecord),
            forKey: "healthmes.pairing.record.v1"
        )

        XCTAssertNil(store.load())
        let transition = try store.beginUnpair()

        XCTAssertNil(transition.previousFingerprint)
        XCTAssertEqual(
            HealthKitSyncRemovalPolicy.scope(
                fingerprint: transition.previousFingerprint,
                userDirectedRemoval: true
            ),
            .all
        )
    }

    func testStagingCrashRecoveryReturnsTheRestoredPairing() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        credentials.tokens["staged-credential"] = "new-secret"
        let journal: [String: Any] = [
            "version": 1,
            "kind": "replacement",
            "phase": "staging",
            "previousFingerprint": original.cacheFingerprint,
            "previousCredentialIdentifier":
                try XCTUnwrap(
                    credentials.tokens.first {
                        $0.value == "old-secret"
                    }?.key
                ),
            "candidateBaseURL": candidate.baseURL.absoluteString,
            "candidateCredentialIdentifier": "staged-credential",
            "candidateFingerprint": candidate.cacheFingerprint,
            "generation": 2,
        ]
        defaults.set(
            try JSONSerialization.data(withJSONObject: journal),
            forKey: "healthmes.pairing.transition.v1"
        )

        let recovery = try XCTUnwrap(
            store.abortStagingTransitionIfSafe()
        )

        XCTAssertEqual(recovery.pairing, original)
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(Set(credentials.tokens.values), ["old-secret"])
    }

    func testStagingRecoveryKeepsJournalWhenCredentialReadIsUnavailable()
        throws
    {
        let (store, defaults, credentials, suiteName) =
            try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        credentials.tokens["staged-credential"] = "new-secret"
        let oldCredential = try XCTUnwrap(
            credentials.tokens.first {
                $0.value == "old-secret"
            }?.key
        )
        let journal: [String: Any] = [
            "version": 1,
            "kind": "replacement",
            "phase": "staging",
            "previousFingerprint": original.cacheFingerprint,
            "previousCredentialIdentifier": oldCredential,
            "candidateBaseURL": candidate.baseURL.absoluteString,
            "candidateCredentialIdentifier": "staged-credential",
            "candidateFingerprint": candidate.cacheFingerprint,
            "generation": 2,
        ]
        defaults.set(
            try JSONSerialization.data(withJSONObject: journal),
            forKey: "healthmes.pairing.transition.v1"
        )
        credentials.failNextRecoveryRead = true

        XCTAssertNil(store.abortStagingTransitionIfSafe())
        XCTAssertTrue(store.hasPendingTransition)
        XCTAssertNil(store.load())
        XCTAssertEqual(
            Set(credentials.tokens.values),
            ["old-secret", "new-secret"]
        )

        let recovery = try XCTUnwrap(
            store.abortStagingTransitionIfSafe()
        )
        XCTAssertEqual(recovery.pairing, original)
        XCTAssertFalse(store.hasPendingTransition)
        XCTAssertEqual(store.load(), original)
        XCTAssertEqual(Set(credentials.tokens.values), ["old-secret"])
    }

    func testRelayGateQuiescesOldAccountBeforeTransition() async throws {
        let (store, defaults, _, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let pairing = try store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        let gate = PairingRelayGate()
        let lease = try XCTUnwrap(
            gate.begin(pairing: pairing, store: store)
        )
        let fenceTask = Task {
            await gate.fenceAndWait()
        }
        for _ in 0..<200 {
            guard gate.isAcceptingNewRelays else { break }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }

        XCTAssertFalse(gate.isAcceptingNewRelays)
        XCTAssertNil(gate.begin(pairing: pairing, store: store))

        gate.end(lease)
        await fenceTask.value
        XCTAssertTrue(gate.reopenIfStable(store: store))
        let reopened = try XCTUnwrap(
            gate.begin(pairing: pairing, store: store)
        )
        gate.end(reopened)
    }

    func testPairingReadAndTransitionAreSerializedAcrossStores() throws {
        let suiteName = "healthmes-pairing-lock-tests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let credentials = FakePairingTokenStore()
        let lockURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "healthmes-pairing-lock-\(UUID().uuidString)"
            )
        defer {
            try? FileManager.default.removeItem(at: lockURL)
        }
        let seed = PairingStore(
            defaults: defaults,
            keychain: credentials,
            lockURL: lockURL
        )
        let reader = PairingStore(
            defaults: defaults,
            keychain: credentials,
            lockURL: lockURL
        )
        let writer = PairingStore(
            defaults: defaults,
            keychain: credentials,
            lockURL: lockURL
        )
        let original = try seed.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let candidate = try PairingStore.validatedPairing(
            baseURLString: "https://new.healthmes.example",
            token: "new-secret"
        )
        let loaded = TestLockedBox<Pairing?>(nil)
        let writerError = TestLockedBox<Error?>(nil)
        let readFinished = DispatchSemaphore(value: 0)
        let writeFinished = DispatchSemaphore(value: 0)

        credentials.blockNextRead()
        DispatchQueue.global().async {
            loaded.set(reader.load())
            readFinished.signal()
        }
        XCTAssertTrue(
            credentials.waitUntilReadBlocks(timeout: 1)
        )
        DispatchQueue.global().async {
            do {
                _ = try writer.beginReplacement(with: candidate)
            } catch {
                writerError.set(error)
            }
            writeFinished.signal()
        }

        XCTAssertEqual(
            writeFinished.wait(timeout: .now() + 0.1),
            .timedOut
        )
        credentials.releaseBlockedRead()
        XCTAssertEqual(
            readFinished.wait(timeout: .now() + 1),
            .success
        )
        XCTAssertEqual(
            writeFinished.wait(timeout: .now() + 1),
            .success
        )

        XCTAssertNil(writerError.get())
        XCTAssertEqual(loaded.get(), original)
        XCTAssertTrue(writer.hasPendingTransition)
        XCTAssertEqual(writer.pendingTransition()?.candidate, candidate)
        writer.abortPendingTransition()
    }

    func testLatestValueBufferDoesNotLoseReplacementDuringDelivery() {
        let buffer = LockedLatestValue("old")
        let deliveryEntered = DispatchSemaphore(value: 0)
        let releaseDelivery = DispatchSemaphore(value: 0)
        let deliveryFinished = DispatchSemaphore(value: 0)
        let replacementFinished = DispatchSemaphore(value: 0)

        DispatchQueue.global().async {
            _ = buffer.deliver { value in
                XCTAssertEqual(value, "old")
                deliveryEntered.signal()
                releaseDelivery.wait()
            }
            deliveryFinished.signal()
        }
        XCTAssertEqual(
            deliveryEntered.wait(timeout: .now() + 1),
            .success
        )
        DispatchQueue.global().async {
            buffer.replace(with: "new")
            replacementFinished.signal()
        }

        XCTAssertEqual(
            replacementFinished.wait(timeout: .now() + 0.1),
            .timedOut
        )
        releaseDelivery.signal()
        XCTAssertEqual(
            deliveryFinished.wait(timeout: .now() + 1),
            .success
        )
        XCTAssertEqual(
            replacementFinished.wait(timeout: .now() + 1),
            .success
        )
        XCTAssertEqual(buffer.snapshot(), "new")
    }

    func testPairingContextApplicationDoesNotReplaceAccountWhenStorageFails() throws {
        let (store, defaults, credentials, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let original = try store.save(
            baseURLString: "https://first.healthmes.example",
            token: "first-token"
        )
        credentials.failNextWrite = true

        XCTAssertFalse(
            PairingContextApplication.apply(
                baseURLString: "https://second.healthmes.example",
                token: "second-token",
                pairingStore: store
            )
        )
        XCTAssertEqual(store.load(), original)
    }

    func testWatchPairingContextCanBeRebuiltFromPersistedPairing() {
        let pairing = Pairing(
            baseURL: URL(string: "https://healthmes.example.com")!,
            token: "secret"
        )

        XCTAssertEqual(
            PairingSyncKeys.context(for: pairing)[PairingSyncKeys.baseURL]
                as? String,
            "https://healthmes.example.com"
        )
        XCTAssertEqual(
            PairingSyncKeys.context(for: pairing)[PairingSyncKeys.token]
                as? String,
            "secret"
        )
        XCTAssertEqual(
            PairingSyncKeys.context(for: nil)[PairingSyncKeys.baseURL]
                as? String,
            ""
        )
        XCTAssertEqual(
            PairingSyncKeys.context(for: nil)[PairingSyncKeys.token]
                as? String,
            ""
        )
    }

    func testPairingScopeRejectsMissingMalformedAndOtherAccountFingerprints() throws {
        let (store, defaults, _, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let active = try store.save(
            baseURLString: "https://healthmes.example.com",
            token: "active-token"
        )
        let other = Pairing(
            baseURL: URL(string: "https://healthmes.example.com")!,
            token: "other-token"
        )

        XCTAssertTrue(PairingScope.isValidFingerprint(active.cacheFingerprint))
        XCTAssertFalse(PairingScope.isValidFingerprint(nil))
        XCTAssertFalse(PairingScope.isValidFingerprint("not-a-fingerprint"))
        XCTAssertFalse(
            PairingScope.isValidFingerprint(active.cacheFingerprint.uppercased())
        )
        XCTAssertTrue(
            PairingScope.matches(
                fingerprint: active.cacheFingerprint,
                pairing: active
            )
        )
        XCTAssertFalse(
            PairingScope.matches(
                fingerprint: active.cacheFingerprint,
                pairing: other
            )
        )
        XCTAssertEqual(
            PairingScope.matchingPairing(
                fingerprint: active.cacheFingerprint,
                store: store
            ),
            active
        )
        XCTAssertNil(
            PairingScope.matchingPairing(
                fingerprint: other.cacheFingerprint,
                store: store
            )
        )
    }

    func testPairingScopeRejectsPreviousGenerationAfterSamePairingReturns()
        throws
    {
        let (store, defaults, _, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let pairing = try store.save(
            baseURLString: "https://healthmes.example.com",
            token: "active-token"
        )
        let firstIdentity = try XCTUnwrap(
            store.cacheIdentity(for: pairing)
        )
        XCTAssertEqual(
            PairingScope.matchingPairing(
                fingerprint: firstIdentity.fingerprint,
                generation: firstIdentity.generation,
                store: store
            ),
            pairing
        )

        XCTAssertTrue(store.clear())
        let restored = try store.save(
            baseURLString: pairing.baseURL.absoluteString,
            token: pairing.token ?? ""
        )
        let restoredIdentity = try XCTUnwrap(
            store.cacheIdentity(for: restored)
        )

        XCTAssertEqual(restored.cacheFingerprint, pairing.cacheFingerprint)
        XCTAssertGreaterThan(
            restoredIdentity.generation,
            firstIdentity.generation
        )
        XCTAssertNil(
            PairingScope.matchingPairing(
                fingerprint: firstIdentity.fingerprint,
                generation: firstIdentity.generation,
                store: store
            )
        )
        XCTAssertEqual(
            PairingScope.matchingPairing(
                fingerprint: restoredIdentity.fingerprint,
                generation: restoredIdentity.generation,
                store: store
            ),
            restored
        )
    }

    #if DEBUG && targetEnvironment(simulator)
        func testPairingScopeBuildsOnlyMatchingTokenlessLoopbackDemoPairing() throws {
            let loopback = Pairing(
                baseURL: try XCTUnwrap(
                    URL(string: "http://127.0.0.1:8201")
                ),
                token: nil
            )
            XCTAssertEqual(
                PairingScope.debugSimulatorLoopbackPairing(
                    baseURLString: loopback.baseURL.absoluteString,
                    fingerprint: loopback.cacheFingerprint
                ),
                loopback
            )
            XCTAssertNil(
                PairingScope.debugSimulatorLoopbackPairing(
                    baseURLString: loopback.baseURL.absoluteString,
                    fingerprint: String(repeating: "a", count: 64)
                )
            )

            let remote = Pairing(
                baseURL: try XCTUnwrap(
                    URL(string: "https://healthmes.example.com")
                ),
                token: nil
            )
            XCTAssertNil(
                PairingScope.debugSimulatorLoopbackPairing(
                    baseURLString: remote.baseURL.absoluteString,
                    fingerprint: remote.cacheFingerprint
                )
            )
        }
    #endif

    func testPairingExchangeReportsExpiredAndConsumedCodes() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        let client = PairingExchangeClient(
            session: URLSession(configuration: configuration)
        )
        let deepLink = try XCTUnwrap(
            URL(
                string:
                    "healthmes://pair?url=https%3A%2F%2Fhealthmes.example.com&code=one-time-code"
            )
        )

        for (status, expected) in [
            (409, PairingError.pairingCodeConsumed),
            (410, PairingError.pairingCodeExpired),
        ] {
            StubURLProtocol.handler = { _ in (status, [:], Data()) }
            do {
                _ = try await client.exchange(deepLink)
                XCTFail("Expected pairing exchange to fail with HTTP \(status)")
            } catch let error as PairingError {
                XCTAssertEqual(error, expected)
            }
        }
        StubURLProtocol.handler = nil
    }

    func testCacheControlMaxAgeParsing() {
        XCTAssertEqual(GlanceClient.maxAgeSeconds(fromCacheControl: "private, max-age=300"), 300)
        XCTAssertEqual(GlanceClient.maxAgeSeconds(fromCacheControl: "MAX-AGE=60"), 60)
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: "private"))
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: nil))
        XCTAssertNil(GlanceClient.maxAgeSeconds(fromCacheControl: "max-age=oops"))
    }

    private func makePairingStore() throws -> (
        PairingStore,
        UserDefaults,
        FakePairingTokenStore,
        String
    ) {
        let suiteName = "healthmes-pairing-tests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let credentials = FakePairingTokenStore()
        return (
            PairingStore(defaults: defaults, keychain: credentials),
            defaults,
            credentials,
            suiteName
        )
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
        let (pairingStore, defaults, _, suiteName) = try makePairingStore()
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let activePairing = try pairingStore.save(
            baseURLString: "https://healthmes.example",
            token: "secret-token"
        )
        let client = GlanceClient(
            session: URLSession(configuration: configuration),
            cache: cache,
            pairingStore: pairingStore
        )

        // First poll: unconditional, answered 200.
        StubURLProtocol.handler = { request in
            XCTAssertNil(request.value(forHTTPHeaderField: "If-None-Match"))
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"), "Bearer secret-token"
            )
            return (200, headers, body)
        }
        let now = Date(timeIntervalSince1970: 1_783_606_980)
        let first = try await client.fetch(pairing: activePairing, now: now)
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
        let second = try await client.fetch(pairing: activePairing, now: later)
        XCTAssertTrue(second.revalidated)
        XCTAssertEqual(second.payload, first.payload)
        XCTAssertEqual(second.nextRefresh, later.addingTimeInterval(300))
        XCTAssertEqual(cache.load()?.fetchedAt, later)

        // Token rejection surfaces as .unauthorized.
        StubURLProtocol.handler = { _ in (401, [:], Data()) }
        do {
            _ = try await client.fetch(pairing: activePairing, now: later)
            XCTFail("expected unauthorized")
        } catch GlanceClientError.unauthorized(let status) {
            XCTAssertEqual(status, 401)
        }
    }
}

final class FakePairingTokenStore: PairingTokenStoring {
    private let stateLock = NSLock()
    private var storedTokens: [String: String] = [:]
    private var nextReadOverride: [String: String] = [:]
    private var shouldBlockNextRead = false
    private let blockedReadEntered = DispatchSemaphore(value: 0)
    private let blockedReadRelease = DispatchSemaphore(value: 0)
    var failNextWrite = false
    var corruptReadAfterNextWrite = false
    var failNextRecoveryRead = false

    var tokens: [String: String] {
        get {
            stateLock.lock()
            defer { stateLock.unlock() }
            return storedTokens
        }
        set {
            stateLock.lock()
            defer { stateLock.unlock() }
            storedTokens = newValue
        }
    }

    var token: String? {
        get {
            stateLock.lock()
            defer { stateLock.unlock() }
            return storedTokens["api-token"]
                ?? storedTokens.values.first
        }
        set {
            stateLock.lock()
            defer { stateLock.unlock() }
            if let newValue {
                storedTokens["api-token"] = newValue
            } else {
                storedTokens.removeAll()
            }
        }
    }

    func readToken(identifier: String) -> String? {
        stateLock.lock()
        let shouldBlock = shouldBlockNextRead
        shouldBlockNextRead = false
        stateLock.unlock()
        if shouldBlock {
            blockedReadEntered.signal()
            blockedReadRelease.wait()
        }

        stateLock.lock()
        defer { stateLock.unlock() }
        if let override = nextReadOverride.removeValue(
            forKey: identifier
        ) {
            return override
        }
        return storedTokens[identifier]
    }

    func readTokenForRecovery(identifier: String) throws -> String? {
        stateLock.lock()
        if failNextRecoveryRead {
            failNextRecoveryRead = false
            stateLock.unlock()
            throw PairingError.credentialStorageFailed
        }
        stateLock.unlock()
        return readToken(identifier: identifier)
    }

    func writeToken(_ token: String, identifier: String) throws {
        stateLock.lock()
        defer { stateLock.unlock() }
        if failNextWrite {
            failNextWrite = false
            throw PairingError.credentialStorageFailed
        }
        storedTokens[identifier] = token
        if corruptReadAfterNextWrite {
            corruptReadAfterNextWrite = false
            nextReadOverride[identifier] = "read-back-mismatch"
        }
    }

    func deleteToken(identifier: String) {
        stateLock.lock()
        defer { stateLock.unlock() }
        storedTokens.removeValue(forKey: identifier)
        nextReadOverride.removeValue(forKey: identifier)
    }

    func blockNextRead() {
        stateLock.lock()
        defer { stateLock.unlock() }
        shouldBlockNextRead = true
    }

    func waitUntilReadBlocks(timeout: TimeInterval) -> Bool {
        blockedReadEntered.wait(
            timeout: .now() + timeout
        ) == .success
    }

    func releaseBlockedRead() {
        blockedReadRelease.signal()
    }
}

private final class RecordingKeychainSecurity {
    private let token: String
    private(set) var readAccessGroups: [String?] = []
    private(set) var updatedAccessGroups: [String?] = []
    private(set) var addedAccessGroups: [String?] = []

    init(token: String) {
        self.token = token
    }

    var operations: KeychainSecurityOperations {
        KeychainSecurityOperations(
            copyMatching: { [self] query, item in
                let accessGroup = accessGroup(in: query)
                readAccessGroups.append(accessGroup)
                guard accessGroup == nil else {
                    return errSecMissingEntitlement
                }
                item?.pointee = Data(token.utf8) as CFData
                return errSecSuccess
            },
            update: { [self] query, _ in
                let accessGroup = accessGroup(in: query)
                updatedAccessGroups.append(accessGroup)
                return accessGroup == nil
                    ? errSecItemNotFound
                    : errSecMissingEntitlement
            },
            add: { [self] attributes, _ in
                let accessGroup = accessGroup(in: attributes)
                addedAccessGroups.append(accessGroup)
                return accessGroup == nil
                    ? errSecSuccess
                    : errSecMissingEntitlement
            },
            delete: { _ in errSecSuccess }
        )
    }

    private func accessGroup(in dictionary: CFDictionary) -> String? {
        let attributes = dictionary as NSDictionary
        return attributes[kSecAttrAccessGroup as String] as? String
    }
}

final class TestLockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) {
        self.value = value
    }

    func get() -> Value {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ value: Value) {
        lock.lock()
        defer { lock.unlock() }
        self.value = value
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
