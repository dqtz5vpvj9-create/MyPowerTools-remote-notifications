namespace RemoteNotifications.Android;

/// <summary>
/// Stable identifiers shared by the Android module, the mobile surface, the packaged
/// manifests and the tests. Keeping them in one place is what lets a test assert that
/// <c>package/module.json</c>, <c>package/ui/tool.json</c> and the data-directory mapping
/// still agree after a rename.
/// </summary>
internal static class RemoteNotificationsAndroidOptions
{
    /// <summary>Module id, package id and tool id intentionally agree (file-transfer does the same).</summary>
    public const string ModuleId = "remote-notifications-android";

    public const string PackageId = ModuleId;

    public const string ToolId = ModuleId;

    public const string DisplayName = "远程通知";

    /// <summary>SecretStore entry that holds the OpenSSH Ed25519 private key on Android.</summary>
    public const string SigningKeySecretName = "signing-key";

    /// <summary>Title of the persistent foreground notification while background polling is active.</summary>
    public const string BackgroundActivityTitle = "远程通知接收";

    public const string SettingsFileName = "settings.json";

    /// <summary>Android-only preferences that must not be mixed into the shared desktop settings shape.</summary>
    public const string AndroidPreferencesFileName = "android-preferences.json";

    public const string HistoryFileName = "history.json";

    public const string SurfaceAssemblyFileName = "MyPowerTools.MobileNotifications.dll";

    public const string SurfaceTypeName = "MyPowerTools.MobileNotifications.MobileNotificationsSurfaceFactory";

    public const int DefaultPullLimit = 20;

    public const string CommandStatus = "remote-notifications-android.status";
    public const string CommandSyncNow = "remote-notifications-android.sync-now";
    public const string CommandInboxSummary = "remote-notifications-android.inbox.summary";
    public const string CommandInboxClear = "remote-notifications-android.inbox.clear";
    public const string CommandPollingStart = "remote-notifications-android.polling.start";
    public const string CommandPollingStop = "remote-notifications-android.polling.stop";
    public const string CommandConfigure = "remote-notifications-android.configure";
    public const string CommandSigningKeyImport = "remote-notifications-android.signing-key.import";
    public const string CommandSigningKeyClear = "remote-notifications-android.signing-key.clear";
    public const string CommandBannerPreview = "remote-notifications-android.banner.preview";

    public static IReadOnlyList<string> CommandIds { get; } =
    [
        CommandStatus,
        CommandSyncNow,
        CommandInboxSummary,
        CommandInboxClear,
        CommandPollingStart,
        CommandPollingStop,
        CommandConfigure,
        CommandSigningKeyImport,
        CommandSigningKeyClear,
        CommandBannerPreview
    ];

    /// <summary>Command arguments that are never echoed back or persisted in plain text.</summary>
    public const string SigningKeyArgument = "privateKey";

    public static Version ModuleVersion { get; } = new(0, 1, 0);
}

/// <summary>
/// Resolves the private data root the Android module and the mobile surface share.
///
/// The runtime hands a module <c>&lt;state&gt;/modules/&lt;moduleId&gt;/data</c>, while the Shell hands a
/// dotnet surface <c>&lt;state&gt;/tools/&lt;toolId&gt;</c>. Both must read and write the same
/// <c>settings.json</c> / <c>history.json</c>, otherwise the surface shows a second inbox and the
/// seen-id ring stops deduplicating notifications. The mapping below is the same structural
/// mapping the desktop AndroidTools adapter performs, restricted to the module layout so an
/// unexpected directory shape falls back to the module's own private directory instead of
/// guessing.
/// </summary>
internal static class RemoteNotificationAndroidPaths
{
    public static string ResolveDataRoot(string moduleDataDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(moduleDataDirectory);
        var data = new DirectoryInfo(Path.GetFullPath(moduleDataDirectory));
        var module = data.Parent;
        var modules = module?.Parent;
        var state = modules?.Parent;
        if (string.Equals(data.Name, "data", StringComparison.OrdinalIgnoreCase) &&
            string.Equals(module?.Name, RemoteNotificationsAndroidOptions.ModuleId, StringComparison.OrdinalIgnoreCase) &&
            string.Equals(modules?.Name, "modules", StringComparison.OrdinalIgnoreCase) &&
            state is not null)
        {
            return Path.Combine(state.FullName, "tools", RemoteNotificationsAndroidOptions.ToolId);
        }

        return data.FullName;
    }

    public static string SettingsPath(string dataRoot) =>
        Path.Combine(dataRoot, RemoteNotificationsAndroidOptions.SettingsFileName);

    public static string HistoryPath(string dataRoot) =>
        Path.Combine(dataRoot, RemoteNotificationsAndroidOptions.HistoryFileName);

    public static string AndroidPreferencesPath(string dataRoot) =>
        Path.Combine(dataRoot, RemoteNotificationsAndroidOptions.AndroidPreferencesFileName);
}
