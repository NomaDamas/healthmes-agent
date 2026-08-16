import CryptoKit
import Foundation

enum ScreenTimeActivityOutboxError: Error, Equatable {
    case itemTooLarge(maxBytes: Int)
    case persistenceFailed
}

struct ScreenTimeActivityOutboxEntry: Codable, Equatable {
    var id: String
    let destinationID: String
    let deviceID: String
    let enqueuedAt: Date
    var report: ScreenTimeActivityReport
    var failedAttempts: Int
    var nextAttemptAt: Date
    var terminalReason: String? = nil
    var terminalStatusCode: Int? = nil
    var terminalAt: Date? = nil

    var isTerminal: Bool {
        terminalReason != nil
    }
}

struct ScreenTimeActivityRetryPolicy: Equatable {
    let initialDelay: TimeInterval
    let maximumDelay: TimeInterval

    static let `default` = ScreenTimeActivityRetryPolicy(
        initialDelay: 60,
        maximumDelay: 6 * 60 * 60
    )

    func nextAttemptDate(
        afterFailedAttempts failedAttempts: Int,
        now: Date
    ) -> Date {
        let exponent = max(0, min(failedAttempts - 1, 16))
        let multiplier = pow(2.0, Double(exponent))
        return now.addingTimeInterval(
            min(maximumDelay, initialDelay * multiplier)
        )
    }
}

enum ScreenTimeActivityReportIdentity {
    static func reportID(
        _ report: ScreenTimeActivityReport
    ) throws -> String {
        let data = try ScreenTimeActivityHTTP.encoder().encode(report)
        return "hm-ios-st-v1-" + sha256Hex(data)
    }

    static func destinationID(for pairing: Pairing) -> String {
        var material = Data(
            "healthmes-screen-time-destination-v1\u{0}".utf8
        )
        material.append(Data(pairing.baseURL.absoluteString.utf8))
        material.append(0)
        material.append(Data((pairing.token ?? "").utf8))
        return "hm-node-v1-" + sha256Hex(material)
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }
}

