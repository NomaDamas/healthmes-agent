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

        @Parameter(title: "Pairing fingerprint")
        public var pairingFingerprint: String

        @Parameter(title: "Pairing generation")
        public var pairingGeneration: String

        public init() {
            proposalID = ""
            pairingFingerprint = ""
            pairingGeneration = ""
        }

        public init(
            proposalID: String,
            pairingFingerprint: String,
            pairingGeneration: String
        ) {
            self.proposalID = proposalID
            self.pairingFingerprint = pairingFingerprint
            self.pairingGeneration = pairingGeneration
        }

        public func perform() async throws -> some IntentResult {
            await DecisionActivityResolver.resolve(
                proposalID: proposalID,
                pairingFingerprint: pairingFingerprint,
                pairingGeneration: pairingGeneration,
                action: .decline
            )
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

        @Parameter(title: "Pairing fingerprint")
        public var pairingFingerprint: String

        @Parameter(title: "Pairing generation")
        public var pairingGeneration: String

        public init() {
            proposalID = ""
            pairingFingerprint = ""
            pairingGeneration = ""
        }

        public init(
            proposalID: String,
            pairingFingerprint: String,
            pairingGeneration: String
        ) {
            self.proposalID = proposalID
            self.pairingFingerprint = pairingFingerprint
            self.pairingGeneration = pairingGeneration
        }

        public func perform() async throws -> some IntentResult {
            await DecisionActivityResolver.resolve(
                proposalID: proposalID,
                pairingFingerprint: pairingFingerprint,
                pairingGeneration: pairingGeneration,
                action: .accept
            )
            return .result()
        }
    }

    private enum DecisionActivityResolver {
        static func resolve(
            proposalID: String,
            pairingFingerprint: String,
            pairingGeneration: String,
            action: ProposalAction
        ) async {
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

            let parsedGeneration = PairingScope.generation(
                from: pairingGeneration
            )
            guard
                let generation = parsedGeneration,
                generation > 0,
                let pairing = PairingScope.matchingPairing(
                    fingerprint: pairingFingerprint,
                    generation: generation
                ),
                let relayLease = PairingRelayGate.shared.begin(
                    pairing: pairing
                )
            else {
                await update(
                    proposalID: proposalID,
                    pairingFingerprint: pairingFingerprint,
                    pairingGeneration: parsedGeneration,
                    status: .expired,
                    shouldEnd: true
                )
                return
            }
            defer {
                PairingRelayGate.shared.end(relayLease)
            }

            await update(
                proposalID: proposalID,
                pairingFingerprint: pairingFingerprint,
                pairingGeneration: generation,
                status: .applying,
                shouldEnd: false
            )

            do {
                let api = HealthMesAPI()
                let pending = try await api.getProposal(
                    id,
                    pairing: pairing
                )
                guard pending.isActionable else {
                    let status = activityStatus(forExisting: pending)
                    await update(
                        proposalID: proposalID,
                        pairingFingerprint: pairingFingerprint,
                        pairingGeneration: generation,
                        status: status,
                        shouldEnd: true
                    )
                    return
                }
                let resolved = try await api.resolveProposal(
                    pending,
                    action: action,
                    surface: "ios_live_activity",
                    pairing: pairing
                )
                let status = activityStatus(forResolved: resolved.status)
                await update(
                    proposalID: proposalID,
                    pairingFingerprint: pairingFingerprint,
                    pairingGeneration: generation,
                    status: status,
                    shouldEnd: true
                )
            } catch let error as HealthMesAPIError where error.isAlreadyResolved {
                let status =
                    error.alreadyResolvedStatus.flatMap(activityStatus(forExisting:))
                    ?? .expired
                await update(
                    proposalID: proposalID,
                    pairingFingerprint: pairingFingerprint,
                    pairingGeneration: generation,
                    status: status,
                    shouldEnd: true
                )
            } catch let error as HealthMesAPIError where error.isProposalExpired {
                await update(
                    proposalID: proposalID,
                    pairingFingerprint: pairingFingerprint,
                    pairingGeneration: generation,
                    status: .expired,
                    shouldEnd: true
                )
            } catch {
                await update(
                    proposalID: proposalID,
                    pairingFingerprint: pairingFingerprint,
                    pairingGeneration: generation,
                    status: .failed,
                    shouldEnd: false
                )
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
            pairingFingerprint: String? = nil,
            pairingGeneration: UInt64? = nil,
            status: DecisionActivityStatus,
            shouldEnd: Bool
        ) async {
            for activity in Activity<DecisionActivityAttributes>.activities
            where activity.attributes.proposalID == proposalID
                && (
                    pairingFingerprint == nil
                        || activity.attributes.pairingFingerprint
                            == pairingFingerprint
                )
                && (
                    pairingGeneration == nil
                        || activity.attributes.pairingGeneration
                            == pairingGeneration
                )
            {
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
