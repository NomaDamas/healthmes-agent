import Foundation

// One client for every non-glance endpoint the companion app uses
// (GlanceClient keeps owning /v1/briefing/glance and its ETag story).
// Local-first: every request goes to the paired base URL — this file and
// GlanceClient are the ONLY places in the project that build network
// requests. All request builders are static and pure so the unit-test bundle
// exercises them without any network.

/// `{"error": {"code": …, "message": …, "detail": …}}` — the standard
/// envelope every healthmes error returns (healthmes/api/errors.py).
public struct APIErrorEnvelope: Codable, Equatable {
    public struct Body: Codable, Equatable {
        public let code: String
        public let message: String
        public let detail: JSONValue?
    }

    public let error: Body
}

public enum HealthMesAPIError: Error {
    case notPaired
    case unauthorized(statusCode: Int)
    /// Non-2xx with a decodable envelope: machine `code` + human `message`.
    /// `invalid_transition` (409) carries `detail.current`/`detail.requested`
    /// — the "already resolved" render for double-tapped proposal buttons.
    case server(statusCode: Int, code: String, message: String, detail: JSONValue?)
    case httpStatus(Int)
    case transport(underlying: Error)
    case decoding(underlying: Error)

    /// True when the proposal was already resolved by an earlier tap
    /// (server answered 409 invalid_transition).
    public var isAlreadyResolved: Bool {
        if case .server(409, "invalid_transition", _, _) = self { return true }
        return false
    }

    /// Current proposal status out of a 409 invalid_transition detail.
    public var alreadyResolvedStatus: String? {
        guard case .server(409, "invalid_transition", _, let detail) = self,
            case .object(let fields)? = detail,
            case .string(let current)? = fields["current"]
        else { return nil }
        return current
    }
}

public final class HealthMesAPI {
    public let pairingStore: PairingStore
    private let session: URLSession

    public init(
        session: URLSession = GlanceClient.makeSession(),
        pairingStore: PairingStore = .shared
    ) {
        self.session = session
        self.pairingStore = pairingStore
    }

    // MARK: - Request builders (pure, unit-tested)

    static func baseRequest(pairing: Pairing, path: String, method: String) -> URLRequest {
        var request = URLRequest(url: pairing.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    /// `GET /v1/alerts?hours=…&limit=…&offset=…` — alert history, newest
    /// first, same "unresolved == recent pushed" semantics as the glance.
    public static func alertsRequest(
        pairing: Pairing, hours: Int = 24, limit: Int = 50, offset: Int = 0
    ) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/alerts", method: "GET")
        var components = URLComponents(
            url: request.url!, resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "hours", value: String(hours)),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
        request.url = components.url
        return request
    }

    /// `GET /reports/weekly.json` — the weekly report as data (same payload
    /// the HTML page renders, healthmes/api/reports.py).
    public static func weeklyReportRequest(pairing: Pairing) -> URLRequest {
        baseRequest(pairing: pairing, path: "reports/weekly.json", method: "GET")
    }

    /// `GET /v1/schedule/proposals[?status=…]`.
    public static func proposalsRequest(
        pairing: Pairing, status: ProposalStatus? = nil, limit: Int = 50
    ) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/schedule/proposals", method: "GET")
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let status {
            query.append(URLQueryItem(name: "status", value: status.rawValue))
        }
        components.queryItems = query
        request.url = components.url
        return request
    }

    /// `POST /v1/schedule/proposals/{id}/accept|decline` — the real endpoint
    /// behind the §8.5 ✅/❌ buttons.
    public static func proposalActionRequest(
        pairing: Pairing, proposalID: UUID, action: ProposalAction
    ) -> URLRequest {
        baseRequest(
            pairing: pairing,
            path: "v1/schedule/proposals/\(proposalID.uuidString.lowercased())/\(action.rawValue)",
            method: "POST"
        )
    }

