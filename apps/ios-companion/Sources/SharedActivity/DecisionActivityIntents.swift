#if canImport(ActivityKit) && canImport(AppIntents)
    import ActivityKit
    import AppIntents
    import Foundation

    public struct DeclineDecisionIntent: LiveActivityIntent {
        public static let title: LocalizedStringResource = "Decline HealthMes decision"
        public static let openAppWhenRun = false
        public static let authenticationPolicy: IntentAuthenticationPolicy =
            .requiresLocalDeviceAuthentication

        @Parameter(title: "Proposal ID")
        public var proposalID: String

        public init() {
            proposalID = ""
        }

        public init(proposalID: String) {
            self.proposalID = proposalID
        }

        public func perform() async throws -> some IntentResult {
            await DecisionActivityResolver.resolve(proposalID: proposalID, action: .decline)
            return .result()
        }
    }

    public struct AcceptDecisionIntent: LiveActivityIntent {
        public static let title: LocalizedStringResource = "Accept HealthMes decision"
        public static let openAppWhenRun = false
        public static let authenticationPolicy: IntentAuthenticationPolicy =
            .requiresLocalDeviceAuthentication

        @Parameter(title: "Proposal ID")
        public var proposalID: String

        public init() {
            proposalID = ""
        }

        public init(proposalID: String) {
            self.proposalID = proposalID
        }

        public func perform() async throws -> some IntentResult {
            await DecisionActivityResolver.resolve(proposalID: proposalID, action: .accept)
            return .result()
        }
    }

    private enum DecisionActivityResolver {
        static func resolve(proposalID: String, action: ProposalAction) async {
            #if DEBUG
                if proposalID == DecisionActivityAttributes.demoProposalID {
                    let status: DecisionActivityStatus =
                        action == .accept ? .accepted : .declined
                    await update(proposalID: proposalID, status: status, shouldEnd: true)
                    return
                }
            #endif

            guard let id = UUID(uuidString: proposalID) else {
                await update(proposalID: proposalID, status: .failed, shouldEnd: false)
                return
            }

            do {
                let api = HealthMesAPI()
                let pending = try await api.getProposal(id)
                guard pending.isActionable else {
                    await update(
                        proposalID: proposalID,
                        status: activityStatus(for: pending.status) ?? .failed,
                        shouldEnd: activityStatus(for: pending.status) != nil
                    )
                    return
                }
                let resolved = try await api.resolveProposal(
                    pending,
                    action: action,
                    surface: "ios_live_activity"
                )
                let status: DecisionActivityStatus =
                    resolved.status == .accepted ? .accepted : .declined
                await update(proposalID: proposalID, status: status, shouldEnd: true)
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                let status =
                    error.alreadyResolvedStatus.flatMap(activityStatus(for:)) ?? .failed
                await update(
                    proposalID: proposalID,
                    status: status,
                    shouldEnd: status != .failed
                )
            } catch {
                await update(proposalID: proposalID, status: .failed, shouldEnd: false)
            }
        }

        private static func activityStatus(for status: ProposalStatus) -> DecisionActivityStatus? {
            switch status {
            case .accepted, .pushed:
                .accepted
            case .declined:
                .declined
            case .proposed, .invalidated:
                nil
            }
        }

        private static func activityStatus(for rawStatus: String) -> DecisionActivityStatus? {
            guard let status = ProposalStatus(rawValue: rawStatus) else { return nil }
            return activityStatus(for: status)
        }

        private static func update(
            proposalID: String,
            status: DecisionActivityStatus,
            shouldEnd: Bool
        ) async {
            for activity in Activity<DecisionActivityAttributes>.activities
            where activity.attributes.proposalID == proposalID {
                var state = activity.content.state
                state.status = status
                let content = ActivityContent(
                    state: state,
                    staleDate: shouldEnd ? nil : state.expiresAt
                )
                if shouldEnd {
                    await activity.end(
                        content,
                        dismissalPolicy: .after(Date().addingTimeInterval(4))
                    )
                } else {
                    await activity.update(content)
                }
            }
        }
    }
#endif
