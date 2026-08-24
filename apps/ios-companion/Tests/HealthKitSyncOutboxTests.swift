import CryptoKit
import Foundation
import XCTest

private final class RecordingHealthKitKeyProvider:
    @unchecked Sendable,
    HealthKitSyncOutboxKeyProviding
{
    private let key: SymmetricKey
    var isAvailable = true
    var deleteError: HealthKitSyncOutboxError?
    var loadKeyErrors: [HealthKitSyncOutboxError] = []
    private(set) var deleteCalls = 0

    init(seed: UInt8 = 7) {
        key = SymmetricKey(
            data: Data(repeating: seed, count: 32)
        )
    }

    func loadOrCreateKey() throws -> SymmetricKey {
        key
    }

    func loadKey() throws -> SymmetricKey? {
        if !loadKeyErrors.isEmpty {
            throw loadKeyErrors.removeFirst()
        }
        return isAvailable ? key : nil
    }

    func deleteKey() throws {
        deleteCalls += 1
        if let deleteError {
            throw deleteError
        }
        isAvailable = false
    }

    func fixtureKey() -> SymmetricKey {
        key
    }
}

private struct LegacyHealthKitOutboxStoredFile: Encodable {
    let version: Int
    let sealedData: Data
}

private struct LegacyHealthKitOutboxEnvelope: Encodable {
    let version: Int
    let entries: [LegacyHealthKitOutboxEntry]
}

private struct LegacyHealthKitOutboxEntry: Encodable {
    let idempotencyKey: String
    let body: Data
    let anchors: [String: Data]
    let destinationFingerprint: String
    let enqueuedAt: Date
    let failedAttempts: Int
    let nextAttemptAt: Date
    let terminalFailure: String?
    let terminalAnchorsCommitted: Bool?
    let forwardingFailedAttempts: Int?
}

