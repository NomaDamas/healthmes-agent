using System.Security.Cryptography;
using System.Text;
using HealthMes.Glance.Core;

namespace HealthMes.Windows.Common;

/// <summary>
/// DPAPI-protected pairing storage (issue #11): the Windows sibling of the
/// iOS Keychain / Android EncryptedSharedPreferences stores. The bearer
/// token is protected with <see cref="ProtectedData"/> in
/// <see cref="DataProtectionScope.CurrentUser"/> scope — only this Windows
/// user on this machine can decrypt it — and persisted (base64) in the
/// shared settings.json next to the plain base URL.
/// </summary>
public sealed class DpapiPairingStore(LocalSettings settings) : IPairingStore
{
    public DpapiPairingStore()
        : this(new LocalSettings()) { }

    public Pairing? Load()
    {
        var document = settings.Load();
        if (document.BaseUrl is null
            || !Uri.TryCreate(document.BaseUrl, UriKind.Absolute, out var baseUrl))
        {
            return null;
        }
        return new Pairing(baseUrl, UnprotectToken(document.TokenDpapi));
    }

    public void Save(Pairing pairing)
    {
        settings.Mutate(document =>
        {
            document.BaseUrl = pairing.BaseUrl.AbsoluteUri.TrimEnd('/');
            document.TokenDpapi = ProtectToken(pairing.Token);
        });
    }

    public void Clear()
    {
        settings.Mutate(document =>
        {
            document.BaseUrl = null;
            document.TokenDpapi = null;
        });
    }

    private static string? ProtectToken(string? token)
    {
        if (string.IsNullOrEmpty(token))
        {
            return null;
        }
        var protectedBytes = ProtectedData.Protect(
            Encoding.UTF8.GetBytes(token), optionalEntropy: null, DataProtectionScope.CurrentUser);
        return Convert.ToBase64String(protectedBytes);
    }

    private static string? UnprotectToken(string? base64)
    {
        if (string.IsNullOrEmpty(base64))
        {
            return null;
        }
        try
        {
            var clear = ProtectedData.Unprotect(
                Convert.FromBase64String(base64), optionalEntropy: null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(clear);
        }
        catch (Exception error) when (error is CryptographicException or FormatException)
        {
            // Copied from another machine/user profile: undecryptable by
            // design. Treat as token-less; the UI shows "token rejected".
            return null;
        }
    }
}
