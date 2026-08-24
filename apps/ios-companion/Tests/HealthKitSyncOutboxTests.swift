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
        isAvailable ? key : nil
    }

    func deleteKey() throws {
        deleteCalls += 1
        if let deleteError {
            throw deleteError
        }
        isAvailable = false
    }
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

    private func makeFixture(
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
                maximumBytes: maximumBytes,
                retryPolicy: retryPolicy
            ),
            fileURL: fileURL,
            keyProvider: keyProvider
        )
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
