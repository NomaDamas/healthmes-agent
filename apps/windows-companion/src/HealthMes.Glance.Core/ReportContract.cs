using System.Text.Json.Serialization;

namespace HealthMes.Glance.Core;

/// <summary>
/// Envelope of <c>GET /reports/weekly.json</c> (healthmes/api/reports.py,
/// <c>WeeklyReportOut</c>) — deliberately ONLY the stable envelope fields the
/// desktop glance surfaces need. The desktop use case is "open the weekly
/// report page in the browser": the app fetches this JSON with its bearer
/// token and hands <see cref="ReportUrl"/> (which embeds the derived
/// read-only viewer <c>?token=</c>) to the OS browser. Rendering the report's
/// deep sections natively is phone-app scope (issue #10); unknown fields are
/// skipped on purpose here so this envelope never breaks on report growth.
/// </summary>
public sealed record WeeklyReportInfo
{
    [JsonPropertyName("generated_at")]
    public required DateTimeOffset GeneratedAt { get; init; }

    [JsonPropertyName("timezone")]
    public required string Timezone { get; init; }

    [JsonPropertyName("week_start")]
    public required DateOnly WeekStart { get; init; }

    [JsonPropertyName("week_end")]
    public required DateOnly WeekEnd { get; init; }

    /// <summary>Browser-tappable weekly-report page link (viewer token embedded).</summary>
    [JsonPropertyName("report_url")]
    public required string ReportUrl { get; init; }
}