actor ScreenTimeActivityOutbox {
    private struct Envelope: Codable {
        let version: Int
        let entries: [ScreenTimeActivityOutboxEntry]
    }

    static let defaultRetentionInterval: TimeInterval =
        14 * 24 * 60 * 60

    static let shared = ScreenTimeActivityOutbox(
        fileURL: defaultFileURL()
    )

    private let fileURL: URL
    private let maximumEntries: Int
    private let maximumBytes: Int
    private let retryPolicy: ScreenTimeActivityRetryPolicy
    private let retentionInterval: TimeInterval
    private let fileManager: FileManager
    private var entries: [ScreenTimeActivityOutboxEntry]

    init(
        fileURL: URL,
        maximumEntries: Int = 8,
        maximumBytes: Int = 16 * 1_024 * 1_024,
        retryPolicy: ScreenTimeActivityRetryPolicy = .default,
        retentionInterval: TimeInterval =
            ScreenTimeActivityOutbox.defaultRetentionInterval,
        fileManager: FileManager = .default,
        now: Date = Date()
    ) {
        self.fileURL = fileURL
        self.maximumEntries = max(1, maximumEntries)
        self.maximumBytes = max(1, maximumBytes)
        self.retryPolicy = retryPolicy
        self.retentionInterval = max(0, retentionInterval)
        self.fileManager = fileManager
        entries = Self.loadEntries(
            from: fileURL,
            fileManager: fileManager,
            maximumEntries: self.maximumEntries,
            maximumBytes: self.maximumBytes,
            retentionInterval: self.retentionInterval,
            now: now
        )
    }

    static func defaultFileURL(
        fileManager: FileManager = .default
    ) -> URL {
        let root = AppGroup.containerURL
            ?? fileManager.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first
            ?? fileManager.temporaryDirectory
        return root
            .appendingPathComponent(
                "HealthMes/ScreenTime",
                isDirectory: true
            )
            .appendingPathComponent("outbox-v1.json")
    }

    func reconcile(
        deviceID: String,
        pairing: Pairing?,
        state: ScreenTimeCollectionState? = nil,
        authorization: ScreenTimeCollectorResult? = nil,
        now: Date = Date()
    ) throws {
        try purgeExpired(now: now)
        guard let pairing else {
            guard !entries.isEmpty else { return }
            try persist([])
            entries = []
            return
        }
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        var candidate = entries.filter { entry in
            guard
                entry.deviceID == deviceID,
                entry.destinationID == destinationID
            else {
                return false
            }
            if let state {
                guard state.enabled else { return false }
                guard Self.isCompatible(entry.report, with: state) else {
                    return false
                }
            }
            if let authorization {
                guard Self.isCompatible(
                    entry.report,
                    with: authorization
                ) else {
                    return false
                }
            }
            return true
        }
        candidate.sort(by: Self.entryOrder)
        guard candidate != entries else { return }
        try persist(candidate)
        entries = candidate
    }

    @discardableResult
    func enqueue(
        report: ScreenTimeActivityReport,
        pairing: Pairing,
        now: Date
    ) throws -> ScreenTimeActivityOutboxEntry {
        try purgeExpired(now: now)
        let reportID = try ScreenTimeActivityReportIdentity.reportID(report)
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        if let existing = entries.first(where: {
            $0.id == reportID
                && $0.destinationID == destinationID
                && $0.deviceID == report.deviceID
        }) {
            return existing
        }

        let newEntry = ScreenTimeActivityOutboxEntry(
            id: reportID,
            destinationID: destinationID,
            deviceID: report.deviceID,
            enqueuedAt: now,
            report: report,
            failedAttempts: 0,
            nextAttemptAt: now
        )
        let singleEntryBytes = try encodedData([newEntry]).count
        guard singleEntryBytes <= maximumBytes else {
            throw ScreenTimeActivityOutboxError.itemTooLarge(
                maxBytes: maximumBytes
            )
        }

        var candidate = entries.filter {
            !Self.isSuperseded($0, by: newEntry)
        }
        candidate.append(newEntry)
        candidate.sort(by: Self.entryOrder)
        candidate = try bounded(candidate, preserving: newEntry.id)
        try persist(candidate)
        entries = candidate
        return newEntry
    }

    func oldest(
        deviceID: String,
        pairing: Pairing
    ) -> ScreenTimeActivityOutboxEntry? {
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        return entries
            .filter {
                $0.deviceID == deviceID
                    && $0.destinationID == destinationID
                    && !$0.isTerminal
            }
            .min(by: Self.entryOrder)
    }

    func markSucceeded(id: String) throws {
        let candidate = entries.filter { $0.id != id }
        guard candidate != entries else { return }
        try persist(candidate)
        entries = candidate
    }

    @discardableResult
    func markFailed(
        id: String,
        now: Date
    ) throws -> ScreenTimeActivityOutboxEntry? {
        try purgeExpired(now: now)
        guard let index = entries.firstIndex(where: { $0.id == id }) else {
            return nil
        }
        var candidate = entries
        candidate[index].failedAttempts += 1
        candidate[index].nextAttemptAt =
            retryPolicy.nextAttemptDate(
                afterFailedAttempts: candidate[index].failedAttempts,
                now: now
            )
        try persist(candidate)
        entries = candidate
        return candidate[index]
    }

    @discardableResult
    func markTerminal(
        id: String,
        report: ScreenTimeActivityReport? = nil,
        reason: String,
        statusCode: Int?,
        now: Date
    ) throws -> ScreenTimeActivityOutboxEntry? {
        try purgeExpired(now: now)
        guard let index = entries.firstIndex(where: { $0.id == id }) else {
            return nil
        }
        var candidate = entries
        if let report {
            candidate[index].report = report
            candidate[index].id =
                try ScreenTimeActivityReportIdentity.reportID(report)
        }
        candidate[index].terminalReason = reason
        candidate[index].terminalStatusCode = statusCode
        candidate[index].terminalAt = now
        try persist(candidate)
        entries = candidate
        return candidate[index]
    }

    @discardableResult
    func replaceAndMarkFailed(
        id: String,
        with report: ScreenTimeActivityReport,
        pairing: Pairing,
        now: Date
    ) throws -> ScreenTimeActivityOutboxEntry {
        try purgeExpired(now: now)
        let prior = entries.first(where: { $0.id == id })
        var candidate = entries.filter { $0.id != id }
        let replacementID =
            try ScreenTimeActivityReportIdentity.reportID(report)
        let replacement = ScreenTimeActivityOutboxEntry(
            id: replacementID,
            destinationID:
                ScreenTimeActivityReportIdentity.destinationID(
                    for: pairing
                ),
            deviceID: report.deviceID,
            enqueuedAt: prior?.enqueuedAt ?? now,
            report: report,
            failedAttempts: (prior?.failedAttempts ?? 0) + 1,
            nextAttemptAt: retryPolicy.nextAttemptDate(
                afterFailedAttempts:
                    (prior?.failedAttempts ?? 0) + 1,
                now: now
            )
        )
        guard try encodedData([replacement]).count <= maximumBytes else {
            throw ScreenTimeActivityOutboxError.itemTooLarge(
                maxBytes: maximumBytes
            )
        }
        candidate.removeAll {
            Self.isSuperseded($0, by: replacement)
        }
        candidate.append(replacement)
        candidate.sort(by: Self.entryOrder)
        candidate = try bounded(
            candidate,
            preserving: replacement.id
        )
        try persist(candidate)
        entries = candidate
        return replacement
    }

    func pendingCount(
        deviceID: String,
        pairing: Pairing
    ) -> Int {
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        return entries.filter {
            $0.deviceID == deviceID
                && $0.destinationID == destinationID
                && !$0.isTerminal
        }.count
    }

    func quarantinedCount(
        deviceID: String,
        pairing: Pairing
    ) -> Int {
        let destinationID =
            ScreenTimeActivityReportIdentity.destinationID(for: pairing)
        return entries.filter {
            $0.deviceID == deviceID
                && $0.destinationID == destinationID
                && $0.isTerminal
        }.count
    }

    func quarantinedEntries() -> [ScreenTimeActivityOutboxEntry] {
        entries
            .filter(\.isTerminal)
            .sorted(by: Self.entryOrder)
    }

    func allEntries() -> [ScreenTimeActivityOutboxEntry] {
        entries.sorted(by: Self.entryOrder)
    }

    @discardableResult
    func purgeAll() throws -> Int {
        let removed = entries.count
        guard removed > 0 else { return 0 }
        try persist([])
        entries = []
        return removed
    }

    @discardableResult
    func purgeExpired(now: Date) throws -> Int {
        let candidate = Self.retainedEntries(
            entries,
            retentionInterval: retentionInterval,
            now: now
        )
        let removed = entries.count - candidate.count
        guard removed > 0 else { return 0 }
        try persist(candidate)
        entries = candidate
        return removed
    }

    private func bounded(
        _ source: [ScreenTimeActivityOutboxEntry],
        preserving preservedID: String
    ) throws -> [ScreenTimeActivityOutboxEntry] {
        var candidate = source
        while true {
            let exceedsCount = candidate.count > maximumEntries
            let exceedsBytes =
                try encodedData(candidate).count > maximumBytes
            guard exceedsCount || exceedsBytes else { break }
            guard
                let removalIndex =
                    candidate.firstIndex(where: {
                        $0.id != preservedID && $0.isTerminal
                    })
                    ?? candidate.firstIndex(where: {
                        $0.id != preservedID
                    })
            else {
                throw ScreenTimeActivityOutboxError.itemTooLarge(
                    maxBytes: maximumBytes
                )
            }
            candidate.remove(at: removalIndex)
        }
        return candidate
    }

    private func persist(
        _ candidate: [ScreenTimeActivityOutboxEntry]
    ) throws {
        do {
            try Self.persist(
                candidate,
                to: fileURL,
                fileManager: fileManager
            )
        } catch let error as ScreenTimeActivityOutboxError {
            throw error
        } catch {
            throw ScreenTimeActivityOutboxError.persistenceFailed
        }
    }

    private func encodedData(
        _ candidate: [ScreenTimeActivityOutboxEntry]
    ) throws -> Data {
        try Self.encodedData(candidate)
    }

    private static func encodedData(
        _ candidate: [ScreenTimeActivityOutboxEntry]
    ) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .millisecondsSince1970
        return try encoder.encode(
            Envelope(version: 1, entries: candidate)
        )
    }

    private static func loadEntries(
        from fileURL: URL,
        fileManager: FileManager,
        maximumEntries: Int,
        maximumBytes: Int,
        retentionInterval: TimeInterval,
        now: Date
    ) -> [ScreenTimeActivityOutboxEntry] {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return []
        }
        if let attributes = try? fileManager.attributesOfItem(
            atPath: fileURL.path
        ), let size = attributes[.size] as? NSNumber,
            size.uint64Value > UInt64(maximumBytes)
        {
            try? fileManager.removeItem(at: fileURL)
            return []
        }
        guard let data = try? Data(contentsOf: fileURL) else {
            return []
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        guard
            let envelope = try? decoder.decode(
                Envelope.self,
                from: data
            ),
            envelope.version == 1,
            envelope.entries.count <= maximumEntries,
            data.count <= maximumBytes
        else {
            try? fileManager.removeItem(at: fileURL)
            return []
        }
        let sorted = envelope.entries.sorted(by: entryOrder)
        let retained = retainedEntries(
            sorted,
            retentionInterval: retentionInterval,
            now: now
        )
        do {
            if retained != sorted {
                try persist(
                    retained,
                    to: fileURL,
                    fileManager: fileManager
                )
            } else {
                try excludeFromBackup(
                    fileURL.deletingLastPathComponent()
                )
                try excludeFromBackup(fileURL)
            }
        } catch {
            try? fileManager.removeItem(at: fileURL)
            return []
        }
        return retained
    }

    private static func retainedEntries(
        _ source: [ScreenTimeActivityOutboxEntry],
        retentionInterval: TimeInterval,
        now: Date
    ) -> [ScreenTimeActivityOutboxEntry] {
        let cutoff = now.addingTimeInterval(-retentionInterval)
        return source.filter { $0.enqueuedAt > cutoff }
    }

    private static func persist(
        _ candidate: [ScreenTimeActivityOutboxEntry],
        to fileURL: URL,
        fileManager: FileManager
    ) throws {
        let directoryURL = fileURL.deletingLastPathComponent()
        try fileManager.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true
        )
        try excludeFromBackup(directoryURL)
        try encodedData(candidate).write(
            to: fileURL,
            options: .atomic
        )
        try excludeFromBackup(fileURL)
    }

    private static func excludeFromBackup(_ sourceURL: URL) throws {
        var url = sourceURL
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try url.setResourceValues(resourceValues)
    }

    private static func isCompatible(
        _ report: ScreenTimeActivityReport,
        with state: ScreenTimeCollectionState
    ) -> Bool {
        guard report.collectionRevision == state.configRevision else {
            return false
        }
        guard let cutoff = state.rawRetentionCutoff else {
            return true
        }
        if let snapshotStart = report.snapshotStart,
            snapshotStart <= cutoff
        {
            return false
        }
        if report.samples.contains(where: { $0.bucketStart <= cutoff }) {
            return false
        }
        return !report.authoritativeBucketStarts.contains(where: {
            $0 <= cutoff
        })
    }

    private static func isCompatible(
        _ report: ScreenTimeActivityReport,
        with authorization: ScreenTimeCollectorResult
    ) -> Bool {
        if authorization.permitsAggregateUpload {
            return report.capability == .aggregate
                && report.permissionStatus == .granted
        }
        return report.capability == .unavailable
            && report.permissionStatus
                == authorization.permissionStatus
    }

    private static func isSuperseded(
        _ existing: ScreenTimeActivityOutboxEntry,
        by replacement: ScreenTimeActivityOutboxEntry
    ) -> Bool {
        guard
            existing.destinationID == replacement.destinationID,
            existing.deviceID == replacement.deviceID,
            existing.report.capability == .aggregate,
            replacement.report.capability == .aggregate,
            existing.report.collectionRevision
                == replacement.report.collectionRevision,
            existing.report.collectionGeneration
                == replacement.report.collectionGeneration,
            let existingSequence = existing.report.snapshotSequence,
            let replacementSequence = replacement.report.snapshotSequence
        else {
            return false
        }
        return existingSequence <= replacementSequence
    }

    private static func entryOrder(
        _ lhs: ScreenTimeActivityOutboxEntry,
        _ rhs: ScreenTimeActivityOutboxEntry
    ) -> Bool {
        if lhs.enqueuedAt != rhs.enqueuedAt {
            return lhs.enqueuedAt < rhs.enqueuedAt
        }
        return lhs.id < rhs.id
    }
}
