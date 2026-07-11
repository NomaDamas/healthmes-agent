using HealthMes.Glance.Core;
using HealthMes.Windows.Common;

namespace HealthMes.Tray;

/// <summary>
/// Pair the desktop with the user's OWN healthmes instance (base URL +
/// bearer token) — the only network destination this app ever has. The
/// token lands DPAPI-protected via <see cref="DpapiPairingStore"/>.
/// Keyboard-first: labeled fields, Enter saves, Esc cancels.
/// </summary>
internal sealed class PairingForm : Form
{
    private readonly TextBox _baseUrlBox = new();
    private readonly TextBox _tokenBox = new();
    private readonly Label _errorLabel = new();
    private readonly IPairingStore _store;

    public PairingForm(IPairingStore store)
    {
        _store = store;
        Text = L10n.Get("Pairing_Title");
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(420, 210);
        Padding = new Padding(12);

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1 };

        var urlLabel = new Label { Text = L10n.Get("Pairing_BaseUrlLabel"), AutoSize = true };
        _baseUrlBox.Width = 380;
        _baseUrlBox.AccessibleName = L10n.Get("Pairing_BaseUrlLabel");
        _baseUrlBox.PlaceholderText = "http://192.168.1.20:8100";
        _baseUrlBox.Text = _store.Load()?.BaseUrl.AbsoluteUri.TrimEnd('/') ?? string.Empty;

        var tokenLabel = new Label { Text = L10n.Get("Pairing_TokenLabel"), AutoSize = true };
        _tokenBox.Width = 380;
        _tokenBox.UseSystemPasswordChar = true;
        _tokenBox.AccessibleName = L10n.Get("Pairing_TokenLabel");

        _errorLabel.ForeColor = Color.Firebrick;
        _errorLabel.AutoSize = true;
        _errorLabel.MaximumSize = new Size(380, 0);

        var note = new Label
        {
            Text = L10n.Get("Pairing_LocalFirstNote"),
            ForeColor = SystemColors.GrayText,
            AutoSize = true,
            MaximumSize = new Size(380, 0),
        };

        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Bottom,
            AutoSize = true,
        };
        var saveButton = new Button { Text = L10n.Get("Pairing_Save"), AutoSize = true };
        var cancelButton = new Button
        {
            Text = L10n.Get("Pairing_Cancel"),
            AutoSize = true,
            DialogResult = DialogResult.Cancel,
        };
        saveButton.Click += (_, _) => Save();
        buttons.Controls.Add(saveButton);
        buttons.Controls.Add(cancelButton);

        layout.Controls.Add(urlLabel);
        layout.Controls.Add(_baseUrlBox);
        layout.Controls.Add(tokenLabel);
        layout.Controls.Add(_tokenBox);
        layout.Controls.Add(_errorLabel);
        layout.Controls.Add(note);
        Controls.Add(layout);
        Controls.Add(buttons);

        AcceptButton = saveButton;
        CancelButton = cancelButton;
    }

    private void Save()
    {
        try
        {
            var baseUrl = Pairing.NormalizeBaseUrl(_baseUrlBox.Text);
            _store.Save(new Pairing(baseUrl, _tokenBox.Text));
            DialogResult = DialogResult.OK;
            Close();
        }
        catch (PairingException)
        {
            _errorLabel.Text = L10n.Get("Pairing_InvalidUrl");
            _errorLabel.AccessibleName = _errorLabel.Text;
            _baseUrlBox.Focus();
        }
    }
}
