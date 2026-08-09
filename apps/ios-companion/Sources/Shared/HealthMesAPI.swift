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

    public var isProposalExpired: Bool {
        if case .server(409, "proposal_expired", _, _) = self { return true }
        return false
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

    /// `GET /v1/goals` for the current product plan.
    public static func goalsRequest(
        pairing: Pairing,
        weekStart: String? = nil,
        status: String? = nil,
        limit: Int = 50
    ) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/goals", method: "GET")
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let weekStart {
            query.append(URLQueryItem(name: "week_start", value: weekStart))
        }
        if let status {
            query.append(URLQueryItem(name: "status", value: status))
        }
        components.queryItems = query
        request.url = components.url
        return request
    }

    /// `GET /v1/tasks`, ordered by the server by deadline.
    public static func tasksRequest(pairing: Pairing, limit: Int = 100) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/tasks", method: "GET")
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        request.url = components.url
        return request
    }

    public static func decisionsRequest(pairing: Pairing, limit: Int = 100) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/decisions", method: "GET")
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: "0"),
        ]
        request.url = components.url
        return request
    }

    public static func setupReadinessRequest(pairing: Pairing) -> URLRequest {
        baseRequest(
            pairing: pairing,
            path: "v1/setup/readiness",
            method: "GET"
        )
    }

    /// `GET /v1/schedule/events?start=…&end=…`.
    public static func scheduleEventsRequest(
        pairing: Pairing,
        start: Date,
        end: Date,
        limit: Int = 100
    ) -> URLRequest {
        var request = baseRequest(pairing: pairing, path: "v1/schedule/events", method: "GET")
        var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        components.queryItems = [
            URLQueryItem(name: "start", value: formatter.string(from: start)),
            URLQueryItem(name: "end", value: formatter.string(from: end)),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        request.url = components.url
        return request
    }

    public static func wellnessSceneRequest(
        pairing: Pairing,
        query: String,
        source: WellnessSceneRequest.Source = .user,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/wellness/scenes",
            body: WellnessSceneRequest(
                query: query,
                source: source,
                proposalID: proposalID,
                decisionRecordID: decisionRecordID
            )
        )
    }

    public static func createGoalRequest(
        pairing: Pairing,
        body: WeeklyGoalCreateBody
    ) throws -> URLRequest {
        try jsonRequest(pairing: pairing, path: "v1/goals", body: body)
    }

    public static func createTaskRequest(
        pairing: Pairing,
        body: TaskCreateBody
    ) throws -> URLRequest {
        try jsonRequest(pairing: pairing, path: "v1/tasks", body: body)
    }

    /// `GET /v1/schedule/proposals/{id}` — direct notification/deep-link lookup.
    public static func proposalRequest(pairing: Pairing, proposalID: UUID) -> URLRequest {
        baseRequest(
            pairing: pairing,
            path: "v1/schedule/proposals/\(proposalID.uuidString.lowercased())",
            method: "GET"
        )
    }

    /// `POST /v1/schedule/proposals/{id}/accept|decline` — the real endpoint
    /// behind the §8.5 ✅/❌ buttons.
    public static func proposalActionRequest(
        pairing: Pairing,
        proposalID: UUID,
        action: ProposalAction,
        resolutionToken: String,
        surface: String = "ios_app"
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/schedule/proposals/\(proposalID.uuidString.lowercased())/\(action.rawValue)",
            body: ProposalResolutionBody(
                resolutionToken: resolutionToken,
                surface: surface
            )
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

    public static func nutritionPhotoAnalysisRequest(
        pairing: Pairing,
        body: NutritionPhotoAnalysisBody
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/nutrition-observations/analyze",
            body: body
        )
    }

    public static func intakeAnalysisRequest(
        pairing: Pairing,
        body: IntakeInteractionAnalysisBody
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/intake-interactions/analyze",
            body: body
        )
    }

    public static func photoIntakeRequest(
        pairing: Pairing,
        body: PhotoIntakeInteractionBody
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/intake-interactions",
            body: body
        )
    }

    public static func intakeOutcomeRequest(
        pairing: Pairing,
        interactionID: UUID,
        body: IntakeOutcomeBody
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path:
                "v1/intake-interactions/\(interactionID.uuidString.lowercased())/outcomes",
            body: body
        )
    }

    /// `POST /v1/medical-records` — REST twin of the create_medical_record
    /// MCP tool; the server attaches the health snapshot itself.
    public static func medicalRecordRequest(
        pairing: Pairing, body: MedicalRecordCreateBody
    ) throws -> URLRequest {
        try jsonRequest(pairing: pairing, path: "v1/medical-records", body: body)
    }

    public static func storageSettingsRequest(pairing: Pairing) -> URLRequest {
        baseRequest(pairing: pairing, path: "v1/storage/settings", method: "GET")
    }

    public static func storageRetentionRequest(
        pairing: Pairing,
        dataClass: String,
        preset: String
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: "v1/storage/settings/\(dataClass)",
            method: "PUT",
            body: StorageRetentionUpdate(preset: preset)
        )
    }

    public static func storageMaintenanceRequest(
        pairing: Pairing,
        dryRun: Bool
    ) -> URLRequest {
        var request = baseRequest(
            pairing: pairing,
            path: "v1/storage/maintenance",
            method: "POST"
        )
        var components = URLComponents(
            url: request.url!,
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "dry_run", value: dryRun ? "true" : "false")
        ]
        request.url = components.url
        return request
    }

    static func jsonRequest<Body: Encodable>(
        pairing: Pairing, path: String, body: Body
    ) throws -> URLRequest {
        try jsonRequest(
            pairing: pairing,
            path: path,
            method: "POST",
            body: body
        )
    }

    static func jsonRequest<Body: Encodable>(
        pairing: Pairing,
        path: String,
        method: String,
        body: Body
    ) throws -> URLRequest {
        var request = baseRequest(pairing: pairing, path: path, method: method)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
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
        try await listAlerts(
            pairing: try pairing(),
            hours: hours,
            limit: limit,
            offset: offset
        )
    }

    public func listAlerts(
        pairing: Pairing,
        hours: Int = 24,
        limit: Int = 50,
        offset: Int = 0
    ) async throws -> AlertsPage {
        let request = Self.alertsRequest(
            pairing: pairing, hours: hours, limit: limit, offset: offset
        )
        return try await perform(request, expecting: AlertsPage.self)
    }

    public func weeklyReport() async throws -> WeeklyReport {
        try await weeklyReport(pairing: try pairing())
    }

    public func weeklyReport(pairing: Pairing) async throws -> WeeklyReport {
        try await perform(
            Self.weeklyReportRequest(pairing: pairing), expecting: WeeklyReport.self
        )
    }

    public func listProposals(status: ProposalStatus? = nil) async throws -> ProposalsPage {
        try await listProposals(pairing: try pairing(), status: status)
    }

    public func listProposals(
        pairing: Pairing,
        status: ProposalStatus? = nil
    ) async throws -> ProposalsPage {
        try await perform(
            Self.proposalsRequest(pairing: pairing, status: status),
            expecting: ProposalsPage.self
        )
    }

    public func listGoals(
        weekStart: String? = nil,
        status: String? = nil
    ) async throws -> WeeklyGoalsPage {
        try await listGoals(
            pairing: try pairing(),
            weekStart: weekStart,
            status: status
        )
    }

    public func listGoals(
        pairing: Pairing,
        weekStart: String? = nil,
        status: String? = nil
    ) async throws -> WeeklyGoalsPage {
        try await perform(
            Self.goalsRequest(
                pairing: pairing,
                weekStart: weekStart,
                status: status
            ),
            expecting: WeeklyGoalsPage.self
        )
    }

    public func listTasks() async throws -> TasksPage {
        try await listTasks(pairing: try pairing())
    }

    public func listTasks(pairing: Pairing) async throws -> TasksPage {
        try await perform(
            Self.tasksRequest(pairing: pairing),
            expecting: TasksPage.self
        )
    }

    public func listDecisionRecords() async throws -> ProductDecisionsPage {
        try await listDecisionRecords(pairing: try pairing())
    }

    public func listDecisionRecords(pairing: Pairing) async throws -> ProductDecisionsPage {
        try await perform(
            Self.decisionsRequest(pairing: pairing),
            expecting: ProductDecisionsPage.self
        )
    }

    public func setupReadiness() async throws -> SetupReadiness {
        try await perform(
            Self.setupReadinessRequest(pairing: try pairing()),
            expecting: SetupReadiness.self
        )
    }

    public func listScheduleEvents(start: Date, end: Date) async throws -> CalendarEventsPage {
        try await listScheduleEvents(
            pairing: try pairing(),
            start: start,
            end: end
        )
    }

    public func listScheduleEvents(
        pairing: Pairing,
        start: Date,
        end: Date
    ) async throws -> CalendarEventsPage {
        try await perform(
            Self.scheduleEventsRequest(pairing: pairing, start: start, end: end),
            expecting: CalendarEventsPage.self
        )
    }

    public func createWellnessScene(
        query: String,
        source: WellnessSceneRequest.Source = .user,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) async throws -> WellnessScene {
        try await createWellnessScene(
            query: query,
            source: source,
            proposalID: proposalID,
            decisionRecordID: decisionRecordID,
            pairing: try pairing()
        )
    }

    public func createWellnessScene(
        query: String,
        source: WellnessSceneRequest.Source = .user,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil,
        pairing: Pairing
    ) async throws -> WellnessScene {
        let scene = try await perform(
            Self.wellnessSceneRequest(
                pairing: pairing,
                query: query,
                source: source,
                proposalID: proposalID,
                decisionRecordID: decisionRecordID
            ),
            expecting: WellnessScene.self
        )
        try WellnessSceneValidator.validate(
            scene,
            pairedBaseURL: pairing.baseURL,
            expectedProposalID: proposalID
        )
        return scene
    }

    public func createGoal(_ body: WeeklyGoalCreateBody) async throws -> WeeklyGoalItem {
        try await createGoal(body, pairing: try pairing())
    }

    public func createGoal(
        _ body: WeeklyGoalCreateBody,
        pairing: Pairing
    ) async throws -> WeeklyGoalItem {
        try await perform(
            Self.createGoalRequest(pairing: pairing, body: body),
            expecting: WeeklyGoalItem.self
        )
    }

    public func createTask(_ body: TaskCreateBody) async throws -> TaskItem {
        try await createTask(body, pairing: try pairing())
    }

    public func createTask(
        _ body: TaskCreateBody,
        pairing: Pairing
    ) async throws -> TaskItem {
        try await perform(
            Self.createTaskRequest(pairing: pairing, body: body),
            expecting: TaskItem.self
        )
    }

    public func getProposal(_ proposalID: UUID) async throws -> ProposalItem {
        try await getProposal(proposalID, pairing: try pairing())
    }

    public func getProposal(
        _ proposalID: UUID,
        pairing: Pairing
    ) async throws -> ProposalItem {
        try await perform(
            Self.proposalRequest(pairing: pairing, proposalID: proposalID),
            expecting: ProposalItem.self
        )
    }

    public func resolveProposal(
        _ proposal: ProposalItem,
        action: ProposalAction,
        surface: String = "ios_app"
    ) async throws -> ProposalItem {
        try await resolveProposal(
            proposal,
            action: action,
            surface: surface,
            pairing: try pairing()
        )
    }

    public func resolveProposal(
        _ proposal: ProposalItem,
        action: ProposalAction,
        surface: String = "ios_app",
        pairing: Pairing
    ) async throws -> ProposalItem {
        guard let resolutionToken = proposal.resolutionToken(for: action) else {
            throw HealthMesAPIError.httpStatus(422)
        }
        return try await perform(
            Self.proposalActionRequest(
                pairing: pairing,
                proposalID: proposal.id,
                action: action,
                resolutionToken: resolutionToken,
                surface: surface
            ),
            expecting: ProposalItem.self
        )
    }

    public func uploadMedia(data: Data, mediaType: CaptureMediaType) async throws -> MediaUpload {
        try await perform(
            Self.mediaUploadRequest(pairing: try pairing(), data: data, mediaType: mediaType),
            expecting: MediaUpload.self
        )
    }

    public func analyzeNutritionPhoto(
        _ body: NutritionPhotoAnalysisBody
    ) async throws -> NutritionObservationResult {
        try await perform(
            Self.nutritionPhotoAnalysisRequest(
                pairing: try pairing(),
                body: body
            ),
            expecting: NutritionObservationResult.self
        )
    }

    public func analyzeIntake(
        _ body: IntakeInteractionAnalysisBody
    ) async throws -> IntakeInteractionResult {
        try await perform(
            Self.intakeAnalysisRequest(pairing: try pairing(), body: body),
            expecting: IntakeInteractionResult.self
        )
    }

    public func createPhotoIntake(
        _ body: PhotoIntakeInteractionBody
    ) async throws -> IntakeInteractionResult {
        try await perform(
            Self.photoIntakeRequest(pairing: try pairing(), body: body),
            expecting: IntakeInteractionResult.self
        )
    }

    public func confirmIntake(
        interactionID: UUID,
        body: IntakeOutcomeBody
    ) async throws -> IntakeInteractionResult {
        try await perform(
            Self.intakeOutcomeRequest(
                pairing: try pairing(),
                interactionID: interactionID,
                body: body
            ),
            expecting: IntakeInteractionResult.self
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

    public func storageSettings() async throws -> StorageSettingsSnapshot {
        try await perform(
            Self.storageSettingsRequest(pairing: try pairing()),
            expecting: StorageSettingsSnapshot.self
        )
    }

    public func updateStorageRetention(
        dataClass: String,
        preset: String
    ) async throws -> StorageRetentionPolicy {
        try await perform(
            Self.storageRetentionRequest(
                pairing: try pairing(),
                dataClass: dataClass,
                preset: preset
            ),
            expecting: StorageRetentionPolicy.self
        )
    }

    public func maintainStorage(dryRun: Bool) async throws -> StorageMaintenanceReport {
        try await perform(
            Self.storageMaintenanceRequest(
                pairing: try pairing(),
                dryRun: dryRun
            ),
            expecting: StorageMaintenanceReport.self
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
        case 401:
            throw HealthMesAPIError.unauthorized(statusCode: http.statusCode)
        default:
            throw Self.responseError(statusCode: http.statusCode, data: data)
        }
    }

    static func responseError(statusCode: Int, data: Data) -> HealthMesAPIError {
        if let envelope = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data) {
            return HealthMesAPIError.server(
                statusCode: statusCode,
                code: envelope.error.code,
                message: envelope.error.message,
                detail: envelope.error.detail
            )
        }
        return HealthMesAPIError.httpStatus(statusCode)
    }
}

public struct WellnessSceneRequest: Codable, Equatable {
    public enum Source: String, Codable {
        case user
        case proactive
    }

    public let query: String
    public let source: Source
    public let proposalID: UUID?
    public let decisionRecordID: UUID?

    public init(
        query: String,
        source: Source = .user,
        proposalID: UUID? = nil,
        decisionRecordID: UUID? = nil
    ) {
        self.query = query
        self.source = source
        self.proposalID = proposalID
        self.decisionRecordID = decisionRecordID
    }

    enum CodingKeys: String, CodingKey {
        case query
        case source
        case proposalID = "proposal_id"
        case decisionRecordID = "decision_record_id"
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
