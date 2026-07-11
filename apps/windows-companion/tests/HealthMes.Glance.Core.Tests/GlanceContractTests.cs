using System.Text.Json;
using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// The glance fixtures are the SAME bytes the iOS and Android suites pin
/// (and tests/api/test_glance_fixtures.py validates against the live server
/// model) — these tests prove the C# parser reads that exact contract.
/// </summary>
public class GlanceContractTests
{
    [Fact]
    public void ParsesTheIosReferencePayload()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());

        Assert.Equal(DateTimeOffset.Parse("2026-07-09T14:23:00Z"), payload.GeneratedAt);
        Assert.Equal("UTC", payload.Timezone);
        Assert.Equal(58, payload.Energy.Score);
        Assert.Equal(GlanceConfidence.High, payload.Energy.Confidence);
        Assert.Equal(24, payload.Energy.Curve24h.Count);
        Assert.Equal(71, payload.Energy.Curve24h[8].Score);
        Assert.Null(payload.Energy.Curve24h[0].Score);

        Assert.Equal(3, payload.NextBlocks.Count);
        var first = payload.NextBlocks[0];
        Assert.Equal("Deep work block", first.Title);
        Assert.Equal(GlanceEnergyDemand.High, first.EnergyDemand);
        Assert.Equal(GlanceBlockSource.Calendar, first.Source);
        Assert.Equal(GlanceBlockSource.Proposal, payload.NextBlocks[1].Source);
        Assert.Null(payload.NextBlocks[2].Title);
        Assert.Null(payload.NextBlocks[2].EnergyDemand);

        Assert.Equal(2, payload.Alerts.UnresolvedCount);
        var top = Assert.IsType<GlanceTopAlert>(payload.Alerts.Top);
        Assert.Equal("stress_spike_vs_baseline", top.RuleId);
        Assert.Equal("Stress 82 vs baseline 55", top.Summary);
        Assert.StartsWith("http://192.168.1.20:8100/decisions/", top.DecisionUrl);

        var decision = Assert.IsType<GlanceDecision>(payload.LatestDecision);
        Assert.Equal(Guid.Parse("7e6a1b2c-93d4-4f58-a1c0-5b8e2f7d9a34"), decision.Id);
    }

    [Fact]
    public void ParsesTheAndroidFullPayload()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceFull());

        Assert.Equal("Asia/Seoul", payload.Timezone);
        Assert.Equal(72, payload.Energy.Score);
        Assert.Equal(GlanceConfidence.Medium, payload.Energy.Confidence);
        Assert.Equal(GlanceEnergyDemand.Low, payload.NextBlocks[2].EnergyDemand);
        Assert.Equal("stress_spike", payload.Alerts.Top!.RuleId);
    }

    [Fact]
    public void ParsesTheHonestEmptyPayload()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceEmpty());

        Assert.Null(payload.Energy.Score);
        Assert.Equal(GlanceConfidence.Low, payload.Energy.Confidence);
        Assert.All(payload.Energy.Curve24h, point => Assert.Null(point.Score));
        Assert.Empty(payload.NextBlocks);
        Assert.Equal(0, payload.Alerts.UnresolvedCount);
        Assert.Null(payload.Alerts.Top);
        Assert.Null(payload.LatestDecision);
    }

    [Fact]
    public void UnknownEnumValueIsAContractBreak()
    {
        var json = Fixtures.GlanceIos().Replace("\"confidence\": \"high\"", "\"confidence\": \"critical\"");
        Assert.Throws<JsonException>(() => GlanceJson.DeserializeGlance(json));
    }

    [Fact]
    public void NumericEnumValueIsAContractBreak()
    {
        var json = Fixtures.GlanceIos().Replace("\"confidence\": \"high\"", "\"confidence\": 1");
        Assert.Throws<JsonException>(() => GlanceJson.DeserializeGlance(json));
    }

    [Fact]
    public void MissingRequiredKeyIsAContractBreak()
    {
        var json = Fixtures.GlanceIos().Replace("\"unresolved_count\": 2,", "");
        Assert.Throws<JsonException>(() => GlanceJson.DeserializeGlance(json));
    }

    [Fact]
    public void ShortCurveIsAContractBreak()
    {
        var json = Fixtures.GlanceIos().Replace("      {\"hour\": 23, \"score\": null}\n", "");
        var error = Assert.Throws<JsonException>(() => GlanceJson.DeserializeGlance(json));
        Assert.Contains("24", error.Message);
    }

    [Fact]
    public void UnknownAdditiveFieldsAreSkippedNotFatal()
    {
        var json = Fixtures.GlanceIos().Replace(
            "\"timezone\": \"UTC\",",
            "\"timezone\": \"UTC\", \"future_field\": {\"nested\": [1, 2]},");
        Assert.Equal("UTC", GlanceJson.DeserializeGlance(json).Timezone);
    }
}
