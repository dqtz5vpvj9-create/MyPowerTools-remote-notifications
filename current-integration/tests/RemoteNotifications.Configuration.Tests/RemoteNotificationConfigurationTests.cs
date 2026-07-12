using System.Net;
using System.Text;
using System.Text.Json;
using MyPowerTools.RemoteNotifications.Configuration;
using MyPowerTools.Shell.Avalonia.Services;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationConfigurationTests
{
    [Fact]
    public void Product_settings_round_trip_every_runtime_option()
    {
        using var fixture = TemporaryDirectory.Create();
        var path = Path.Combine(fixture.Path, "settings.json");
        var store = new RemoteNotificationSettingsStore(path);
        var expected = new RemoteNotificationSettings(
            "http",
            "0.0.0.0",
            19091,
            "automation",
            11,
            @"D:\keys\notification_ed25519",
            true);

        store.Save(expected);
        var actual = store.Load();

        Assert.Equal(expected, actual);
        using var document = JsonDocument.Parse(File.ReadAllText(path));
        Assert.Equal("automation", document.RootElement.GetProperty("channel").GetString());
        Assert.Equal(11, document.RootElement.GetProperty("pollIntervalSeconds").GetInt32());
        Assert.True(document.RootElement.GetProperty("keepWindowsBanners").GetBoolean());
        Assert.DoesNotContain("PRIVATE KEY", File.ReadAllText(path), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Poller_consumes_endpoint_channel_and_key_from_product_settings()
    {
        using var fixture = OpenSshKeyFixture.Create();
        var handler = new RecordingHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("{\"notifications\":[]}", Encoding.UTF8, "application/json")
        });
        using var httpClient = new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
        var settings = new RemoteNotificationSettings(
            "http",
            "127.0.0.1",
            19091,
            "build-events",
            17,
            fixture.KeyPath,
            true);

        var result = await new RemoteNotificationHttpPoller(settings, httpClient).PullAsync("");

        Assert.True(result.IsSuccess);
        var request = Assert.Single(handler.Requests);
        Assert.Equal("http", request.RequestUri!.Scheme);
        Assert.Equal("127.0.0.1", request.RequestUri.Host);
        Assert.Equal(19091, request.RequestUri.Port);
        Assert.Equal("build-events", ParseQuery(request.RequestUri)["channel"]);
        Assert.True(ParseQuery(request.RequestUri).ContainsKey("sig"));
    }

    [Theory]
    [InlineData("ftp", "host", 8888, "default", 5, "key")]
    [InlineData("https", "https://host", 8888, "default", 5, "key")]
    [InlineData("https", "host", 0, "default", 5, "key")]
    [InlineData("https", "host", 8888, "", 5, "key")]
    [InlineData("https", "host", 8888, "default", 4, "key")]
    [InlineData("https", "host", 8888, "default", 5, "")]
    public void Invalid_product_settings_are_rejected(
        string protocol,
        string host,
        int port,
        string channel,
        int pollInterval,
        string keyPath)
    {
        var settings = new RemoteNotificationSettings(
            protocol,
            host,
            port,
            channel,
            pollInterval,
            keyPath,
            false);

        Assert.False(settings.Validate().IsValid);
    }

    private static Dictionary<string, string> ParseQuery(Uri uri)
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

    private sealed class RecordingHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
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

    private sealed class OpenSshKeyFixture : IDisposable
    {
        private OpenSshKeyFixture(TemporaryDirectory directory, string keyPath)
        {
            Directory = directory;
            KeyPath = keyPath;
        }

        private TemporaryDirectory Directory { get; }
        public string KeyPath { get; }

        public static OpenSshKeyFixture Create()
        {
            var directory = TemporaryDirectory.Create();
            var keyPath = Path.Combine(directory.Path, "id_ed25519");
            var seed = Enumerable.Range(1, 32).Select(value => (byte)value).ToArray();
            var privateKey = new Ed25519PrivateKeyParameters(seed);
            var keyBlob = OpenSshPrivateKeyUtilities.EncodePrivateKey(privateKey);
            using (var writer = File.CreateText(keyPath))
            using (var pemWriter = new PemWriter(writer))
            {
                pemWriter.WriteObject(new PemObject("OPENSSH PRIVATE KEY", keyBlob));
            }

            return new OpenSshKeyFixture(directory, keyPath);
        }

        public void Dispose() => Directory.Dispose();
    }

    private sealed class TemporaryDirectory : IDisposable
    {
        private TemporaryDirectory(string path)
        {
            Path = path;
        }

        public string Path { get; }

        public static TemporaryDirectory Create()
        {
            var path = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                "mpt-remote-notification-tests",
                Guid.NewGuid().ToString("N"));
            System.IO.Directory.CreateDirectory(path);
            return new TemporaryDirectory(path);
        }

        public void Dispose()
        {
            if (System.IO.Directory.Exists(Path))
            {
                System.IO.Directory.Delete(Path, recursive: true);
            }
        }
    }
}
