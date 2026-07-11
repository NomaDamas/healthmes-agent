import Foundation

/// Pure selection logic for the "current focus block" surface (issue #10
/// Live Activity): which of the glance `next_blocks` is running right now,
/// and which one comes next. Kept UI-free so it is unit-testable and
/// reusable by the macOS glance surface.
public enum FocusBlockSelector {
    /// The block whose `[start, end)` window contains `now`, if any.
    /// `next_blocks` may include an ongoing block (the endpoint returns
    /// upcoming/ongoing blocks, soonest first).
    public static func current(in blocks: [GlanceBlock], now: Date) -> GlanceBlock? {
        blocks.first { $0.start <= now && now < $0.end }
    }

    /// The soonest block that has not started yet.
    public static func upcoming(in blocks: [GlanceBlock], now: Date) -> GlanceBlock? {
        blocks
            .filter { $0.start > now }
            .min(by: { $0.start < $1.start })
    }

    /// Fraction of the current block already elapsed, 0...1.
    public static func progress(of block: GlanceBlock, now: Date) -> Double {
        let total = block.end.timeIntervalSince(block.start)
        guard total > 0 else { return 1 }
        let elapsed = now.timeIntervalSince(block.start)
        return min(max(elapsed / total, 0), 1)
    }
}
