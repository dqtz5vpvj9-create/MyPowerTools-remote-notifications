using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

namespace RemoteNotifications.Configuration.Tests;

/// <summary>A scratch directory that is removed when the test finishes.</summary>
internal sealed class TestDirectory : IDisposable
{
    private TestDirectory(string path)
    {
        Path = path;
    }

    public string Path { get; }

    public static TestDirectory Create()
    {
        var path = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "mpt-remote-notification-tests",
            Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
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

/// <summary>An OpenSSH ed25519 key file backed by a fixed seed.</summary>
internal sealed class TestSigningKey : IDisposable
{
    private readonly TestDirectory _directory;

    private TestSigningKey(TestDirectory directory, string path, Ed25519PrivateKeyParameters privateKey)
    {
        _directory = directory;
        Path = path;
        PrivateKey = privateKey;
    }

    public string Path { get; }

    public Ed25519PrivateKeyParameters PrivateKey { get; }

    public static TestSigningKey Create()
    {
        var directory = TestDirectory.Create();
        var path = System.IO.Path.Combine(directory.Path, "id_ed25519");
        var seed = Enumerable.Range(1, 32).Select(value => (byte)value).ToArray();
        var privateKey = new Ed25519PrivateKeyParameters(seed);
        var keyBlob = OpenSshPrivateKeyUtilities.EncodePrivateKey(privateKey);
        using (var writer = File.CreateText(path))
        using (var pemWriter = new PemWriter(writer))
        {
            pemWriter.WriteObject(new PemObject("OPENSSH PRIVATE KEY", keyBlob));
        }

        return new TestSigningKey(directory, path, privateKey);
    }

    public void Dispose() => _directory.Dispose();
}

/// <summary>Captures the requests a poller issues and replays canned responses.</summary>
internal sealed class RecordingHttpHandler(Func<HttpRequestMessage, HttpResponseMessage> respond)
    : HttpMessageHandler
{
    public List<HttpRequestMessage> Requests { get; } = [];

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        Requests.Add(request);
        return Task.FromResult(respond(request));
    }
}

internal static class TestPaths
{
    /// <summary>
    /// The <c>current-integration</c> directory, resolved from the test
    /// assembly so the suite works both inside the host repository and in a
    /// standalone submodule checkout.
    /// </summary>
    public static string IntegrationRoot { get; } = FindIntegrationRoot();

    public static string SurfaceRoot { get; } =
        Path.Combine(IntegrationRoot, "src", "RemoteNotifications.Surface");

    private static string FindIntegrationRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            var candidate = Path.Combine(
                current.FullName,
                "src",
                "RemoteNotifications.Surface",
                "RemoteNotifications.Surface.csproj");
            if (File.Exists(candidate))
            {
                return current.FullName;
            }

            current = current.Parent;
        }

        throw new DirectoryNotFoundException(
            "The remote-notifications current-integration root was not found.");
    }
}

internal static class TestQuery
{
    public static Dictionary<string, string> Parse(Uri uri)
    {
        return uri.Query
            .TrimStart('?')
            .Split('&', StringSplitOptions.RemoveEmptyEntries)
            .Select(part => part.Split('=', 2))
            .ToDictionary(
                part => Uri.UnescapeDataString(part[0]),
                part => Uri.UnescapeDataString(part.Length == 2 ? part[1] : ""),
                StringComparer.Ordinal);
    }
}
