using System.Globalization;
using System.Runtime.InteropServices;
using HealthMes.Glance.Core;
using HealthMes.Windows.Common;

namespace HealthMes.Screensaver;

/// <summary>
/// The ambient briefing: clock, energy score, 24h curve, next block, gentle
/// alert count — all owner-drawn. Honest states: "not paired / no data", and
/// the privacy toggle (issue #11) hides every health number/summary while
/// keeping the clock and the next calendar block. Visual design is
/// PLACEHOLDER (docs/design/WATCH-NOTIFICATIONS.ko.md owns the final look,
/// including Q6 night behaviour).
///
/// Data path: paints immediately from the on-disk snapshot the tray app
/// maintains, then keeps ETag-revalidating against the paired instance every
/// max-age (5 min) while running.
/// </summary>
internal sealed partial class ScreensaverForm : Form
{
    private readonly bool _renderBriefing;
    private readonly bool _isPreview;
    private readonly IntPtr _previewParent;

    private readonly LocalSettings _settings = new();
    private readonly DpapiPairingStore _pairingStore;
    private readonly GlanceClient _client = new(cache: new FileSnapshotStore());

    private GlancePayload? _payload;
    private bool _hideNumbers;
    private Point? _initialMouse;
    private System.Windows.Forms.Timer? _clockTimer;
    private System.Windows.Forms.Timer? _pollTimer;

    public ScreensaverForm(Screen screen, bool renderBriefing)
    {
        _pairingStore = new DpapiPairingStore(_settings);
        _renderBriefing = renderBriefing;
        _isPreview = false;
        _previewParent = IntPtr.Zero;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        Bounds = screen.Bounds;
        TopMost = true;
        BackColor = Color.Black;
        DoubleBuffered = true;
        Cursor.Hide();
        WireLifecycle();
    }

    /// <summary>Preview mode: render small inside the settings dialog's window.</summary>
    public ScreensaverForm(IntPtr previewParent)
    {
        _pairingStore = new DpapiPairingStore(_settings);
        _renderBriefing = true;
        _isPreview = true;
        _previewParent = previewParent;
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        BackColor = Color.Black;
        DoubleBuffered = true;
        WireLifecycle();
    }

