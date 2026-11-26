using System.Text.Json;
using System.Text.Json.Serialization;

namespace RemoteNotifications.Android;

/// <summary>
/// Android-only preferences kept beside (never inside) the desktop-compatible <c>settings.json</c>.
///
/// <see cref="BackgroundPollingRequested"/> records the user's last explicit choice so the tool page can
/// offer a one-tap resume. It is deliberately only an <em>intent</em>: the module must not acquire a
/// <c>background.activity</c> lease while the app is starting, so a fresh process always begins with
/// polling stopped and the surface offers the resume action instead of faking an enabled state.
/// </summary>
internal sealed class RemoteNotificationAndroidPreferences
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true
    };

    [JsonPropertyName("backgroundPollingRequested")]
    public bool BackgroundPollingRequested { get; init; }

    public static RemoteNotificationAndroidPreferences Load(string path)
    {
        try
        {
            if (!File.Exists(path))
            {
                return new RemoteNotificationAndroidPreferences();
            }

            return JsonSerializer.Deserialize<RemoteNotificationAndroidPreferences>(File.ReadAllText(path), JsonOptions)
                ?? new RemoteNotificationAndroidPreferences();
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return new RemoteNotificationAndroidPreferences();
        }
    }

    public static void Save(string path, RemoteNotificationAndroidPreferences preferences)
    {
        ArgumentNullException.ThrowIfNull(preferences);
        var directory = Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException("远程通知私有目录不可用。");
        Directory.CreateDirectory(directory);
        var temporary = $"{path}.{Guid.NewGuid():N}.tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(preferences, JsonOptions));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
        }
    }

    public static RemoteNotificationAndroidPreferences WithBackgroundPolling(bool requested) =>
        new() { BackgroundPollingRequested = requested };
}
