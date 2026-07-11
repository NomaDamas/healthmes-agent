using System.Text.Json;
using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// Adaptive Card mapping for the (deferred, see DEFERRED.md) Widgets Board
/// provider: the data→slots translation is proven now so the provider is
/// pure plumbing later.
/// </summary>
public class WidgetCardTests
{
    [Fact]
    public void CardCarriesTheGlanceSlotsAndWhyThisAction()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());

        using var card = JsonDocument.Parse(WidgetCard.BuildJson(payload));
        var root = card.RootElement;

        Assert.Equal("AdaptiveCard", root.GetProperty("type").GetString());
        Assert.Equal("1.5", root.GetProperty("version").GetString());

        var texts = root.GetProperty("body").EnumerateArray()
            .Select(block => block.GetProperty("text").GetString())
            .ToList();
        Assert.Contains("Energy 58 · high", texts);
        Assert.Contains("14:00 Deep work block [high]", texts);
        Assert.Contains("2 alerts · Stress 82 vs baseline 55", texts);

        var action = root.GetProperty("actions").EnumerateArray().Single();
        Assert.Equal("Action.OpenUrl", action.GetProperty("type").GetString());
        Assert.Equal(payload.Alerts.Top!.DecisionUrl, action.GetProperty("url").GetString());
    }

    [Fact]
    public void UnpairedCardIsHonest()
    {
        using var card = JsonDocument.Parse(WidgetCard.BuildJson(null));

        var texts = card.RootElement.GetProperty("body").EnumerateArray()
            .Select(block => block.GetProperty("text").GetString())
            .ToList();
        Assert.Contains(texts, text => text!.Contains("Not paired"));
        Assert.False(card.RootElement.TryGetProperty("actions", out _));
    }

    [Fact]
    public void QuietCardFallsBackToLatestDecisionLink()
    {
        var payload = GlanceJson.DeserializeGlance(Fixtures.GlanceIos());
        var quiet = payload with
        {
            Alerts = new GlanceAlerts { UnresolvedCount = 0, Top = null },
        };

        using var card = JsonDocument.Parse(WidgetCard.BuildJson(quiet));

        var action = card.RootElement.GetProperty("actions").EnumerateArray().Single();
        Assert.Equal(payload.LatestDecision!.Url, action.GetProperty("url").GetString());
    }
}
