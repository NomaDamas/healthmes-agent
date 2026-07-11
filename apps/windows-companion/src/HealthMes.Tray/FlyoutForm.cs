using HealthMes.Glance.Core;
using HealthMes.Windows.Common;

namespace HealthMes.Tray;

/// <summary>
/// The tray flyout: one glance payload rendered as the briefing sections
/// (energy + curve, next blocks, alerts with the "why this?" link, latest
/// decision). Information architecture is the §8.5 grammar; visual styling
/// is PLACEHOLDER (docs/design/WATCH-NOTIFICATIONS.ko.md owns the final UX).
///
/// Accessibility: every value carries an AccessibleName so Narrator reads
/// "Cognitive energy 58, confidence high" instead of bare digits (worksheet
/// §1.4); Tab walks the links/buttons; Esc closes.
/// </summary>
internal sealed class FlyoutForm : Form
{
    private readonly Label _energyLabel = new();
    private readonly SparklineControl _sparkline = new();
    private readonly Label _blocksHeader = new();
    private readonly Label[] _blockLabels = [new Label(), new Label(), new Label()];
    private readonly Label _alertsHeader = new();
    private readonly Label _alertsLabel = new();
    private readonly LinkLabel _whyThisLink = new();
    private readonly LinkLabel _latestDecisionLink = new();
    private readonly Label _statusLabel = new();
    private readonly Button _refreshButton = new();

    private string? _whyThisUrl;
    private string? _latestDecisionUrl;

    public event EventHandler? RefreshRequested;

    public FlyoutForm()
    {
        Text = L10n.Get("Flyout_Title");
        FormBorderStyle = FormBorderStyle.FixedToolWindow;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        KeyPreview = true;
        ClientSize = new Size(380, 356);
        Padding = new Padding(12);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            AutoScroll = true,
        };

        _energyLabel.Font = new Font(Font.FontFamily, 16f, FontStyle.Bold);
        _energyLabel.AutoSize = true;

        _sparkline.Size = new Size(340, 48);
        _sparkline.Margin = new Padding(3, 3, 3, 9);

        StyleHeader(_blocksHeader, L10n.Get("Flyout_BlocksSection"));
        foreach (var label in _blockLabels)
        {
            label.AutoSize = true;
            label.Margin = new Padding(9, 1, 3, 1);
        }

        StyleHeader(_alertsHeader, L10n.Get("Flyout_AlertsSection"));
        _alertsLabel.AutoSize = true;
        _alertsLabel.MaximumSize = new Size(340, 0);
        _alertsLabel.Margin = new Padding(9, 1, 3, 1);

        _whyThisLink.Text = L10n.Get("Flyout_WhyThis");
        _whyThisLink.AutoSize = true;
        _whyThisLink.Margin = new Padding(9, 1, 3, 6);
        _whyThisLink.AccessibleName = L10n.Get("Flyout_WhyThis");
        _whyThisLink.LinkClicked += (_, _) => OpenUrl(_whyThisUrl);

        _latestDecisionLink.Text = L10n.Get("Flyout_LatestDecision");
        _latestDecisionLink.AutoSize = true;
        _latestDecisionLink.Margin = new Padding(3, 6, 3, 1);
        _latestDecisionLink.AccessibleName = L10n.Get("Flyout_LatestDecision");
        _latestDecisionLink.LinkClicked += (_, _) => OpenUrl(_latestDecisionUrl);

        _statusLabel.AutoSize = true;
        _statusLabel.ForeColor = SystemColors.GrayText;
        _statusLabel.Margin = new Padding(3, 9, 3, 3);

        _refreshButton.Text = L10n.Get("Menu_Refresh");
        _refreshButton.AutoSize = true;
        _refreshButton.AccessibleName = L10n.Get("Menu_Refresh");
        _refreshButton.Click += (_, _) => RefreshRequested?.Invoke(this, EventArgs.Empty);

