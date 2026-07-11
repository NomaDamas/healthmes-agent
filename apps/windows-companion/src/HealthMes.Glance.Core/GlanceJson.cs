using System.Text.Json;
using System.Text.Json.Serialization;

namespace HealthMes.Glance.Core;

/// <summary>
/// JSON (de)coding pinned to the server's shapes (mirrors GlanceJSON in
/// apps/ios-companion). Enum vocabularies are strict: an unknown string (or
/// a numeric value) throws instead of rendering garbage. Unknown OBJECT
/// members are skipped — additive server fields must not brick installed
/// desktop surfaces (same stance as the Swift/Kotlin parsers).
/// </summary>
public static class GlanceJson
{
    public static JsonSerializerOptions Options { get; } = CreateOptions();

    private static JsonSerializerOptions CreateOptions()
    {
        var options = new JsonSerializerOptions
        {
            // Contract names are pinned with [JsonPropertyName]; no policy.
            PropertyNamingPolicy = null,
            Converters =
            {
                new JsonStringEnumConverter(JsonNamingPolicy.CamelCase, allowIntegerValues: false),
            },
        };
        return options;
    }

    /// <summary>
    /// Strict parse of a glance payload. Enforces the one structural
    /// invariant the type system alone does not pin: <c>curve_24h</c> always
    /// carries exactly 24 hourly entries (briefing.py contract; the same
    /// assertion lives in tests/api/test_glance_fixtures.py server-side).
    /// </summary>
    /// <exception cref="JsonException">Contract break (missing key, unknown enum value, wrong shape).</exception>
    public static GlancePayload DeserializeGlance(string json)
    {
        var payload = JsonSerializer.Deserialize<GlancePayload>(json, Options)
            ?? throw new JsonException("glance payload was JSON null");
        if (payload.Energy.Curve24h.Count != 24)
        {
            throw new JsonException(
                $"energy.curve_24h must have exactly 24 entries, got {payload.Energy.Curve24h.Count}");
        }
        return payload;
    }

    public static AlertsPage DeserializeAlertsPage(string json) =>
        JsonSerializer.Deserialize<AlertsPage>(json, Options)
            ?? throw new JsonException("alerts page was JSON null");

    public static WeeklyReportInfo DeserializeWeeklyReportInfo(string json) =>
        JsonSerializer.Deserialize<WeeklyReportInfo>(json, Options)
            ?? throw new JsonException("weekly report was JSON null");
}
