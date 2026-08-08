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

            await update(proposalID: proposalID, status: .applying, shouldEnd: false)

            do {
                let api = HealthMesAPI()
                let pending = try await api.getProposal(id)
                guard pending.isActionable else {
                    let status = activityStatus(forExisting: pending)
                    await update(
                        proposalID: proposalID,
                        status: status,
                        shouldEnd: true
                    )
                    return
                }
                let resolved = try await api.resolveProposal(
                    pending,
                    action: action,
                    surface: "ios_live_activity"
                )
                let status = activityStatus(forResolved: resolved.status)
                await update(proposalID: proposalID, status: status, shouldEnd: true)
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                let status =
                    error.alreadyResolvedStatus.flatMap(activityStatus(forExisting:))
                    ?? .expired
                await update(
                    proposalID: proposalID,
                    status: status,
                    shouldEnd: true
                )
            } catch let error as HealthMesAPIError where error.isProposalExpired {
                await update(proposalID: proposalID, status: .expired, shouldEnd: true)
            } catch {
                await update(proposalID: proposalID, status: .failed, shouldEnd: false)
            }
        }

        private static func activityStatus(
            forExisting proposal: ProposalItem
        ) -> DecisionActivityStatus {
            if proposal.status == .proposed {
                return .expired
            }
            return activityStatus(forExisting: proposal.status.rawValue) ?? .expired
        }

        private static func activityStatus(
            forExisting rawStatus: String
        ) -> DecisionActivityStatus? {
            guard let status = ProposalStatus(rawValue: rawStatus) else { return nil }
            switch status {
            case .accepted:
                return .alreadyAccepted
            case .pushed:
                return .alreadyPushed
            case .declined:
                return .alreadyDeclined
            case .proposed, .invalidated:
                return .expired
            }
        }

        private static func activityStatus(
            forResolved status: ProposalStatus
        ) -> DecisionActivityStatus {
            switch status {
            case .accepted:
                return .accepted
            case .pushed:
                return .pushed
            case .declined:
                return .declined
            case .proposed:
                return .failed
            case .invalidated:
                return .expired
            }
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
