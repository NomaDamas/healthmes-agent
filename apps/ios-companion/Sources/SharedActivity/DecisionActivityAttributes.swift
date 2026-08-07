#if canImport(ActivityKit)
    import ActivityKit
    import Foundation

    public enum DecisionActivityStatus: String, Codable, Hashable {
        case pending
        case accepted
        case declined
        case failed
    }

    /// A pending HealthMes decision rendered as an interactive lock-screen
    /// remote. The proposal id is stable while the display state can update.
    public struct DecisionActivityAttributes: ActivityAttributes {
        #if DEBUG
            public static let demoProposalID =
                "00000000-0000-0000-0000-000000000091"
        #endif

        public struct ContentState: Codable, Hashable {
            public var title: String
            public var reason: String
            public var target: String
            public var expiresAt: Date
            public var status: DecisionActivityStatus

            public init(
                title: String,
                reason: String,
                target: String,
                expiresAt: Date,
                status: DecisionActivityStatus = .pending
            ) {
                self.title = title
                self.reason = reason
                self.target = target
                self.expiresAt = expiresAt
                self.status = status
            }
        }

        public let proposalID: String

        public init(proposalID: String) {
            self.proposalID = proposalID
        }
    }
#endif
