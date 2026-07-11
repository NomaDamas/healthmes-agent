using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>
/// Base-URL normalization — the same accept/reject cases the iOS
/// PairingStore.normalizeBaseURL pins (apps/ios-companion Pairing.swift).
/// </summary>
public class PairingTests
{
    [Theory]
    [InlineData("http://192.168.1.20:8100", "http://192.168.1.20:8100")]
    [InlineData("  http://192.168.1.20:8100/  ", "http://192.168.1.20:8100")]
    [InlineData("http://192.168.1.20:8100///", "http://192.168.1.20:8100")]
    [InlineData("https://home.example/healthmes/", "https://home.example/healthmes")]
    [InlineData("HTTP://LAN-HOST:8100", "http://lan-host:8100")]
    public void NormalizesWhatAHumanTypes(string raw, string expectedPrefix)
    {
        var url = Pairing.NormalizeBaseUrl(raw);
        Assert.StartsWith(expectedPrefix, url.AbsoluteUri.TrimEnd('/'), StringComparison.OrdinalIgnoreCase);
        Assert.False(url.AbsoluteUri.TrimEnd('/').EndsWith("//"));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not a url")]
    [InlineData("ftp://192.168.1.20")]
    [InlineData("file:///etc/passwd")]
    [InlineData("192.168.1.20:8100")] // scheme required, mirrors iOS
    public void RejectsNonHttpBases(string raw)
    {
        Assert.Throws<PairingException>(() => Pairing.NormalizeBaseUrl(raw));
    }

    [Fact]
    public void BlankTokensCollapseToNull()
    {
        var url = Pairing.NormalizeBaseUrl("http://127.0.0.1:8100");
        Assert.Null(new Pairing(url, null).Token);
        Assert.Null(new Pairing(url, "   ").Token);
        Assert.Equal("tok", new Pairing(url, " tok \n").Token);
    }
}
