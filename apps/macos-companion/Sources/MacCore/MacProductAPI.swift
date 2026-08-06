import Foundation

/// Small Mac-only client for decision history. Every product API already
/// present in the shared Apple layer is consumed through `HealthMesAPI`.
public final class MacDecisionAPI {
    public let pairingStore: PairingStore
    private let session: URLSession

    public init(
        session: URLSession = GlanceClient.makeSession(),
        pairingStore: PairingStore = .shared
    ) {
        self.session = session
        self.pairingStore = pairingStore
    }

    public static func decisionsRequest(pairing: Pairing) -> URLRequest {
        var request = HealthMesAPI.baseRequest(
            pairing: pairing, path: "v1/decisions", method: "GET"
        )
        request.url = paginatedURL(request.url!, limit: 100)
        return request
    }

    public func listDecisions() async throws -> MacDecisionsPage {
        try await perform(
            Self.decisionsRequest(pairing: try pairing()), expecting: MacDecisionsPage.self
        )
    }

    private func pairing() throws -> Pairing {
        guard let pairing = pairingStore.load() else {
            throw HealthMesAPIError.notPaired
        }
        return pairing
    }

    private func perform<Response: Decodable>(
        _ request: URLRequest,
        expecting: Response.Type
    ) async throws -> Response {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw HealthMesAPIError.transport(underlying: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw HealthMesAPIError.httpStatus(-1)
        }
        switch http.statusCode {
        case 200...299:
            do {
                return try GlanceJSON.decoder().decode(Response.self, from: data)
            } catch {
                throw HealthMesAPIError.decoding(underlying: error)
            }
        case 401:
            throw HealthMesAPIError.unauthorized(statusCode: http.statusCode)
        default:
            throw HealthMesAPI.responseError(statusCode: http.statusCode, data: data)
        }
    }

    private static func paginatedURL(_ url: URL, limit: Int) -> URL {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: "0"),
        ]
        return components.url!
    }

}

public enum MacWebLinks {
    public static func dashboard(pairing: Pairing) -> URL {
        ViewerURL.make(pairing: pairing, pathComponents: ["dashboard"])
    }

    public static func plan(pairing: Pairing) -> URL {
        ViewerURL.make(
            pairing: pairing,
            pathComponents: ["dashboard"],
            fragment: "plan"
        )
    }

    public static func connections(pairing: Pairing) -> URL {
        ViewerURL.make(pairing: pairing, pathComponents: ["connect"])
    }

    public static func decision(id: UUID, pairing: Pairing) -> URL {
        ViewerURL.make(
            pairing: pairing,
            pathComponents: ["decisions", id.uuidString.lowercased()]
        )
    }

    public static func decision(for alert: AlertItem, pairing: Pairing?) -> URL? {
        if let raw = alert.decisionCard?.decisionUrl ?? alert.decisionUrl,
            let url = URL(string: raw),
            let pairing,
            ViewerURL.hasSameOrigin(url, as: pairing.baseURL)
        {
            return ViewerURL.authenticate(url, pairing: pairing)
        }
        guard let id = alert.decisionCard?.decisionId, let pairing else { return nil }
        return decision(id: id, pairing: pairing)
    }

    public static func decision(for proposal: ProposalItem, pairing: Pairing?) -> URL? {
        guard let id = proposal.decisionRecordId, let pairing else { return nil }
        return decision(id: id, pairing: pairing)
    }

    public static func weeklyReport(_ rawURL: String, pairing: Pairing?) -> URL? {
        guard
            let pairing,
            let url = URL(string: rawURL),
            ViewerURL.hasSameOrigin(url, as: pairing.baseURL)
        else { return nil }
        return ViewerURL.authenticate(url, pairing: pairing)
    }
}
