import Foundation

public enum WellnessDecisionProjector {
    public static func project(
        _ output: WellnessDecisionOutput,
        lens: WellnessLens = .now,
        timezone: String = "UTC",
        generatedAt: Date = Date()
    ) -> WellnessScene {
        let summary = primaryText(output)
        var modules = [
            WellnessSceneModule(
                id: "decision-result",
                kind: primaryModuleKind(output.status),
                title: title(output.status),
                summary: summary,
                items: textItems(output)
            )
        ]

        if !output.actions.isEmpty {
            modules.append(
                WellnessSceneModule(
                    id: "wellness-actions",
                    kind: .planImpact,
                    title: "Suggested wellness actions",
                    summary:
                        "These are wellness recommendations, not calendar mutations.",
                    items: output.actions.enumerated().map {
                        actionItem(index: $0.offset, action: $0.element)
                    }
                )
            )
        }

        if !output.sourceRefs.isEmpty {
            modules.append(
                WellnessSceneModule(
                    id: "evidence-coverage",
                    kind: .constraints,
                    title: "Validated evidence coverage",
                    summary:
                        "HealthMes used these source categories without exposing raw record identifiers.",
                    items: output.sourceRefs.enumerated().map {
                        sourceItem(index: $0.offset, source: $0.element)
                    }
                )
            )
        }

        return WellnessScene(
            id: "wellness-decision-\(output.requestID.uuidString.lowercased())",
            intent: "wellness_decision",
            lens: lens,
            title: title(output.status),
            summary: summary,
            severity: severity(output),
            freshness: freshness(output),
            confidence: confidence(output),
            modules: modules,
            actions: [
                WellnessSceneAction(
                    id: "refresh-decision",
                    kind: .refresh,
                    label: "Refresh"
                )
            ],
            generatedAt: generatedAt,
            timezone: timezone
        )
    }

    private static func title(_ status: WellnessDecisionStatus) -> String {
        switch status {
        case .completed:
            return "Wellness decision"
        case .needsClarification:
            return "More context needed"
        case .blocked:
            return "Decision blocked"
        case .failed:
            return "Decision unavailable"
        }
    }

    private static func primaryText(
        _ output: WellnessDecisionOutput
    ) -> String {
        [
            output.answer,
            output.clarificationQuestion,
            output.uncertainty,
            output.limitations.first,
        ]
        .compactMap(nonBlank)
        .first
            ?? "HealthMes did not return a displayable decision."
    }

    private static func primaryModuleKind(
        _ status: WellnessDecisionStatus
    ) -> WellnessModuleKind {
        switch status {
        case .completed:
            return .decision
        case .needsClarification:
            return .clarification
        case .blocked, .failed:
            return .fallback
        }
    }

    private static func textItems(
        _ output: WellnessDecisionOutput
    ) -> [WellnessSceneItem] {
        var items: [WellnessSceneItem] = []
        append(
            output.answer,
            id: "decision-answer",
            label: "Answer",
            to: &items
        )
        append(
            output.clarificationQuestion,
            id: "decision-clarification",
            label: "Clarification",
            to: &items
        )
        append(
            output.uncertainty,
            id: "decision-uncertainty",
            label: "Uncertainty",
            to: &items
        )
        if let confidence = output.confidence {
            items.append(
                WellnessSceneItem(
                    id: "decision-confidence",
                    label: "Confidence",
                    value: "\(Int((confidence * 100).rounded()))%"
                )
            )
        }
        for (index, limitation) in output.limitations.enumerated() {
            items.append(
                WellnessSceneItem(
                    id: "decision-limitation-\(index)",
                    label: "Limitation",
                    value: limitation
                )
            )
        }
        append(
            output.followUpQuestion,
            id: "decision-follow-up",
            label: "Follow-up",
            to: &items
        )
        return items
    }

    private static func actionItem(
        index: Int,
        action: WellnessDecisionAction
    ) -> WellnessSceneItem {
        let value: String
        switch action.kind {
        case .walk:
            if let minutes = action.durationMinutes {
                value = "Take a \(minutes)-minute walk"
            } else {
                value = "Take a short walk"
            }
        case .drinkWater:
            value = "Drink water"
        case .sleepPreparation:
            if let minutes = action.advanceMinutes {
                value = "Start sleep preparation \(minutes) minutes earlier"
            } else {
                value = "Start sleep preparation earlier"
            }
        }
        return WellnessSceneItem(
            id: "wellness-action-\(index)",
            label: "Suggested action",
            value: value,
            detail: action.state.rawValue.replacingOccurrences(
                of: "_",
                with: " "
            )
        )
    }

    private static func sourceItem(
        index: Int,
        source: WellnessDecisionSourceRef
    ) -> WellnessSceneItem {
        let detailParts = [
            source.freshness.rawValue,
            source.coverage.map {
                "\(Int(($0 * 100).rounded()))% coverage"
            },
        ].compactMap { $0 }
        return WellnessSceneItem(
            id: "decision-source-\(index)",
            label: source.domain,
            value:
                "\(source.sourceProvider) · \(source.resourceType.replacingOccurrences(of: "_", with: " "))",
            detail: detailParts.isEmpty
                ? nil
                : detailParts.joined(separator: " · ")
        )
    }

    private static func severity(
        _ output: WellnessDecisionOutput
    ) -> WellnessSceneSeverity {
        switch output.status {
        case .completed:
            return output.proposedAction ? .action : .supportive
        case .needsClarification:
            return .neutral
        case .blocked, .failed:
            return .caution
        }
    }

    private static func freshness(
        _ output: WellnessDecisionOutput
    ) -> WellnessFreshness {
        guard output.status == .completed else {
            return .insufficientData
        }
        if output.sourceRefs.contains(where: { $0.freshness == .stale }) {
            return .stale
        }
        if output.sourceRefs.contains(where: {
            $0.freshness == .unavailable
        }) {
            return .insufficientData
        }
        return .current
    }

    private static func confidence(
        _ output: WellnessDecisionOutput
    ) -> WellnessConfidence {
        let level: WellnessConfidence.Level
        if let value = output.confidence {
            if value >= 0.8 {
                level = .high
            } else if value >= 0.5 {
                level = .medium
            } else {
                level = .low
            }
        } else {
            level = .insufficientData
        }
        let sourceCount = output.sourceRefs.count
        return WellnessConfidence(
            level: level,
            coverage:
                sourceCount == 1
                ? "1 validated source"
                : "\(sourceCount) validated sources",
            limitations: output.limitations
        )
    }

    private static func append(
        _ value: String?,
        id: String,
        label: String,
        to items: inout [WellnessSceneItem]
    ) {
        guard let value = nonBlank(value) else { return }
        items.append(
            WellnessSceneItem(
                id: id,
                label: label,
                value: value
            )
        )
    }

    private static func nonBlank(_ value: String?) -> String? {
        guard
            let value = value?.trimmingCharacters(
                in: .whitespacesAndNewlines
            ),
            !value.isEmpty
        else { return nil }
        return value
    }
}
