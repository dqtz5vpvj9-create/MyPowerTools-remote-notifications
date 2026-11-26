using System.Globalization;
using System.Reflection;
using MyPowerTools.Abstractions;
using MyPowerTools.Platform.Abstractions;
using RemoteNotifications.Surface.Services;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

// The desktop receiver resolves its store through the process-wide MPT_TOOL_DATA_ROOT variable,
// so the parity suite must not run concurrently with other tests.
[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// A scratch directory removed when the test finishes.
///
/// It is rooted in the tool's own git-ignored <c>artifacts/</c> tree - never in the OS temp
/// directory - and it becomes <c>MPT_DATA_ROOT</c> for the duration of the test, so the history lock
/// files the Android adapter creates stay inside the workspace as well.
/// </summary>
internal sealed class TestDirectory : IDisposable
{
    private static readonly object Gate = new();
    private static bool _rootCleaned;

    private TestDirectory(string path)
    {
        Path = path;
    }

    public string Path { get; }

    public static TestDirectory Create()
    {
        var root = System.IO.Path.Combine(TestPaths.IntegrationRoot, "artifacts", "test-state");
        lock (Gate)
        {
            if (!_rootCleaned)
            {
                // Serialized suite: stale scratch from a previous run is safe to drop.
                if (Directory.Exists(root))
                {
                    Directory.Delete(root, recursive: true);
                }

                _rootCleaned = true;
            }
        }

        var path = System.IO.Path.Combine(root, Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        Environment.SetEnvironmentVariable("MPT_DATA_ROOT", path);
        return new TestDirectory(path);
    }

    public void Dispose()
    {
        if (Directory.Exists(Path))
        {
            Directory.Delete(Path, recursive: true);
        }
    }
}

/// <summary>An OpenSSH ed25519 key generated from a fixed seed; no user credential is ever read.</summary>
internal sealed class TestSigningKey : IDisposable
{
    private readonly TestDirectory _directory;

    private TestSigningKey(TestDirectory directory, string path, string pem, Ed25519PrivateKeyParameters privateKey)
    {
        _directory = directory;
        Path = path;
        Pem = pem;
        PrivateKey = privateKey;
    }

    public string Path { get; }
    public string Pem { get; }
    public Ed25519PrivateKeyParameters PrivateKey { get; }

    public static TestSigningKey Create(int seedOffset = 0)
    {
        var directory = TestDirectory.Create();
        var path = System.IO.Path.Combine(directory.Path, "id_ed25519");
        var seed = Enumerable.Range(1, 32).Select(value => (byte)(value + seedOffset)).ToArray();
        var privateKey = new Ed25519PrivateKeyParameters(seed);
        var keyBlob = OpenSshPrivateKeyUtilities.EncodePrivateKey(privateKey);
        string pem;
        using (var writer = new StringWriter(CultureInfo.InvariantCulture))
        {
            using (var pemWriter = new PemWriter(writer))
            {
                pemWriter.WriteObject(new PemObject("OPENSSH PRIVATE KEY", keyBlob));
            }

            pem = writer.ToString();
        }

        File.WriteAllText(path, pem);
        return new TestSigningKey(directory, path, pem, privateKey);
    }

    public void Dispose() => _directory.Dispose();
}

/// <summary>Captures requests and replays canned responses; it never opens a socket.</summary>
internal sealed class RecordingHttpHandler(Func<HttpRequestMessage, HttpResponseMessage> respond)
    : HttpMessageHandler
{
    private int _calls;

    public List<HttpRequestMessage> Requests { get; } = [];
    public List<string> RequestUris { get; } = [];
    public int Calls => Volatile.Read(ref _calls);

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        Interlocked.Increment(ref _calls);
        Requests.Add(request);
        RequestUris.Add(request.RequestUri?.ToString() ?? "");
        return Task.FromResult(respond(request));
    }
}

internal sealed class FakeNotificationService : INotificationService
{
    public List<DesktopNotificationRequest> Published { get; } = [];
    public Exception? Failure { get; set; }

    public Task PublishAsync(string title, string body, CancellationToken cancellationToken) =>
        PublishAsync(new DesktopNotificationRequest(Guid.NewGuid().ToString("N"), title, body), cancellationToken);

    public Task PublishAsync(DesktopNotificationRequest request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (Failure is not null)
        {
            throw Failure;
        }

        Published.Add(request);
        return Task.CompletedTask;
    }
}

internal sealed class FakeBackgroundActivityService : IBackgroundActivityService
{
    public int AcquireCount { get; private set; }
    public int ReleaseCount { get; private set; }
    public int ActiveCount => AcquireCount - ReleaseCount;
    public Exception? Failure { get; set; }
    public List<string> Titles { get; } = [];

    public Task<IDisposable> BeginAsync(
        string moduleId,
        string title,
        bool waitingForPeers,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (Failure is not null)
        {
            throw Failure;
        }

        AcquireCount++;
        Titles.Add(title);
        return Task.FromResult<IDisposable>(new Lease(this));
    }

    private sealed class Lease(FakeBackgroundActivityService owner) : IDisposable
    {
        private int _released;

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _released, 1) == 0)
            {
                owner.ReleaseCount++;
            }
        }
    }
}

