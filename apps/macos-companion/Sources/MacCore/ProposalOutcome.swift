import Foundation

/// What happened when the user tapped a §8.5 button (✅ Apply / ❌ Keep) —
/// pure mapping from the API result so the popover and the notification
/// handler render identical outcomes, and the 409 story is unit-tested.
public enum ProposalOutcome: Equatable {
    case applied
    case kept
    /// Server answered 409 invalid_transition: someone (Telegram, phone,
    /// another surface) resolved it first. `status` is the server's
    /// `detail.current` ("accepted"/"declined"/"pushed").
    case alreadyResolved(status: String)
    case failed

    public static func from(action: ProposalAction, error: HealthMesAPIError?) -> ProposalOutcome {
        guard let error else {
            return action == .accept ? .applied : .kept
        }
        if error.isAlreadyResolved {
            return .alreadyResolved(status: error.alreadyResolvedStatus ?? "resolved")
        }
        return .failed
    }
}
