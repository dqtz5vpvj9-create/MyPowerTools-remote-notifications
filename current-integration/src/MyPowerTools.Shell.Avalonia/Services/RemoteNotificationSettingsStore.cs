using System.Text.Json;

namespace MyPowerTools.RemoteNotifications.Configuration;

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed record RemoteNotificationSettings(
    string Protocol,
    string Host,
    int Port,
    string Channel,
    int PollIntervalSeconds,
    string PrivateKeyPath,
    bool KeepWindowsBanners)
{
    public const string DefaultProtocol = "https";
    public const string DefaultHost = "message.lixinrui000.cn";
    public const int DefaultPort = 8888;
    public const string DefaultChannel = "default";
    public const int DefaultPollIntervalSeconds = 5;
    public const string DefaultPrivateKeyPath = "~/.ssh/id_ed25519";

    public static RemoteNotificationSettings Default { get; } = new(
        DefaultProtocol,
        DefaultHost,
        DefaultPort,
        DefaultChannel,
        DefaultPollIntervalSeconds,
        DefaultPrivateKeyPath,
        false);

    public string Endpoint => $"{Protocol}://{Host}:{Port}";

    public string ExpandedPrivateKeyPath => ExpandPath(PrivateKeyPath);

    public RemoteNotificationSettings Normalize()
    {
        return this with
        {
            Protocol = (Protocol ?? "").Trim().ToLowerInvariant(),
            Host = (Host ?? "").Trim(),
            Channel = (Channel ?? "").Trim(),
            PrivateKeyPath = (PrivateKeyPath ?? "").Trim()
        };
    }

    public RemoteNotificationSettingsValidation Validate()
    {
        var settings = Normalize();
        if (settings.Protocol is not ("http" or "https"))
        {
            return RemoteNotificationSettingsValidation.Failed("Protocol must be http or https.");
        }

        if (string.IsNullOrWhiteSpace(settings.Host) ||
            settings.Host.Any(char.IsWhiteSpace) ||
            settings.Host.Contains('/') ||
            settings.Host.Contains('\\'))
        {
            return RemoteNotificationSettingsValidation.Failed("Host must contain a DNS name or IP address without a scheme or path.");
        }

        if (settings.Port is < 1 or > 65535)
        {
            return RemoteNotificationSettingsValidation.Failed("Port must be between 1 and 65535.");
        }

        if (string.IsNullOrWhiteSpace(settings.Channel) ||
            settings.Channel.Length > 128 ||
            settings.Channel.Any(char.IsControl))
        {
            return RemoteNotificationSettingsValidation.Failed("Channel must contain 1 to 128 printable characters.");
        }

        if (settings.PollIntervalSeconds is < 5 or > 3600)
        {
            return RemoteNotificationSettingsValidation.Failed("Poll interval must be between 5 and 3600 seconds.");
        }

        if (string.IsNullOrWhiteSpace(settings.PrivateKeyPath))
        {
            return RemoteNotificationSettingsValidation.Failed("SSH private key path is required.");
        }

        try
        {
            _ = settings.ExpandedPrivateKeyPath;
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException)
        {
            return RemoteNotificationSettingsValidation.Failed($"SSH private key path is invalid: {exception.Message}");
        }

        return RemoteNotificationSettingsValidation.Succeeded(settings);
    }

    private static string ExpandPath(string path)
    {
        var expanded = Environment.ExpandEnvironmentVariables((path ?? "").Trim());
        if (expanded == "~")
        {
            return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }

        if (expanded.StartsWith("~/", StringComparison.Ordinal) ||
            expanded.StartsWith("~\\", StringComparison.Ordinal))
        {
            expanded = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                expanded[2..]);
        }

        return Path.GetFullPath(expanded);
    }
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed record RemoteNotificationSettingsValidation(
    bool IsValid,
    string Error,
    RemoteNotificationSettings? Settings)
{
    public static RemoteNotificationSettingsValidation Succeeded(RemoteNotificationSettings settings) =>
        new(true, "", settings);

    public static RemoteNotificationSettingsValidation Failed(string error) =>
        new(false, error, null);
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
interface IRemoteNotificationSettingsStore
{
    string SettingsPath { get; }
    RemoteNotificationSettings Load();
    void Save(RemoteNotificationSettings settings);
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed class RemoteNotificationSettingsStore : IRemoteNotificationSettingsStore
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true
    };

    public RemoteNotificationSettingsStore(string? settingsPath = null)
    {
        SettingsPath = string.IsNullOrWhiteSpace(settingsPath)
            ? GetDefaultSettingsPath()
            : Path.GetFullPath(settingsPath);
    }

    public string SettingsPath { get; }

    public RemoteNotificationSettings Load()
    {
        if (!File.Exists(SettingsPath))
        {
            return RemoteNotificationSettings.Default;
        }

        try
        {
            var loaded = JsonSerializer.Deserialize<RemoteNotificationSettings>(
                File.ReadAllText(SettingsPath),
                JsonOptions);
            if (loaded is null)
            {
                return RemoteNotificationSettings.Default;
            }

            var normalized = loaded.Normalize();
            return normalized.Validate().IsValid ? normalized : RemoteNotificationSettings.Default;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return RemoteNotificationSettings.Default;
        }
    }

    public void Save(RemoteNotificationSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        var validation = settings.Validate();
        if (!validation.IsValid || validation.Settings is null)
        {
            throw new ArgumentException(validation.Error, nameof(settings));
        }

        var directory = Path.GetDirectoryName(SettingsPath)
            ?? throw new InvalidOperationException("Remote notification settings directory could not be resolved.");
        Directory.CreateDirectory(directory);
        var temporaryPath = $"{SettingsPath}.{Guid.NewGuid():N}.tmp";
        try
        {
            File.WriteAllText(temporaryPath, JsonSerializer.Serialize(validation.Settings, JsonOptions));
            File.Move(temporaryPath, SettingsPath, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    public static string GetDefaultSettingsPath()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
        {
            localAppData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".mypowertools");
        }

        return Path.Combine(localAppData, "MyPowerTools", "RemoteNotifications", "settings.json");
    }
}
