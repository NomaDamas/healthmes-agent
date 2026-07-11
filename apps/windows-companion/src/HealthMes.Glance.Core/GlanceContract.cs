using System.Text.Json.Serialization;

namespace HealthMes.Glance.Core;

// Contract for `GET /v1/briefing/glance` (healthmes/api/briefing.py).
//
// Field names and value vocabularies are pinned VERBATIM to the server schema
// via [JsonPropertyName] — no naming-policy magic. The same reference
// payloads used by the iOS and Android companions are copied byte-identical
// into tests/HealthMes.Glance.Core.Tests/Fixtures and pinned server-side by
// tests/api/test_glance_fixtures.py. Parsing is strict on purpose: a missing
// key (`required`) or an unknown enum value is a contract break we want to
// see, not silently render (mirrors apps/ios-companion GlanceContract.swift).

/// <summary>Response of <c>GET /v1/briefing/glance</c>.</summary>
public sealed record GlancePayload
{
    /// <summary>Server-side generation instant (aware UTC).</summary>
    [JsonPropertyName("generated_at")]
    public required DateTimeOffset GeneratedAt { get; init; }

    /// <summary>User timezone (IANA name when HEALTHMES_TIMEZONE is set).</summary>
    [JsonPropertyName("timezone")]
    public required string Timezone { get; init; }

    [JsonPropertyName("energy")]
    public required GlanceEnergy Energy { get; init; }

    /// <summary>0..3 upcoming/ongoing blocks, soonest first.</summary>
    [JsonPropertyName("next_blocks")]
    public required IReadOnlyList<GlanceBlock> NextBlocks { get; init; }

    [JsonPropertyName("alerts")]
    public required GlanceAlerts Alerts { get; init; }

    [JsonPropertyName("latest_decision")]
    public required GlanceDecision? LatestDecision { get; init; }
}

/// <summary>
/// Today's persisted cognitive-energy picture. Missing hours are honest
/// nulls — the server never computes energy on demand for a widget poll.
/// </summary>
public sealed record GlanceEnergy
{
    /// <summary>Latest persisted window at/before now; null when nothing persisted.</summary>
    [JsonPropertyName("score")]
    public required int? Score { get; init; }

    /// <summary>Freshness ladder over the latest window's age.</summary>
    [JsonPropertyName("confidence")]
    public required GlanceConfidence Confidence { get; init; }

    /// <summary>Exactly 24 entries, local wall-clock hour ascending.</summary>
    [JsonPropertyName("curve_24h")]
    public required IReadOnlyList<GlanceCurvePoint> Curve24h { get; init; }
}

public enum GlanceConfidence
{
    High,
    Medium,
    Low,
}

public sealed record GlanceCurvePoint
{
    /// <summary>Hour of the user's local day, 0-23.</summary>
    [JsonPropertyName("hour")]
    public required int Hour { get; init; }

    [JsonPropertyName("score")]
    public required int? Score { get; init; }
}

/// <summary>One upcoming block: a mirrored calendar event or an accepted proposal.</summary>
public sealed record GlanceBlock
{
    [JsonPropertyName("start")]
    public required DateTimeOffset Start { get; init; }

    [JsonPropertyName("end")]
    public required DateTimeOffset End { get; init; }

    [JsonPropertyName("title")]
    public required string? Title { get; init; }

    [JsonPropertyName("energy_demand")]
    public required GlanceEnergyDemand? EnergyDemand { get; init; }

    [JsonPropertyName("source")]
    public required GlanceBlockSource Source { get; init; }
}

/// <summary>Mirror of healthmes.store.enums.EnergyDemand ("low"/"med"/"high").</summary>
public enum GlanceEnergyDemand
{
    Low,
    Med,
    High,
}

public enum GlanceBlockSource
{
    Calendar,
    Proposal,
}

public sealed record GlanceAlerts
{
    /// <summary>
    /// Recent pushed alerts (server-side placeholder policy: recency stands
    /// in for resolution until the domain expert defines one).
    /// </summary>
    [JsonPropertyName("unresolved_count")]
    public required int UnresolvedCount { get; init; }

    [JsonPropertyName("top")]
    public required GlanceTopAlert? Top { get; init; }
}

public sealed record GlanceTopAlert
{
    [JsonPropertyName("rule_id")]
    public required string RuleId { get; init; }

    [JsonPropertyName("summary")]
    public required string Summary { get; init; }

    /// <summary>
    /// Browser-tappable decision-viewer link (read-only ?token= embedded
    /// server-side when the instance is token-gated); null when the alert has
    /// no recorded decision yet.
    /// </summary>
    [JsonPropertyName("decision_url")]
    public required string? DecisionUrl { get; init; }
}

public sealed record GlanceDecision
{
    [JsonPropertyName("id")]
    public required Guid Id { get; init; }

    [JsonPropertyName("url")]
    public required string Url { get; init; }
}
