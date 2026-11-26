using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// The shipped <c>RemoteNotificationsLegacyStore</c> serializes <c>history.json</c> with a lock. On
/// Android the original named <see cref="System.Threading.Mutex"/> cannot be created (the PAL tries
/// <c>/data/local/tmp/.dotnet-*</c> and gets EACCES, quarantining the module after three faults), so
/// the Android projects compile the same source with the simple name <c>Mutex</c> bound to
/// <see cref="RemoteNotificationAndroidFileLock"/>.
///
/// These tests contend over the real lock with real store instances, in one process and across two
/// processes, and verify that releasing it lets the writer proceed.
/// </summary>
public sealed class HistoryLockTests
{
    /// <summary>Exactly the name the shipped store computes for its state path.</summary>
    private static string LockNameFor(string statePath) =>
        $"Local\\MyPowerTools.RemoteNotifications.{Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(statePath)))[..24]}";

    private static RemoteNotificationsLegacyStore CreateStore(string root) =>
        new(new RemoteNotificationSettingsStore(Path.Combine(root, "settings.json")), root);

    private static RemoteNotificationRecord Record(string id, string message) =>
        new(id, "default", message, "codex", DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture));

    [Fact]
    public async Task History_lock_excludes_another_instance_on_another_thread()
    {
        using var directory = TestDirectory.Create();
        var name = LockNameFor(Path.Combine(directory.Path, "history.json"));

        using var holder = new RemoteNotificationAndroidFileLock(false, name);
        Assert.True(holder.WaitOne(TimeSpan.FromSeconds(5)));

        var blocked = await Task.Run(() =>
        {
            using var contender = new RemoteNotificationAndroidFileLock(false, name);
            return contender.WaitOne(TimeSpan.FromMilliseconds(300));
        });
        Assert.False(blocked);

        holder.ReleaseMutex();

        var acquired = await Task.Run(() =>
        {
            using var contender = new RemoteNotificationAndroidFileLock(false, name);
            return contender.WaitOne(TimeSpan.FromSeconds(5));
        });
        Assert.True(acquired);
    }

    [Fact]
    public async Task Shipped_store_write_waits_for_the_history_lock_and_then_succeeds()
    {
        using var directory = TestDirectory.Create();
        var historyPath = Path.Combine(directory.Path, "history.json");
        var store = CreateStore(directory.Path);
        using var holder = new RemoteNotificationAndroidFileLock(false, LockNameFor(historyPath));
        Assert.True(holder.WaitOne(TimeSpan.FromSeconds(5)));

        // If the compiled store still used a named mutex this would complete immediately, so the
        // pending task is the proof that the shipped store really uses this file lock.
        var write = Task.Run(() => store.SaveMessages([Record("n1", "[Codex] locked write")]));
        await Task.Delay(400);
        Assert.False(write.IsCompleted);

        holder.ReleaseMutex();
        await write.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Contains("locked write", await File.ReadAllTextAsync(historyPath), StringComparison.Ordinal);
        Assert.Single(store.Load().MessagesOldestFirst);
    }

    [Fact]
    public async Task Store_in_a_second_process_waits_for_the_history_lock_and_then_writes()
    {
        using var directory = TestDirectory.Create();
        var historyPath = Path.Combine(directory.Path, "history.json");
        using var holder = new RemoteNotificationAndroidFileLock(false, LockNameFor(historyPath));
        Assert.True(holder.WaitOne(TimeSpan.FromSeconds(5)));

        var startInfo = new ProcessStartInfo(TestPaths.DotnetHost)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        startInfo.ArgumentList.Add(TestPaths.LockProbeAssembly);
        startInfo.ArgumentList.Add(directory.Path);
        startInfo.Environment["MPT_DATA_ROOT"] = directory.Path;
        using var probe = Process.Start(startInfo)
            ?? throw new InvalidOperationException("The lock probe process could not be started.");

        // The child signals that it is inside the real store write before it can have the lock.
        Assert.True(
            await WaitForLineAsync(probe.StandardOutput, "STARTED", TimeSpan.FromSeconds(30)),
            "The lock probe did not start its store write.");
        var completed = WaitForLineAsync(probe.StandardOutput, "WRITE-OK", TimeSpan.FromSeconds(30));
        await Task.Delay(500);
        Assert.False(completed.IsCompleted);

        holder.ReleaseMutex();
        Assert.True(await completed);
        await probe.WaitForExitAsync();
        Assert.Equal(0, probe.ExitCode);
        Assert.Contains("probe write", await File.ReadAllTextAsync(historyPath), StringComparison.Ordinal);

        // The child closed its lock on exit, so this process can take it again.
        Assert.True(holder.WaitOne(TimeSpan.FromSeconds(5)));
        holder.ReleaseMutex();
    }

    [Fact]
    public async Task Shared_store_instances_wait_for_the_lock_instead_of_failing()
    {
        using var directory = TestDirectory.Create();
        var historyPath = Path.Combine(directory.Path, "history.json");
        var stores = Enumerable.Range(0, 3).Select(_ => CreateStore(directory.Path)).ToArray();
        var failures = new List<Exception>();
        var writers = Enumerable.Range(0, 24).Select(index => Task.Run(() =>
        {
            try
            {
                var store = stores[index % stores.Length];
                store.SaveMessages([Record($"n{index}", $"[Codex] writer {index}")]);
                _ = store.Load();
            }
            catch (Exception exception)
            {
                lock (failures)
                {
                    failures.Add(exception);
                }
            }
        })).ToArray();

        var all = Task.WhenAll(writers);
        while (!all.IsCompleted)
        {
            if (File.Exists(historyPath))
            {
                // The file must always be readable: the lock serializes the read-modify-write.
                _ = JsonNode.Parse(await File.ReadAllTextAsync(historyPath));
            }

            await Task.Delay(20);
        }

        await all;
        Assert.Empty(failures);
        var snapshot = stores[0].Load();
        Assert.NotEmpty(snapshot.MessagesOldestFirst);
        Assert.NotEmpty(snapshot.SeenMessageIds ?? []);
    }

    [Fact]
    public void Lock_file_is_rooted_in_the_app_private_state_directory()
    {
        using var directory = TestDirectory.Create();
        var name = LockNameFor(Path.Combine(directory.Path, "history.json"));

        using var mutex = new RemoteNotificationAndroidFileLock(false, name);
        Assert.True(mutex.WaitOne(TimeSpan.FromSeconds(5)));

        var expectedRoot = Path.Combine(directory.Path, "state", "notification-locks");
        var lockPath = mutex.LockPath;
        Assert.StartsWith(Path.GetFullPath(expectedRoot), Path.GetFullPath(lockPath), StringComparison.Ordinal);
        Assert.True(File.Exists(lockPath));
        Assert.DoesNotContain(
            Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar),
            Path.GetFullPath(lockPath),
            StringComparison.Ordinal);
        Assert.Single(Directory.GetFiles(expectedRoot, "*.lock"));
    }

    [Fact]
    public void Every_project_that_compiles_the_store_binds_the_android_lock()
    {
        string[] projects =
        [
            Path.Combine(TestPaths.IntegrationRoot, "src", "RemoteNotifications.Android", "RemoteNotifications.Android.csproj"),
            Path.Combine(TestPaths.MobileSurfaceRoot, "MyPowerTools.MobileNotifications.csproj"),
            Path.Combine(TestPaths.IntegrationRoot, "tests", "RemoteNotifications.Android.Tests", "RemoteNotifications.Android.Tests.csproj"),
            Path.Combine(TestPaths.IntegrationRoot, "tests", "RemoteNotifications.Android.LockProbe", "RemoteNotifications.Android.LockProbe.csproj")
        ];
        foreach (var project in projects)
        {
            var content = File.ReadAllText(project);
            Assert.Contains("RemoteNotificationAndroidFileLock", content, StringComparison.Ordinal);
            Assert.Contains("Alias=\"Mutex\"", content, StringComparison.Ordinal);
        }

        // The Android swap relies on the shipped store's own call shape; if upstream rewrites the lock
        // (or someone forks it locally), this guard fails before an on-device fault can come back.
        var store = File.ReadAllText(Path.Combine(
            TestPaths.RepoRoot,
            "tools", "remote-notifications", "current-integration", "src", "RemoteNotifications.Surface",
            "Services", "RemoteNotificationsLegacyStore.cs"));
        Assert.Contains("new Mutex(false, mutexName)", store, StringComparison.Ordinal);
        Assert.DoesNotContain("FileShare.None", store, StringComparison.Ordinal);
    }

    private static async Task<bool> WaitForLineAsync(StreamReader reader, string expected, TimeSpan timeout)
    {
        using var cancellation = new CancellationTokenSource(timeout);
        try
        {
            while (await reader.ReadLineAsync(cancellation.Token) is { } line)
            {
                if (string.Equals(line.Trim(), expected, StringComparison.Ordinal))
                {
                    return true;
                }
            }
        }
        catch (OperationCanceledException)
        {
        }

        return false;
    }
}
