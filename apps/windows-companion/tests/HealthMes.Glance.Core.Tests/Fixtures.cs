namespace HealthMes.Glance.Core.Tests;

/// <summary>Loads the pinned contract fixtures copied next to the test binary.</summary>
public static class Fixtures
{
    public static string Read(string name) =>
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", name));

    /// <summary>iOS reference payload (apps/ios-companion/Tests/Fixtures/glance.json).</summary>
    public static string GlanceIos() => Read("glance.json");

    /// <summary>Android "full" payload (companion/src/test/resources/glance_full.json).</summary>
    public static string GlanceFull() => Read("glance_full.json");

    /// <summary>Android "honest empty" payload (all-null energy, no blocks/alerts).</summary>
    public static string GlanceEmpty() => Read("glance_empty.json");

    public static string AlertsPage() => Read("alerts_page.json");

    public static string WeeklyReport() => Read("weekly_report.json");
}