    /// `POST /v1/media` — multipart upload, field name `file`. Bearer-only
    /// per the server contract (the viewer ?token= never uploads); the
    /// filename is a constant because the server ignores and never stores it.
    public static func mediaUploadRequest(
        pairing: Pairing,
        data: Data,
        mediaType: CaptureMediaType,
        boundary: String = "healthmes-\(UUID().uuidString)"
    ) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/media", method: "POST")
        request.setValue(
            "multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = MultipartFormData.fileBody(
            boundary: boundary,
            fieldName: "file",
            fileName: "capture.\(mediaType.fileExtension)",
            contentType: mediaType.rawValue,
            data: data
        )
        return request
    }

    /// `POST /v1/food-logs`.
    public static func foodLogRequest(
        pairing: Pairing, body: FoodLogCreateBody
    ) throws -> URLRequest {
        try jsonRequest(pairing: pairing, path: "v1/food-logs", body: body)
    }

    /// `POST /v1/medical-records` — REST twin of the create_medical_record
    /// MCP tool; the server attaches the health snapshot itself.
    public static func medicalRecordRequest(
        pairing: Pairing, body: MedicalRecordCreateBody
    ) throws -> URLRequest {
        try jsonRequest(pairing: pairing, path: "v1/medical-records", body: body)
    }

    static func jsonRequest<Body: Encodable>(
        pairing: Pairing, path: String, body: Body
    ) throws -> URLRequest {
        var request = baseRequest(pairing: pairing, path: path, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        request.httpBody = try encoder.encode(body)
        return request
    }

    /// URL that serves an uploaded file back (`GET /v1/media/{media_path}`);
    /// used by in-app previews. The upload's `media_path` token is appended
    /// verbatim per the server contract.
    public static func mediaURL(pairing: Pairing, mediaPath: String) -> URL {
        pairing.baseURL.appendingPathComponent("v1/media/\(mediaPath)")
    }

    // MARK: - Calls

    private func pairing() throws -> Pairing {
        guard let pairing = pairingStore.load() else {
            throw HealthMesAPIError.notPaired
        }
        return pairing
    }

    public func listAlerts(hours: Int = 24, limit: Int = 50, offset: Int = 0)
        async throws -> AlertsPage
    {
        let request = Self.alertsRequest(
            pairing: try pairing(), hours: hours, limit: limit, offset: offset
        )
        return try await perform(request, expecting: AlertsPage.self)
    }

    public func weeklyReport() async throws -> WeeklyReport {
        try await perform(
            Self.weeklyReportRequest(pairing: try pairing()), expecting: WeeklyReport.self
        )
    }

    public func listProposals(status: ProposalStatus? = nil) async throws -> ProposalsPage {
        try await perform(
            Self.proposalsRequest(pairing: try pairing(), status: status),
            expecting: ProposalsPage.self
        )
    }

    public func resolveProposal(id: UUID, action: ProposalAction) async throws -> ProposalItem {
        try await perform(
            Self.proposalActionRequest(pairing: try pairing(), proposalID: id, action: action),
            expecting: ProposalItem.self
        )
    }

    public func uploadMedia(data: Data, mediaType: CaptureMediaType) async throws -> MediaUpload {
        try await perform(
            Self.mediaUploadRequest(pairing: try pairing(), data: data, mediaType: mediaType),
            expecting: MediaUpload.self
        )
    }

    public func createFoodLog(_ body: FoodLogCreateBody) async throws -> FoodLogItem {
        try await perform(
            Self.foodLogRequest(pairing: try pairing(), body: body),
            expecting: FoodLogItem.self
        )
    }

    public func createMedicalRecord(
        _ body: MedicalRecordCreateBody
    ) async throws -> MedicalRecordItem {
        try await perform(
            Self.medicalRecordRequest(pairing: try pairing(), body: body),
            expecting: MedicalRecordItem.self
        )
    }

    // MARK: - Transport + envelope mapping

    private func perform<Response: Decodable>(
        _ request: URLRequest, expecting: Response.Type
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
        case 401, 403:
            throw HealthMesAPIError.unauthorized(statusCode: http.statusCode)
        default:
            if let envelope = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data) {
                throw HealthMesAPIError.server(
                    statusCode: http.statusCode,
                    code: envelope.error.code,
                    message: envelope.error.message,
                    detail: envelope.error.detail
                )
            }
            throw HealthMesAPIError.httpStatus(http.statusCode)
        }
    }
}

/// Deterministic multipart/form-data encoder for the single-file upload the
/// capture flow needs (unit-tested byte-for-byte).
public enum MultipartFormData {
    public static func fileBody(
        boundary: String,
        fieldName: String,
        fileName: String,
        contentType: String,
        data: Data
    ) -> Data {
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(
            Data(
                "Content-Disposition: form-data; name=\"\(fieldName)\"; filename=\"\(fileName)\"\r\n"
                    .utf8
            )
        )
        body.append(Data("Content-Type: \(contentType)\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        return body
    }
}
