using System.Text.Json;
using HealthMes.Glance.Core;

namespace HealthMes.Windows.Common;

/// <summary>
/// On-disk conditional-GET cache (<c>%LOCALAPPDATA%\HealthMes\glance-cache.json</c>)
/// shared by the tray process and the screensaver so a fresh screensaver
/// launch can paint the last briefing immediately and every poll stays an
/// ETag revalidation. Plain JSON on the user's own disk — same trust domain
/// as the paired instance's own data directory.
/// </summary>
public sealed class FileSnapshotStore(string? path = null) : ISnapshotStore
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = false };

    private readonly string _path =
        path ?? Path.Combine(LocalSettings.DefaultDirectory(), "glance-cache.json");

    public CachedGlance? Load()
    {
        try
        {
            return File.Exists(_path)
                ? JsonSerializer.Deserialize<CachedGlance>(File.ReadAllText(_path), JsonOptions)
                : null;
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException)
        {
            return null; // treat a broken cache as a cold start
        }
    }

    public void Store(CachedGlance snapshot)
    {
        try
        {
            var directory = Path.GetDirectoryName(_path)!;
            Directory.CreateDirectory(directory);
            var temp = Path.Combine(directory, $".glance-{Guid.NewGuid():N}.tmp");
            File.WriteAllText(temp, JsonSerializer.Serialize(snapshot, JsonOptions));
            File.Move(temp, _path, overwrite: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // Cache persistence is best-effort; polling works without it.
        }
    }
}