        layout.Controls.Add(_energyLabel);
        layout.Controls.Add(_sparkline);
        layout.Controls.Add(_blocksHeader);
        foreach (var label in _blockLabels)
        {
            layout.Controls.Add(label);
        }
        layout.Controls.Add(_alertsHeader);
        layout.Controls.Add(_alertsLabel);
        layout.Controls.Add(_whyThisLink);
        layout.Controls.Add(_latestDecisionLink);
        layout.Controls.Add(_statusLabel);
        layout.Controls.Add(_refreshButton);
        Controls.Add(layout);
    }

    private static void StyleHeader(Label label, string text)
    {
        label.Text = text;
        label.AutoSize = true;
        label.Font = new Font(label.Font, FontStyle.Bold);
        label.Margin = new Padding(3, 9, 3, 2);
    }

    /// <summary>Render a payload (or the honest empty/errored states).</summary>
    public void Render(GlancePayload? payload, string statusText)
    {
        if (payload is null)
        {
            _energyLabel.Text = "HealthMes";
            _energyLabel.AccessibleName = statusText;
            _sparkline.SetCurve([]);
            foreach (var label in _blockLabels)
            {
                label.Text = string.Empty;
            }
            _blockLabels[0].Text = L10n.Get("Flyout_NoBlocks");
            _alertsLabel.Text = string.Empty;
            _whyThisLink.Visible = false;
            _latestDecisionLink.Visible = false;
        }
        else
        {
            _energyLabel.Text = GlanceFormat.EnergyLine(payload);
            _energyLabel.AccessibleName = L10n.EnergyAccessibleText(payload);
            _sparkline.SetCurve(payload.Energy.Curve24h);

            for (var i = 0; i < _blockLabels.Length; i++)
            {
                if (i < payload.NextBlocks.Count)
                {
                    var line = GlanceFormat.BlockLine(payload.NextBlocks[i], payload.Timezone);
                    _blockLabels[i].Text = line;
                    _blockLabels[i].AccessibleName = line;
                }
                else
                {
                    _blockLabels[i].Text = i == 0 ? L10n.Get("Flyout_NoBlocks") : string.Empty;
                }
            }

            var alertsLine = GlanceFormat.AlertsLine(payload);
            _alertsLabel.Text = alertsLine;
            _alertsLabel.AccessibleName = alertsLine;

            _whyThisUrl = payload.Alerts.Top?.DecisionUrl;
            _whyThisLink.Visible = _whyThisUrl is not null;
            _latestDecisionUrl = payload.LatestDecision?.Url;
            _latestDecisionLink.Visible = _latestDecisionUrl is not null;
        }
        _statusLabel.Text = statusText;
        _statusLabel.AccessibleName = statusText;
    }

    /// <summary>Bottom-right of the working area, i.e. near the tray.</summary>
    public void ShowNearTray()
    {
        var area = Screen.PrimaryScreen?.WorkingArea
            ?? new Rectangle(0, 0, 1280, 720);
        Location = new Point(area.Right - Width - 8, area.Bottom - Height - 8);
        Show();
        Activate();
    }

    internal static void OpenUrl(string? url)
    {
        // Decision/report links come from the paired instance's payload;
        // only ever hand http(s) to the OS browser.
        if (url is null
            || !Uri.TryCreate(url, UriKind.Absolute, out var parsed)
            || (parsed.Scheme != Uri.UriSchemeHttp && parsed.Scheme != Uri.UriSchemeHttps))
        {
            return;
        }
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            // No browser association — nothing sensible to do from a flyout.
        }
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        if (e.KeyCode == Keys.Escape)
        {
            Hide();
            e.Handled = true;
        }
    }

    protected override void OnDeactivate(EventArgs e)
    {
        base.OnDeactivate(e);
        Hide();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        // The flyout is reused for the lifetime of the tray icon.
        if (e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            Hide();
        }
        base.OnFormClosing(e);
    }
}
