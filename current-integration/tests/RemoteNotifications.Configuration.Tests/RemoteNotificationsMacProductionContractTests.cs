using System.Text.Json;

namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationsMacProductionContractTests
{
    private static string ServiceRoot =>
        Path.Combine(TestPaths.IntegrationRoot, "src", "RemoteNotifications.Service");

    [Fact]
    public void Mac_service_uses_the_remote_notifications_tool_data_directory()
    {
        var manifestPath = Path.Combine(ServiceRoot, "unit-manifest.macos.json");
        using var manifest = JsonDocument.Parse(File.ReadAllText(manifestPath));
        var root = manifest.RootElement;
        var environment = root.GetProperty("environment");
        var expected = "~/Library/Application Support/MyPowerTools/state/tools/remote-notifications";

        Assert.Equal(expected, environment.GetProperty("MPT_TOOL_DATA_ROOT").GetString());
        Assert.Equal("1", environment.GetProperty("MPT_REMOTE_NOTIFICATIONS_SKIP_LEGACY_IMPORT").GetString());
        Assert.Equal(expected, root.GetProperty("dataRoots")[0].GetString());
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
    public void Recording_backend_requires_an_explicit_test_gate()
    {
        var source = File.ReadAllText(
            Path.Combine(ServiceRoot, "MacUserNotificationService.cs"));

        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_ALLOW_TEST_BACKEND", source, StringComparison.Ordinal);
        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_MODE", source, StringComparison.Ordinal);
        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_RECORD_PATH", source, StringComparison.Ordinal);
        Assert.Contains("using osascript fallback", source, StringComparison.Ordinal);
    }
}
