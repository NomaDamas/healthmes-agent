import Foundation
import XCTest

final class WorkspaceContractTests: XCTestCase {
    func testDefaultsContainProtectedSystemChannels() {
        let state = WorkspaceState.defaults()
        XCTAssertEqual(state.categories.count, 1)
        XCTAssertTrue(state.categories[0].isSystem)
        XCTAssertEqual(
            state.categories[0].channels.compactMap(\.systemKind),
            WorkspaceSystemChannel.allCases
        )
        XCTAssertEqual(
            state.selectedChannelID,
            WorkspaceState.systemChannelID(.overview)
        )
    }

    func testNormalizationRepairsSystemChannelsAndKeepsUserCategories() {
        let custom = WorkspaceCategory(
            title: "  Work  ",
            channels: [
                WorkspaceChannel(
                    title: "  Deep work  ",
                    canvas: .dashboard
                )
            ]
        )
        let state = WorkspaceState(
            categories: [custom],
            selectedChannelID: custom.channels[0].id
        ).normalized()

        XCTAssertTrue(state.categories[0].isSystem)
        XCTAssertEqual(state.categories[1].title, "Work")
        XCTAssertEqual(state.categories[1].channels[0].title, "Deep work")
        XCTAssertEqual(
            state.categories[0].channels.compactMap(\.systemKind),
            WorkspaceSystemChannel.allCases
        )
    }

    func testNormalizationDropsThreadsForMissingChannels() {
        let state = WorkspaceState(
            categories: [],
            threads: [
                WorkspaceThread(
                    channelID: UUID(),
                    anchor: WorkspaceThreadAnchor(
                        kind: .post,
                        localID: "missing",
                        title: "Missing"
                    )
                )
            ]
        ).normalized()

        XCTAssertTrue(state.threads.isEmpty)
    }

    func testNormalizationPreservesSystemSidebarPreferences() {
        var state = WorkspaceState.defaults()
        state.categories[0].isCollapsed = true
        state.categories[0].channels[0].isHidden = true
        state.categories[0].channels[1].isFavorite = true

        let normalized = state.normalized()

        XCTAssertTrue(normalized.categories[0].isCollapsed)
        XCTAssertTrue(normalized.categories[0].channels[0].isHidden)
        XCTAssertTrue(normalized.categories[0].channels[1].isFavorite)
        XCTAssertEqual(
            normalized.selectedChannelID,
            WorkspaceState.systemChannelID(.calendar)
        )
    }

    func testNormalizationRepairsUserChannelUsingReservedSystemID() {
        let reservedID = WorkspaceState.systemChannelID(.calendar)
        let custom = WorkspaceCategory(
            title: "Work",
            channels: [
                WorkspaceChannel(
                    id: reservedID,
                    title: "My calendar notes",
                    canvas: .conversation
                )
            ]
        )
        let thread = WorkspaceThread(
            channelID: reservedID,
            anchor: WorkspaceThreadAnchor(
                kind: .post,
                localID: "reserved-id-collision",
                title: "Collision"
            )
        )

        let normalized = WorkspaceState(
            categories: [custom],
            threads: [thread],
            selectedChannelID: reservedID
        ).normalized()
        let repaired = normalized.categories[1].channels[0]

        XCTAssertNotEqual(repaired.id, reservedID)
        XCTAssertEqual(normalized.threads[0].channelID, repaired.id)
        XCTAssertEqual(normalized.selectedChannelID, repaired.id)
        XCTAssertEqual(
            normalized.categories[0].channels[1].id,
            reservedID
        )
    }

    func testLocalStoreRoundTripsDeviceLocalConversation() {
        let suite = "WorkspaceContractTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = WorkspaceLocalStore(
            defaults: defaults,
            namespace: { "pairing-a" }
        )
        var state = WorkspaceState.defaults()
        let channelID = WorkspaceState.systemChannelID(.agent)
        state.threads = [
            WorkspaceThread(
                channelID: channelID,
                anchor: WorkspaceThreadAnchor(
                    kind: .post,
                    localID: "local-command",
                    title: "오늘 일정 조정"
                ),
                messages: [
                    WorkspaceThreadMessage(
                        author: .user,
                        body: "오늘 일정을 가볍게 조정해줘"
                    )
                ]
            )
        ]

