import Foundation

public enum SettingsHubClientError: Error, LocalizedError {
    case notPaired
    case pairingChanged
    case unauthorized(Int)
    case server(Int, String)
    case transport(Error)
    case decoding(Error)

    public var errorDescription: String? {
        switch self {
        case .notPaired:
            return "Connect HealthMes first."
        case .pairingChanged:
            return "The paired HealthMes instance changed. Refresh settings."
        case .unauthorized:
            return "The paired HealthMes instance rejected this device."
        case .server(_, let code):
            return "HealthMes could not load Settings Hub (\(code))."
        case .transport:
            return "Could not reach the paired HealthMes instance."
        case .decoding:
            return "HealthMes returned an unreadable Settings Hub response."
        }
    }
}

public final class SettingsHubClient {
    private let session: URLSession
    private let pairingStore: PairingStore

    public init(
        session: URLSession = SettingsHubClient.makeSession(),
        pairingStore: PairingStore = .shared
    ) {
        self.session = session
        self.pairingStore = pairingStore
    }

    public static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 15
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }

    public static func snapshotRequest(pairing: Pairing) -> URLRequest {
        var request = URLRequest(
            url: pairing.baseURL
                .appendingPathComponent("v1")
                .appendingPathComponent("settings")
                .appendingPathComponent("hub")
        )
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    public func fetchSnapshot() async throws -> SettingsHubSnapshot {
        guard let pairing = pairingStore.load() else {
            throw SettingsHubClientError.notPaired
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(
                for: Self.snapshotRequest(pairing: pairing)
            )
        } catch {
            throw SettingsHubClientError.transport(error)
        }
        guard pairingStore.load() == pairing else {
            throw SettingsHubClientError.pairingChanged
        }
        guard let http = response as? HTTPURLResponse else {
            throw SettingsHubClientError.server(-1, "non_http_response")
        }
        guard (200...299).contains(http.statusCode) else {
            if http.statusCode == 401 || http.statusCode == 403 {
                throw SettingsHubClientError.unauthorized(http.statusCode)
            }
            let code = (try? JSONDecoder().decode(
                APIErrorEnvelope.self,
                from: data
            ))?.error.code ?? "http_error"
            throw SettingsHubClientError.server(http.statusCode, code)
        }
        do {
            return try WearableManagementJSON.decoder().decode(
                SettingsHubSnapshot.self,
                from: data
            )
        } catch {
            throw SettingsHubClientError.decoding(error)
        }
    }
}
