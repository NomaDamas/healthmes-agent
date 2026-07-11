using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// PLAN.md §8.5 grammar mapping — the same assertions the Android suite
/// makes in NotificationGrammarTest.kt, against the same glance_full.json
/// bytes, so both platforms phrase an alert identically.
/// </summary>
public class NotificationGrammarTests
{
    [Fact]
    public void PhrasesTheTopAlertAsObservationEvidenceProposal()
    {
        var grammar = NotificationGrammar.FromGlance(
            GlanceJson.DeserializeGlance(Fixtures.GlanceFull()))!;

        Assert.Equal("Stress spiked 45% above your 14-day baseline", grammar.Observation);
        Assert.Contains("stress_spike", grammar.Evidence);
        Assert.Contains("2 unresolved", grammar.Evidence);
        Assert.Contains("energy 72 (medium)", grammar.Evidence);
        Assert.Contains("decision record", grammar.Proposal);
        Assert.Equal(
            "http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123",
            grammar.DecisionUrl);
        Assert.Equal(
            new[] { grammar.Observation, grammar.Evidence, grammar.Proposal },
            grammar.BodyText().Split('\n'));
    }

    [Fact]
    public void NoTopAlertMeansNothingToPhrase()
    {
        Assert.Null(NotificationGrammar.FromGlance(
            GlanceJson.DeserializeGlance(Fixtures.GlanceEmpty())));
    }

    [Fact]
    public void ProposalAdaptsWhenTheAlertHasNoDecisionUrl()
    {
        var json = Fixtures.GlanceFull().Replace(
            "\"decision_url\": \"http://192.168.1.20:8100/decisions/0b8f3e0a-2b9f-4c47-a9d4-2f2b7f6f3a11?token=viewer-abc123\"",
            "\"decision_url\": null");

        var grammar = NotificationGrammar.FromGlance(GlanceJson.DeserializeGlance(json))!;

        Assert.Null(grammar.DecisionUrl);
        Assert.Contains("Telegram", grammar.Proposal);
    }

    [Fact]
    public void AlertHistoryItemsUseTheRecordedLinesVerbatim()
    {
        var item = GlanceJson.DeserializeAlertsPage(Fixtures.AlertsPage()).Data[0];

        var grammar = NotificationGrammar.FromAlert(item);

        Assert.Equal("Stress 82 vs baseline 55", grammar.Observation);
        Assert.Equal("hrv_delta_pct -18 · baseline_days 14", grammar.Evidence);
        Assert.Equal("Move the 14:00 block to tomorrow.", grammar.Proposal); // server line, untouched
        Assert.Equal(item.DecisionUrl, grammar.DecisionUrl);
    }

    [Fact]
    public void LegacyAlertItemsGetHonestFallbackLines()
    {
        var legacy = GlanceJson.DeserializeAlertsPage(Fixtures.AlertsPage()).Data[1];

        var grammar = NotificationGrammar.FromAlert(legacy);

        Assert.Equal("legacy_rule", grammar.Observation);
        Assert.Equal("Rule legacy_rule fired", grammar.Evidence);
        Assert.Contains("Telegram", grammar.Proposal);
        Assert.Null(grammar.DecisionUrl);
    }

    [Fact]
    public void EvidenceRenderingSkipsNestedValues()
    {
        using var document = System.Text.Json.JsonDocument.Parse(
            "{\"delta\": -18, \"label\": \"hrv\", \"nested\": {\"x\": 1}, \"flag\": true}");

        var line = NotificationGrammar.RenderEvidence(document.RootElement.Clone());

        Assert.Equal("delta -18 · label hrv · flag true", line);
    }
}
