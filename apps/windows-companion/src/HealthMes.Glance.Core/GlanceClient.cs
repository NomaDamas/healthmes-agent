using System.Net;
using System.Text.Json;

namespace HealthMes.Glance.Core;

/// <summary>
/// What a glance fetch produced: the decoded payload plus refresh guidance
/// derived from the server's <c>Cache-Control: private, max-age=N</c>.
/// </summary>
public sealed record GlanceSnapshot
{
    public required GlancePayload Payload { get; init; }

    public required DateTimeOffset FetchedAt { get; init; }

    /// <summary>
    /// True when the server answered <c>304 Not Modified</c> and the cached
    /// body was re-served (data unchanged; polling stayed cheap).
    /// </summary>
    public required bool Revalidated { get; init; }

    /// <summary>Earliest sensible next poll (<c>FetchedAt + max-age</c>).</summary>
    public required DateTimeOffset NextRefresh { get; init; }
}

/// <summary>Persisted conditional-GET state (ETag + last body).</summary>
public sealed record CachedGlance
{
    public required string? Etag { get; init; }

    public required DateTimeOffset FetchedAt { get; init; }

    public required int MaxAgeSeconds { get; init; }

    /// <summary>The raw 200 body; re-served on 304.</summary>
    public required string PayloadJson { get; init; }
}

/// <summary>
/// Snapshot persistence between polls/processes. The core ships an in-memory
/// store; HealthMes.Windows.Common adds the on-disk one the tray and the
/// screensaver share.
/// </summary>
public interface ISnapshotStore
{
    CachedGlance? Load();

    void Store(CachedGlance snapshot);
}

public sealed class InMemorySnapshotStore : ISnapshotStore
{
    private CachedGlance? _cached;

    public CachedGlance? Load() => _cached;

    public void Store(CachedGlance snapshot) => _cached = snapshot;
}

public abstract class GlanceClientException(string message, Exception? inner = null)
    : Exception(message, inner);

/// <summary>401/403 — token missing/rejected by the instance.</summary>
public sealed class GlanceUnauthorizedException(int statusCode)
    : GlanceClientException($"instance rejected the pairing token (HTTP {statusCode})")
{
    public int StatusCode { get; } = statusCode;
}

public sealed class GlanceHttpException(int statusCode)
    : GlanceClientException($"unexpected HTTP status {statusCode}")
{
    public int StatusCode { get; } = statusCode;
}

public sealed class GlanceTransportException(Exception inner)
    : GlanceClientException("network error talking to the paired instance", inner);

public sealed class GlanceDecodingException(JsonException inner)
    : GlanceClientException("payload did not match the pinned contract", inner);

/// <summary>
/// Minimal client for the paired healthmes instance, shared by the tray app
/// and the screensaver. HTTP behaviour mirrors the glance endpoint contract
/// (healthmes/api/briefing.py) exactly like the iOS/Android clients:
///
/// <list type="bullet">
/// <item><c>Authorization: Bearer &lt;token&gt;</c> when a token is paired.</item>
/// <item><c>If-None-Match</c> with the cached ETag on every glance poll; a
/// <c>304</c> re-serves the cached body without re-downloading.</item>
/// <item><c>Cache-Control: max-age</c> is parsed into
/// <see cref="GlanceSnapshot.NextRefresh"/> so pollers can schedule.</item>
/// <item>No transparent HTTP caching layer — this client owns
/// conditional-GET semantics end to end (the raw 304 must be observable).</item>
/// </list>
/// </summary>
public sealed class GlanceClient
{
    public const string GlancePath = "/v1/briefing/glance";
    public const string AlertsPath = "/v1/alerts";
    public const string WeeklyReportJsonPath = "/reports/weekly.json";

    /// <summary>
    /// Fallback when the server omits/mangles Cache-Control (contract says it
    /// never does; matches CACHE_MAX_AGE_SECONDS server-side).
    /// </summary>
    public const int DefaultMaxAgeSeconds = 300;

    private readonly HttpClient _http;
    private readonly ISnapshotStore _cache;

    public GlanceClient(HttpMessageHandler? handler = null, ISnapshotStore? cache = null)
    {
        _http = handler is null ? new HttpClient() : new HttpClient(handler);
        _http.Timeout = TimeSpan.FromSeconds(15);
        _cache = cache ?? new InMemorySnapshotStore();
    }

    public ISnapshotStore Cache => _cache;

    /// <summary>`max-age` seconds out of a Cache-Control header value, null when absent.</summary>
    public static int? MaxAgeSeconds(string? cacheControl)
    {
        if (cacheControl is null)
        {
            return null;
        }
        foreach (var directive in cacheControl.Split(','))
        {
            var trimmed = directive.Trim();
            const string prefix = "max-age=";
            if (trimmed.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                && int.TryParse(trimmed.AsSpan(prefix.Length), out var seconds))
            {
                return seconds;
            }
        }
        return null;
    }

    /// <summary>Conditional GET of the glance payload honoring ETag/Cache-Control.</summary>
    public async Task<GlanceSnapshot> FetchGlanceAsync(
        Pairing pairing, DateTimeOffset? now = null, CancellationToken cancellationToken = default)
    {
        var instant = now ?? DateTimeOffset.UtcNow;
        var snapshot = await PerformGlanceFetchAsync(pairing, _cache.Load(), instant, cancellationToken)
            .ConfigureAwait(false);
        if (snapshot is not null)
        {
            return snapshot;
        }
        // The server said 304 but our cached body was gone (evicted or
        // corrupted): one unconditional retry fetches a full body.
        return await PerformGlanceFetchAsync(pairing, cached: null, instant, cancellationToken)
                .ConfigureAwait(false)
            ?? throw new GlanceHttpException(304);
    }

