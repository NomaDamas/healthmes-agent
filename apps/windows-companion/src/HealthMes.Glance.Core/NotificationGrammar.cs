using System.Text.Json;

namespace HealthMes.Glance.Core;

/// <summary>
/// The docs/PLAN.md §8.5 notification grammar, as data:
///
/// <code>
/// [observation, 1 line]
/// [evidence, 1 line]
/// [proposal, 1 line]
/// [link]     Why this? -> decision viewer URL
/// </code>
///
/// Pure C# so the mapping from a payload is unit-testable on any OS. The
/// glance mapping mirrors the Android companion's NotificationGrammar.kt
/// verbatim (same evidence/proposal wording) so the two desktop/mobile
/// surfaces phrase the same alert the same way.
///
/// PLACEHOLDER CONTENT: wording rules are the healthcare domain expert's
/// deliverable (docs/design/WATCH-NOTIFICATIONS.ko.md). Buttons (✅✏️❌) are
/// a phone/Telegram surface — the desktop toast carries the observation/
/// evidence/proposal lines plus the "why this?" decision link, which §1.1 of
/// the worksheet says no surface may drop.
/// </summary>
public sealed record NotificationGrammar
{
    public required string Observation { get; init; }

    public required string Evidence { get; init; }

    public required string Proposal { get; init; }

    public required string? DecisionUrl { get; init; }

    /// <summary>The three grammar lines, one per line (toast/flyout body).</summary>
    public string BodyText() => $"{Observation}\n{Evidence}\n{Proposal}";

    /// <summary>Null when the payload has no top alert to phrase.</summary>
    public static NotificationGrammar? FromGlance(GlancePayload payload)
    {
        var top = payload.Alerts.Top;
        if (top is null)
        {
            return null;
        }
        var score = GlanceFormat.ScoreText(payload.Energy.Score);
        var evidence =
            $"Rule {top.RuleId} fired · {payload.Alerts.UnresolvedCount} unresolved in 24h" +
            $" · energy {score} ({payload.Energy.Confidence.WireName()})";
        var proposal = top.DecisionUrl is not null
            ? "Open the decision record for the reasoning; reply in Telegram to adjust."
            : "Reply in Telegram to review and adjust today's plan.";
        return new NotificationGrammar
        {
            Observation = top.Summary,
            Evidence = evidence,
            Proposal = proposal,
            DecisionUrl = top.DecisionUrl,
        };
    }

    /// <summary>
    /// Grammar for one `GET /v1/alerts` item. Unlike the glance top alert,
    /// alert-history items carry the REAL evidence facts and proposal line
    /// recorded at fire time — those are used verbatim when present.
    /// </summary>
    public static NotificationGrammar FromAlert(AlertItem alert)
    {
        var evidence = RenderEvidence(alert.Evidence) ?? $"Rule {alert.RuleId} fired";
        var proposal = alert.Proposal
            ?? (alert.DecisionUrl is not null
                ? "Open the decision record for the reasoning; reply in Telegram to adjust."
                : "Reply in Telegram to review and adjust today's plan.");
        return new NotificationGrammar
        {
            Observation = alert.Summary,
            Evidence = evidence,
            Proposal = proposal,
            DecisionUrl = alert.DecisionUrl,
        };
    }

    /// <summary>
    /// "hrv_delta_pct -18 · baseline_days 14" out of the evidence facts
    /// object; null when there is nothing renderable.
    /// </summary>
    public static string? RenderEvidence(JsonElement? evidence)
    {
        if (evidence is not { ValueKind: JsonValueKind.Object } facts)
        {
            return null;
        }
        var parts = new List<string>();
        foreach (var property in facts.EnumerateObject())
        {
            var value = property.Value.ValueKind switch
            {
                JsonValueKind.String => property.Value.GetString(),
                JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False =>
                    property.Value.GetRawText(),
                _ => null, // nested objects/arrays/nulls stay off the one-line surface
            };
            if (value is not null)
            {
                parts.Add($"{property.Name} {value}");
            }
        }
        return parts.Count > 0 ? string.Join(" · ", parts) : null;
    }
}
