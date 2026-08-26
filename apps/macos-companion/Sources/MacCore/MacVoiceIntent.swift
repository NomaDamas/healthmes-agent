import Foundation

public enum MacVoiceIntent: Equatable {
    case showToday
    case showPlan
    case showDecisions
    case showSettings
    case refresh
    case taskDraft(String)
    case goalDraft(String)
}

/// Deterministic, side-effect-free routing for the Mac voice surface.
/// Navigation commands can execute locally; mutating utterances require an
/// explicit task/goal prefix and still require an explicit confirmation click.
public enum MacVoiceIntentParser {
    public static func parse(_ transcript: String) -> MacVoiceIntent? {
        let trimmed = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let normalized = trimmed.lowercased()

        if let title = strippedTitle(
            original: trimmed,
            normalized: normalized,
            prefixes: ["주간 목표 ", "목표 ", "weekly goal ", "goal "]
        ) {
            return .goalDraft(title)
        }
        if let title = strippedTitle(
            original: trimmed,
            normalized: normalized,
            prefixes: [
                "할 일 ", "할일 ", "작업 ", "기억해 ", "task ", "todo ", "remember to ",
            ]
        ) {
            return .taskDraft(title)
        }

        if containsAny(normalized, ["새로고침", "업데이트", "refresh", "reload"]) {
            return .refresh
        }
        if containsAny(normalized, ["설정", "settings", "connection", "연결"]) {
            return .showSettings
        }
        if containsAny(normalized, ["결정", "제안", "decisions", "decision"]) {
            return .showDecisions
        }
        if containsAny(normalized, ["계획", "일정", "plan", "calendar", "schedule"]) {
            return .showPlan
        }
        if containsAny(normalized, ["오늘", "상태", "today", "how am i"]) {
            return .showToday
        }

        return nil
    }

    private static func containsAny(_ text: String, _ needles: [String]) -> Bool {
        needles.contains(where: text.contains)
    }

    private static func strippedTitle(
        original: String,
        normalized: String,
        prefixes: [String]
    ) -> String? {
        guard let prefix = prefixes.first(where: normalized.hasPrefix) else { return nil }
        let title = String(original.dropFirst(prefix.count))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? nil : title
    }
}
