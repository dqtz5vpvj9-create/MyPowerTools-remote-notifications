namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationsReliabilityContractTests
{
    [Fact]
    public void Worker_retries_initialization_and_resumes_from_the_persisted_cursor()
    {
        var worker = File.ReadAllText(Path.Combine(
            TestPaths.IntegrationRoot,
            "src",
            "RemoteNotifications.Service",
            "Program.cs"));

        Assert.Contains("if (RunOnePollCycle(state, desktopNotifications, startupBackfillPending))", worker, StringComparison.Ordinal);
        Assert.Contains("settingsStore.LoadValidation()", worker, StringComparison.Ordinal);
        Assert.Contains("return pull.IsSuccess;", worker, StringComparison.Ordinal);
        Assert.Contains("var persistedWaterline = ResolveWaterline", worker, StringComparison.Ordinal);
        Assert.Contains("var performBackfill = startupBackfill && string.IsNullOrWhiteSpace(persistedWaterline);", worker, StringComparison.Ordinal);
        Assert.Contains("var shown = performBackfill", worker, StringComparison.Ordinal);
        Assert.Contains("performBackfill ? RemoteNotificationsLegacyStore.MaximumMessages : null", worker, StringComparison.Ordinal);
    }

    [Fact]
    public void Locally_injected_notifications_cannot_advance_the_remote_server_cursor()
    {
        var worker = File.ReadAllText(Path.Combine(
            TestPaths.IntegrationRoot,
            "src",
            "RemoteNotifications.Service",
            "Program.cs"));
        var injectStart = worker.IndexOf("static object HandleInjectCore", StringComparison.Ordinal);
        var helperStart = worker.IndexOf("// Helpers", injectStart, StringComparison.Ordinal);

        Assert.True(injectStart >= 0);
        Assert.True(helperStart > injectStart);
        var injectImplementation = worker[injectStart..helperStart]
            .Replace("\r\n", "\n", StringComparison.Ordinal);
        Assert.Contains("now.ToString(\"O\", CultureInfo.InvariantCulture),\n        \"\");", injectImplementation, StringComparison.Ordinal);
        Assert.DoesNotContain("now.ToString(\"O\", CultureInfo.InvariantCulture),\n        now.ToString", injectImplementation, StringComparison.Ordinal);
    }

    [Fact]
    public void Recording_backend_rejects_an_unavailable_native_notification_bridge()
    {
        var factory = File.ReadAllText(Path.Combine(
            TestPaths.IntegrationRoot,
            "src",
            "RemoteNotifications.Service",
            "RemoteNotificationDesktopServiceFactory.cs"));

        Assert.Contains("\"authorized\" or \"provisional\" or \"not-determined\" or \"denied\"", factory, StringComparison.Ordinal);
        Assert.DoesNotContain("string.Equals(authorization, \"unknown\"", factory, StringComparison.Ordinal);
    }

    [Fact]
    public void Observer_maps_the_runner_module_directory_to_the_shared_tool_store()
    {
        var observer = File.ReadAllText(Path.Combine(
            TestPaths.IntegrationRoot,
            "src",
            "AndroidTools.MyPowerTools",
            "RemoteNotificationsServiceObserverModule.cs"));

        Assert.Contains("ResolveSharedToolDataDirectory", observer, StringComparison.Ordinal);
        Assert.Contains("state.FullName, \"tools\", \"remote-notifications\"", observer, StringComparison.Ordinal);
        Assert.Contains("new RemoteNotificationsLegacyStore(_settingsStore, _sharedDataDirectory)", observer, StringComparison.Ordinal);
        Assert.Contains("[\"dataDirectory\"] = _sharedDataDirectory", observer, StringComparison.Ordinal);
    }
}
