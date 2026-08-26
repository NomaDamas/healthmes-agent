import Foundation

public enum WearableManagementClientError: Error, LocalizedError {
    case notPaired
    case pairingChanged
    case unauthorized(Int)
    case server(Int, String)
    case transport(Error)
    case decoding(Error)
    case invalidContract(String)

    public var errorDescription: String? {
        switch self {
        case .notPaired:
            return "Connect HealthMes first."
        case .pairingChanged:
            return "The paired HealthMes instance changed. Refresh settings."
        case .unauthorized:
            return "The paired HealthMes instance rejected this device."
        case .server(_, let code):
            return "HealthMes could not complete the wearable action (\(code))."
        case .transport:
            return "Could not reach the paired HealthMes instance."
        case .decoding:
            return "HealthMes returned an unreadable wearable response."
        case .invalidContract(let message):
            return message
        }
    }
}

public final class WearableManagementClient {
    private struct ErrorEnvelope: Decodable {
        struct Body: Decodable {
            let code: String
        }

        let error: Body
    }

    private struct HistoricalSyncBody: Encodable {
        let days: Int
    }

    private let session: URLSession
    private let pairingStore: PairingStore

    public init(
        session: URLSession = WearableManagementClient.makeSession(),
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
        request(pairing: pairing, path: ["v1", "wearables"], method: "GET")
    }

    public static func authorizeRequest(
        pairing: Pairing,
        provider: String
    ) throws -> URLRequest {
        try providerRequest(
            pairing: pairing,
            provider: provider,
            action: "authorize",
            method: "GET"
        )
    }

    public static func disconnectRequest(
        pairing: Pairing,
        provider: String
    ) throws -> URLRequest {
        try providerRequest(
            pairing: pairing,
            provider: provider,
            action: "disconnect",
            method: "POST"
        )
    }

    public static func syncRequest(
        pairing: Pairing,
        provider: String
    ) throws -> URLRequest {
        try providerRequest(
            pairing: pairing,
            provider: provider,
            action: "sync",
            method: "POST"
        )
    }

