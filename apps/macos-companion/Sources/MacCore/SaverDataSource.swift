import Foundation

/// Where the screensaver gets its briefing: the shared on-disk glance
/// snapshot (`GlanceSnapshotCache`) that the menu bar app and the widget
/// keep <= 5 minutes fresh (the endpoint's own freshness floor).
///
/// The saver process deliberately does NO networking and NEVER touches the
/// Keychain: third-party savers run inside Apple's sandboxed
/// legacyScreenSaver host, where reading another app's login-keychain item
/// can pop a password prompt *behind* the full-screen saver. Reading the
/// cache file + the pairing base-URL default is prompt-free and keeps the
/// local-first guarantee (the cached bytes came from the paired instance,
/// nothing else). README documents the consequence: the saver needs the
/// menu bar app (or widget) alive to stay fresh.
public struct SaverDataSource {
    /// DUPLICATED CONTRACT STRING — mirrors the private
    /// `PairingStore.baseURLDefaultsKey` in
    /// apps/ios-companion/Sources/Shared/Pairing.swift (the saver must
    /// detect "paired" without the Keychain half of the pairing).
    /// Integrator note: make that constant public in Shared and delete this
    /// copy.
    public static let pairedBaseURLDefaultsKey = "healthmes.pairing.baseURL"

    private let cache: GlanceSnapshotCache
    private let defaults: UserDefaults

    public init(
        cache: GlanceSnapshotCache = .shared,
        defaults: UserDefaults = AppGroup.userDefaults
    ) {
        self.cache = cache
        self.defaults = defaults
    }

    public func briefing(hideNumbers: Bool, now: Date = Date()) -> SaverBriefing {
        let isPaired = defaults.string(forKey: Self.pairedBaseURLDefaultsKey) != nil
        let cached = cache.load()
        let payload = cached.flatMap { try? GlanceJSON.decodePayload($0.payloadData) }
        return SaverBriefing.make(
            payload: payload,
            fetchedAt: cached?.fetchedAt,
            isPaired: isPaired,
            hideNumbers: hideNumbers,
            now: now
        )
    }
}
