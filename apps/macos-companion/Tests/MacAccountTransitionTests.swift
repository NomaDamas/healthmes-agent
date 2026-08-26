import UserNotifications
import XCTest

@MainActor
final class MacAccountTransitionTests: XCTestCase {
    func testReplacementCleanupFailureKeepsOldPairingVisible() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let original = try fixture.store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        let glanceStore = GlanceStore(
            client: GlanceClient(
                cache: fixture.cache,
                pairingStore: fixture.store
            ),
            api: HealthMesAPI(pairingStore: fixture.store),
            pairingStore: fixture.store,
            notificationCleanup: { false }
        )

        do {
            try await glanceStore.pair(
                baseURLString: "https://new.healthmes.example",
                token: "new-secret"
            )
            XCTFail("expected cleanup failure")
        } catch MacPairingTransitionError.notificationCleanupFailed {
            XCTAssertEqual(fixture.store.load(), original)
            XCTAssertFalse(fixture.store.hasPendingTransition)
            XCTAssertTrue(glanceStore.isPaired)
        }
    }

    func testUnpairCleanupFailureDoesNotPresentFalseUnpairedState() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let original = try fixture.store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        let glanceStore = GlanceStore(
            client: GlanceClient(
                cache: fixture.cache,
                pairingStore: fixture.store
            ),
            api: HealthMesAPI(pairingStore: fixture.store),
            pairingStore: fixture.store,
            notificationCleanup: { false }
        )

        do {
            try await glanceStore.unpair()
            XCTFail("expected cleanup failure")
        } catch MacPairingTransitionError.notificationCleanupFailed {
            XCTAssertEqual(fixture.store.load(), original)
            XCTAssertFalse(fixture.store.hasPendingTransition)
            XCTAssertTrue(glanceStore.isPaired)
        }
    }

    func testReplacementCommitFailureKeepsConnectedPresentationFenced() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        _ = try fixture.store.save(
            baseURLString: "https://old.healthmes.example",
            token: "old-secret"
        )
        fixture.tokenStore.failCleanupDeletion = true
        let glanceStore = GlanceStore(
            client: GlanceClient(
                cache: fixture.cache,
                pairingStore: fixture.store
            ),
            api: HealthMesAPI(pairingStore: fixture.store),
            pairingStore: fixture.store,
            notificationCleanup: { true }
        )

        do {
            try await glanceStore.pair(
                baseURLString: "https://new.healthmes.example",
                token: "new-secret"
            )
            XCTFail("expected commit failure")
        } catch {
            XCTAssertTrue(fixture.store.hasPendingTransition)
            XCTAssertTrue(fixture.store.hasPersistedPairingState)
            XCTAssertNil(fixture.store.load())
            XCTAssertFalse(glanceStore.isPaired)
            XCTAssertNil(glanceStore.payload)
            XCTAssertTrue(glanceStore.alerts.isEmpty)
            XCTAssertTrue(glanceStore.pendingProposals.isEmpty)
            XCTAssertEqual(glanceStore.pairingRevision, 1)
        }
    }

    func testNotificationMarksSeenOnlyAfterSuccessfulScheduling() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let pairing = try fixture.store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        fixture.defaults.set(
            true,
            forKey: MacNotificationManager.enabledDefaultsKey
        )
        var attempts = 0
        let manager = MacNotificationManager(
            seenStore: fixture.seenStore,
            pairingStore: fixture.store,
            defaults: fixture.defaults,
            notificationOperations: MacNotificationOperations(
                add: { _ in
                    attempts += 1
                    if attempts == 1 {
                        throw URLError(.cannotConnectToHost)
                    }
                },
                removePending: { _ in },
                deletionSurface: emptyNotificationSurface()
            )
        )
        let first = alert()
        let second = alert()

        await manager.process(
            alerts: [first, second],
            pendingProposals: [],
            pairing: pairing
        )

        XCTAssertEqual(fixture.seenStore.unseen(from: [first]), [first])
        XCTAssertTrue(fixture.seenStore.unseen(from: [second]).isEmpty)
    }

    func testNotificationLeaseBlocksGenerationChangeDuringScheduling()
        async throws
    {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let pairing = try fixture.store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        fixture.defaults.set(
            true,
            forKey: MacNotificationManager.enabledDefaultsKey
        )
        var transitionWasBlocked = false
        let manager = MacNotificationManager(
            seenStore: fixture.seenStore,
            pairingStore: fixture.store,
            defaults: fixture.defaults,
            notificationOperations: MacNotificationOperations(
                add: { _ in
                    do {
                        _ = try fixture.store.save(
                            baseURLString:
                                "https://other.healthmes.example",
                            token: "other-secret"
                        )
                    } catch PairingError.storageLockFailed {
                        transitionWasBlocked = true
                    }
                },
                removePending: { _ in },
                deletionSurface: emptyNotificationSurface()
            )
        )
        let candidate = alert()

        await manager.process(
            alerts: [candidate],
            pendingProposals: [],
            pairing: pairing
        )

        XCTAssertTrue(transitionWasBlocked)
        XCTAssertEqual(fixture.store.load(), pairing)
        XCTAssertTrue(
            fixture.seenStore.unseen(from: [candidate]).isEmpty
        )
    }

    func testDisablingNotificationsRemovesInFlightDelivery() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let pairing = try fixture.store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        fixture.defaults.set(
            true,
            forKey: MacNotificationManager.enabledDefaultsKey
        )
        let addStarted = expectation(description: "notification add started")
        let gate = NotificationAddGate()
        var removedPending: [String] = []
        var removedDelivered: [String] = []
        let manager = MacNotificationManager(
            seenStore: fixture.seenStore,
            pairingStore: fixture.store,
            defaults: fixture.defaults,
            notificationOperations: MacNotificationOperations(
                add: { _ in
                    addStarted.fulfill()
                    await gate.wait()
                },
                removePending: {
                    removedPending.append(contentsOf: $0)
                },
                removeDelivered: {
                    removedDelivered.append(contentsOf: $0)
                },
                deletionSurface: emptyNotificationSurface()
            )
        )
        let candidate = alert()
        let processing = Task {
            await manager.process(
                alerts: [candidate],
                pendingProposals: [],
                pairing: pairing
            )
        }
        await fulfillment(of: [addStarted], timeout: 2)

        await manager.setEnabled(
            false,
            currentAlerts: [candidate],
            hasLoadedAlerts: true
        )
        await gate.release()
        await processing.value

        let identifier =
            "healthmes-alert-\(candidate.id.uuidString.lowercased())"
        XCTAssertEqual(removedPending, [identifier])
        XCTAssertEqual(removedDelivered, [identifier])
        XCTAssertEqual(fixture.seenStore.unseen(from: [candidate]), [candidate])
    }

    func testConcurrentNotificationFeedsAreSerialized() async throws {
        let fixture = try makeFixture()
        defer { fixture.cleanup() }
        let pairing = try fixture.store.save(
            baseURLString: "https://healthmes.example",
            token: "secret"
        )
        fixture.defaults.set(
            true,
            forKey: MacNotificationManager.enabledDefaultsKey
        )
        let firstAddStarted = expectation(
            description: "first notification add started"
        )
        let gate = NotificationAddGate()
        var identifiers: [String] = []
        let manager = MacNotificationManager(
            seenStore: fixture.seenStore,
            pairingStore: fixture.store,
            defaults: fixture.defaults,
            notificationOperations: MacNotificationOperations(
                add: { request in
                    identifiers.append(request.identifier)
                    if identifiers.count == 1 {
                        firstAddStarted.fulfill()
                        await gate.wait()
                    }
                },
                removePending: { _ in },
                deletionSurface: emptyNotificationSurface()
            )
        )
        let first = alert()
        let second = alert()
        let firstFeed = Task {
            await manager.process(
                alerts: [first],
                pendingProposals: [],
                pairing: pairing
            )
        }
        await fulfillment(of: [firstAddStarted], timeout: 2)
        let secondFeed = Task {
            await manager.process(
                alerts: [first, second],
                pendingProposals: [],
                pairing: pairing
            )
        }

        await gate.release()
        await firstFeed.value
        await secondFeed.value

        XCTAssertEqual(
            identifiers,
            [
                "healthmes-alert-\(first.id.uuidString.lowercased())",
                "healthmes-alert-\(second.id.uuidString.lowercased())",
            ]
        )
        XCTAssertTrue(fixture.seenStore.unseen(from: [first, second]).isEmpty)
    }

    func testOutcomeNotificationCarriesPairingIdentity() {
        let identity = PairingCacheIdentity(
            fingerprint: String(repeating: "a", count: 64),
            generation: 7
        )

        let content = MacNotificationManager.outcomeNotificationContent(
            .accepted,
            identity: identity
        )

        XCTAssertEqual(
            content.userInfo[
                AlertNotificationContent.userInfoPairingFingerprint
            ] as? String,
            identity.fingerprint
        )
        XCTAssertEqual(
            content.userInfo[
                AlertNotificationContent.userInfoPairingGeneration
            ] as? String,
            "7"
        )
    }

    func testNotificationDeletionCancellationFailsClosed() async {
        let sleepStarted = expectation(description: "sleep started")
        let barrier = NotificationDeletionBarrier(
            maximumPolls: 3,
            requiredEmptyPolls: 2,
            sleep: {
                sleepStarted.fulfill()
                try await Task.sleep(nanoseconds: 5_000_000_000)
            }
        )
        let operation = Task {
            await barrier.clear(
                using: NotificationSurface(
                    removeAll: {},
                    snapshot: {
                        NotificationSurfaceSnapshot(
                            pendingCount: 0,
                            deliveredCount: 0
                        )
                    }
                )
            )
        }
        await fulfillment(of: [sleepStarted], timeout: 2)

        operation.cancel()

        let result = await operation.value
        guard case .failure(.cancelled) = result else {
            return XCTFail("expected cancellation to fail closed")
        }
    }

    private func alert() -> AlertItem {
        AlertItem(
            id: UUID(),
            ruleId: "test",
            firedAt: Date(),
            summary: "Review recovery.",
            proposal: nil,
            evidence: nil,
            decisionUrl: nil,
            proposalId: nil
        )
    }

    private func emptyNotificationSurface() -> NotificationSurface {
        NotificationSurface(
            removeAll: {},
            snapshot: {
                NotificationSurfaceSnapshot(
                    pendingCount: 0,
                    deliveredCount: 0
                )
            }
        )
    }

    private func makeFixture() throws -> AccountTransitionFixture {
        let suite = "healthmes-mac-transition-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        let lockURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("healthmes-mac-transition-\(UUID().uuidString)")
        let cacheURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "healthmes-mac-transition-cache-\(UUID().uuidString).json"
            )
        let tokenStore = MacTestPairingTokenStore()
        return AccountTransitionFixture(
            suite: suite,
            defaults: defaults,
            lockURL: lockURL,
            cacheURL: cacheURL,
            cache: GlanceSnapshotCache(fileURL: cacheURL),
            store: PairingStore(
                defaults: defaults,
                keychain: tokenStore,
                lockURL: lockURL
            ),
            seenStore: SeenAlertsStore(defaults: defaults),
            tokenStore: tokenStore
        )
    }
}

