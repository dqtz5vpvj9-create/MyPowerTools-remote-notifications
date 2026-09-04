using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using MyPowerTools.Abstractions;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace AndroidTools.MyPowerTools;

public sealed partial class RemoteNotificationsServiceObserverModule
{
    public ValueTask<SettingsSchemaDocument> GetSettingsSchemaAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.FromResult(new SettingsSchemaDocument(Id, """
        {
          "type": "object",
          "properties": {
            "enabled": { "type": "boolean", "default": true },
            "serverProtocol": {
              "type": "string",
              "enum": ["http", "https"],
              "default": "https"
            },
            "serverHost": { "type": "string" },
            "serverPort": {
              "type": "integer",
              "minimum": 1,
              "maximum": 65535
            },
            "defaultChannel": { "type": "string", "default": "default" },
            "pollIntervalSeconds": {
              "type": "integer",
              "minimum": 5,
              "maximum": 3600,
              "default": 5
            },
            "privateKeyPath": {
              "type": "string",
              "default": "~/.ssh/id_ed25519"
            },
            "keepWindowsBanners": { "type": "boolean", "default": false }
          }
        }
        """));
    }

    public ValueTask<SettingsSnapshotDocument> GetSettingsAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ThrowIfDisposed();
        var settings = SettingsStore.Load();
        return ValueTask.FromResult(new SettingsSnapshotDocument(
            Id,
            checked((ulong)Math.Max(1, Interlocked.Read(ref _settingsRevision))),
            ToSettingsJson(settings),
            DateTimeOffset.UtcNow));
    }

    public ValueTask<SettingsValidationResult> ValidateSettingsAsync(
        SettingsPatch patch,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ThrowIfDisposed();
        try
        {
            var validation = FromSettingsJson(
                patch.Patch, SettingsStore.Load()).Validate();
            return ValueTask.FromResult(validation.IsValid
                ? new SettingsValidationResult(true, [])
                : new SettingsValidationResult(false, [validation.Error]));
        }
        catch (Exception exception) when (
            exception is ArgumentException or InvalidOperationException or FormatException)
        {
            return ValueTask.FromResult(
                new SettingsValidationResult(false, [exception.Message]));
        }
    }

    public async ValueTask<SettingsSnapshotDocument> ApplySettingsAsync(
        SettingsSnapshotDocument snapshot,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ThrowIfDisposed();
        var validation = FromSettingsJson(
            snapshot.Values, SettingsStore.Load()).Validate();
        if (!validation.IsValid || validation.Settings is null)
        {
            throw new ArgumentException(validation.Error, nameof(snapshot));
        }

        SettingsStore.Save(validation.Settings);
        var revision = Interlocked.Increment(ref _settingsRevision);
        _ = await RemoteNotificationServiceControl.TrySendAsync(
            "poll", null, cancellationToken).ConfigureAwait(false);
        return snapshot with
        {
            Revision = checked((ulong)revision),
            Values = ToSettingsJson(validation.Settings),
            UpdatedAt = DateTimeOffset.UtcNow
        };
    }

    public ValueTask<IReadOnlyList<UiSurfaceDescriptor>> ListSurfacesAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        IReadOnlyList<UiSurfaceDescriptor> surfaces =
        [
            new(
                "android-tools.notifications.dashboard",
                "dashboard-card",
                "Remote Notifications",
                new JsonObject { ["moduleId"] = Id }),
            new(
                "android-tools.notifications.detail",
                "detail-page",
                "Remote Notifications",
                new JsonObject { ["moduleId"] = Id })
        ];
        return ValueTask.FromResult(surfaces);
    }

    private (RemoteNotificationsSnapshot Value, string Error) LoadSnapshot()
    {
        try
        {
            return (Store.Load(), "");
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return (
                new RemoteNotificationsSnapshot([], [], null, false, []),
                $"The persisted notification inbox could not be read: {exception.Message}");
        }
    }

    private static RemoteNotificationSettings FromSettingsJson(
        JsonObject values,
        RemoteNotificationSettings current)
    {
        return new RemoteNotificationSettings(
            ReadString(values, "serverProtocol", current.Protocol),
            ReadString(values, "serverHost", current.Host),
            ReadInt(values, "serverPort", current.Port),
            ReadString(values, "defaultChannel", current.Channel),
            ReadInt(values, "pollIntervalSeconds", current.PollIntervalSeconds),
            ReadString(values, "privateKeyPath", current.PrivateKeyPath),
            ReadBool(values, "keepWindowsBanners", current.KeepWindowsBanners));
    }

    private static JsonObject ToSettingsJson(RemoteNotificationSettings settings)
    {
        return new JsonObject
        {
            ["enabled"] = true,
            ["serverProtocol"] = settings.Protocol,
            ["serverHost"] = settings.Host,
            ["serverPort"] = settings.Port,
            ["defaultChannel"] = settings.Channel,
            ["pollIntervalSeconds"] = settings.PollIntervalSeconds,
            ["privateKeyPath"] = settings.PrivateKeyPath,
            ["keepWindowsBanners"] = settings.KeepWindowsBanners
        };
    }

    private MptCommandDescriptor Command(
        string id,
        string title,
        string subtitle,
        int timeoutMs = 30000)
    {
        return new MptCommandDescriptor(
            id,
            Id,
            title,
            subtitle,
            "action",
            Category: "Android Tools",
            TimeoutMs: timeoutMs,
            Execution: new JsonObject { ["type"] = "module.execute" });
    }

    private static CommandExecutionResult Succeeded(
        CommandRequest request,
        string output)
    {
        return new CommandExecutionResult(
            request.InvocationId,
            request.CommandId,
            "succeeded",
            true,
            output);
    }

    private static CommandExecutionResult Failed(
        CommandRequest request,
        string code,
        string message,
        bool retryable = false)
    {
        return new CommandExecutionResult(
            request.InvocationId,
            request.CommandId,
            "failed",
            false,
            "",
            new MptRuntimeError(code, message, retryable));
    }

    private static string FirstFailure(IEnumerable<HealthCheckSnapshot> checks)
    {
        return checks.FirstOrDefault(check => !check.Ok)?.Message
            ?? "Remote Notifications is not ready.";
    }

    private static string ReadString(
        JsonObject? values,
        string key,
        string fallback)
    {
        if (values?[key] is JsonValue value &&
            value.TryGetValue<string>(out var result) &&
            !string.IsNullOrWhiteSpace(result))
        {
            return result;
        }
        return fallback;
    }

    private static int ReadInt(JsonObject values, string key, int fallback)
    {
        if (values[key] is JsonValue value)
        {
            if (value.TryGetValue<int>(out var integer))
            {
                return integer;
            }
            if (int.TryParse(
                value.ToString(),
                NumberStyles.Integer,
                CultureInfo.InvariantCulture,
                out integer))
            {
                return integer;
            }
        }
        return fallback;
    }

    private static bool ReadBool(JsonObject values, string key, bool fallback)
    {
        return values[key] is JsonValue value &&
            value.TryGetValue<bool>(out var result)
                ? result
                : fallback;
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }
}