    private const int WsChild = 0x40000000;

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            if (_isPreview)
            {
                parameters.Style |= WsChild;
            }
            return parameters;
        }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        if (_isPreview && _previewParent != IntPtr.Zero)
        {
            SetParent(Handle, _previewParent);
            if (GetClientRect(_previewParent, out var rect))
            {
                Bounds = new Rectangle(0, 0, rect.Right - rect.Left, rect.Bottom - rect.Top);
            }
        }
    }

    private void WireLifecycle()
    {
        Load += (_, _) =>
        {
            _hideNumbers = _settings.Load().HideHealthNumbers;
            LoadCachedPayload();
            _clockTimer = new System.Windows.Forms.Timer { Interval = 1000 };
            _clockTimer.Tick += (_, _) => Invalidate();
            _clockTimer.Start();
            if (!_isPreview)
            {
                _pollTimer = new System.Windows.Forms.Timer
                {
                    Interval = GlanceClient.DefaultMaxAgeSeconds * 1000,
                };
                _pollTimer.Tick += (_, _) => _ = PollAsync();
                _pollTimer.Start();
                _ = PollAsync();
            }
        };
        FormClosed += (_, _) =>
        {
            _clockTimer?.Dispose();
            _pollTimer?.Dispose();
            if (!_isPreview)
            {
                Cursor.Show();
            }
        };
        if (!_isPreview)
        {
            KeyPreview = true;
            KeyDown += (_, _) => ExitSaver();
            MouseDown += (_, _) => ExitSaver();
            MouseMove += (_, e) =>
            {
                _initialMouse ??= e.Location;
                var delta = new Size(
                    Math.Abs(e.X - _initialMouse.Value.X), Math.Abs(e.Y - _initialMouse.Value.Y));
                if (delta.Width > 10 || delta.Height > 10)
                {
                    ExitSaver();
                }
            };
        }
    }

    private static void ExitSaver() => Application.Exit();

    private void LoadCachedPayload()
    {
        var cached = _client.Cache.Load();
        if (cached is null)
        {
            return;
        }
        try
        {
            _payload = GlanceJson.DeserializeGlance(cached.PayloadJson);
        }
        catch (System.Text.Json.JsonException)
        {
            _payload = null;
        }
    }

    private async Task PollAsync()
    {
        var pairing = _pairingStore.Load();
        if (pairing is null)
        {
            _payload = null;
            Invalidate();
            return;
        }
        try
        {
            var snapshot = await _client.FetchGlanceAsync(pairing);
            _payload = snapshot.Payload;
        }
        catch (GlanceClientException)
        {
            // Keep whatever we had; the footer already shows staleness time.
        }
        _hideNumbers = _settings.Load().HideHealthNumbers;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var graphics = e.Graphics;
        graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAlias;

        var scale = Math.Max(0.2f, Math.Min(Width / 1920f, Height / 1080f));
        using var clockFont = new Font("Segoe UI Light", 96f * scale, FontStyle.Regular);
        using var bigFont = new Font("Segoe UI", 48f * scale, FontStyle.Bold);
        using var textFont = new Font("Segoe UI", 20f * scale, FontStyle.Regular);
        using var dimBrush = new SolidBrush(Color.FromArgb(150, 150, 155));
        using var mainBrush = new SolidBrush(Color.FromArgb(235, 235, 240));

        var centerX = Width / 2f;
        var y = Height * 0.18f;

        // Clock is always shown — the saver must be useful even unpaired.
        var clock = DateTime.Now.ToString("HH:mm", CultureInfo.InvariantCulture);
        DrawCentered(graphics, clock, clockFont, mainBrush, centerX, ref y);

        if (!_renderBriefing)
        {
            return;
        }

        if (_payload is null)
        {
            DrawCentered(graphics, L10n.Get("Saver_NotPaired"), textFont, dimBrush, centerX, ref y);
            return;
        }

        if (_hideNumbers)
        {
            // Privacy mode (shared spaces / screen sharing): no score, no
            // curve, no alert summaries or counts — just the schedule.
            DrawCentered(graphics, L10n.Get("Saver_PrivacyHidden"), textFont, dimBrush, centerX, ref y);
            if (GlanceFormat.NextBlockLine(_payload) is { } block)
            {
                DrawCentered(
                    graphics,
                    $"{L10n.Get("Saver_NextBlockLabel")} · {block}",
                    textFont, mainBrush, centerX, ref y);
            }
            return;
        }

        DrawCentered(graphics, GlanceFormat.EnergyLine(_payload), bigFont, mainBrush, centerX, ref y);
        y += Height * 0.02f;
        DrawCurve(graphics, new RectangleF(Width * 0.25f, y, Width * 0.5f, Height * 0.12f));
        y += Height * 0.15f;
        if (GlanceFormat.NextBlockLine(_payload) is { } next)
        {
            DrawCentered(
                graphics,
                $"{L10n.Get("Saver_NextBlockLabel")} · {next}",
                textFont, mainBrush, centerX, ref y);
        }
        DrawCentered(graphics, GlanceFormat.AlertsLine(_payload), textFont, dimBrush, centerX, ref y);
    }

    private static void DrawCentered(
        Graphics graphics, string text, Font font, Brush brush, float centerX, ref float y)
    {
        var size = graphics.MeasureString(text, font);
        graphics.DrawString(text, font, brush, centerX - size.Width / 2f, y);
        y += size.Height * 1.15f;
    }

    /// <summary>Same honest-null curve as the tray sparkline, scaled up.</summary>
    private void DrawCurve(Graphics graphics, RectangleF rect)
    {
        var points = _payload?.Energy.Curve24h;
        if (points is null || points.Count == 0)
        {
            return;
        }
        using var pen = new Pen(Color.FromArgb(120, 170, 255), 3f);
        using var dot = new SolidBrush(Color.FromArgb(120, 170, 255));

        PointF? At(int index)
        {
            var score = points[index].Score;
            if (score is null)
            {
                return null;
            }
            var x = rect.Left + rect.Width * (index / (float)Math.Max(1, points.Count - 1));
            var yPoint = rect.Bottom - rect.Height * (Math.Clamp(score.Value, 0, 100) / 100f);
            return new PointF(x, yPoint);
        }

        for (var i = 0; i < points.Count; i++)
        {
            var current = At(i);
            if (current is null)
            {
                continue;
            }
            var next = i + 1 < points.Count ? At(i + 1) : null;
            var previous = i > 0 ? At(i - 1) : null;
            if (next is not null)
            {
                graphics.DrawLine(pen, current.Value, next.Value);
            }
            if (next is null && previous is null)
            {
                graphics.FillEllipse(dot, current.Value.X - 3f, current.Value.Y - 3f, 6f, 6f);
            }
        }
    }

    [LibraryImport("user32.dll", SetLastError = true)]
    private static partial IntPtr SetParent(IntPtr hWndChild, IntPtr hWndNewParent);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetClientRect(IntPtr hWnd, out Rect lpRect);

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
