import Foundation

/// What happened when the user resolved a schedule proposal —
/// pure mapping from the API result so the popover and the notification
/// handler render identical outcomes, and the 409 story is unit-tested.
public enum ProposalOutcome: Equatable {
    case accepted
    case applied
    case kept
    case expired
    /// Server answered 409 invalid_transition: someone (Telegram, phone,
    /// another surface) resolved it first. `status` is the server's
    /// `detail.current` ("accepted"/"declined"/"pushed").
    case alreadyResolved(status: String)
    case failed

    public static func from(
        action _: ProposalAction,
        resolvedStatus: ProposalStatus? = nil,
        error: HealthMesAPIError?
    ) -> ProposalOutcome {
        if let error {
            if error.isAlreadyResolved {
                return .alreadyResolved(status: error.alreadyResolvedStatus ?? "resolved")
            }
            if error.isProposalExpired {
                return .expired
            }
            return .failed
        }
        switch resolvedStatus {
        case .pushed:
            return .applied
        case .invalidated:
            return .expired
        case .declined:
            return .kept
        case .accepted:
            return .accepted
        case .proposed, nil:
            return .failed
        }
    }
}
