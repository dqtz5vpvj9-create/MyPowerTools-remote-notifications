using System.Text.Json;

namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationsMacProductionContractTests
{
    private static string ServiceRoot =>
        Path.Combine(TestPaths.IntegrationRoot, "src", "RemoteNotifications.Service");

    [Fact]
    public void Mac_service_uses_the_helper_bundle_and_remote_notifications_tool_data_directory()
    {
        var manifestPath = Path.Combine(ServiceRoot, "unit-manifest.macos.json");
        using var manifest = JsonDocument.Parse(File.ReadAllText(manifestPath));
        var root = manifest.RootElement;
        var environment = root.GetProperty("environment");
        var expectedDataRoot = "~/Library/Application Support/MyPowerTools/state/tools/remote-notifications";
        const string expectedExecutable =
            "../../Helpers/MyPowerTools Remote Notifications.app/Contents/MacOS/RemoteNotifications.Service";
        const string expectedWorkingDirectory =
            "../../Helpers/MyPowerTools Remote Notifications.app/Contents/MacOS";

        Assert.Equal(expectedExecutable, root.GetProperty("exec").GetString());
        Assert.Equal(expectedWorkingDirectory, root.GetProperty("workingDirectory").GetString());
        Assert.Equal(expectedDataRoot, environment.GetProperty("MPT_TOOL_DATA_ROOT").GetString());
        Assert.Equal("1", environment.GetProperty("MPT_REMOTE_NOTIFICATIONS_SKIP_LEGACY_IMPORT").GetString());
        Assert.Equal(expectedDataRoot, root.GetProperty("dataRoots")[0].GetString());
    }

    [Fact]
    public void Mac_native_bridge_registers_early_and_waits_for_delivery_completion()
    {
        var source = File.ReadAllText(
            Path.Combine(ServiceRoot, "native", "RemoteNotificationsMac.mm"));
        var managed = File.ReadAllText(
            Path.Combine(ServiceRoot, "MacUserNotificationService.cs"));

        Assert.Contains("remote_notifications_mac_initialize", source, StringComparison.Ordinal);
        Assert.Contains("center.delegate = NotificationDelegateInstance()", source, StringComparison.Ordinal);
        Assert.Contains("Native.Initialize()", managed, StringComparison.Ordinal);
        Assert.Contains("getNotificationSettingsWithCompletionHandler", source, StringComparison.Ordinal);
        Assert.Contains("requestAuthorizationWithOptions", source, StringComparison.Ordinal);
        Assert.Contains("addNotificationRequest:request withCompletionHandler", source, StringComparison.Ordinal);
        Assert.Contains("dispatch_semaphore_wait", source, StringComparison.Ordinal);
        Assert.Contains("RemoteNotificationsMacAuthorizationDenied", source, StringComparison.Ordinal);
        Assert.Contains("RemoteNotificationsMacTimedOut", source, StringComparison.Ordinal);
        Assert.Contains("NSWorkspace.sharedWorkspace openURL", source, StringComparison.Ordinal);
        Assert.DoesNotContain("withCompletionHandler:nil", source, StringComparison.Ordinal);
    }

    [Fact]
    public void Mac_native_bridge_is_published_for_apple_silicon_and_intel()
    {
        var project = File.ReadAllText(
            Path.Combine(ServiceRoot, "RemoteNotifications.Service.csproj"));

        Assert.Contains("osx-arm64", project, StringComparison.Ordinal);
        Assert.Contains("osx-x64", project, StringComparison.Ordinal);
        Assert.Contains("libRemoteNotificationsMac.dylib", project, StringComparison.Ordinal);
        Assert.Contains("AfterTargets=\"Publish\"", project, StringComparison.Ordinal);
        Assert.Contains("-framework UserNotifications", project, StringComparison.Ordinal);
    }

    [Fact]
    public void Recording_backend_cannot_hide_a_broken_helper_bundle()
    {
        var source = File.ReadAllText(
            Path.Combine(ServiceRoot, "MacUserNotificationService.cs"));

        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_ALLOW_TEST_BACKEND", source, StringComparison.Ordinal);
        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_MODE", source, StringComparison.Ordinal);
        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_RECORD_PATH", source, StringComparison.Ordinal);
        Assert.Contains("EnsureNativeInitializationSucceeded", source, StringComparison.Ordinal);
        Assert.Contains("not running from the signed MyPowerTools helper bundle", source, StringComparison.Ordinal);
    }

    [Fact]
    public void Worker_enforces_single_instance_and_owner_only_unix_objects_without_blocking_activation()
    {
        var source = File.ReadAllText(
            Path.Combine(ServiceRoot, "RemoteNotificationsProcessSecurity.cs"));

        Assert.Contains("[ModuleInitializer]", source, StringComparison.Ordinal);
        Assert.Contains("ToolActivationProtocol.ArgumentName", source, StringComparison.Ordinal);
        Assert.Contains("LegacyActivationArgument", source, StringComparison.Ordinal);
        Assert.Contains("FileShare.None", source, StringComparison.Ordinal);
        Assert.Contains("remote-notifications.service.lock", source, StringComparison.Ordinal);
        Assert.Contains("MacUmask(OwnerOnlyMask)", source, StringComparison.Ordinal);
        Assert.Contains("LinuxUmask(OwnerOnlyMask)", source, StringComparison.Ordinal);
        Assert.Contains("UnixFileMode.UserRead | UnixFileMode.UserWrite", source, StringComparison.Ordinal);
        Assert.Contains("Another Remote Notifications worker already owns", source, StringComparison.Ordinal);
    }
}
