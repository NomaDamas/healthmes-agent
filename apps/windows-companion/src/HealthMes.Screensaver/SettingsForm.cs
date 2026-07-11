using HealthMes.Windows.Common;

namespace HealthMes.Screensaver;

/// <summary>
/// The screensaver's /c dialog — where the issue-#11 privacy toggle lives
/// ("hide health numbers": shared spaces / screen sharing). Pairing itself
/// is done in the tray app; this dialog only SHOWS pairing status so the
/// saver stays a read-only surface. Keyboard-first: checkbox + OK/Cancel,
/// Enter accepts, Esc cancels; controls carry accessible names for Narrator.
/// </summary>
internal sealed class SettingsForm : Form
{
    private readonly LocalSettings _settings = new();
    private readonly CheckBox _hideNumbers = new();

    public SettingsForm()
    {
        Text = L10n.Get("Settings_Title");
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(420, 150);
        Padding = new Padding(12);

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1 };

        _hideNumbers.Text = L10n.Get("Settings_HideNumbers");
        _hideNumbers.AccessibleName = L10n.Get("Settings_HideNumbers");
        _hideNumbers.AutoSize = true;
        _hideNumbers.Checked = _settings.Load().HideHealthNumbers;

        var pairing = new DpapiPairingStore(_settings).Load();
        var statusLabel = new Label
        {
            Text = pairing is null
                ? L10n.Get("Settings_NotPaired")
                : L10n.Format("Settings_PairedWith", pairing.BaseUrl.AbsoluteUri.TrimEnd('/')),
            ForeColor = SystemColors.GrayText,
            AutoSize = true,
            MaximumSize = new Size(380, 0),
            Margin = new Padding(3, 9, 3, 3),
        };
        statusLabel.AccessibleName = statusLabel.Text;

        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Bottom,
            AutoSize = true,
        };
        var okButton = new Button { Text = L10n.Get("Settings_OK"), AutoSize = true };
        var cancelButton = new Button
        {
            Text = L10n.Get("Settings_Cancel"),
            AutoSize = true,
            DialogResult = DialogResult.Cancel,
        };
        okButton.Click += (_, _) =>
        {
            _settings.Mutate(document => document.HideHealthNumbers = _hideNumbers.Checked);
            DialogResult = DialogResult.OK;
            Close();
        };
        buttons.Controls.Add(okButton);
        buttons.Controls.Add(cancelButton);

        layout.Controls.Add(_hideNumbers);
        layout.Controls.Add(statusLabel);
        Controls.Add(layout);
        Controls.Add(buttons);
        AcceptButton = okButton;
        CancelButton = cancelButton;
    }
}
