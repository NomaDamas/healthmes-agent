import EventKit
import XCTest

@MainActor
final class DeviceCalendarPermissionTests: XCTestCase {
    func testDemoAdapterStartsReadyWithoutRequestingSystemPermission() {
        let model = DeviceCalendarPermissionModel(
            authorizer: DemoCalendarAuthorizer(status: .fullAccess)
        )

        XCTAssertEqual(model.status, .fullAccess)
        XCTAssertEqual(model.label, "Device access granted")
    }

    func testDeniedAdapterRemainsHonest() async {
        let model = DeviceCalendarPermissionModel(
            authorizer: DemoCalendarAuthorizer(status: .denied)
        )

        await model.request()

        XCTAssertEqual(model.status, .denied)
        XCTAssertEqual(model.label, "Permission denied")
        XCTAssertNil(model.message)
    }

    func testRefreshReadsPermissionChangedOutsideTheApp() {
        final class MutableAuthorizer: DeviceCalendarAuthorizing {
            var authorizationStatus: EKAuthorizationStatus = .denied
            func requestFullAccess() async throws {}
        }

        let authorizer = MutableAuthorizer()
        let model = DeviceCalendarPermissionModel(authorizer: authorizer)
        XCTAssertEqual(model.status, .denied)

        authorizer.authorizationStatus = .fullAccess
        model.refresh()

        XCTAssertEqual(model.status, .fullAccess)
        XCTAssertEqual(model.label, "Device access granted")
    }
}
