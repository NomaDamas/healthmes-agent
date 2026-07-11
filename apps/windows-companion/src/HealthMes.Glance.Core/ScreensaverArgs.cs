namespace HealthMes.Glance.Core;

/// <summary>
/// The Windows screensaver command-line contract, parsed as pure data so the
/// .scr behaves exactly per the shell's rules (and so this logic is
/// unit-testable off-Windows):
///
/// <list type="bullet">
/// <item><c>/s</c> — run the saver full-screen.</item>
/// <item><c>/p &lt;hwnd&gt;</c> (or <c>/p:&lt;hwnd&gt;</c>) — render a preview into the given window.</item>
/// <item><c>/c</c> (or <c>/c:&lt;hwnd&gt;</c>) — show the settings dialog (the issue-#11 privacy toggle lives here).</item>
/// <item>no arguments — settings dialog (what Windows does for a bare launch).</item>
/// </list>
/// </summary>
public enum ScreensaverMode
{
    Show,
    Preview,
    Configure,
}

public readonly record struct ScreensaverLaunch(ScreensaverMode Mode, long PreviewHandle)
{
    public static ScreensaverLaunch Parse(IReadOnlyList<string> args)
    {
        if (args.Count == 0)
        {
            return new ScreensaverLaunch(ScreensaverMode.Configure, 0);
        }

        var first = args[0].Trim();
        var switchChar = first.StartsWith('/') || first.StartsWith('-') ? first[1..] : first;
        var parts = switchChar.Split(':', 2);
        var flag = parts[0].ToLowerInvariant();
        var inlineValue = parts.Length > 1 ? parts[1] : null;

        switch (flag)
        {
            case "s":
                return new ScreensaverLaunch(ScreensaverMode.Show, 0);
            case "p":
            {
                var raw = inlineValue ?? (args.Count > 1 ? args[1] : null);
                return long.TryParse(raw, out var handle) && handle != 0
                    ? new ScreensaverLaunch(ScreensaverMode.Preview, handle)
                    // A preview request without a window: nothing to draw
                    // into, fall back to the settings dialog.
                    : new ScreensaverLaunch(ScreensaverMode.Configure, 0);
            }
            default:
                // "/c", "/c:<hwnd of the owner dialog>", or anything unknown.
                return new ScreensaverLaunch(ScreensaverMode.Configure, 0);
        }
    }
}
