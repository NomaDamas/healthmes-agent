using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// Placeholder text renderers — pinned to the exact strings the iOS
/// GlanceFormat produces so surfaces stay consistent across platforms.
/// </summary>
public class GlanceFormatTests
{
    [Fact]
    public void EnergyLineMatchesTheIosRendering()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal("Energy 58 · high", GlanceFormat.EnergyLine(payload));
    }

    [Fact]
    public void BlockLineRendersInTheServerTimezone()
    {
        // iOS fixture: timezone UTC, block start 14:00Z -> "14:00".
        var utcPayload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal("14:00 Deep work block [high]", GlanceFormat.NextBlockLine(utcPayload));

        // Android fixture: timezone Asia/Seoul, block start 05:00Z -> 14:00 KST.
        var seoulPayload = GlanceJson.DeserializeGlance(Fixtures.GlanceFull());
        Assert.Equal("14:00 Deep work: PLAN review [high]", GlanceFormat.NextBlockLine(seoulPayload));
    }

    [Fact]
    public void UntitledAndProposedBlocksGetHonestFallbackTitles()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal(
            "16:00 Untitled block",
            GlanceFormat.BlockLine(payload.NextBlocks[2], payload.Timezone));

        var proposal = payload.NextBlocks[1] with { Title = null };
        Assert.Equal(
            "15:00 Proposed block [med]",
            GlanceFormat.BlockLine(proposal, payload.Timezone));
    }

    [Fact]
    public void AlertsLineCoversPluralSingularAndQuiet()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal("2 alerts · Stress 82 vs baseline 55", GlanceFormat.AlertsLine(payload));

        var single = payload with
        {
            Alerts = payload.Alerts with { UnresolvedCount = 1 },
        };
        Assert.StartsWith("1 alert ·", GlanceFormat.AlertsLine(single));

        var quiet = GlanceJson.DeserializeGlance(Fixtures.GlanceEmpty());
        Assert.Equal("No recent alerts", GlanceFormat.AlertsLine(quiet));
    }

    [Fact]
    public void InlineLineMatchesTheIosRendering()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal("HM 58 · 2!", GlanceFormat.InlineLine(payload));

        var quiet = GlanceJson.DeserializeGlance(Fixtures.GlanceEmpty());
        Assert.Equal("HM --", GlanceFormat.InlineLine(quiet));
    }

    [Fact]
    public void TrayTooltipIsHonestAndFitsTheNotifyIconBudget()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        Assert.Equal("HealthMes · Energy 58 · high · 2 alerts", GlanceFormat.TrayTooltip(payload));
        Assert.Equal("HealthMes · not paired", GlanceFormat.TrayTooltip(null));

        var noisy = payload with
        {
            Alerts = payload.Alerts with { UnresolvedCount = int.MaxValue },
        };
        Assert.True(GlanceFormat.TrayTooltip(noisy).Length <= 127);
    }

    [Fact]
    public void MissingScoreRendersAsDashes()
    {
        Assert.Equal("--", GlanceFormat.ScoreText(null));
        Assert.Equal("58", GlanceFormat.ScoreText(58));
    }

    [Fact]
    public void UnknownTimezoneFallsBackToLocalInsteadOfCrashing()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        var block = payload.NextBlocks[0];
        // Must not throw; exact hour depends on the machine's zone.
        var line = GlanceFormat.BlockLine(block, "Not/AZone");
        Assert.EndsWith("Deep work block [high]", line);
    }
}
