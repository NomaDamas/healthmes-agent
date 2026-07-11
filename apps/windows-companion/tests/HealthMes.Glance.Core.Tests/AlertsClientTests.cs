using System.Net;
using System.Text;
using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// `GET /v1/alerts` contract (healthmes/api/alerts.py): the §8.5 grammar
/// lines recorded at fire time + the "why this?" link, in the standard Page
/// envelope. The server pins that alerts[0] and the glance top alert agree —
/// the fixture here mirrors that: its first item matches glance.json's top.
/// </summary>
public class AlertsClientTests
{
    [Fact]
    public void ParsesTheAlertsPageFixture()
    {
        var page = GlanceJson.DeserializeAlertsPage(Fixtures.AlertsPage());

        Assert.Equal(2, page.Pagination.TotalCount);
        Assert.Equal(20, page.Pagination.Limit);
        Assert.False(page.Pagination.HasMore);

        var top = page.Data[0];
        Assert.Equal("stress_spike_vs_baseline", top.RuleId);
        Assert.Equal("Stress 82 vs baseline 55", top.Summary);
        Assert.Equal("Move the 14:00 block to tomorrow.", top.Proposal);
        Assert.Equal(DateTimeOffset.Parse("2026-07-09T13:50:00Z"), top.FiredAt);
        Assert.NotNull(top.Evidence);
        Assert.Equal(-18, top.Evidence!.Value.GetProperty("hrv_delta_pct").GetInt32());
        Assert.NotNull(top.DecisionUrl);
    }

    [Fact]
    public void LegacyPayloadlessRowsCarryHonestFallbacks()
    {
        var legacy = GlanceJson.DeserializeAlertsPage(Fixtures.AlertsPage()).Data[1];

        Assert.Equal(legacy.RuleId, legacy.Summary); // server falls back to rule_id
        Assert.Null(legacy.Proposal);
        Assert.Null(legacy.Evidence);
        Assert.Null(legacy.DecisionUrl);
    }

    [Fact]
    public void AlertsTopAgreesWithGlanceTopVerbatim()
    {
        // Mirror of the server-side pinning test: an app listing alerts must
        // never disagree with its own widget.
        var glanceTop = GlanceJson.DeserializeGlance(Fixtures.GlanceIos()).Alerts.Top!;
        var alertsTop = GlanceJson.DeserializeAlertsPage(Fixtures.AlertsPage()).Data[0];

        Assert.Equal(glanceTop.RuleId, alertsTop.RuleId);
        Assert.Equal(glanceTop.Summary, alertsTop.Summary);
        Assert.Equal(glanceTop.DecisionUrl, alertsTop.DecisionUrl);
    }

    [Fact]
    public async Task FetchSendsWindowAndPaginationQuery()
    {
        HttpRequestMessage? seen = null;
        var handler = new StubHandler(request =>
        {
            seen = request;
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(Fixtures.AlertsPage(), Encoding.UTF8, "application/json"),
            };
        });
        var client = new GlanceClient(handler);
        var pairing = new Pairing(Pairing.NormalizeBaseUrl("http://192.168.1.20:8100"), "secret");

        var page = await client.FetchAlertsAsync(pairing, hours: 48, limit: 5, offset: 10);

        Assert.Equal(
            "http://192.168.1.20:8100/v1/alerts?hours=48&limit=5&offset=10",
            seen!.RequestUri!.AbsoluteUri);
        Assert.Equal("Bearer secret", seen.Headers.GetValues("Authorization").Single());
        Assert.Equal(2, page.Data.Count);
    }

    [Fact]
    public void ParsesTheWeeklyReportEnvelope()
    {
        var info = GlanceJson.DeserializeWeeklyReportInfo(Fixtures.WeeklyReport());

        Assert.Equal(new DateOnly(2026, 7, 6), info.WeekStart);
        Assert.Equal(new DateOnly(2026, 7, 12), info.WeekEnd);
        Assert.Equal("http://192.168.1.20:8100/reports/weekly?token=hm-ro-3q2b8d1f7c6e5a4", info.ReportUrl);
    }

    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond)
        : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(respond(request));
    }
}
