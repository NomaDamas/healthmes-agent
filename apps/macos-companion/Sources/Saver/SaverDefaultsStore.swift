import Foundation
import ScreenSaver

/// The issue-#11 privacy toggle, persisted the way screensavers must:
/// `ScreenSaverDefaults` (per-module ByHost preferences that the
/// legacyScreenSaver host reads/writes without extra entitlements).
public struct SaverDefaultsStore {
    public static let moduleName = "com.healthmes.saver"
    public static let hideNumbersKey = "hideHealthNumbers"

    private let defaults: ScreenSaverDefaults

    /// `moduleName` is injectable so unit tests use a scratch domain.
    public init?(moduleName: String = SaverDefaultsStore.moduleName) {
        guard let defaults = ScreenSaverDefaults(forModuleWithName: moduleName) else {
            return nil
        }
        defaults.register(defaults: [Self.hideNumbersKey: false])
        self.defaults = defaults
    }

    public var hideNumbers: Bool {
        get { defaults.bool(forKey: Self.hideNumbersKey) }
        nonmutating set {
            defaults.set(newValue, forKey: Self.hideNumbersKey)
            defaults.synchronize()
        }
    }
}

/// Saver strings must resolve against the .saver bundle, not the host
/// process (legacyScreenSaver is the main bundle at runtime).
func saverLocalized(_ key: String) -> String {
    Bundle(for: HealthMesSaverView.self).localizedString(forKey: key, value: key, table: nil)
}