private actor NotificationAddGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false

    func wait() async {
        guard !released else { return }
        await withCheckedContinuation { continuation in
            self.continuation = continuation
        }
    }

    func release() {
        released = true
        continuation?.resume()
        continuation = nil
    }
}

private struct AccountTransitionFixture {
    let suite: String
    let defaults: UserDefaults
    let lockURL: URL
    let cacheURL: URL
    let cache: GlanceSnapshotCache
    let store: PairingStore
    let seenStore: SeenAlertsStore
    let tokenStore: MacTestPairingTokenStore

    func cleanup() {
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: lockURL)
        try? FileManager.default.removeItem(at: cacheURL)
        try? FileManager.default.removeItem(
            at: cacheURL.appendingPathExtension("lock")
        )
    }
}

private final class MacTestPairingTokenStore: PairingTokenStoring {
    private var tokens: [String: String] = [:]
    var failCleanupDeletion = false

    func readToken(identifier: String) -> String? {
        tokens[identifier]
    }

    func writeToken(_ token: String, identifier: String) throws {
        tokens[identifier] = token
    }

    func deleteToken(identifier: String) {
        tokens.removeValue(forKey: identifier)
    }

    func deleteTokenForCleanup(identifier: String) throws {
        if failCleanupDeletion {
            throw PairingError.credentialStorageFailed
        }
        deleteToken(identifier: identifier)
    }
}
