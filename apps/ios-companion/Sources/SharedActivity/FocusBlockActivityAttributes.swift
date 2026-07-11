#if canImport(ActivityKit)
    import ActivityKit
    import Foundation

    /// Live Activity attributes for the current focus block (issue #10).
    /// Compiled into the iOS app (which starts/updates the activity) and the
    /// widget extension (which renders it) — NOT into Shared, because
    /// ActivityKit does not exist on watchOS/macOS.
    ///
    /// Update path is polling only (no push token, local-first): the app
    /// refreshes the activity from the glance payload on foreground and from
    /// BGAppRefreshTask runs. `staleDate` is set to the block's end so iOS
    /// dims the surface by itself when the app gets no background budget.
    public struct FocusBlockActivityAttributes: ActivityAttributes {
        public struct ContentState: Codable, Hashable {
            /// Block title (placeholder "Focus block" when the calendar
            /// event is untitled).
            public var title: String
            public var start: Date
            public var end: Date
            /// Raw GlanceEnergyDemand value ("low"/"med"/"high"), if any.
            public var energyDemand: String?

            public init(title: String, start: Date, end: Date, energyDemand: String?) {
                self.title = title
                self.start = start
                self.end = end
                self.energyDemand = energyDemand
            }
        }

        public init() {}
    }
#endif
