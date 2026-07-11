using System.Drawing.Text;
using System.Runtime.InteropServices;

namespace HealthMes.Tray;

/// <summary>
/// Draws the score badge tray icon at runtime (no binary asset in the repo):
/// a dark disc with the energy score (or "--") in white. PLACEHOLDER visual —
/// state colors/severity coding are the domain expert's call
/// (docs/design/WATCH-NOTIFICATIONS.ko.md Q2/Q3).
/// </summary>
internal static partial class TrayIconRenderer
{
    public static Icon Render(int? score)
    {
        var text = HealthMes.Glance.Core.GlanceFormat.ScoreText(score);
        using var bitmap = new Bitmap(32, 32);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.Clear(Color.Transparent);
            graphics.TextRenderingHint = TextRenderingHint.AntiAlias;
            using var background = new SolidBrush(Color.FromArgb(235, 28, 28, 30));
            graphics.FillEllipse(background, 0, 0, 31, 31);
            using var font = new Font("Segoe UI", text.Length > 2 ? 12f : 15f, FontStyle.Bold, GraphicsUnit.Pixel);
            var size = graphics.MeasureString(text, font);
            graphics.DrawString(
                text, font, Brushes.White, (32f - size.Width) / 2f, (32f - size.Height) / 2f);
        }

        // Icon.FromHandle does not own the HICON: clone to a managed icon,
        // then destroy the native handle to avoid a GDI leak on every poll.
        var handle = bitmap.GetHicon();
        try
        {
            using var borrowed = Icon.FromHandle(handle);
            return (Icon)borrowed.Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool DestroyIcon(IntPtr hIcon);
}
