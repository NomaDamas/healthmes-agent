using HealthMes.Glance.Core;
using HealthMes.Windows.Common;

namespace HealthMes.Tray;

/// <summary>
/// Tray lifetime: the NotifyIcon (score badge + tooltip), the 15-minute
/// ETag-honoring poll, the flyout, and §8.5-grammar toasts for newly pushed
/// alerts (from <c>GET /v1/alerts</c>, deduplicated against seen ids).
///
/// Toast transport is the classic NotifyIcon balloon — on Windows 10/11 the
/// shell renders balloons AS toast notifications, and clicking one opens the
/// alert's decision-viewer deep link ("why this?", which no surface may drop
/// per the design worksheet §1.1). Action BUTTONS (✅✏️❌) stay on the
/// phone/Telegram surfaces: a glance alert carries no proposal id to act on,
/// and packaged toast buttons would force the MSIX path (see DEFERRED.md).
///
/// Polling floor is 15 minutes (the Android WorkManager floor; the server's
/// max-age is 5) — a conscious battery/noise choice, and every poll is a
/// cheap 304 revalidation when nothing changed.
/// </summary>
internal sealed class TrayApplicationContext : ApplicationContext
{
    private const int PollIntervalMilliseconds = 15 * 60 * 1000;
    private const int MaxToastsPerPoll = 1; // newest alert; "+N more" in the title

    private readonly NotifyIcon _icon;
    private readonly System.Windows.Forms.Timer _pollTimer;
    private readonly LocalSettings _settings = new();
    private readonly DpapiPairingStore _pairingStore;
    private readonly GlanceClient _client;
    private readonly FlyoutForm _flyout = new();
    private readonly ToolStripMenuItem _hideNumbersItem;

    private GlancePayload? _lastPayload;
    private string _statusText;
    private string? _pendingToastUrl;
    private bool _refreshing;

    public TrayApplicationContext()
    {
        _pairingStore = new DpapiPairingStore(_settings);
        _client = new GlanceClient(cache: new FileSnapshotStore());
        _statusText = L10n.Get("Flyout_NotPaired");

        _hideNumbersItem = new ToolStripMenuItem(L10n.Get("Menu_HideNumbers"))
        {
            CheckOnClick = true,
            Checked = _settings.Load().HideHealthNumbers,
        };
        _hideNumbersItem.CheckedChanged += (_, _) =>
            _settings.Mutate(document => document.HideHealthNumbers = _hideNumbersItem.Checked);

        var menu = new ContextMenuStrip();
        menu.Items.Add(new ToolStripMenuItem(
            L10n.Get("Menu_OpenBriefing"), null, (_, _) => ShowFlyout()));
        menu.Items.Add(new ToolStripMenuItem(
            L10n.Get("Menu_Refresh"), null, (_, _) => _ = RefreshAsync()));
        menu.Items.Add(new ToolStripMenuItem(
            L10n.Get("Menu_OpenWeeklyReport"), null, (_, _) => _ = OpenWeeklyReportAsync()));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem(
            L10n.Get("Menu_Pairing"), null, (_, _) => ShowPairing()));
        menu.Items.Add(_hideNumbersItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem(L10n.Get("Menu_Quit"), null, (_, _) => ExitThread()));

        _icon = new NotifyIcon
        {
            Icon = TrayIconRenderer.Render(null),
            Text = GlanceFormat.TrayTooltip(null),
            ContextMenuStrip = menu,
            Visible = true,
        };
        // NotifyIcon has no AccessibleObject; assistive tech reads .Text —
        // GlanceFormat.TrayTooltip keeps it a full sentence, not bare digits.
        _icon.MouseClick += (_, e) =>
        {
            if (e.Button == MouseButtons.Left)
            {
                ShowFlyout();
            }
        };
        _icon.BalloonTipClicked += (_, _) => FlyoutForm.OpenUrl(_pendingToastUrl);

        _flyout.RefreshRequested += (_, _) => _ = RefreshAsync();

        _pollTimer = new System.Windows.Forms.Timer { Interval = PollIntervalMilliseconds };
        _pollTimer.Tick += (_, _) => _ = RefreshAsync();
        _pollTimer.Start();

