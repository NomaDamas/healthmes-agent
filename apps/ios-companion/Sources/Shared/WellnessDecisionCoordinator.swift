import Foundation

public struct WellnessDecisionRetryPolicy: Equatable {
    public let maximumPOSTAttempts: Int
    public let maximumRecoveryAttempts: Int
    public let initialDelay: TimeInterval
    public let maximumDelay: TimeInterval

    public init(
        maximumPOSTAttempts: Int = 3,
        maximumRecoveryAttempts: Int = 6,
        initialDelay: TimeInterval = 0.5,
        maximumDelay: TimeInterval = 8
    ) {
        self.maximumPOSTAttempts = max(1, maximumPOSTAttempts)
        self.maximumRecoveryAttempts = max(1, maximumRecoveryAttempts)
        self.initialDelay = max(0, initialDelay)
        self.maximumDelay = max(0, maximumDelay)
    }

    public static let standard = WellnessDecisionRetryPolicy()

    func delay(
        afterAttempt attempt: Int,
        response: HTTPURLResponse?
    ) -> TimeInterval {
        if let raw = response?.value(forHTTPHeaderField: "Retry-After"),
            let seconds = TimeInterval(raw),
            seconds >= 0
        {
            return min(seconds, maximumDelay)
        }
        let exponential = initialDelay * pow(2, Double(max(0, attempt - 1)))
        return min(exponential, maximumDelay)
    }
}

public struct WellnessDecisionCoordinator {
    public typealias Sleeper = (TimeInterval) async throws -> Void

    private let transport: WellnessDecisionTransporting
    private let retryPolicy: WellnessDecisionRetryPolicy
    private let sleep: Sleeper

    public init(
        transport: WellnessDecisionTransporting,
        retryPolicy: WellnessDecisionRetryPolicy = .standard,
        sleep: Sleeper? = nil
    ) {
        self.transport = transport
        self.retryPolicy = retryPolicy
        self.sleep = sleep ?? Self.defaultSleep
    }

    public func submit(
        _ submission: WellnessDecisionSubmission,
        pairing: Pairing
    ) async throws -> WellnessDecisionOutput {
        var postAttempt = 0
        while postAttempt < retryPolicy.maximumPOSTAttempts {
            postAttempt += 1
            do {
                let response = try await transport.send(
                    submission.request(pairing: pairing)
                )
                switch response.response.statusCode {
                case 200:
                    return try decode(response.data)
                case 202:
                    let accepted = try decode(response.data)
                    let location = try recoveryURL(
                        from: response.response,
                        pairing: pairing
                    )
                    try validateRecoveryIdentity(
                        location: location,
                        expectedRequestID: accepted.requestID
                    )
                    return try await recover(
                        requestID: accepted.requestID,
                        location: location,
                        pairing: pairing
                    )
                case 401:
                    throw HealthMesAPIError.unauthorized(
                        statusCode: response.response.statusCode
                    )
                default:
                    if isRetryable(response),
                        postAttempt < retryPolicy.maximumPOSTAttempts
                    {
                        try await wait(
                            afterAttempt: postAttempt,
                            response: response.response
                        )
                        continue
                    }
                    throw HealthMesAPI.responseError(
                        statusCode: response.response.statusCode,
                        data: response.data
                    )
                }
            } catch let error as HealthMesAPIError {
                throw error
            } catch let error as WellnessDecisionClientError {
                throw error
            } catch {
                if error is CancellationError {
                    throw error
                }
                guard postAttempt < retryPolicy.maximumPOSTAttempts else {
                    throw HealthMesAPIError.transport(underlying: error)
                }
                try await wait(afterAttempt: postAttempt, response: nil)
            }
        }
        throw HealthMesAPIError.httpStatus(-1)
    }

    public static func recoveryURL(
        location: String,
        pairing: Pairing
    ) -> URL? {
        guard
            let components = URLComponents(string: location),
            components.fragment == nil
        else { return nil }

        if components.scheme != nil || components.host != nil {
            guard
                let absolute = components.url,
                ViewerURL.hasSameOrigin(absolute, as: pairing.baseURL),
                isInsidePairedPath(absolute, pairing: pairing)
            else { return nil }
            return absolute
        }

        var resolved = pairing.baseURL
        for component in components.path.split(separator: "/") {
            resolved.appendPathComponent(String(component))
        }
        guard var resolvedComponents = URLComponents(
            url: resolved,
            resolvingAgainstBaseURL: false
        ) else { return nil }
        resolvedComponents.queryItems = components.queryItems
        return resolvedComponents.url
    }

