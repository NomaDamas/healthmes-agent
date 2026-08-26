import Foundation

public struct InputControlPlaneVersionedDescriptor: Equatable {
    public let descriptor: InputSourceDescriptor
    public let etag: String
}

public enum InputControlPlaneClientError: Error {
    case notPaired
    case pairingChanged
    case unauthorized(statusCode: Int)
    case revisionConflict(
        expectedRevision: String,
        currentRevision: String,
        message: String
    )
    case server(statusCode: Int, code: String, message: String)
    case httpStatus(Int)
    case transport(underlying: Error)
    case decoding(underlying: Error)
    case invalidContract(String)
}

extension InputControlPlaneClientError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .notPaired:
            return "Connect HealthMes first."
        case .pairingChanged:
            return "The paired HealthMes instance changed. Refresh settings."
        case .unauthorized:
            return "The paired HealthMes instance rejected this device."
        case .revisionConflict(_, _, let message):
            return message
        case .server(_, _, let message):
            return message
        case .httpStatus(let status):
            return "HealthMes returned HTTP \(status)."
        case .transport:
            return "Could not reach the paired HealthMes instance."
        case .decoding:
            return "HealthMes returned an unreadable input descriptor."
        case .invalidContract(let message):
            return message
        }
    }
}

public final class InputControlPlaneClient {
    private struct ErrorEnvelope: Decodable {
        struct Body: Decodable {
            let code: String
            let message: String
            let detail: JSONValue?
        }

        let error: Body
    }

    private let session: URLSession
    private let pairingStore: PairingStore

    public init(
        session: URLSession = InputControlPlaneClient.makeSession(),
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

    public static func listRequest(pairing: Pairing) -> URLRequest {
        request(
            pairing: pairing,
            url: pairing.baseURL
                .appendingPathComponent("v1")
                .appendingPathComponent("inputs"),
            method: "GET"
        )
    }

    public static func sourceRequest(
        pairing: Pairing,
        sourceID: String
    ) -> URLRequest {
        request(
            pairing: pairing,
            url: sourceURL(pairing: pairing, sourceID: sourceID),
            method: "GET"
        )
    }

    public static func updateRequest(
        pairing: Pairing,
        sourceID: String,
        update: InputControlPlaneSettingsUpdate,
        ifMatch etag: String
    ) throws -> URLRequest {
        guard InputControlPlaneContract.revision(fromStrongETag: etag) != nil else {
            throw InputControlPlaneClientError.invalidContract(
                "Input settings require one exact strong descriptor ETag."
            )
        }
        var request = request(
            pairing: pairing,
            url: sourceURL(pairing: pairing, sourceID: sourceID)
                .appendingPathComponent("settings"),
            method: "PUT"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(etag, forHTTPHeaderField: "If-Match")
        request.httpBody = try InputControlPlaneJSON.encoder().encode(update)
        return request
    }

    public func listSources() async throws -> [InputSourceDescriptor] {
        let pairing = try currentPairing()
        let (data, response) = try await send(
            Self.listRequest(pairing: pairing),
            pairing: pairing
        )
        guard (200...299).contains(response.statusCode) else {
            throw responseError(statusCode: response.statusCode, data: data)
        }
        let payload: InputSourcesResponse
        do {
            payload = try InputControlPlaneJSON.decoder().decode(
                InputSourcesResponse.self,
                from: data
            )
        } catch {
            throw InputControlPlaneClientError.decoding(underlying: error)
        }
        for source in payload.sources {
            try validateRevision(source.revision)
        }
        return payload.sources
    }

    public func source(
        _ sourceID: String
    ) async throws -> InputControlPlaneVersionedDescriptor {
        let pairing = try currentPairing()
        let (data, response) = try await send(
            Self.sourceRequest(pairing: pairing, sourceID: sourceID),
            pairing: pairing
        )
        guard (200...299).contains(response.statusCode) else {
            throw responseError(statusCode: response.statusCode, data: data)
        }
        return try versionedDescriptor(data: data, response: response)
    }

    public func update(
        _ sourceID: String,
        settings: InputControlPlaneSettingsUpdate,
        ifMatch etag: String
    ) async throws -> InputControlPlaneVersionedDescriptor {
        let pairing = try currentPairing()
        let request = try Self.updateRequest(
            pairing: pairing,
            sourceID: sourceID,
            update: settings,
            ifMatch: etag
        )
        let (data, response) = try await send(request, pairing: pairing)
        guard (200...299).contains(response.statusCode) else {
            throw responseError(statusCode: response.statusCode, data: data)
        }
        return try versionedDescriptor(data: data, response: response)
    }

    private static func request(
        pairing: Pairing,
        url: URL,
        method: String
    ) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue(
                "Bearer \(token)",
                forHTTPHeaderField: "Authorization"
            )
        }
        return request
    }