    public static func historicalSyncRequest(
        pairing: Pairing,
        provider: String,
        days: Int
    ) throws -> URLRequest {
        guard (1...365).contains(days) else {
            throw WearableManagementClientError.invalidContract(
                "Historical wearable sync must be between 1 and 365 days."
            )
        }
        var request = try providerRequest(
            pairing: pairing,
            provider: provider,
            action: "sync/historical",
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try WearableManagementJSON.encoder().encode(
            HistoricalSyncBody(days: days)
        )
        return request
    }

    public func fetchSnapshot() async throws -> WearablesManagementSnapshot {
        let pairing = try currentPairing()
        let (data, response) = try await send(
            Self.snapshotRequest(pairing: pairing),
            pairing: pairing
        )
        try validateStatus(response, data: data)
        do {
            return try WearableManagementJSON.decoder().decode(
                WearablesManagementSnapshot.self,
                from: data
            )
        } catch {
            throw WearableManagementClientError.decoding(error)
        }
    }

    public func authorize(provider: String) async throws -> WearableAuthorizationResponse {
        let pairing = try currentPairing()
        let (data, response) = try await send(
            try Self.authorizeRequest(pairing: pairing, provider: provider),
            pairing: pairing
        )
        try validateStatus(response, data: data)
        do {
            let result = try WearableManagementJSON.decoder().decode(
                WearableAuthorizationResponse.self,
                from: data
            )
            guard Self.isSafeAuthorizationURL(result.authorizationURL) else {
                throw WearableManagementClientError.invalidContract(
                    "HealthMes returned an unsafe wearable authorization URL."
                )
            }
            return result
        } catch let error as WearableManagementClientError {
            throw error
        } catch {
            throw WearableManagementClientError.decoding(error)
        }
    }

    public func disconnect(provider: String) async throws -> WearableMutationResponse {
        let pairing = try currentPairing()
        return try await performMutation(
            try Self.disconnectRequest(pairing: pairing, provider: provider),
            pairing: pairing
        )
    }

    public func sync(provider: String) async throws -> WearableMutationResponse {
        let pairing = try currentPairing()
        return try await performMutation(
            try Self.syncRequest(pairing: pairing, provider: provider),
            pairing: pairing
        )
    }

    public func syncHistorical(
        provider: String,
        days: Int
    ) async throws -> WearableMutationResponse {
        let pairing = try currentPairing()
        return try await performMutation(
            try Self.historicalSyncRequest(
                pairing: pairing,
                provider: provider,
                days: days
            ),
            pairing: pairing
        )
    }

    private static func request(
        pairing: Pairing,
        path: [String],
        method: String
    ) -> URLRequest {
        var url = pairing.baseURL
        path.forEach { url.appendPathComponent($0) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private static func providerRequest(
        pairing: Pairing,
        provider: String,
        action: String,
        method: String
    ) throws -> URLRequest {
        guard isSafeIdentifier(provider) else {
            throw WearableManagementClientError.invalidContract(
                "HealthMes returned an invalid wearable provider."
            )
        }
        var request = request(
            pairing: pairing,
            path: ["v1", "wearables", provider] + action.split(separator: "/").map(String.init),
            method: method
        )
        if method == "POST" {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func currentPairing() throws -> Pairing {
        guard let pairing = pairingStore.load() else {
            throw WearableManagementClientError.notPaired
        }
        return pairing
    }

    private func performMutation(
        _ request: URLRequest,
        pairing: Pairing
    ) async throws -> WearableMutationResponse {
        let (data, response) = try await send(request, pairing: pairing)
        try validateStatus(response, data: data)
        do {
            return try WearableManagementJSON.decoder().decode(
                WearableMutationResponse.self,
                from: data
            )
        } catch {
            throw WearableManagementClientError.decoding(error)
        }
    }

    private func send(
        _ request: URLRequest,
        pairing: Pairing
    ) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw WearableManagementClientError.transport(error)
        }
        guard pairingStore.load() == pairing else {
            throw WearableManagementClientError.pairingChanged
        }
        guard let http = response as? HTTPURLResponse else {
            throw WearableManagementClientError.invalidContract(
                "HealthMes returned a non-HTTP response."
            )
        }
        return (data, http)
    }

    private func validateStatus(
        _ response: HTTPURLResponse,
        data: Data
    ) throws {
        guard (200...299).contains(response.statusCode) else {
            if let envelope = try? WearableManagementJSON.decoder().decode(
                ErrorEnvelope.self,
                from: data
            ) {
                if response.statusCode == 401 || response.statusCode == 403 {
                    throw WearableManagementClientError.unauthorized(response.statusCode)
                }
                throw WearableManagementClientError.server(
                    response.statusCode,
                    envelope.error.code
                )
            }
            if response.statusCode == 401 || response.statusCode == 403 {
                throw WearableManagementClientError.unauthorized(response.statusCode)
            }
            throw WearableManagementClientError.server(response.statusCode, "http_error")
        }
    }

    private static func isSafeIdentifier(_ raw: String) -> Bool {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, value.utf8.count <= 128 else { return false }
        return value.utf8.allSatisfy { byte in
            (byte >= 0x30 && byte <= 0x39)
                || (byte >= 0x41 && byte <= 0x5a)
                || (byte >= 0x61 && byte <= 0x7a)
                || byte == 0x2d
                || byte == 0x5f
        }
    }

    private static func isSafeAuthorizationURL(_ url: URL) -> Bool {
        guard
            let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            url.host != nil,
            url.user == nil,
            url.password == nil,
            url.fragment == nil
        else { return false }
        let forbidden = Set([
            "access_token",
            "api_key",
            "apikey",
            "client_secret",
            "refresh_token",
            "secret",
        ])
        return !(URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems ?? []).contains {
                let normalized = $0.name
                    .lowercased()
                    .replacingOccurrences(of: "-", with: "_")
                return forbidden.contains(normalized)
                    || normalized.contains("secret")
                    || normalized.contains("token")
                    || normalized.contains("password")
                    || normalized.contains("private_key")
            }
    }
}
