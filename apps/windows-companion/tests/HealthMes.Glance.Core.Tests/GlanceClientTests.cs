using System.Net;
using System.Text;
using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// Conditional-GET behaviour against a scripted HttpMessageHandler — the
/// same 200→304 ETag flow the server proves live in tests/api/test_briefing
/// and the iOS suite pins in GlanceClientTests.swift.
/// </summary>
public class GlanceClientTests
{
    private const string Etag = "\"abc123\"";

    private static Pairing PairedInstance(string? token = "secret-token") =>
        new(Pairing.NormalizeBaseUrl("http://192.168.1.20:8100"), token);

    private sealed class ScriptedHandler(Func<HttpRequestMessage, int, HttpResponseMessage> script)
        : HttpMessageHandler
    {
        public List<HttpRequestMessage> Requests { get; } = [];

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Requests.Add(request);
            return Task.FromResult(script(request, Requests.Count));
        }
    }

    private static HttpResponseMessage Ok(string body, string? etag = Etag, string? cacheControl = "private, max-age=300")
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
        if (etag is not null)
        {
            response.Headers.TryAddWithoutValidation("ETag", etag);
        }
        if (cacheControl is not null)
        {
            response.Headers.TryAddWithoutValidation("Cache-Control", cacheControl);
        }
        return response;
    }

    private static HttpResponseMessage NotModified()
    {
        var response = new HttpResponseMessage(HttpStatusCode.NotModified);
        response.Headers.TryAddWithoutValidation("ETag", Etag);
        response.Headers.TryAddWithoutValidation("Cache-Control", "private, max-age=300");
        return response;
    }

    [Fact]
    public async Task FirstFetchSendsBearerAndCachesEtag()
    {
        var handler = new ScriptedHandler((_, _) => Ok(Fixtures.GlanceIos()));
        var client = new GlanceClient(handler);
        var now = DateTimeOffset.Parse("2026-07-09T14:23:00Z");

        var snapshot = await client.FetchGlanceAsync(PairedInstance(), now);

        var request = Assert.Single(handler.Requests);
        Assert.Equal("http://192.168.1.20:8100/v1/briefing/glance", request.RequestUri!.AbsoluteUri);
        Assert.Equal("Bearer secret-token", request.Headers.GetValues("Authorization").Single());
        Assert.False(request.Headers.Contains("If-None-Match"));
        Assert.False(snapshot.Revalidated);
        Assert.Equal(58, snapshot.Payload.Energy.Score);
        Assert.Equal(now.AddSeconds(300), snapshot.NextRefresh);
        Assert.Equal(Etag, client.Cache.Load()!.Etag);
    }

    [Fact]
    public async Task SecondFetchRevalidatesWith304AndReservesCachedBody()
    {
        var handler = new ScriptedHandler((request, call) =>
            call == 1 ? Ok(Fixtures.GlanceIos()) : NotModified());
        var client = new GlanceClient(handler);

        await client.FetchGlanceAsync(PairedInstance());
        var second = await client.FetchGlanceAsync(PairedInstance());

        Assert.Equal(Etag, handler.Requests[1].Headers.GetValues("If-None-Match").Single());
        Assert.True(second.Revalidated);
        Assert.Equal(58, second.Payload.Energy.Score); // cached body re-served
    }

    [Fact]
    public async Task NotModifiedWithoutCachedBodyRetriesUnconditionally()
    {
        // Cache primed with an ETag but a corrupted body: the 304 cannot be
        // served, so the client must retry once without If-None-Match.
        var cache = new InMemorySnapshotStore();
        cache.Store(new CachedGlance
        {
            Etag = Etag,
            FetchedAt = DateTimeOffset.UtcNow,
            MaxAgeSeconds = 300,
            PayloadJson = "{not json",
        });
        var handler = new ScriptedHandler((request, call) =>
            request.Headers.Contains("If-None-Match") ? NotModified() : Ok(Fixtures.GlanceIos()));
        var client = new GlanceClient(handler, cache);

        var snapshot = await client.FetchGlanceAsync(PairedInstance());

        Assert.Equal(2, handler.Requests.Count);
        Assert.False(snapshot.Revalidated);
        Assert.Equal(58, snapshot.Payload.Energy.Score);
    }

    [Fact]
    public async Task TokenlessPairingSendsNoAuthorizationHeader()
    {
        var handler = new ScriptedHandler((_, _) => Ok(Fixtures.GlanceIos()));
        var client = new GlanceClient(handler);

        await client.FetchGlanceAsync(PairedInstance(token: null));

        Assert.False(handler.Requests[0].Headers.Contains("Authorization"));
    }

    [Fact]
    public async Task UnauthorizedSurfacesAsTypedError()
    {
        var handler = new ScriptedHandler((_, _) => new HttpResponseMessage(HttpStatusCode.Unauthorized));
        var client = new GlanceClient(handler);

        var error = await Assert.ThrowsAsync<GlanceUnauthorizedException>(
            () => client.FetchGlanceAsync(PairedInstance()));
        Assert.Equal(401, error.StatusCode);
    }

    [Fact]
    public async Task ContractBreakSurfacesAsDecodingError()
    {
        var handler = new ScriptedHandler((_, _) => Ok("{\"generated_at\": \"2026-07-09T14:23:00Z\"}"));
        var client = new GlanceClient(handler);

        await Assert.ThrowsAsync<GlanceDecodingException>(() => client.FetchGlanceAsync(PairedInstance()));
    }

    [Fact]
    public async Task SubpathBaseUrlsArePreserved()
    {
        var handler = new ScriptedHandler((_, _) => Ok(Fixtures.GlanceIos()));
        var client = new GlanceClient(handler);
        var pairing = new Pairing(Pairing.NormalizeBaseUrl("https://home.example/healthmes/"), "t");

        await client.FetchGlanceAsync(pairing);

        Assert.Equal(
            "https://home.example/healthmes/v1/briefing/glance",
            handler.Requests[0].RequestUri!.AbsoluteUri);
    }

    [Theory]
    [InlineData("private, max-age=300", 300)]
    [InlineData("max-age=60", 60)]
    [InlineData("private", null)]
    [InlineData(null, null)]
    public void MaxAgeParsing(string? header, int? expected)
    {
        Assert.Equal(expected, GlanceClient.MaxAgeSeconds(header));
    }

    [Fact]
    public async Task MissingCacheControlFallsBackToFiveMinutes()
    {
        var handler = new ScriptedHandler((_, _) => Ok(Fixtures.GlanceIos(), cacheControl: null));
        var client = new GlanceClient(handler);
        var now = DateTimeOffset.Parse("2026-07-09T14:23:00Z");

        var snapshot = await client.FetchGlanceAsync(PairedInstance(), now);

        Assert.Equal(now.AddSeconds(GlanceClient.DefaultMaxAgeSeconds), snapshot.NextRefresh);
    }
}
