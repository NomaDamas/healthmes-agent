using System.Text.Json;

namespace HealthMes.Glance.Core;

/// <summary>
/// Adaptive Card payload for the Windows 11 Widgets Board card (issue #11).
///
/// The widget PROVIDER is deferred — it requires MSIX packaging
/// (see apps/windows-companion/DEFERRED.md) — but the data→card mapping is
/// real and unit-tested here, so wiring the provider later is pure plumbing.
/// Slots follow the §8.5 grammar subset the other glance surfaces use:
/// energy score+confidence, next block, alerts line, "Why this?" action.
/// Visual styling is placeholder (docs/design/WATCH-NOTIFICATIONS.ko.md owns
/// the final look).
/// </summary>
public static class WidgetCard
{
    /// <summary>Adaptive Card 1.5 JSON for one glance payload.</summary>
    public static string BuildJson(GlancePayload? payload)
    {
        var body = new List<object>();
        var actions = new List<object>();

        if (payload is null)
        {
            body.Add(TextBlock("HealthMes", size: "medium", weight: "bolder"));
            body.Add(TextBlock("Not paired — open HealthMes Tray to pair.", isSubtle: true, wrap: true));
        }
        else
        {
            body.Add(TextBlock(GlanceFormat.EnergyLine(payload), size: "large", weight: "bolder"));
            if (GlanceFormat.NextBlockLine(payload) is { } nextBlock)
            {
                body.Add(TextBlock(nextBlock, wrap: true));
            }
            body.Add(TextBlock(GlanceFormat.AlertsLine(payload), isSubtle: true, wrap: true));
            var decisionUrl = payload.Alerts.Top?.DecisionUrl ?? payload.LatestDecision?.Url;
            if (decisionUrl is not null)
            {
                actions.Add(new Dictionary<string, object>
                {
                    ["type"] = "Action.OpenUrl",
                    ["title"] = "Why this?",
                    ["url"] = decisionUrl,
                });
            }
        }

        var card = new Dictionary<string, object>
        {
            ["$schema"] = "http://adaptivecards.io/schemas/adaptive-card.json",
            ["type"] = "AdaptiveCard",
            ["version"] = "1.5",
            ["body"] = body,
        };
        if (actions.Count > 0)
        {
            card["actions"] = actions;
        }
        return JsonSerializer.Serialize(card, SerializeOptions);
    }

    private static readonly JsonSerializerOptions SerializeOptions = new() { WriteIndented = false };

    private static Dictionary<string, object> TextBlock(
        string text,
        string? size = null,
        string? weight = null,
        bool isSubtle = false,
        bool wrap = false)
    {
        var block = new Dictionary<string, object> { ["type"] = "TextBlock", ["text"] = text };
        if (size is not null)
        {
            block["size"] = size;
        }
        if (weight is not null)
        {
            block["weight"] = weight;
        }
        if (isSubtle)
        {
            block["isSubtle"] = true;
        }
        if (wrap)
        {
            block["wrap"] = true;
        }
        return block;
    }
}
