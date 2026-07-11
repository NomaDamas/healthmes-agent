using HealthMes.Glance.Core;

namespace HealthMes.Screensaver;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var launch = ScreensaverLaunch.Parse(args);
        switch (launch.Mode)
        {
            case ScreensaverMode.Configure:
                Application.Run(new SettingsForm());
                break;

            case ScreensaverMode.Preview:
                Application.Run(new ScreensaverForm(new IntPtr(launch.PreviewHandle)));
                break;

            case ScreensaverMode.Show:
            default:
            {
                // One form per monitor; the primary renders the briefing,
                // secondaries go dark. Any input on any form exits all.
                var forms = Screen.AllScreens
                    .Select(screen => new ScreensaverForm(screen, renderBriefing: screen.Primary))
                    .Cast<Form>()
                    .ToList();
                if (forms.Count == 0)
                {
                    return;
                }
                foreach (var form in forms.Skip(1))
                {
                    form.Show();
                }
                Application.Run(forms[0]);
                break;
            }
        }
    }
}