    private static func sourceURL(pairing: Pairing, sourceID: String) -> URL {
        pairing.baseURL
            .appendingPathComponent("v1")
            .appendingPathComponent("inputs")
            .appendingPathComponent(sourceID)
    }

    private func currentPairing() throws -> Pairing {
        guard let pairing = pairingStore.load() else {
            throw InputControlPlaneClientError.notPaired
        }
        return pairing
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
            throw InputControlPlaneClientError.transport(underlying: error)
        }
        guard pairingStore.load() == pairing else {
            throw InputControlPlaneClientError.pairingChanged
        }
        guard let http = response as? HTTPURLResponse else {
            throw InputControlPlaneClientError.httpStatus(-1)
        }
        return (data, http)
    }

    private func versionedDescriptor(
        data: Data,
        response: HTTPURLResponse
    ) throws -> InputControlPlaneVersionedDescriptor {
        let descriptor: InputSourceDescriptor
        do {
            descriptor = try InputControlPlaneJSON.decoder().decode(
                InputSourceDescriptor.self,
                from: data
            )
        } catch {
            throw InputControlPlaneClientError.decoding(underlying: error)
        }
        try validateRevision(descriptor.revision)
        guard
            let etag = response.value(forHTTPHeaderField: "ETag"),
            InputControlPlaneContract.revision(fromStrongETag: etag)
                == descriptor.revision
        else {
            throw InputControlPlaneClientError.invalidContract(
                "HealthMes returned a missing, weak, or mismatched input descriptor ETag."
            )
        }
        return InputControlPlaneVersionedDescriptor(
            descriptor: descriptor,
            etag: etag
        )
    }

    private func validateRevision(_ revision: String) throws {
        guard InputControlPlaneContract.isValidRevision(revision) else {
            throw InputControlPlaneClientError.invalidContract(
                "HealthMes returned an invalid input descriptor revision."
            )
        }
    }

    private func responseError(
        statusCode: Int,
        data: Data
    ) -> InputControlPlaneClientError {
        if let envelope = try? InputControlPlaneJSON.decoder().decode(
            ErrorEnvelope.self,
            from: data
        ) {
            if
                statusCode == 409,
                envelope.error.code == "input_settings_revision_conflict",
                case .object(let detail)? = envelope.error.detail,
                case .string(let expected)? = detail["expected_revision"],
                case .string(let current)? = detail["current_revision"]
            {
                return .revisionConflict(
                    expectedRevision: expected,
                    currentRevision: current,
                    message: envelope.error.message
                )
            }
            if statusCode == 401 || statusCode == 403 {
                return .unauthorized(statusCode: statusCode)
            }
            return .server(
                statusCode: statusCode,
                code: envelope.error.code,
                message: envelope.error.message
            )
        }
        if statusCode == 401 || statusCode == 403 {
            return .unauthorized(statusCode: statusCode)
        }
        return .httpStatus(statusCode)
    }
}
