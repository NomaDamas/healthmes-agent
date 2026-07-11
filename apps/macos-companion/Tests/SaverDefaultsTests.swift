import ScreenSaver
import XCTest

/// The privacy toggle must persist through the REAL mechanism the saver
/// uses at runtime (ScreenSaverDefaults → ByHost preferences), not a mock.
final class SaverDefaultsTests: XCTestCase {
    private let testModule = "com.healthmes.saver.tests"

    override func tearDown() {
        ScreenSaverDefaults(forModuleWithName: testModule)?
            .removePersistentDomain(forName: testModule)
        super.tearDown()
    }

    func testHideNumbersDefaultsToOff() throws {
        let store = try XCTUnwrap(SaverDefaultsStore(moduleName: testModule))
        XCTAssertFalse(store.hideNumbers)
    }

    func testHideNumbersPersistsAcrossInstances() throws {
        let store = try XCTUnwrap(SaverDefaultsStore(moduleName: testModule))
        store.hideNumbers = true

        // A fresh instance (fresh ScreenSaverDefaults) must read it back —
        // the same round-trip the configure sheet and the saver view do.
        let reread = try XCTUnwrap(SaverDefaultsStore(moduleName: testModule))
        XCTAssertTrue(reread.hideNumbers)

        reread.hideNumbers = false
        let again = try XCTUnwrap(SaverDefaultsStore(moduleName: testModule))
        XCTAssertFalse(again.hideNumbers)
    }
}
