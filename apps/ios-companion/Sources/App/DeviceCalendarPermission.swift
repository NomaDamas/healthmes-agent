import EventKit
import Foundation

@MainActor
protocol DeviceCalendarAuthorizing {
    var authorizationStatus: EKAuthorizationStatus { get }
    func requestFullAccess() async throws
}

@MainActor
struct EventKitCalendarAuthorizer: DeviceCalendarAuthorizing {
    private let store = EKEventStore()

    var authorizationStatus: EKAuthorizationStatus {
        EKEventStore.authorizationStatus(for: .event)
    }

    func requestFullAccess() async throws {
        _ = try await store.requestFullAccessToEvents()
    }
}

@MainActor
struct DemoCalendarAuthorizer: DeviceCalendarAuthorizing {
    var authorizationStatus: EKAuthorizationStatus

    init(status: EKAuthorizationStatus = .fullAccess) {
        authorizationStatus = status
    }

    func requestFullAccess() async throws {}
}

@MainActor
final class DeviceCalendarPermissionModel: ObservableObject {
    @Published private(set) var status: EKAuthorizationStatus
    @Published private(set) var message: String?

    private let authorizer: any DeviceCalendarAuthorizing

    init(authorizer: (any DeviceCalendarAuthorizing)? = nil) {
        let resolved: any DeviceCalendarAuthorizing
        if let authorizer {
            resolved = authorizer
        } else if ProcessInfo.processInfo.environment["HEALTHMES_DEMO_CALENDAR"] == "1" {
            resolved = DemoCalendarAuthorizer()
        } else {
            resolved = EventKitCalendarAuthorizer()
        }
        self.authorizer = resolved
        status = resolved.authorizationStatus
    }

    func request() async {
        do {
            try await authorizer.requestFullAccess()
            refresh()
            message = nil
        } catch {
            refresh()
            message = error.localizedDescription
        }
    }

    func refresh() {
        status = authorizer.authorizationStatus
    }

    var label: String {
        switch status {
        case .fullAccess, .authorized:
            return String(localized: "Device access granted")
        case .writeOnly:
            return String(localized: "Write-only access")
        case .denied, .restricted:
            return String(localized: "Permission denied")
        case .notDetermined:
            return String(localized: "Not requested")
        @unknown default:
            return String(localized: "Unknown")
        }
    }
}
