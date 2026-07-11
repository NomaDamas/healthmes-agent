namespace HealthMes.Tray;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        // One tray icon per session.
        using var mutex = new Mutex(initiallyOwned: true, "HealthMes.Tray.SingleInstance", out var createdNew);
        if (!createdNew)
        {
            return;
        }
        ApplicationConfiguration.Initialize();
        using var context = new TrayApplicationContext();
        Application.Run(context);
    }
}