final class HealthKitSyncOutboxTests: XCTestCase {
    func testEnqueuePreservesExactBodyAndAnchorsAcrossReload() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data(#"{"data":{"records":[]},"schema":"healthmes.healthkit.v1"}"#.utf8)
        let anchors = [
            "heartRate": Data([0, 1, 2, 3]),
            "sleep": Data([9, 8, 7]),
        ]
        let destination = "destination-a"
        let now = Date(timeIntervalSince1970: 1_787_000_000)

        let entry = try await fixture.outbox.enqueue(
            body: body,
            anchors: anchors,
            destinationFingerprint: destination,
            enqueuedAt: now
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        let persisted = try await reloaded.pendingEntries(
            destinationFingerprint: destination
        )

        XCTAssertEqual(persisted.count, 1)
        XCTAssertEqual(persisted[0].body, body)
        XCTAssertEqual(persisted[0].anchors, anchors)
        XCTAssertEqual(persisted[0].enqueuedAt, now)
        XCTAssertEqual(
            entry.idempotencyKey,
            HealthKitSyncOutboxIdentity.idempotencyKey(for: body)
        )
        XCTAssertTrue(entry.idempotencyKey.hasPrefix("hm-ios-hk-v1-"))

        let encrypted = try Data(contentsOf: fixture.fileURL)
        XCTAssertNil(encrypted.range(of: body))
        XCTAssertNil(encrypted.range(of: Data(destination.utf8)))
    }

    func testIdentityUsesExactBytesAndPairingDestination() {
        let firstBody = Data(#"{"value":1}"#.utf8)
        let secondBody = Data(#"{ "value": 1 }"#.utf8)
        let baseURL = URL(string: "https://healthmes.example")!

        XCTAssertNotEqual(
            HealthKitSyncOutboxIdentity.idempotencyKey(for: firstBody),
            HealthKitSyncOutboxIdentity.idempotencyKey(for: secondBody)
        )
        XCTAssertEqual(
            HealthKitSyncOutboxIdentity.destinationFingerprint(
                baseURL: baseURL,
                token: "first"
            ),
            HealthKitSyncOutboxIdentity.destinationFingerprint(
                baseURL: baseURL,
                token: "first"
            )
        )
        XCTAssertNotEqual(
            HealthKitSyncOutboxIdentity.destinationFingerprint(
                baseURL: baseURL,
                token: "first"
            ),
            HealthKitSyncOutboxIdentity.destinationFingerprint(
                baseURL: baseURL,
                token: "second"
            )
        )
    }

    func testSameDestinationDeduplicatesButDifferentAnchorsFailClosed() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        let firstAnchors = ["heartRate": Data([1])]
        let secondAnchors = ["heartRate": Data([2])]

        let first = try await fixture.outbox.enqueue(
            body: body,
            anchors: firstAnchors,
            destinationFingerprint: "destination-a"
        )
        let duplicate = try await fixture.outbox.enqueue(
            body: body,
            anchors: firstAnchors,
            destinationFingerprint: "destination-a"
        )

        XCTAssertEqual(first, duplicate)
        await assertOutboxError(.duplicatePayloadConflict) {
            _ = try await fixture.outbox.enqueue(
                body: body,
                anchors: secondAnchors,
                destinationFingerprint: "destination-a"
            )
        }
    }

    func testDestinationsAreIsolatedForCountsAndCompletion() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)

        let first = try await fixture.outbox.enqueue(
            body: body,
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        _ = try await fixture.outbox.enqueue(
            body: body,
            anchors: [:],
            destinationFingerprint: "destination-b"
        )

        let firstCount = try await fixture.outbox.pendingCount(
            destinationFingerprint: "destination-a"
        )
        let secondCount = try await fixture.outbox.pendingCount(
            destinationFingerprint: "destination-b"
        )
        XCTAssertEqual(firstCount, 1)
        XCTAssertEqual(secondCount, 1)

        try await fixture.outbox.markSucceeded(
            idempotencyKey: first.idempotencyKey,
            destinationFingerprint: "destination-a"
        )
        let completedCount = try await fixture.outbox.pendingCount(
            destinationFingerprint: "destination-a"
        )
        let retainedCount = try await fixture.outbox.pendingCount(
            destinationFingerprint: "destination-b"
        )
        XCTAssertEqual(completedCount, 0)
        XCTAssertEqual(retainedCount, 1)
    }

    func testExponentialBackoffPersistsAcrossReload() async throws {
        let fixture = makeFixture(
            retryPolicy: HealthKitSyncRetryPolicy(
                initialDelay: 10,
                maximumDelay: 60
            )
        )
        defer { removeFixture(fixture) }
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        let entry = try await fixture.outbox.enqueue(
            body: body,
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now
        )

        let firstFailure = try await fixture.outbox.markFailed(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            now: now
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider,
            retryPolicy: HealthKitSyncRetryPolicy(
                initialDelay: 10,
                maximumDelay: 60
            )
        )
        let secondFailure = try await reloaded.markFailed(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            now: now.addingTimeInterval(10)
        )

        XCTAssertEqual(firstFailure?.failedAttempts, 1)
        XCTAssertEqual(
            firstFailure?.nextAttemptAt,
            now.addingTimeInterval(10)
        )
        XCTAssertEqual(secondFailure?.failedAttempts, 2)
        XCTAssertEqual(
            secondFailure?.nextAttemptAt,
            now.addingTimeInterval(30)
        )
        let deferred = try await reloaded.nextPending(
            destinationFingerprint: "destination-a",
            now: now.addingTimeInterval(20)
        )
        XCTAssertNil(deferred)
    }

    func testForwardingFailureCounterExcludesTransportFailures() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let entry = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )

        for _ in 0..<7 {
            _ = try await fixture.outbox.markFailed(
                idempotencyKey: entry.idempotencyKey,
                destinationFingerprint: "destination-a"
            )
        }
        let forwardingFailure = try await fixture.outbox.markFailed(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            countsTowardAutomaticQuarantine: true
        )

