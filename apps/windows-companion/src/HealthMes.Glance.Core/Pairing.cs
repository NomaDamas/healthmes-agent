namespace HealthMes.Glance.Core;

// Pairing = the base URL + bearer token of the user's OWN healthmes
// instance. Local-first contract (issues #7/#10/#11): this URL is the only
// network destination anything in apps/windows-companion ever talks to —
// no third-party endpoint, no analytics, no push relay.
//
// Storage is platform-specific and lives OUTSIDE this project:
// HealthMes.Windows.Common protects the token with DPAPI (ProtectedData,
// CurrentUser scope) — the Windows sibling of the iOS Keychain / Android
// EncryptedSharedPreferences pairings.

public sealed class PairingException : Exception
{
    public PairingException()
        : base("Enter a valid http(s) URL, e.g. http://192.168.1.20:8100") { }
}

/// <summary>A validated pairing; <see cref="Token"/> is null for token-less loopback instances.</summary>
public sealed record Pairing
{
    /// <summary>Normalized (no trailing slash) http(s) base URL of the instance.</summary>
    public Uri BaseUrl { get; }

    /// <summary>Bearer token; null/blank collapses to null.</summary>
    public string? Token { get; }

    public Pairing(Uri baseUrl, string? token)
    {
        BaseUrl = baseUrl;
        var trimmed = token?.Trim();
        Token = string.IsNullOrEmpty(trimmed) ? null : trimmed;
    }

    /// <summary>
    /// Accepts what a human types: whitespace and trailing slashes are
    /// stripped; scheme+host are required. Subpath bases (reverse proxies,
    /// e.g. <c>https://home.example/healthmes</c>) are preserved. Mirrors
    /// PairingStore.normalizeBaseURL in apps/ios-companion.
    /// </summary>
    /// <exception cref="PairingException">Not an http(s) URL with a host.</exception>
    public static Uri NormalizeBaseUrl(string raw)
    {
        var trimmed = raw.Trim();
        while (trimmed.EndsWith('/'))
        {
            trimmed = trimmed[..^1];
        }
        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out var url)
            || (url.Scheme != Uri.UriSchemeHttp && url.Scheme != Uri.UriSchemeHttps)
            || string.IsNullOrEmpty(url.Host))
        {
            throw new PairingException();
        }
        return url;
    }
}

/// <summary>
/// Where a pairing lives between runs. Implemented in
/// HealthMes.Windows.Common (settings.json + DPAPI-protected token); kept as
/// an interface here so the core client and its tests never touch Windows.
/// </summary>
public interface IPairingStore
{
    Pairing? Load();

    void Save(Pairing pairing);

    void Clear();
}