    /// <summary>Recent pushed alerts, newest first (`GET /v1/alerts`).</summary>
    public async Task<AlertsPage> FetchAlertsAsync(
        Pairing pairing,
        int hours = 24,
        int limit = 20,
        int offset = 0,
        CancellationToken cancellationToken = default)
    {
        var url = $"{pairing.BaseUrl.AbsoluteUri.TrimEnd('/')}{AlertsPath}?hours={hours}&limit={limit}&offset={offset}";
        var body = await GetStringAsync(pairing, url, cancellationToken).ConfigureAwait(false);
        try
        {
            return GlanceJson.DeserializeAlertsPage(body);
        }
        catch (JsonException error)
        {
            throw new GlanceDecodingException(error);
        }
    }

    /// <summary>
    /// Weekly-report envelope (`GET /reports/weekly.json`) — used to obtain
    /// the browser-openable <see cref="WeeklyReportInfo.ReportUrl"/>.
    /// </summary>
    public async Task<WeeklyReportInfo> FetchWeeklyReportInfoAsync(
        Pairing pairing, CancellationToken cancellationToken = default)
    {
        var url = $"{pairing.BaseUrl.AbsoluteUri.TrimEnd('/')}{WeeklyReportJsonPath}";
        var body = await GetStringAsync(pairing, url, cancellationToken).ConfigureAwait(false);
        try
        {
            return GlanceJson.DeserializeWeeklyReportInfo(body);
        }
        catch (JsonException error)
        {
            throw new GlanceDecodingException(error);
        }
    }

    private static HttpRequestMessage MakeRequest(Pairing pairing, string url, string? ifNoneMatch)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.TryAddWithoutValidation("Accept", "application/json");
        if (pairing.Token is not null)
        {
            request.Headers.TryAddWithoutValidation("Authorization", $"Bearer {pairing.Token}");
        }
        if (ifNoneMatch is not null)
        {
            request.Headers.TryAddWithoutValidation("If-None-Match", ifNoneMatch);
        }
        return request;
    }

    private async Task<string> GetStringAsync(
        Pairing pairing, string url, CancellationToken cancellationToken)
    {
        using var request = MakeRequest(pairing, url, ifNoneMatch: null);
        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            throw new GlanceTransportException(error);
        }
        using (response)
        {
            ThrowForAuthOrStatus(response);
            return await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>Null result = 304-with-missing-cache (caller retries unconditionally once).</summary>
    private async Task<GlanceSnapshot?> PerformGlanceFetchAsync(
        Pairing pairing, CachedGlance? cached, DateTimeOffset now, CancellationToken cancellationToken)
    {
        var url = pairing.BaseUrl.AbsoluteUri.TrimEnd('/') + GlancePath;
        using var request = MakeRequest(pairing, url, cached?.Etag);
        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            throw new GlanceTransportException(error);
        }

        using (response)
        {
            var maxAge = MaxAgeSeconds(HeaderValue(response, "Cache-Control")) ?? DefaultMaxAgeSeconds;
            var nextRefresh = now.AddSeconds(maxAge);

            switch (response.StatusCode)
            {
                case HttpStatusCode.OK:
                {
                    var body = await response.Content.ReadAsStringAsync(cancellationToken)
                        .ConfigureAwait(false);
                    GlancePayload payload;
                    try
                    {
                        payload = GlanceJson.DeserializeGlance(body);
                    }
                    catch (JsonException error)
                    {
                        throw new GlanceDecodingException(error);
                    }
                    _cache.Store(new CachedGlance
                    {
                        Etag = HeaderValue(response, "ETag"),
                        FetchedAt = now,
                        MaxAgeSeconds = maxAge,
                        PayloadJson = body,
                    });
                    return new GlanceSnapshot
                    {
                        Payload = payload, FetchedAt = now, Revalidated = false, NextRefresh = nextRefresh,
                    };
                }

                case HttpStatusCode.NotModified:
                {
                    if (cached is null)
                    {
                        return null;
                    }
                    GlancePayload payload;
                    try
                    {
                        payload = GlanceJson.DeserializeGlance(cached.PayloadJson);
                    }
                    catch (JsonException)
                    {
                        return null; // corrupted cache — retry unconditionally
                    }
                    // Same data, refreshed validity window (304 carries the
                    // same ETag/Cache-Control per the endpoint contract).
                    _cache.Store(cached with { FetchedAt = now, MaxAgeSeconds = maxAge });
                    return new GlanceSnapshot
                    {
                        Payload = payload, FetchedAt = now, Revalidated = true, NextRefresh = nextRefresh,
                    };
                }

                default:
                    ThrowForAuthOrStatus(response);
                    throw new GlanceHttpException((int)response.StatusCode); // unreachable
            }
        }
    }

    private static void ThrowForAuthOrStatus(HttpResponseMessage response)
    {
        var status = (int)response.StatusCode;
        if (status is 401 or 403)
        {
            throw new GlanceUnauthorizedException(status);
        }
        if (status is < 200 or >= 300)
        {
            throw new GlanceHttpException(status);
        }
    }

    private static string? HeaderValue(HttpResponseMessage response, string name)
    {
        if (response.Headers.TryGetValues(name, out var values))
        {
            return values.FirstOrDefault();
        }
        return response.Content.Headers.TryGetValues(name, out var contentValues)
            ? contentValues.FirstOrDefault()
            : null;
    }
}
