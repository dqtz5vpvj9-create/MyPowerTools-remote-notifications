using System.Text.RegularExpressions;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// Packaging drift guard. The Android host loads this module from a bundled APK asset directory, so
/// the identifiers in <c>module.json</c>, <c>ui/tool.json</c>, <c>commands.index.json</c> and the DLL
/// output manifest must agree with the compiled module and with the surface the parent agent bundles.
/// </summary>
public sealed class PackageManifestTests
{
    private static JsonObject Read(string relativePath) =>
        JsonNode.Parse(File.ReadAllText(Path.Combine(TestPaths.IntegrationRoot, relativePath)))!.AsObject();

    [Fact]
    public void Module_manifest_is_android_only_and_points_at_the_shipped_adapter()
    {
        var module = Read(Path.Combine("package", "module.json"));

        Assert.Equal("1.0", module["schemaVersion"]!.GetValue<string>());
        Assert.Equal(RemoteNotificationsAndroidOptions.ModuleId, module["id"]!.GetValue<string>());
        Assert.Equal(RemoteNotificationsAndroidOptions.PackageId, module["packageId"]!.GetValue<string>());
        Assert.Equal(RemoteNotificationsAndroidOptions.ModuleVersion.ToString(), module["version"]!.GetValue<string>());

        var entrypoint = Assert.Single(module["entrypoints"]!.AsArray())!.AsObject();
        Assert.Equal("inproc-dotnet", entrypoint["kind"]!.GetValue<string>());
        Assert.Equal("RemoteNotifications.Android.dll", entrypoint["assembly"]!.GetValue<string>());
        Assert.Equal(
            "RemoteNotifications.Android.RemoteNotificationsAndroidModule",
            entrypoint["type"]!.GetValue<string>());
        Assert.Equal(
            ["android-arm64", "android-x64"],
            entrypoint["platforms"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray());

        var declared = module["requires"]!.AsArray()
            .Select(node => node!.AsObject()["capability"]!.GetValue<string>())
            .ToArray();
        Assert.Contains("secret.store", declared);
        Assert.Contains("background.activity", declared);
        Assert.Contains("notification.desktop", declared);
    }

    [Fact]
    public void Tool_manifest_declares_the_mobile_surface_and_the_same_owner_module()
    {
        var tool = Read(Path.Combine("package", "ui", "tool.json"));

        Assert.Equal(RemoteNotificationsAndroidOptions.ToolId, tool["toolId"]!.GetValue<string>());
        Assert.Equal(RemoteNotificationsAndroidOptions.ModuleId, tool["ownerModuleId"]!.GetValue<string>());
        Assert.Equal("dotnet-surface", tool["type"]!.GetValue<string>());

        // The Android deep-link host activates dynamic tools with route id "main"; the tool must
        // therefore serve that route id (file-transfer does the same).
        Assert.Equal("main", tool["primaryRouteId"]!.GetValue<string>());
        var route = Assert.Single(tool["routes"]!.AsArray())!.AsObject();
        Assert.Equal("main", route["routeId"]!.GetValue<string>());
        var surface = route["surface"]!.AsObject();
        Assert.Equal("dotnet", surface["kind"]!.GetValue<string>());
        Assert.Equal(
            $"surface/{RemoteNotificationsAndroidOptions.SurfaceAssemblyFileName}",
            surface["assembly"]!.GetValue<string>());
        Assert.Equal(RemoteNotificationsAndroidOptions.SurfaceTypeName, surface["type"]!.GetValue<string>());

        var prefixes = tool["activationUriPrefixes"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Contains("mypowertools://remote-notification", prefixes);
    }

    [Fact]
    public async Task Every_static_command_index_entry_maps_to_a_module_command_or_a_navigation_route()
    {
        var index = Read(Path.Combine("package", "commands.index.json"));
        var indexed = index["commands"]!.AsArray()
            .Select(node => node!.AsObject()["id"]!.GetValue<string>())
            .ToHashSet(StringComparer.Ordinal);

        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();
        var implemented = (await harness.Module.ListCommandsAsync(CancellationToken.None))
            .Select(descriptor => descriptor.Id)
            .ToHashSet(StringComparer.Ordinal);

        // Every palette entry must resolve: either a real module command or the navigation route.
        var unresolvable = indexed
            .Where(id => !implemented.Contains(id) && id != "remote-notifications-android.open-inbox")
            .ToArray();
        Assert.Empty(unresolvable);

        // The commands a user can run without arguments must stay reachable from the palette.
        string[] core =
        [
            RemoteNotificationsAndroidOptions.CommandStatus,
            RemoteNotificationsAndroidOptions.CommandSyncNow,
            RemoteNotificationsAndroidOptions.CommandInboxSummary,
            RemoteNotificationsAndroidOptions.CommandPollingStart,
            RemoteNotificationsAndroidOptions.CommandPollingStop,
            RemoteNotificationsAndroidOptions.CommandInboxClear
        ];
        Assert.All(core, id => Assert.Contains(id, indexed));
    }

    [Fact]
    public void Package_files_declared_in_the_output_manifest_exist()
    {
        var manifest = JsonNode.Parse(File.ReadAllText(TestPaths.ManifestPath))!.AsObject();
        Assert.Equal(RemoteNotificationsAndroidOptions.PackageId, manifest["packageId"]!.GetValue<string>());
        Assert.Equal(
            $"tools/remote-notifications/android-integration/artifacts/package/{RemoteNotificationsAndroidOptions.PackageId}",
            manifest["packageRoot"]!.GetValue<string>());
        Assert.Equal(
            $"modules/{RemoteNotificationsAndroidOptions.PackageId}",
            manifest["apkAssetRoot"]!.GetValue<string>());

        var files = manifest["files"]!.AsArray().Select(node => node!.AsObject()).ToArray();
        Assert.NotEmpty(files);
        Assert.All(files, file => Assert.True(file["required"]!.GetValue<bool>()));

        // Files that live in the repository (templates and descriptors) must exist now; the two
        // compiled DLLs are produced by build.ps1, which re-checks the same manifest after building.
        foreach (var file in files)
        {
            var source = file["source"]!.GetValue<string>();
            if (!source.StartsWith("package/", StringComparison.Ordinal))
            {
                continue;
            }

            var path = Path.Combine(TestPaths.IntegrationRoot, source.Replace('/', Path.DirectorySeparatorChar));
            Assert.True(File.Exists(path), $"{source} is declared in the manifest but missing on disk.");
            Assert.Equal(
                source["package/".Length..],
                file["path"]!.GetValue<string>());
        }

        var declaredPaths = files.Select(file => file["path"]!.GetValue<string>()).ToHashSet(StringComparer.Ordinal);
        Assert.Contains("RemoteNotifications.Android.dll", declaredPaths);
        Assert.Contains($"ui/surface/{RemoteNotificationsAndroidOptions.SurfaceAssemblyFileName}", declaredPaths);
        Assert.Contains("BouncyCastle.Cryptography.dll", declaredPaths);
    }

    [Fact]
    public void Ui_surfaces_declared_by_the_module_exist()
    {
        var module = Read(Path.Combine("package", "module.json"));
        foreach (var relative in module["uiSurfaces"]!.AsArray().Select(node => node!.GetValue<string>()))
        {
            Assert.True(
                File.Exists(Path.Combine(TestPaths.IntegrationRoot, "package", relative.Replace('/', Path.DirectorySeparatorChar))),
                $"uiSurfaces entry '{relative}' is missing.");
        }

        foreach (var relative in module["tools"]!.AsArray().Select(node => node!.GetValue<string>()))
        {
            Assert.True(
                File.Exists(Path.Combine(TestPaths.IntegrationRoot, "package", relative.Replace('/', Path.DirectorySeparatorChar))),
                $"tools entry '{relative}' is missing.");
        }
    }

    [Fact]
    public void Mobile_surface_only_calls_commands_the_module_implements()
    {
        var surfaceSource = File.ReadAllText(Path.Combine(
            TestPaths.MobileSurfaceRoot,
            "MobileNotificationsViewModel.cs"));
        var referenced = Regex.Matches(surfaceSource, "\"remote-notifications-android\\.[a-z0-9.-]+\"")
            .Select(match => match.Value.Trim('"'))
            .Distinct(StringComparer.Ordinal)
            .OrderBy(id => id, StringComparer.Ordinal)
            .ToArray();

        Assert.NotEmpty(referenced);
        Assert.All(referenced, id => Assert.Contains(id, RemoteNotificationsAndroidOptions.CommandIds));
    }

    [Fact]
    public void Mobile_surface_is_not_a_second_copy_of_the_product_sources()
    {
        // The surface links the shipped RemoteNotifications.Surface sources; it must not contain a
        // private re-implementation of the store, the settings model or the message formatting.
        var files = Directory.EnumerateFiles(TestPaths.MobileSurfaceRoot, "*.cs", SearchOption.TopDirectoryOnly)
            .Select(Path.GetFileName)
            .ToArray();
        Assert.DoesNotContain("RemoteNotificationsLegacyStore.cs", files);
        Assert.DoesNotContain("RemoteNotificationSettingsStore.cs", files);
        Assert.DoesNotContain("RemoteNotificationItemViewModels.cs", files);

        var project = File.ReadAllText(Path.Combine(TestPaths.MobileSurfaceRoot, "MyPowerTools.MobileNotifications.csproj"));
        Assert.Contains("current-integration/src/RemoteNotifications.Surface", project, StringComparison.Ordinal);
    }

    [Fact]
    public void Mobile_surface_does_not_poll_on_its_own()
    {
        // The module owns the single signed pull loop; the surface must only call module commands.
        var surfaceSource = File.ReadAllText(Path.Combine(
            TestPaths.MobileSurfaceRoot,
            "MobileNotificationsViewModel.cs"));
        Assert.DoesNotContain("RemoteNotificationHttpPoller", surfaceSource, StringComparison.Ordinal);
        Assert.DoesNotContain("HttpClient", surfaceSource, StringComparison.Ordinal);
        Assert.Contains("remote-notifications-android.sync-now", surfaceSource, StringComparison.Ordinal);
    }
}
