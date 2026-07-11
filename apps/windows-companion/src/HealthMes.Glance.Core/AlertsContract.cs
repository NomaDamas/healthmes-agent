using System.Text.Json;
using System.Text.Json.Serialization;

namespace HealthMes.Glance.Core;

// Contract for `GET /v1/alerts` (healthmes/api/alerts.py, issues #10/#11):
// recent PUSHED trigger events with the same "unresolved == recent"
// placeholder semantics as the glance `alerts` block. Each item carries the
// §8.5 grammar lines recorded at fire time (observation `summary`, `evidence`
// facts, `proposal`) plus the "why this?" decision-viewer deep link — the
// server pins that alerts[0] and the glance top alert agree verbatim.

/// <summary>One pushed alert, shaped after the §8.5 notification grammar.</summary>
public sealed record AlertItem
{
    [JsonPropertyName("id")]
    public required Guid Id { get; init; }

    [JsonPropertyName("rule_id")]
    public required string RuleId { get; init; }

    [JsonPropertyName("fired_at")]
    public required DateTimeOffset FiredAt { get; init; }

    /// <summary>Observation line (server falls back to rule_id on legacy payload-less rows).</summary>
    [JsonPropertyName("summary")]
    public required string Summary { get; init; }

    /// <summary>Proposal line; null when the rule recorded none.</summary>
    [JsonPropertyName("proposal")]
    public required string? Proposal { get; init; }

    /// <summary>Evidence facts (free-form object; the client renders the line).</summary>
    [JsonPropertyName("evidence")]
    public required JsonElement? Evidence { get; init; }

    /// <summary>"Why this?" decision-viewer deep link (viewer token embedded server-side).</summary>
    [JsonPropertyName("decision_url")]
    public required string? DecisionUrl { get; init; }
}

/// <summary>Pagination block of every healthmes list endpoint (healthmes/api/pagination.py).</summary>
public sealed record PageMeta
{
    [JsonPropertyName("total_count")]
    public required int TotalCount { get; init; }

    [JsonPropertyName("limit")]
    public required int Limit { get; init; }

    [JsonPropertyName("offset")]
    public required int Offset { get; init; }

    [JsonPropertyName("has_more")]
    public required bool HasMore { get; init; }
}

/// <summary>Standard list envelope: <c>{"data": [...], "pagination": {...}}</c>.</summary>
public sealed record AlertsPage
{
    [JsonPropertyName("data")]
    public required IReadOnlyList<AlertItem> Data { get; init; }

    [JsonPropertyName("pagination")]
    public required PageMeta Pagination { get; init; }
}
