import Foundation

public enum WellnessDecisionClientError: LocalizedError, Equatable {
    case invalidIdempotencyKey
    case missingRecoveryLocation
    case unsafeRecoveryLocation
    case recoveryRequestMismatch
    case recoveryExhausted(requestID: UUID)

    public var errorDescription: String? {
        switch self {
        case .invalidIdempotencyKey:
            return "The wellness decision retry identity is invalid."
        case .missingRecoveryLocation:
            return "HealthMes accepted the decision but omitted its recovery location."
        case .unsafeRecoveryLocation:
            return "HealthMes returned a recovery location outside the paired instance."
        case .recoveryRequestMismatch:
            return "HealthMes returned a recovery result for a different request."
        case .recoveryExhausted:
            return "HealthMes did not confirm the durable decision result in time."
        }
    }
}

public struct WellnessDecisionSubmission: Equatable {
    public let input: WellnessDecisionInput
    public let idempotencyKey: String
    public let body: Data

    public init(
        input: WellnessDecisionInput,
        idempotencyKey: String
    ) throws {
        guard Self.isValid(idempotencyKey: idempotencyKey) else {
            throw WellnessDecisionClientError.invalidIdempotencyKey
        }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        self.input = input
        self.idempotencyKey = idempotencyKey
        body = try encoder.encode(input)
    }

    public func request(pairing: Pairing) -> URLRequest {
        var request = HealthMesAPI.baseRequest(
            pairing: pairing,
            path: "v1/wellness-decisions",
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key")
        request.httpBody = body
        return request
    }

    public static func isValid(idempotencyKey: String) -> Bool {
        !idempotencyKey.isEmpty
            && idempotencyKey.count <= 255
            && idempotencyKey == idempotencyKey.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            && idempotencyKey.unicodeScalars.allSatisfy {
                !CharacterSet.controlCharacters.contains($0)
            }
    }
}

public struct WellnessDecisionHTTPResponse {
    public let data: Data
    public let response: HTTPURLResponse

    public init(data: Data, response: HTTPURLResponse) {
        self.data = data
        self.response = response
    }
}

public protocol WellnessDecisionTransporting {
    func send(_ request: URLRequest) async throws -> WellnessDecisionHTTPResponse
}

public final class WellnessDecisionURLSessionTransport:
    WellnessDecisionTransporting
{
    private let session: URLSession

    public init(session: URLSession = WellnessDecisionURLSessionTransport.makeSession()) {
        self.session = session
    }

    public static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 75
        configuration.timeoutIntervalForResource = 90
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }

    public func send(
        _ request: URLRequest
    ) async throws -> WellnessDecisionHTTPResponse {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw HealthMesAPIError.httpStatus(-1)
        }
        return WellnessDecisionHTTPResponse(data: data, response: http)
    }
}
