using System.Globalization;

namespace HealthMes.Glance.Core;

/// <summary>
/// Tiny, deliberately plain text renderers over the glance payload —
/// the C# twin of GlanceFormat.swift (iOS) / BriefingDisplayState (Android).
///
/// NOTE (issues #7/#11): these strings are PLACEHOLDER plumbing. What a
/// glance surface should actually say — wording, urgency, thresholds, when
/// to stay silent — is healthcare-domain UX reserved for the domain expert
/// (docs/design/WATCH-NOTIFICATIONS.ko.md; grammar: docs/PLAN.md §8.5).
/// </summary>
public static class GlanceFormat
{
    public const string NoScore = "--";

    /// <summary>Lowercase wire vocabulary ("high"/"medium"/"low").</summary>
    public static string WireName(this GlanceConfidence confidence) =>
        confidence.ToString().ToLowerInvariant();

    /// <summary>Lowercase wire vocabulary ("low"/"med"/"high").</summary>
    public static string WireName(this GlanceEnergyDemand demand) =>
        demand.ToString().ToLowerInvariant();

    public static string ScoreText(int? score) =>
        score?.ToString(CultureInfo.InvariantCulture) ?? NoScore;

    /// <summary>"Energy 58 · high"</summary>
    public static string EnergyLine(GlancePayload payload) =>
        $"Energy {ScoreText(payload.Energy.Score)} · {payload.Energy.Confidence.WireName()}";

    /// <summary>
    /// "14:00 Deep work block [high]" — times rendered in the SERVER's user
    /// timezone so desktop clock drift never lies about the plan. Unknown
    /// IANA ids fall back to the machine's local zone (mirrors iOS).
    /// </summary>
    public static string BlockLine(GlanceBlock block, string timezone)
    {
        var zone = ResolveTimeZone(timezone);
        var local = TimeZoneInfo.ConvertTime(block.Start, zone);
        var title = block.Title
            ?? (block.Source == GlanceBlockSource.Proposal ? "Proposed block" : "Untitled block");
        var line = $"{local.ToString("HH:mm", CultureInfo.InvariantCulture)} {title}";
        if (block.EnergyDemand is { } demand)
        {
            line += $" [{demand.WireName()}]";
        }
        return line;
    }

    public static string? NextBlockLine(GlancePayload payload) =>
        payload.NextBlocks.Count > 0 ? BlockLine(payload.NextBlocks[0], payload.Timezone) : null;

    /// <summary>"2 alerts · Stress 82 vs baseline 55" / "No recent alerts"</summary>
    public static string AlertsLine(GlancePayload payload)
    {
        var count = payload.Alerts.UnresolvedCount;
        if (count <= 0)
        {
            return "No recent alerts";
        }
        var noun = count == 1 ? "alert" : "alerts";
        var top = payload.Alerts.Top;
        return top is not null && top.Summary.Length > 0
            ? $"{count} {noun} · {top.Summary}"
            : $"{count} {noun}";
    }

    /// <summary>Single-line variant for one-slot surfaces: "HM 58 · 2!"</summary>
    public static string InlineLine(GlancePayload payload)
    {
        var line = $"HM {ScoreText(payload.Energy.Score)}";
        if (payload.Alerts.UnresolvedCount > 0)
        {
            line += $" · {payload.Alerts.UnresolvedCount}!";
        }
        return line;
    }

    /// <summary>
    /// Tray tooltip: "HealthMes · Energy 58 · high · 2 alerts", clamped to
    /// the 127-char NotifyIcon.Text budget.
    /// </summary>
    public static string TrayTooltip(GlancePayload? payload)
    {
        if (payload is null)
        {
            return "HealthMes · not paired";
        }
        var count = payload.Alerts.UnresolvedCount;
        var alerts = count == 1 ? "1 alert" : $"{count} alerts";
        var text = $"HealthMes · {EnergyLine(payload)} · {alerts}";
        return text.Length <= 127 ? text : text[..127];
    }

    public static TimeZoneInfo ResolveTimeZone(string timezone)
    {
        try
        {
            // .NET 8 resolves IANA ids on every OS (ICU); "UTC" also works.
            return TimeZoneInfo.FindSystemTimeZoneById(timezone);
        }
        catch (Exception error) when (error is TimeZoneNotFoundException or InvalidTimeZoneException)
        {
            return TimeZoneInfo.Local;
        }
    }
}
