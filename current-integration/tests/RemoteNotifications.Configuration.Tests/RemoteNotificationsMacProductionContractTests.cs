using System.Text.Json;

namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationsMacProductionContractTests
{
    private static string ServiceRoot =>
        Path.Combine(TestPaths.IntegrationRoot, "src", "RemoteNotifications.Service");

    [Fact]
    public void Mac_service_manifest_runs_the_helper_and_shares_the_surface_data_root()
    {
        var manifestPath = Path.Combine(ServiceRoot, "unit-manifest.macos.json");
        using var manifest = JsonDocument.Parse(File.ReadAllText(manifestPath));
        var root = manifest.RootElement;
        const string helperExecutable =
            "../../Helpers/MyPowerTools Remote Notifications.app/Contents/MacOS/RemoteNotifications.Service";
        const string helperWorkingDirectory =
            "../../Helpers/MyPowerTools Remote Notifications.app/Contents/MacOS";
        const string dataRoot =
            "~/Library/Application Support/MyPowerTools/state/tools/remote-notifications";

        Assert.Equal(helperExecutable, root.GetProperty("exec").GetString());
        Assert.Equal(helperWorkingDirectory, root.GetProperty("workingDirectory").GetString());
        Assert.Equal(
            dataRoot,
            root.GetProperty("environment").GetProperty("MPT_TOOL_DATA_ROOT").GetString());
        Assert.Equal(dataRoot, root.GetProperty("dataRoots")[0].GetString());
    }

    [Fact]
    public void Worker_uses_one_service_owner_and_preserves_product_activation()
    {
        var guard = File.ReadAllText(
            Path.Combine(ServiceRoot, "RemoteNotificationsServiceProcessGuard.cs"));

        Assert.Contains("[ModuleInitializer]", guard, StringComparison.Ordinal);
        Assert.Contains("ToolActivationProtocol.ArgumentName", guard, StringComparison.Ordinal);
        Assert.Contains("LegacyActivationArgument", guard, StringComparison.Ordinal);
        Assert.Contains("FileShare.None", guard, StringComparison.Ordinal);
        Assert.Contains("MacUmask(OwnerOnlyMask)", guard, StringComparison.Ordinal);
        Assert.Contains("LinuxUmask(OwnerOnlyMask)", guard, StringComparison.Ordinal);
        Assert.Contains("Environment.Exit(17)", guard, StringComparison.Ordinal);
    }

    [Fact]
    public void Recording_backend_requires_the_real_installed_mac_helper()
    {
        var factory = File.ReadAllText(
            Path.Combine(ServiceRoot, "RemoteNotificationDesktopServiceFactory.cs"));

        Assert.Contains("MPT_REMOTE_NOTIFICATIONS_ALLOW_TEST_BACKEND", factory, StringComparison.Ordinal);
        Assert.Contains("ValidateMacRecordingHost", factory, StringComparison.Ordinal);
        Assert.Contains("MyPowerTools Remote Notifications.app", factory, StringComparison.Ordinal);
        Assert.Contains("libMptMacNative.dylib", factory, StringComparison.Ordinal);
        Assert.Contains("GetAuthorizationStatus", factory, StringComparison.Ordinal);
        Assert.Contains("authorization is not", factory, StringComparison.Ordinal);
        Assert.Contains("could not initialize UserNotifications", factory, StringComparison.Ordinal);
    }

    [Fact]
    public void Observer_uses_the_supervised_service_as_the_only_network_owner()
    {
        var integrationRoot = TestPaths.IntegrationRoot;
        var moduleManifest = Path.Combine(
            integrationRoot,
            "modules",
            "android-tools-suite",
            "modules",
            "notifications",
            "module.json");
        using var manifest = JsonDocument.Parse(File.ReadAllText(moduleManifest));
        var entrypoint = Assert.Single(
            manifest.RootElement.GetProperty("entrypoints").EnumerateArray());
        var observer = File.ReadAllText(Path.Combine(
            integrationRoot,
            "src",
            "AndroidTools.MyPowerTools",
            "RemoteNotificationsServiceObserverModule.cs"));

        Assert.Equal(
            "RemoteNotificationsServiceObserverModule",
            entrypoint.GetProperty("type").GetString()!.Split('.').Last());
        Assert.Equal("inproc-dotnet", entrypoint.GetProperty("kind").GetString());
        Assert.InRange(entrypoint.GetProperty("priority").GetInt32(), 0, 100);
        Assert.Contains(
            manifest.RootElement.GetProperty("requires").EnumerateArray(),
            requirement =>
                requirement.GetProperty("capability").GetString() == "service.user" &&
                requirement.GetProperty("required").GetBoolean());
        Assert.Contains(
            "status.State == \"running\" ? \"module.running\" : \"server.disconnected\"",
            observer,
            StringComparison.Ordinal);
    }

    [Fact]
    public void Responsive_surface_has_a_compact_overflow_menu_and_horizontal_project_strip()
    {
        var viewRoot = Path.Combine(
            TestPaths.IntegrationRoot,
            "src",
            "RemoteNotifications.Surface",
            "Views");
        var xaml = File.ReadAllText(Path.Combine(viewRoot, "RemoteNotificationsView.axaml"));
        var responsive = File.ReadAllText(
            Path.Combine(viewRoot, "RemoteNotificationsView.Responsive.cs"));
        var interactions = File.ReadAllText(
            Path.Combine(viewRoot, "RemoteNotificationsView.axaml.cs"));

        Assert.Contains("x:Name=\"OverflowMenuButton\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"HeaderActions\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"ConnectionExpander\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"LabelScroller\"", xaml, StringComparison.Ordinal);
        Assert.Contains("<StackPanel Orientation=\"Horizontal\" />", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("<WrapPanel", xaml, StringComparison.Ordinal);
        Assert.Contains("CompactToolbarThreshold", responsive, StringComparison.Ordinal);
        Assert.Contains("OnOverflowMenuClick", responsive, StringComparison.Ordinal);
        Assert.Contains("UpdateResponsiveLayout", responsive, StringComparison.Ordinal);
        Assert.Contains("OnLabelScrollerPointerMoved", interactions, StringComparison.Ordinal);
        Assert.Contains("OnLabelScrollerPointerWheelChanged", interactions, StringComparison.Ordinal);
    }
}
