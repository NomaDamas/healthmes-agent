namespace HealthMes.Glance.Core.Tests;

/// <summary>Loads the pinned contract fixtures copied next to the test binary.</summary>
public static class Fixtures
{
    // Normalize to LF: Windows checkouts rewrite the fixtures to CRLF
    // (core.autocrlf), which silently breaks tests that splice the fixture
    // text with "\n"-terminated fragments (seen live on the first
    // windows-apps.yml run: ShortCurveIsAContractBreak never matched).
    public static string Read(string name) =>
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", name))
            .Replace("\r\n", "\n");

    /// <summary>iOS reference payload (apps/ios-companion/Tests/Fixtures/glance.json).</summary>
    public static string GlanceIos() => Read("glance.json");

    /// <summary>Android "full" payload (companion/src/test/resources/glance_full.json).</summary>
    public static string GlanceFull() => Read("glance_full.json");

    /// <summary>Android "honest empty" payload (all-null energy, no blocks/alerts).</summary>
    public static string GlanceEmpty() => Read("glance_empty.json");

    public static string AlertsPage() => Read("alerts_page.json");

    public static string WeeklyReport() => Read("weekly_report.json");
}