    private func recover(
        requestID: UUID,
        location: URL,
        pairing: Pairing
    ) async throws -> WellnessDecisionOutput {
        var recoveryAttempt = 0
        var lastTransportError: Error?

        while recoveryAttempt < retryPolicy.maximumRecoveryAttempts {
            recoveryAttempt += 1
            do {
                let response = try await transport.send(
                    recoveryRequest(location: location, pairing: pairing)
                )
                switch response.response.statusCode {
                case 200:
                    let output = try decode(response.data)
                    guard output.requestID == requestID else {
                        throw WellnessDecisionClientError.recoveryRequestMismatch
                    }
                    if output.persistenceStatus != .unknown {
                        return output
                    }
                case 202, 404, 429, 503:
                    break
                case 401:
                    throw HealthMesAPIError.unauthorized(
                        statusCode: response.response.statusCode
                    )
                default:
                    guard isRetryable(response) else {
                        throw HealthMesAPI.responseError(
                            statusCode: response.response.statusCode,
                            data: response.data
                        )
                    }
                }
                guard recoveryAttempt < retryPolicy.maximumRecoveryAttempts else {
                    break
                }
                try await wait(
                    afterAttempt: recoveryAttempt,
                    response: response.response
                )
            } catch let error as HealthMesAPIError {
                throw error
            } catch let error as WellnessDecisionClientError {
                throw error
            } catch {
                if error is CancellationError {
                    throw error
                }
                lastTransportError = error
                guard recoveryAttempt < retryPolicy.maximumRecoveryAttempts else {
                    break
                }
                try await wait(afterAttempt: recoveryAttempt, response: nil)
            }
        }

        if let lastTransportError {
            throw HealthMesAPIError.transport(underlying: lastTransportError)
        }
        throw WellnessDecisionClientError.recoveryExhausted(
            requestID: requestID
        )
    }

    private func recoveryURL(
        from response: HTTPURLResponse,
        pairing: Pairing
    ) throws -> URL {
        guard let location = response.value(forHTTPHeaderField: "Location") else {
            throw WellnessDecisionClientError.missingRecoveryLocation
        }
        guard let url = Self.recoveryURL(location: location, pairing: pairing) else {
            throw WellnessDecisionClientError.unsafeRecoveryLocation
        }
        return url
    }

    private func recoveryRequest(
        location: URL,
        pairing: Pairing
    ) -> URLRequest {
        var request = URLRequest(url: location)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token = pairing.token {
            request.setValue(
                "Bearer \(token)",
                forHTTPHeaderField: "Authorization"
            )
        }
        return request
    }

    private func validateRecoveryIdentity(
        location: URL,
        expectedRequestID: UUID
    ) throws {
        guard
            let rawID = location.pathComponents.last,
            let locationID = UUID(uuidString: rawID),
            locationID == expectedRequestID
        else {
            throw WellnessDecisionClientError.recoveryRequestMismatch
        }
    }

    private func decode(_ data: Data) throws -> WellnessDecisionOutput {
        do {
            return try GlanceJSON.decoder().decode(
                WellnessDecisionOutput.self,
                from: data
            )
        } catch {
            throw HealthMesAPIError.decoding(underlying: error)
        }
    }

    private func isRetryable(
        _ response: WellnessDecisionHTTPResponse
    ) -> Bool {
        if response.response.statusCode == 429 {
            return true
        }
        guard response.response.statusCode == 503 else {
            return false
        }
        guard
            let envelope = try? JSONDecoder().decode(
                APIErrorEnvelope.self,
                from: response.data
            ),
            case .object(let detail)? = envelope.error.detail,
            case .bool(true)? = detail["retryable"]
        else {
            return false
        }
        return true
    }

    private func wait(
        afterAttempt attempt: Int,
        response: HTTPURLResponse?
    ) async throws {
        let delay = retryPolicy.delay(
            afterAttempt: attempt,
            response: response
        )
        if delay > 0 {
            try await sleep(delay)
        }
    }

    private static func defaultSleep(
        _ seconds: TimeInterval
    ) async throws {
        let nanoseconds = UInt64(
            min(seconds, TimeInterval(UInt64.max) / 1_000_000_000)
                * 1_000_000_000
        )
        try await Task.sleep(nanoseconds: nanoseconds)
    }

    private static func isInsidePairedPath(
        _ url: URL,
        pairing: Pairing
    ) -> Bool {
        let basePath = pairing.baseURL.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !basePath.isEmpty else { return true }
        let candidate = url.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return candidate == basePath || candidate.hasPrefix("\(basePath)/")
    }
}