        _ = RefreshAsync();
    }

    private void ShowFlyout()
    {
        _flyout.Render(_lastPayload, _statusText);
        _flyout.ShowNearTray();
    }

    private void ShowPairing()
    {
        using var form = new PairingForm(_pairingStore);
        if (form.ShowDialog() == DialogResult.OK)
        {
            _ = RefreshAsync();
        }
    }

    private async Task RefreshAsync()
    {
        if (_refreshing)
        {
            return;
        }
        _refreshing = true;
        try
        {
            var pairing = _pairingStore.Load();
            if (pairing is null)
            {
                _lastPayload = null;
                _statusText = L10n.Get("Flyout_NotPaired");
                return;
            }
            try
            {
                var snapshot = await _client.FetchGlanceAsync(pairing);
                _lastPayload = snapshot.Payload;
                _statusText = L10n.Format(
                    "Flyout_UpdatedAt", snapshot.FetchedAt.ToLocalTime().ToString("HH:mm"));
                await ToastNewAlertsAsync(pairing);
            }
            catch (GlanceUnauthorizedException)
            {
                _statusText = L10n.Get("Flyout_Unauthorized");
            }
            catch (GlanceClientException)
            {
                // Transport/decoding trouble: keep the cached briefing and say so.
                _statusText = L10n.Get("Flyout_Offline");
            }
        }
        finally
        {
            _refreshing = false;
            _icon.Text = GlanceFormat.TrayTooltip(_lastPayload);
            var previousIcon = _icon.Icon;
            _icon.Icon = TrayIconRenderer.Render(_lastPayload?.Energy.Score);
            previousIcon?.Dispose();
            if (_flyout.Visible)
            {
                _flyout.Render(_lastPayload, _statusText);
            }
        }
    }

    /// <summary>
    /// Toast alerts not yet seen on this machine. The server already gates
    /// noise (quiet hours / cooldown / daily budget — PLAN §11); this only
    /// prevents re-toasting the same event on every poll.
    /// </summary>
    private async Task ToastNewAlertsAsync(Pairing pairing)
    {
        AlertsPage page;
        try
        {
            page = await _client.FetchAlertsAsync(pairing, hours: 24, limit: 10);
        }
        catch (GlanceClientException)
        {
            return; // alert history is additive; the glance render already succeeded
        }
        var seen = _settings.Load().SeenAlertIds.ToHashSet(StringComparer.OrdinalIgnoreCase);
        var fresh = page.Data.Where(alert => !seen.Contains(alert.Id.ToString())).ToList();
        if (fresh.Count == 0)
        {
            return;
        }

        foreach (var alert in fresh.Take(MaxToastsPerPoll))
        {
            var grammar = NotificationGrammar.FromAlert(alert);
            _pendingToastUrl = grammar.DecisionUrl;
            var title = fresh.Count > 1
                ? $"{Truncate(grammar.Observation, 48)} (+{fresh.Count - 1})"
                : Truncate(grammar.Observation, 63);
            var body = grammar.Evidence + "\n" + grammar.Proposal
                + (grammar.DecisionUrl is not null ? "\n" + L10n.Get("Toast_WhyThisLink") : string.Empty);
            // Balloon budget: title 63 chars, body 255 (Shell_NotifyIcon).
            _icon.ShowBalloonTip(10_000, title, Truncate(body, 255), ToolTipIcon.None);
        }
        _settings.MarkAlertsSeen(fresh.Select(alert => alert.Id));
    }

    private async Task OpenWeeklyReportAsync()
    {
        var pairing = _pairingStore.Load();
        if (pairing is null)
        {
            ShowPairing();
            return;
        }
        try
        {
            var info = await _client.FetchWeeklyReportInfoAsync(pairing);
            FlyoutForm.OpenUrl(info.ReportUrl);
        }
        catch (GlanceClientException)
        {
            _statusText = L10n.Get("Flyout_Offline");
        }
    }

    private static string Truncate(string text, int max) =>
        text.Length <= max ? text : text[..(max - 1)] + "…";

    protected override void ExitThreadCore()
    {
        _pollTimer.Stop();
        _icon.Visible = false;
        _icon.Dispose();
        _flyout.Dispose();
        base.ExitThreadCore();
    }
}
