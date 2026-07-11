using System.Text.Json;
using System.Text.Json.Serialization;

namespace HealthMes.Windows.Common;

/// <summary>
/// The one settings file the tray app and the screensaver share:
/// <c>%LOCALAPPDATA%\HealthMes\settings.json</c>.
///
/// The bearer token is NEVER stored in the clear — it is DPAPI-protected
/// (CurrentUser scope) before landing in the file; see
/// <see cref="DpapiPairingStore"/>. Everything else (base URL, the
/// screensaver privacy toggle, seen-alert ids) is plain JSON on the user's
/// own disk, matching what the Android (EncryptedSharedPreferences for the
/// token only) and iOS (Keychain for the token only) companions do.
/// </summary>
public sealed class SettingsDocument
{
    [JsonPropertyName("base_url")]
    public string? BaseUrl { get; set; }

    /// <summary>Base64 of DPAPI-protected token bytes; null when token-less.</summary>
    [JsonPropertyName("token_dpapi")]
    public string? TokenDpapi { get; set; }

    /// <summary>
    /// Screensaver privacy toggle (issue #11): hide health numbers in shared
    /// spaces / while screen sharing. Read by the screensaver on every draw.
    /// </summary>
    [JsonPropertyName("hide_health_numbers")]
    public bool HideHealthNumbers { get; set; }

    /// <summary>Alert ids already toasted (bounded; newest kept).</summary>
    [JsonPropertyName("seen_alert_ids")]
    public List<string> SeenAlertIds { get; set; } = [];
}

public sealed class LocalSettings
{
    /// <summary>Most seen-alert ids kept; the /v1/alerts window is 24 h and budget-capped.</summary>
    public const int MaxSeenAlertIds = 200;

    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    private readonly string _path;

    public LocalSettings(string? path = null)
    {
        _path = path ?? DefaultPath();
    }

    public static string DefaultDirectory() =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "HealthMes");

    public static string DefaultPath() => Path.Combine(DefaultDirectory(), "settings.json");

    public string FilePath => _path;

    public SettingsDocument Load()
    {
        try
        {
            if (!File.Exists(_path))
            {
                return new SettingsDocument();
            }
            return JsonSerializer.Deserialize<SettingsDocument>(File.ReadAllText(_path), JsonOptions)
                ?? new SettingsDocument();
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException)
        {
            // A corrupt/locked settings file must never brick the surfaces;
            // the user simply re-pairs.
            return new SettingsDocument();
        }
    }

    public void Save(SettingsDocument document)
    {
        var directory = Path.GetDirectoryName(_path)!;
        Directory.CreateDirectory(directory);
        // Atomic-ish: write a sibling temp file, then move over the target,
        // so a crash mid-write never leaves half a settings file.
        var temp = Path.Combine(directory, $".settings-{Guid.NewGuid():N}.tmp");
        File.WriteAllText(temp, JsonSerializer.Serialize(document, JsonOptions));
        File.Move(temp, _path, overwrite: true);
    }

    public void Mutate(Action<SettingsDocument> mutate)
    {
        var document = Load();
        mutate(document);
        Save(document);
    }

    /// <summary>Record newly toasted alert ids, keeping the list bounded.</summary>
    public void MarkAlertsSeen(IEnumerable<Guid> ids)
    {
        Mutate(document =>
        {
            foreach (var id in ids)
            {
                var text = id.ToString();
                document.SeenAlertIds.Remove(text);
                document.SeenAlertIds.Insert(0, text);
            }
            if (document.SeenAlertIds.Count > MaxSeenAlertIds)
            {
                document.SeenAlertIds.RemoveRange(
                    MaxSeenAlertIds, document.SeenAlertIds.Count - MaxSeenAlertIds);
            }
        });
    }
}