        XCTAssertEqual(forwardingFailure?.failedAttempts, 8)
        XCTAssertEqual(
            forwardingFailure?.forwardingFailedAttempts,
            1
        )
    }

    func testTerminalFailurePersistsAndStopsAutomaticRetry() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let entry = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now
        )

        let terminal = try await fixture.outbox.markTerminal(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            reason: "HTTP 422 invalid payload",
            anchorsCommitted: true
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )

        XCTAssertEqual(terminal?.terminalFailure, "HTTP 422 invalid payload")
        XCTAssertEqual(terminal?.terminalAnchorsCommitted, true)
        let nextPending = try await reloaded.nextPending(
            destinationFingerprint: "destination-a",
            now: now.addingTimeInterval(86_400)
        )
        XCTAssertNil(nextPending)
        let persisted = try await reloaded.pendingEntries(
            destinationFingerprint: "destination-a"
        )
        XCTAssertEqual(persisted.first?.terminalFailure, "HTTP 422 invalid payload")

        let retried = try await reloaded.markFailed(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            preserveTerminalFailure: true,
            now: now
        )
        XCTAssertEqual(
            retried?.terminalFailure,
            "HTTP 422 invalid payload"
        )
        XCTAssertEqual(retried?.terminalAnchorsCommitted, true)
    }

    func testNextPendingReturnsDueEntryOnly() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let entry = try await fixture.outbox.enqueue(
            body: body,
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now
        )
        _ = try await fixture.outbox.markFailed(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a",
            now: now
        )

        let deferred = try await fixture.outbox.nextPending(
            destinationFingerprint: "destination-a",
            now: now.addingTimeInterval(59)
        )
        let due = try await fixture.outbox.nextPending(
            destinationFingerprint: "destination-a",
            now: now.addingTimeInterval(60)
        )
        XCTAssertNil(deferred)
        XCTAssertEqual(due?.idempotencyKey, entry.idempotencyKey)
    }

    func testPurgeDestinationAndPurgeAllAreExplicit() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8)
        _ = try await fixture.outbox.enqueue(
            body: body,
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1","n":1}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-b"
        )

        let destinationRemoved = try await fixture.outbox.purge(
            destinationFingerprint: "destination-a"
        )
        let retainedCount = try await fixture.outbox.pendingCount(
            destinationFingerprint: "destination-b"
        )
        let allRemoved = try await fixture.outbox.purgeAll()
        XCTAssertEqual(destinationRemoved, 1)
        XCTAssertEqual(retainedCount, 1)
        XCTAssertEqual(allRemoved, 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
    }

    func testCorruptCiphertextIsReportedWithoutDeletingUserData() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: Data(contentsOf: fixture.fileURL)
            ) as? [String: Any]
        )
        let encodedSealedData = try XCTUnwrap(
            object["sealedData"] as? String
        )
        var sealedData = try XCTUnwrap(
            Data(base64Encoded: encodedSealedData)
        )
        sealedData[sealedData.count - 1] ^= 0xff
        object["sealedData"] = sealedData.base64EncodedString()
        let corrupted = try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys]
        )
        try corrupted.write(to: fixture.fileURL, options: .atomic)

        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        await assertOutboxError(.decryptionFailed) {
            _ = try await reloaded.pendingCount(
                destinationFingerprint: "destination-a"
            )
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.fileURL.path))
    }

    func testUnsupportedVersionIsReportedWithoutDeletingUserData() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: Data(contentsOf: fixture.fileURL)
            ) as? [String: Any]
        )
        object["version"] = 99
        let modified = try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys]
        )
        try modified.write(to: fixture.fileURL, options: .atomic)

        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        await assertOutboxError(.unsupportedFileVersion(99)) {
            _ = try await reloaded.pendingCount(
                destinationFingerprint: "destination-a"
            )
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.fileURL.path))
    }

    func testMissingKeyIsReportedWithoutReplacingOrDeletingCiphertext() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        let original = try Data(contentsOf: fixture.fileURL)
        fixture.keyProvider.isAvailable = false

        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        await assertOutboxError(.keyUnavailable) {
            _ = try await reloaded.pendingCount(
                destinationFingerprint: "destination-a"
            )
        }
        XCTAssertEqual(
            try Data(contentsOf: fixture.fileURL),
            original
        )
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 0)
    }

    func testTransientKeychainReadFailureCanRecoverWithoutRestart() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        let transient = HealthKitSyncOutboxError.keychainReadFailed(
            errSecInteractionNotAllowed
        )
        fixture.keyProvider.loadKeyErrors = [transient]

        await assertOutboxError(transient) {
            _ = try await reloaded.pendingCount(
                destinationFingerprint: "destination-a"
            )
        }
        let pendingCount = try await reloaded.pendingCount(
            destinationFingerprint: "destination-a"
        )
        XCTAssertEqual(pendingCount, 1)
    }

    func testOversizedFileIsReportedWithoutDeletingUserData() async throws {
        let fixture = makeFixture(maximumBytes: 128)
        defer { removeFixture(fixture) }
        let oversized = Data(repeating: 0x41, count: 129)
        try FileManager.default.createDirectory(
            at: fixture.fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try oversized.write(to: fixture.fileURL)

        await assertOutboxError(.fileTooLarge(maxBytes: 128)) {
            _ = try await fixture.outbox.pendingCount(
                destinationFingerprint: "destination-a"
            )
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.fileURL.path))
    }

    func testPurgeAllCanRemoveCorruptFileWithoutLoadingIt() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        try FileManager.default.createDirectory(
            at: fixture.fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data([0xff, 0x00, 0x01]).write(to: fixture.fileURL)

        let removed = try await fixture.outbox.purgeAll()
        XCTAssertEqual(removed, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
    }

    func testUserRemovalCanDiscardCorruptQueueWhenKeyDeletionFails() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        try FileManager.default.createDirectory(
            at: fixture.fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data([0xff, 0x00, 0x01]).write(to: fixture.fileURL)
        fixture.keyProvider.deleteError = .keychainDeleteFailed(
            errSecInteractionNotAllowed
        )

        _ = try await fixture.outbox.purgeForUserRemoval(
            destinationFingerprint: "destination-a"
        )

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
        let pendingCount = try await fixture.outbox.pendingCount()
        XCTAssertEqual(pendingCount, 0)
    }

    func testUserRemovalCanDiscardQueueWhenEncryptionKeyIsMissing() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        fixture.keyProvider.isAvailable = false
        fixture.keyProvider.deleteError = .keychainDeleteFailed(
            errSecInteractionNotAllowed
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )

        _ = try await reloaded.purgeForUserRemoval(
            destinationFingerprint: "destination-a"
        )

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
        let pendingCount = try await reloaded.pendingCount()
        XCTAssertEqual(pendingCount, 0)
    }

    func testUserRemovalPreservesQueueOnTransientKeychainFailure() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        let original = try Data(contentsOf: fixture.fileURL)
        let transient = HealthKitSyncOutboxError.keychainReadFailed(
            errSecInteractionNotAllowed
        )
        fixture.keyProvider.loadKeyErrors = [transient]
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )

        await assertOutboxError(transient) {
            _ = try await reloaded.purgeForUserRemoval(
                destinationFingerprint: "destination-a"
            )
        }

        XCTAssertEqual(try Data(contentsOf: fixture.fileURL), original)
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 0)
        let pendingCount = try await reloaded.pendingCount(
            destinationFingerprint: "destination-a"
        )
        XCTAssertEqual(pendingCount, 1)
    }

    func testPurgeAllClearsMemoryWhenKeyDeletionFails() async throws {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        let expected = HealthKitSyncOutboxError.keychainDeleteFailed(-1)
        fixture.keyProvider.deleteError = expected

        await assertOutboxError(expected) {
            _ = try await fixture.outbox.purgeAll()
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
        let pendingCount = try await fixture.outbox.pendingCount()
        XCTAssertEqual(pendingCount, 0)
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
    }

    func testUserRemovalPurgesAllWhenFingerprintCannotBeRecovered()
        async throws
    {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        _ = try await fixture.outbox.enqueue(
            body: Data(#"{"schema":"healthmes.healthkit.v1"}"#.utf8),
            anchors: [:],
            destinationFingerprint: "destination-a"
        )
        fixture.keyProvider.deleteError =
            .keychainDeleteFailed(-1)

        let removed = try await fixture.outbox
            .purgeAllForUserRemoval()

        XCTAssertEqual(removed, 1)
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: fixture.fileURL.path
            )
        )
        let pendingCount = try await fixture.outbox.pendingCount()
        XCTAssertEqual(pendingCount, 0)
        XCTAssertEqual(fixture.keyProvider.deleteCalls, 1)
    }

    func testUnknownFingerprintRemovalScopeIsPrivacySafe() {
        XCTAssertEqual(
            HealthKitSyncRemovalPolicy.scope(
                fingerprint: nil,
                userDirectedRemoval: true
            ),
            .all
        )
        XCTAssertEqual(
            HealthKitSyncRemovalPolicy.scope(
                fingerprint: nil,
                userDirectedRemoval: false
            ),
            .none
        )
        XCTAssertEqual(
            HealthKitSyncRemovalPolicy.scope(
                fingerprint: "destination-a",
                userDirectedRemoval: true
            ),
            .destination("destination-a")
        )
    }

    func testFullLocalStatePurgeKeepsUnrelatedDefaults() throws {
        let suiteName =
            "healthmes-healthkit-local-state-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(
            UserDefaults(suiteName: suiteName)
        )
        defer {
            defaults.removePersistentDomain(forName: suiteName)
        }
        defaults.set(
            Data([1]),
            forKey: "healthmes.healthkit.anchor.first.heartRate"
        )
        defaults.set(
            Data([2]),
            forKey: "healthmes.healthkit.anchor.second.sleep"
        )
        defaults.set(
            true,
            forKey: "healthmes.healthkit.paused.first"
        )
        defaults.set(
            Date(),
            forKey: "healthmes.healthkit.lastUploadAt.second"
        )
        defaults.set("keep", forKey: "healthmes.unrelated")

        HealthKitSyncLocalState.clearAll(defaults: defaults)

        XCTAssertNil(
            defaults.object(
                forKey: "healthmes.healthkit.anchor.first.heartRate"
            )
        )
        XCTAssertNil(
            defaults.object(
                forKey: "healthmes.healthkit.anchor.second.sleep"
            )
        )
        XCTAssertNil(
            defaults.object(
                forKey: "healthmes.healthkit.paused.first"
            )
        )
        XCTAssertNil(
            defaults.object(
                forKey: "healthmes.healthkit.lastUploadAt.second"
            )
        )
        XCTAssertEqual(
            defaults.string(forKey: "healthmes.unrelated"),
            "keep"
        )
    }

    func testCommittedTerminalEntryDoesNotBlockLaterUpload() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let terminal = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("terminal".utf8)
                ),
            body: Data("terminal".utf8),
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now,
            failedAttempts: 0,
            nextAttemptAt: now,
            terminalFailure: "HTTP 422 invalid payload",
            terminalAnchorsCommitted: true
        )
        let retryable = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("retryable".utf8)
                ),
            body: Data("retryable".utf8),
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now.addingTimeInterval(1),
            failedAttempts: 1,
            nextAttemptAt: now.addingTimeInterval(60),
            terminalFailure: nil
        )

        let summary = HealthKitSyncQueueSummary(
            entries: [terminal, retryable]
        )

        XCTAssertEqual(summary.pendingCount, 2)
        XCTAssertEqual(summary.terminalFailureCount, 1)
        XCTAssertEqual(
            summary.latestTerminalFailure,
            "HTTP 422 invalid payload"
        )
        XCTAssertEqual(
            summary.nextRetryAt,
            now.addingTimeInterval(60)
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [terminal, retryable],
                forceRetry: false,
                includeTerminalFailures: false,
                now: now.addingTimeInterval(60)
            ),
            .upload(retryable)
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [terminal, retryable],
                forceRetry: true,
                includeTerminalFailures: true,
                now: now
            ),
            .upload(terminal)
        )
    }

    func testCommittedTerminalHistoryDoesNotConsumeActiveQueueCapacity()
        async throws
    {
        let fixture = makeFixture(maximumEntries: 2)
        defer { removeFixture(fixture) }

        for index in 0..<2 {
            let entry = try await fixture.outbox.enqueue(
                body: Data("terminal-\(index)".utf8),
                anchors: [:],
                destinationFingerprint: "destination-a"
            )
            _ = try await fixture.outbox.markTerminal(
                idempotencyKey: entry.idempotencyKey,
                destinationFingerprint: "destination-a",
                reason: "terminal",
                anchorsCommitted: true
            )
        }

        for index in 0..<2 {
            _ = try await fixture.outbox.enqueue(
                body: Data("active-\(index)".utf8),
                anchors: [:],
                destinationFingerprint: "destination-a"
            )
        }
        let entries = try await fixture.outbox.pendingEntries(
            destinationFingerprint: "destination-a"
        )

        XCTAssertEqual(entries.count, 4)
        XCTAssertEqual(
            entries.filter { $0.terminalFailure != nil }.count,
            2
        )
        await assertOutboxError(.queueTooLarge(maxEntries: 2)) {
            _ = try await fixture.outbox.enqueue(
                body: Data("active-overflow".utf8),
                anchors: [:],
                destinationFingerprint: "destination-a"
            )
        }
    }

    func testUploadAcceptedJournalSurvivesReloadAndRequiresFinalization()
        async throws
    {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let entry = try await fixture.outbox.enqueue(
            body: Data("accepted".utf8),
            anchors: ["heartRate": Data([1, 2, 3])],
            destinationFingerprint: "destination-a"
        )

        let accepted = try await fixture.outbox.markUploadAccepted(
            idempotencyKey: entry.idempotencyKey,
            destinationFingerprint: "destination-a"
        )
        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        let persisted = try await reloaded.pendingEntries(
            destinationFingerprint: "destination-a"
        )

        XCTAssertEqual(accepted?.uploadAccepted, true)
        XCTAssertEqual(persisted.first?.uploadAccepted, true)
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: persisted,
                forceRetry: false,
                includeTerminalFailures: false,
                now: Date()
            ),
            .finalize(try XCTUnwrap(persisted.first))
        )
        let retryable = try await reloaded.nextPending(
            destinationFingerprint: "destination-a",
            now: Date.distantFuture
        )
        XCTAssertNil(retryable)
    }

    func testLegacyEncryptedTerminalEntryMigratesToRetryable()
        async throws
    {
        let fixture = makeFixture()
        defer { removeFixture(fixture) }
        let body = Data("legacy-terminal-journal".utf8)
        let anchors = ["sleep": Data([4, 5, 6])]
        let enqueuedAt = Date(timeIntervalSince1970: 1_787_000_000)
        let retryAt = enqueuedAt.addingTimeInterval(3_600)
        let migrationTime = enqueuedAt.addingTimeInterval(7_200)
        try writeLegacyTerminalFixture(
            fixture,
            body: body,
            anchors: anchors,
            destinationFingerprint: "destination-a",
            enqueuedAt: enqueuedAt,
            nextAttemptAt: retryAt
        )

        let reloaded = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        let legacyEntries = try await reloaded.pendingEntries(
            destinationFingerprint: "destination-a"
        )
        let legacy = try XCTUnwrap(legacyEntries.first)
        XCTAssertEqual(legacy.terminalFailure, "forwarding failed")
        XCTAssertEqual(legacy.terminalAnchorsCommitted, false)
        XCTAssertNil(legacy.terminalFailureIsPermanent)

        let migratedCount = try await reloaded.migrateLegacyTerminalEntries(
            destinationFingerprint: "destination-a",
            now: migrationTime
        )
        XCTAssertEqual(migratedCount, 1)

        let migratedReload = HealthKitSyncOutbox(
            fileURL: fixture.fileURL,
            keyProvider: fixture.keyProvider
        )
        let migratedEntries = try await migratedReload.pendingEntries(
            destinationFingerprint: "destination-a"
        )
        let migrated = try XCTUnwrap(migratedEntries.first)
        XCTAssertEqual(migrated.body, body)
        XCTAssertEqual(migrated.anchors, anchors)
        XCTAssertNil(migrated.terminalFailure)
        XCTAssertNil(migrated.terminalAnchorsCommitted)
        XCTAssertNil(migrated.terminalAnchorCommitPending)
        XCTAssertEqual(migrated.nextAttemptAt, migrationTime)
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: migratedEntries,
                forceRetry: false,
                includeTerminalFailures: false,
                now: migrationTime
            ),
            .upload(migrated)
        )
    }

    func testObserverAcknowledgesOnceAfterSyncFinishes() async {
        var events: [String] = []
        var completionCount = 0

        await HealthKitObserverLifecycle.synchronizeAndAcknowledge(
            completion: {
                events.append("completed")
                completionCount += 1
            },
            synchronize: {
                events.append("sync-started")
                await Task.yield()
                events.append("sync-finished")
            }
        )

        XCTAssertEqual(
            events,
            ["sync-started", "sync-finished", "completed"]
        )
        XCTAssertEqual(completionCount, 1)
    }

    func testBackgroundTaskExpirationCancelsAndCompletesOnce() async {
        let started = expectation(description: "operation started")
        let cancelled = expectation(description: "operation cancelled")
        let completed = expectation(description: "task completed")
        let completions = HealthKitTestLockedValues<Bool>()
        let runner = HealthKitBackgroundTaskRunner(
            operation: {
                started.fulfill()
                do {
                    try await Task.sleep(
                        nanoseconds: 5_000_000_000
                    )
                    return true
                } catch is CancellationError {
                    cancelled.fulfill()
                    return true
                } catch {
                    return false
                }
            },
            completion: { success in
                completions.append(success)
                completed.fulfill()
            }
        )
        runner.start()
        await fulfillment(of: [started], timeout: 2)

        runner.expire()
        runner.expire()

        await fulfillment(
            of: [cancelled, completed],
            timeout: 2
        )
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(completions.values, [false])
    }

    func testUncommittedTerminalEntryKeepsQueueFailClosed() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let terminal = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("terminal".utf8)
                ),
            body: Data("terminal".utf8),
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now,
            failedAttempts: 0,
            nextAttemptAt: now,
            terminalFailure: "HTTP 422 invalid payload",
            terminalAnchorsCommitted: false
        )

        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [terminal],
                forceRetry: false,
                includeTerminalFailures: false,
                now: now
            ),
            .deferred
        )
    }

    func testManualRetryAttemptsEachTerminalOnceBeforeHonoringHeadOfLine() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let first = terminalEntry(
            "first",
            lane: "heartRate",
            committed: false,
            date: now
        )
        let second = terminalEntry(
            "second",
            lane: "sleep",
            committed: true,
            date: now.addingTimeInterval(1)
        )
        let active = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("active".utf8)
            ),
            body: Data("active".utf8),
            anchors: ["workout": Data([3])],
            destinationFingerprint: "destination-a",
            enqueuedAt: now.addingTimeInterval(2),
            failedAttempts: 0,
            nextAttemptAt: now,
            terminalFailure: nil
        )
        let entries = [first, second, active]

        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: entries,
                forceRetry: true,
                includeTerminalFailures: true,
                now: now
            ),
            .upload(first)
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: entries,
                forceRetry: true,
                includeTerminalFailures: true,
                attemptedTerminalKeys: [first.idempotencyKey],
                now: now
            ),
            .upload(second)
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: entries,
                forceRetry: true,
                includeTerminalFailures: true,
                attemptedTerminalKeys: [
                    first.idempotencyKey,
                    second.idempotencyKey,
                ],
                now: now
            ),
            .upload(active)
        )
    }

    func testCommittedRetriedTerminalsDoNotBlockTheActiveQueue() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let first = terminalEntry(
            "first",
            lane: "heartRate",
            committed: true,
            date: now
        )
        let second = terminalEntry(
            "second",
            lane: "sleep",
            committed: true,
            date: now.addingTimeInterval(1)
        )
        let active = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("active".utf8)
            ),
            body: Data("active".utf8),
            anchors: ["workout": Data([3])],
            destinationFingerprint: "destination-a",
            enqueuedAt: now.addingTimeInterval(2),
            failedAttempts: 0,
            nextAttemptAt: now,
            terminalFailure: nil
        )

        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [first, second, active],
                forceRetry: true,
                includeTerminalFailures: true,
                attemptedTerminalKeys: [
                    first.idempotencyKey,
                    second.idempotencyKey,
                ],
                now: now
            ),
            .upload(active)
        )
    }

    func testBlockedLaneDoesNotStopIndependentLaneUpload() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let blocked = terminalEntry(
            "blocked-heart-rate",
            lane: "heartRate",
            committed: false,
            date: now
        )
        let sameLane = activeEntry(
            "new-heart-rate",
            lane: "heartRate",
            date: now.addingTimeInterval(1)
        )
        let independent = activeEntry(
            "new-sleep",
            lane: "sleep",
            date: now.addingTimeInterval(2)
        )

        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [blocked, sameLane, independent],
                forceRetry: false,
                includeTerminalFailures: false,
                now: now.addingTimeInterval(3)
            ),
            .upload(independent)
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.blockedLaneKeys(
                in: [blocked, sameLane]
            ),
            ["heartRate"]
        )
    }

    func testLegacyAnchorlessEntryBlocksEveryLane() {
        let now = Date(timeIntervalSince1970: 1_787_000_000)
        let legacy = HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(
                    for: Data("legacy".utf8)
                ),
            body: Data("legacy".utf8),
            anchors: [:],
            destinationFingerprint: "destination-a",
            enqueuedAt: now,
            failedAttempts: 1,
            nextAttemptAt: now.addingTimeInterval(60),
            terminalFailure: nil
        )
        let independent = activeEntry(
            "new-sleep",
            lane: "sleep",
            date: now.addingTimeInterval(1)
        )

        XCTAssertEqual(
            HealthKitSyncQueuePolicy.select(
                from: [legacy, independent],
                forceRetry: false,
                includeTerminalFailures: false,
                now: now
            ),
            .deferred
        )
        XCTAssertEqual(
            HealthKitSyncQueuePolicy.blockedLaneKeys(in: [legacy]),
            [HealthKitSyncQueuePolicy.legacyGlobalLane]
        )
    }

    func testEntryTooLargeDoesNotWriteAFile() async throws {
        let fixture = makeFixture(maximumBytes: 256)
        defer { removeFixture(fixture) }
        let body = Data(repeating: 0x42, count: 257)

        await assertOutboxError(.entryTooLarge(maxBytes: 256)) {
            _ = try await fixture.outbox.enqueue(
                body: body,
                anchors: [:],
                destinationFingerprint: "destination-a"
            )
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.fileURL.path))
    }

    private struct Fixture {
        let outbox: HealthKitSyncOutbox
        let fileURL: URL
        let keyProvider: RecordingHealthKitKeyProvider
    }

    private func terminalEntry(
        _ body: String,
        lane: String,
        committed: Bool,
        date: Date
    ) -> HealthKitSyncOutboxEntry {
        let data = Data(body.utf8)
        return HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(for: data),
            body: data,
            anchors: [lane: Data([1])],
            destinationFingerprint: "destination-a",
            enqueuedAt: date,
            failedAttempts: 0,
            nextAttemptAt: date,
            terminalFailure: "terminal",
            terminalAnchorsCommitted: committed
        )
    }

    private func activeEntry(
        _ body: String,
        lane: String,
        date: Date
    ) -> HealthKitSyncOutboxEntry {
        let data = Data(body.utf8)
        return HealthKitSyncOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(for: data),
            body: data,
            anchors: [lane: Data([2])],
            destinationFingerprint: "destination-a",
            enqueuedAt: date,
            failedAttempts: 0,
            nextAttemptAt: date,
            terminalFailure: nil
        )
    }

    private func makeFixture(
        maximumEntries: Int =
            HealthKitSyncOutbox.defaultMaximumEntries,
        maximumBytes: Int = HealthKitSyncOutbox.defaultMaximumBytes,
        retryPolicy: HealthKitSyncRetryPolicy = .default
    ) -> Fixture {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "healthmes-healthkit-outbox-\(UUID().uuidString)",
                isDirectory: true
            )
        let fileURL = directory.appendingPathComponent("outbox-v1.enc")
        let keyProvider = RecordingHealthKitKeyProvider()
        return Fixture(
            outbox: HealthKitSyncOutbox(
                fileURL: fileURL,
                keyProvider: keyProvider,
                maximumEntries: maximumEntries,
                maximumBytes: maximumBytes,
                retryPolicy: retryPolicy
            ),
            fileURL: fileURL,
            keyProvider: keyProvider
        )
    }

    private func writeLegacyTerminalFixture(
        _ fixture: Fixture,
        body: Data,
        anchors: [String: Data],
        destinationFingerprint: String,
        enqueuedAt: Date,
        nextAttemptAt: Date
    ) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .millisecondsSince1970
        let entry = LegacyHealthKitOutboxEntry(
            idempotencyKey:
                HealthKitSyncOutboxIdentity.idempotencyKey(for: body),
            body: body,
            anchors: anchors,
            destinationFingerprint: destinationFingerprint,
            enqueuedAt: enqueuedAt,
            failedAttempts: 8,
            nextAttemptAt: nextAttemptAt,
            terminalFailure: "forwarding failed",
            terminalAnchorsCommitted: false,
            forwardingFailedAttempts: 8
        )
        let plaintext = try encoder.encode(
            LegacyHealthKitOutboxEnvelope(
                version: 1,
                entries: [entry]
            )
        )
        let sealedBox = try AES.GCM.seal(
            plaintext,
            using: fixture.keyProvider.fixtureKey(),
            authenticating:
                Data("HealthMes.HealthKitSyncOutbox.v1".utf8)
        )
        let sealedData = try XCTUnwrap(sealedBox.combined)
        let storedData = try encoder.encode(
            LegacyHealthKitOutboxStoredFile(
                version: 1,
                sealedData: sealedData
            )
        )
        try FileManager.default.createDirectory(
            at: fixture.fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try storedData.write(to: fixture.fileURL, options: .atomic)
        XCTAssertNil(storedData.range(of: body))
    }

    private func removeFixture(_ fixture: Fixture) {
        try? FileManager.default.removeItem(
            at: fixture.fileURL.deletingLastPathComponent()
        )
    }

    private func assertOutboxError(
        _ expected: HealthKitSyncOutboxError,
        file: StaticString = #filePath,
        line: UInt = #line,
        operation: () async throws -> Void
    ) async {
        do {
            try await operation()
            XCTFail(
                "Expected HealthKitSyncOutboxError \(expected)",
                file: file,
                line: line
            )
        } catch let error as HealthKitSyncOutboxError {
            XCTAssertEqual(error, expected, file: file, line: line)
        } catch {
            XCTFail(
                "Unexpected error \(error)",
                file: file,
                line: line
            )
        }
    }
}

private final class HealthKitTestLockedValues<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [Value] = []

    var values: [Value] {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func append(_ value: Value) {
        lock.lock()
        defer { lock.unlock() }
        storage.append(value)
    }
}