        _ = store.save(state)
        let loaded = store.load()
        XCTAssertEqual(loaded.threads.count, 1)
        XCTAssertEqual(loaded.threads[0].messages[0].body, "오늘 일정을 가볍게 조정해줘")
        XCTAssertTrue(loaded.threads[0].messages[0].isLocalOnly)
    }

    func testLocalStoreSeparatesPairingNamespaces() {
        let suite = "WorkspaceContractTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var activeNamespace = "pairing-a"
        let store = WorkspaceLocalStore(
            defaults: defaults,
            namespace: { activeNamespace }
        )
        var first = WorkspaceState.defaults()
        first.categories.append(WorkspaceCategory(title: "Private workspace"))
        _ = store.save(first)

        activeNamespace = "pairing-b"
        XCTAssertEqual(store.load().categories.count, 1)

        activeNamespace = "pairing-a"
        XCTAssertEqual(store.load().categories.last?.title, "Private workspace")
    }

    func testAlternativeCommandKeepsProposalContext() {
        let proposalID = UUID()
        let command = AlternativeCommand.compose(
            userText: "내일 오전으로 옮겨줘",
            proposalID: proposalID,
            title: "집중 블록 이동",
            proposedAction: "오늘 16:00에서 내일 09:00로 이동"
        )

        XCTAssertTrue(command.contains(proposalID.uuidString.lowercased()))
        XCTAssertTrue(command.contains("집중 블록 이동"))
        XCTAssertTrue(command.contains("내일 오전으로 옮겨줘"))
    }

    func testFutureSchemaFailsClosedToDefaults() {
        let future = WorkspaceState(
            schemaVersion: WorkspaceState.currentSchemaVersion + 1,
            categories: []
        ).normalized()
        XCTAssertEqual(
            future.selectedChannelID,
            WorkspaceState.systemChannelID(.overview)
        )
        XCTAssertEqual(future.categories[0].channels.count, 5)
    }

    func testFutureSchemaBytesArePreservedUntilExplicitReset() throws {
        let suite = "WorkspaceContractTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = WorkspaceLocalStore(defaults: defaults, namespace: { "future" })
        let key = "\(WorkspaceLocalStore.defaultsKey).future"
        let raw = Data(
            """
            {"schemaVersion":99,"categories":[],"threads":[],"futureField":"keep-me"}
            """.utf8
        )
        defaults.set(raw, forKey: key)

        let loaded = store.loadSnapshot()
        XCTAssertEqual(loaded.mode, .incompatibleFutureSchema(version: 99))
        XCTAssertFalse(loaded.mode.isWritable)

        var attempted = loaded.state
        attempted.categories.append(WorkspaceCategory(title: "Must not save"))
        let result = store.save(attempted)

        XCTAssertEqual(result.mode, .incompatibleFutureSchema(version: 99))
        XCTAssertEqual(defaults.data(forKey: key), raw)
    }

    func testCorruptedWorkspaceBytesArePreservedUntilExplicitReset() {
        let suite = "WorkspaceContractTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = WorkspaceLocalStore(defaults: defaults, namespace: { "corrupt" })
        let key = "\(WorkspaceLocalStore.defaultsKey).corrupt"
        let raw = Data("not-json".utf8)
        defaults.set(raw, forKey: key)

        let result = store.save(WorkspaceState.defaults())

        XCTAssertEqual(result.mode, .corrupted)
        XCTAssertEqual(defaults.data(forKey: key), raw)
    }

    func testNormalizationDoesNotSilentlyTrimThreadMessages() {
        let channelID = WorkspaceState.systemChannelID(.agent)
        let messages = (0...WorkspaceState.maximumMessagesPerThread).map { index in
            WorkspaceThreadMessage(author: .user, body: "message-\(index)")
        }
        let state = WorkspaceState(
            categories: WorkspaceState.defaults().categories,
            threads: [
                WorkspaceThread(
                    channelID: channelID,
                    anchor: WorkspaceThreadAnchor(
                        kind: .post,
                        localID: "large-thread",
                        title: "Large thread"
                    ),
                    messages: messages
                )
            ]
        ).normalized()

        XCTAssertEqual(state.threads[0].messages.count, messages.count)
    }
}