/// <summary>A pull client that never opens a socket and reports what the test wants it to say.</summary>
internal sealed class ScriptedPoller : IRemoteNotificationPoller
{
    private readonly Func<string, RemoteNotificationPullResult> _respond;

    public ScriptedPoller(Func<string, RemoteNotificationPullResult> respond)
    {
        _respond = respond;
    }

    public List<string> Waterlines { get; } = [];
    public int Calls { get; private set; }

    public Task<RemoteNotificationPullResult> PullAsync(string since, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Calls++;
        Waterlines.Add(since);
        return Task.FromResult(_respond(since));
    }
}

internal sealed class ModuleHarness : IAsyncDisposable
{
    private readonly TestDirectory _root = TestDirectory.Create();

    public ModuleHarness(
        FakeNotificationService? notifications = null,
        FakeBackgroundActivityService? background = null,
        InMemorySecretStore? secrets = null,
        bool withoutBackgroundCapability = false)
    {
        Notifications = notifications ?? new FakeNotificationService();
        Background = background ?? new FakeBackgroundActivityService();
        Secrets = secrets ?? new InMemorySecretStore();
        Module = new RemoteNotificationsAndroidModule();
        StateRoot = System.IO.Path.Combine(_root.Path, "state");
        DataDirectory = System.IO.Path.Combine(StateRoot, "modules", RemoteNotificationsAndroidOptions.ModuleId, "data");
        Context = new ModuleContext(
            "0.2.0",
            "1.0",
            RemoteNotificationsAndroidOptions.PackageId,
            RemoteNotificationsAndroidOptions.ModuleId,
            DataDirectory,
            System.IO.Path.Combine(StateRoot, "modules", RemoteNotificationsAndroidOptions.ModuleId, "cache"),
            System.IO.Path.Combine(_root.Path, "logs", RemoteNotificationsAndroidOptions.ModuleId),
            "android-arm64",
            ["lifecycle", "status", "commands", "settings", "logs"],
            BuildProviders(withoutBackgroundCapability));
    }

    private readonly List<TestSigningKey> _ownedKeys = [];

    private IReadOnlyDictionary<string, object> BuildProviders(bool withoutBackgroundCapability)
    {
        var providers = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase)
        {
            ["secret.store"] = Secrets,
            ["notification.desktop"] = Notifications
        };
        if (!withoutBackgroundCapability)
        {
            providers["background.activity"] = Background;
        }

        return providers;
    }

    public RemoteNotificationsAndroidModule Module { get; }
    public ModuleContext Context { get; }
    public FakeNotificationService Notifications { get; }
    public FakeBackgroundActivityService Background { get; }
    public InMemorySecretStore Secrets { get; }
    public string StateRoot { get; }
    public string DataDirectory { get; }

    /// <summary>The directory the tool surface receives from the Shell.</summary>
    public string ToolDataDirectory => System.IO.Path.Combine(
        StateRoot,
        "tools",
        RemoteNotificationsAndroidOptions.ToolId);

    public string HistoryPath => System.IO.Path.Combine(ToolDataDirectory, "history.json");

    public string SettingsPath => System.IO.Path.Combine(ToolDataDirectory, "settings.json");

    public async Task InitializeAsync()
    {
        var result = await Module.InitializeAsync(Context, CancellationToken.None);
        Assert.True(result.Ok, result.Error?.Message);
        await Module.EnableAsync(Context, CancellationToken.None);
        await Module.StartAsync(Context, CancellationToken.None);
    }

    /// <summary>Installs a scripted pull client and imports a real Ed25519 key so signing checks pass.</summary>
    public async Task<ScriptedPoller> UseScriptedPollerAsync(
        Func<string, RemoteNotificationPullResult> respond,
        TestSigningKey? key = null)
    {
        var poller = new ScriptedPoller(respond);
        Module.PollerFactory = (_, _) => poller;
        var material = key ?? TestSigningKey.Create();
        if (key is null)
        {
            _ownedKeys.Add(material);
        }

        await Secrets.SaveAsync(
            RemoteNotificationsAndroidOptions.ModuleId,
            RemoteNotificationsAndroidOptions.SigningKeySecretName,
            material.Pem,
            CancellationToken.None);
        return poller;
    }

    public async Task<RemoteNotificationSigningKey> ImportKeyAsync(TestSigningKey key)
    {
        await Secrets.SaveAsync(
            RemoteNotificationsAndroidOptions.ModuleId,
            RemoteNotificationsAndroidOptions.SigningKeySecretName,
            key.Pem,
            CancellationToken.None);
        return new RemoteNotificationSigningKey(Secrets, RemoteNotificationsAndroidOptions.ModuleId);
    }

    public async Task<bool> WaitUntilAsync(Func<bool> condition, TimeSpan timeout)
    {
        var deadline = DateTimeOffset.UtcNow + timeout;
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (condition())
            {
                return true;
            }

            await Task.Delay(20);
        }

        return condition();
    }

    public Task<CommandExecutionResult> ExecuteAsync(string commandId, JsonObject? args = null) =>
        Module.ExecuteCommandAsync(
            new CommandRequest(Guid.NewGuid().ToString("N"), commandId, args ?? new JsonObject()),
            CancellationToken.None).AsTask();

    public ValueTask DisposeAsync() => DisposeCoreAsync();

    private async ValueTask DisposeCoreAsync()
    {
        await Module.DisposeAsync(CancellationToken.None);
        foreach (var key in _ownedKeys)
        {
            key.Dispose();
        }

        _root.Dispose();
    }
}

