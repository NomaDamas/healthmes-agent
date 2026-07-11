using Xunit;

namespace HealthMes.Glance.Core.Tests;

/// <summary>The .scr command-line contract (/s, /p hwnd, /c[:hwnd], bare).</summary>
public class ScreensaverArgsTests
{
    [Theory]
    [InlineData("/s")]
    [InlineData("/S")]
    [InlineData("-s")]
    public void ShowMode(string flag)
    {
        Assert.Equal(ScreensaverMode.Show, ScreensaverLaunch.Parse([flag]).Mode);
    }

    [Theory]
    [InlineData("/p", "123456", 123456L)]
    [InlineData("/p:98765", null, 98765L)]
    [InlineData("/P", "42", 42L)]
    public void PreviewModeCarriesTheWindowHandle(string flag, string? extra, long handle)
    {
        var args = extra is null ? new[] { flag } : new[] { flag, extra };
        var launch = ScreensaverLaunch.Parse(args);
        Assert.Equal(ScreensaverMode.Preview, launch.Mode);
        Assert.Equal(handle, launch.PreviewHandle);
    }

    [Theory]
    [InlineData]
    [InlineData("/c")]
    [InlineData("/c:5550123")]
    [InlineData("/p")] // preview without a window -> settings
    [InlineData("/p", "not-a-handle")]
    [InlineData("/x")] // unknown flag -> settings, never crash
    public void EverythingElseConfigures(params string[] args)
    {
        Assert.Equal(ScreensaverMode.Configure, ScreensaverLaunch.Parse(args).Mode);
    }
}