internal static class TestPaths
{
    /// <summary>Repository root, resolved from the test assembly location.</summary>
    public static string RepoRoot { get; } = FindRepoRoot();

    /// <summary>Console probe that holds the history lock from a second process.</summary>
    public static string LockProbeAssembly { get; } = ResolveLockProbe();

    /// <summary>Host used to launch the lock probe.</summary>
    public static string DotnetHost { get; } = ResolveDotnetHost();

    public static string IntegrationRoot { get; } =
        System.IO.Path.Combine(RepoRoot, "tools", "remote-notifications", "android-integration");

    public static string PackageRoot { get; } =
        System.IO.Path.Combine(IntegrationRoot, "package");

    public static string ManifestPath { get; } =
        System.IO.Path.Combine(IntegrationRoot, "manifest", "android-package-manifest.json");

    public static string MobileSurfaceRoot { get; } =
        System.IO.Path.Combine(RepoRoot, "src", "MyPowerTools.MobileNotifications");

    private static string ResolveLockProbe()
    {
        var path = typeof(TestPaths).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(attribute => attribute.Key == "RemoteNotificationsLockProbe")
            ?.Value;
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new InvalidOperationException(
                "The lock probe path metadata is missing; add it to RemoteNotifications.Android.Tests.csproj.");
        }

        var resolved = System.IO.Path.GetFullPath(path);
        if (!File.Exists(resolved))
        {
            throw new FileNotFoundException(
                "The cross-process lock probe was not built; the ProjectReference must build it first.",
                resolved);
        }

        return resolved;
    }

    private static string ResolveDotnetHost()
    {
        var configured = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured))
        {
            return configured;
        }

        var process = Environment.ProcessPath;
        if (!string.IsNullOrWhiteSpace(process) &&
            System.IO.Path.GetFileNameWithoutExtension(process).StartsWith("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            return process;
        }

        foreach (var candidate in new[]
                 {
                     System.IO.Path.Combine(Environment.GetEnvironmentVariable("DOTNET_ROOT") ?? "", "dotnet"),
                     System.IO.Path.Combine(
                         Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                         ".dotnet",
                         "dotnet")
                 })
        {
            if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
            {
                return candidate;
            }
        }

        return "dotnet";
    }

    private static string FindRepoRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(System.IO.Path.Combine(current.FullName, "MyPowerTools.slnx")) &&
                Directory.Exists(System.IO.Path.Combine(current.FullName, "tools", "remote-notifications", "android-integration")))
            {
                return current.FullName;
            }

            current = current.Parent;
        }

        throw new DirectoryNotFoundException("The MyPowerTools repository root was not found.");
    }
}

internal static class TestJson
{
    public static HttpResponseMessage Ok(string body) => new(System.Net.HttpStatusCode.OK)
    {
        Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json")
    };

    public static HttpResponseMessage Status(System.Net.HttpStatusCode code, string body = "") => new(code)
    {
        Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json")
    };

    public static string Notification(
        string id,
        string message,
        string channel = "default",
        string icon = "info",
        string? serverTimestamp = null,
        string sessionId = "",
        string sessionName = "",
        string sourceClient = "",
        string contentKind = "",
        string sourceEventId = "",
        string stopReason = "")
    {
        var timestamp = serverTimestamp ?? DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
        return $$"""
        {
          "id": "{{id}}",
          "channel": "{{channel}}",
          "message": {{System.Text.Json.JsonSerializer.Serialize(message)}},
          "icon": "{{icon}}",
          "timestamp": "{{timestamp}}",
          "server_timestamp": "{{timestamp}}",
          "session_id": "{{sessionId}}",
          "session_name": "{{sessionName}}",
          "source_client": "{{sourceClient}}",
          "source_event_id": "{{sourceEventId}}",
          "content_kind": "{{contentKind}}",
          "stop_reason": "{{stopReason}}"
        }
        """;
    }

    public static string Envelope(params string[] notifications) =>
        $$"""{ "notifications": [ {{string.Join(',', notifications)}} ] }""";
}
