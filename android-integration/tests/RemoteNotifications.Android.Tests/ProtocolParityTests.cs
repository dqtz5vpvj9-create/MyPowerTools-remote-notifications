using System.Net;
using MyPowerTools.Platform.Abstractions;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// Proves the Android client speaks exactly the shipped desktop protocol: identical request URIs
/// (including the deterministic Ed25519 signature of <c>hello</c>), identical record mapping and
/// identical error classification. The desktop poller is compiled into this suite from its shipped
/// source, so a change on either side breaks these tests instead of phones in the field.
/// </summary>
public sealed class ProtocolParityTests
{
    private static RemoteNotificationSettings Settings(TestSigningKey key) =>
        new("https", "message.example.test", 8888, "default", 5, key.Path, false);

    private static async Task<RemoteNotificationSigningKey> AndroidKeyAsync(TestSigningKey key)
    {
        var secrets = new InMemorySecretStore();
        var signing = new RemoteNotificationSigningKey(secrets, RemoteNotificationsAndroidOptions.ModuleId);
        await signing.SaveAsync(key.Pem, CancellationToken.None);
        return signing;
    }

    [Fact]
    public async Task Pull_request_uri_and_records_match_the_shipped_desktop_poller()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var envelope = TestJson.Envelope(
            TestJson.Notification("n1", "[Codex] finished", icon: "codex", sourceClient: "codex"),
            TestJson.Notification(
                "n2",
                "> original question\n\n[Claude Task] replied\n\n---\n\nFor reference:\n\n> original question",
                icon: "claude",
                sessionId: "session-1",
                sessionName: "Session one",
                sourceClient: "claude"));

        var androidHandler = new RecordingHttpHandler(_ => TestJson.Ok(envelope));
        var desktopHandler = new RecordingHttpHandler(_ => TestJson.Ok(envelope));
        var android = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(androidHandler));
        var desktop = new RemoteNotificationHttpPoller(new HttpClient(desktopHandler), key.Path, settings.Endpoint, settings.Channel);

        const string since = "2026-09-27T01:02:03.0000000Z";
        var androidResult = await android.PullAsync(since);
        var desktopResult = await desktop.PullAsync(since);

        var androidUri = Assert.Single(androidHandler.RequestUris);
        var desktopUri = Assert.Single(desktopHandler.RequestUris);
        Assert.Equal(desktopUri, androidUri);

        var query = TestQuery.Parse(new Uri(androidUri));
        Assert.Equal("default", query["channel"]);
        Assert.Equal("20", query["limit"]);
        Assert.Equal(since, query["since"]);
        Assert.DoesNotContain("+", query["sig"], StringComparison.Ordinal);
        Assert.DoesNotContain("/", query["sig"], StringComparison.Ordinal);

        Assert.Equal("ok", androidResult.State);
        Assert.Equal(desktopResult.State, androidResult.State);
        Assert.Equal(desktopResult.Error, androidResult.Error);
        Assert.Equal(desktopResult.Notifications, androidResult.Notifications);
        Assert.Equal(2, androidResult.Notifications.Count);
    }

    [Fact]
    public async Task Empty_waterline_omits_since_and_limit_is_clamped_like_the_desktop()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var androidHandler = new RecordingHttpHandler(_ => TestJson.Ok(TestJson.Envelope()));
        var desktopHandler = new RecordingHttpHandler(_ => TestJson.Ok(TestJson.Envelope()));
        var android = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(androidHandler));
        var desktop = new RemoteNotificationHttpPoller(new HttpClient(desktopHandler), key.Path, settings.Endpoint, settings.Channel);

        await android.PullAsync("", CancellationToken.None, limit: 2000);
        await desktop.PullAsync("", CancellationToken.None, limit: 2000);
        Assert.DoesNotContain("since=", androidHandler.RequestUris[0], StringComparison.Ordinal);
        Assert.Equal(desktopHandler.RequestUris[0], androidHandler.RequestUris[0]);

        await android.PullAsync("", CancellationToken.None, limit: 0);
        await desktop.PullAsync("", CancellationToken.None, limit: 0);
        Assert.Equal("1", TestQuery.Parse(new Uri(androidHandler.RequestUris[1]))["limit"]);
        Assert.Equal(desktopHandler.RequestUris[1], androidHandler.RequestUris[1]);
    }

    [Fact]
    public async Task Transient_transport_failures_retry_three_times_like_the_desktop()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var androidFailures = 2;
        var desktopFailures = 2;
        var envelope = TestJson.Envelope(TestJson.Notification("n1", "[Codex] ok"));
        var androidHandler = new RecordingHttpHandler(_ =>
            androidFailures-- > 0
                ? throw new HttpRequestException("connection reset")
                : TestJson.Ok(envelope));
        var desktopHandler = new RecordingHttpHandler(_ =>
            desktopFailures-- > 0
                ? throw new HttpRequestException("connection reset")
                : TestJson.Ok(envelope));

        var android = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(androidHandler));
        var desktop = new RemoteNotificationHttpPoller(new HttpClient(desktopHandler), key.Path, settings.Endpoint, settings.Channel);

        var androidResult = await android.PullAsync("");
        var desktopResult = await desktop.PullAsync("");

        Assert.Equal(3, androidHandler.Calls);
        Assert.Equal(desktopHandler.Calls, androidHandler.Calls);
        Assert.Equal(desktopResult.State, androidResult.State);
        Assert.Equal(desktopResult.Notifications, androidResult.Notifications);
    }

    [Fact]
    public async Task Unauthorized_response_is_reported_as_auth_with_the_signature_redacted()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var androidHandler = new RecordingHttpHandler(_ =>
            TestJson.Status(HttpStatusCode.Unauthorized, "unknown key sig=ABCDEF"));
        var desktopHandler = new RecordingHttpHandler(_ =>
            TestJson.Status(HttpStatusCode.Unauthorized, "unknown key sig=ABCDEF"));
        var android = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(androidHandler));
        var desktop = new RemoteNotificationHttpPoller(new HttpClient(desktopHandler), key.Path, settings.Endpoint, settings.Channel);

        var androidResult = await android.PullAsync("");
        var desktopResult = await desktop.PullAsync("");

        Assert.Equal("auth", androidResult.State);
        Assert.Equal(desktopResult.State, androidResult.State);
        Assert.Equal(desktopResult.Error, androidResult.Error);
        Assert.Contains("sig=<redacted>", androidResult.Error, StringComparison.Ordinal);
        Assert.DoesNotContain("ABCDEF", androidResult.Error, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Missing_signing_key_is_reported_as_auth_without_touching_the_network()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var handler = new RecordingHttpHandler(_ => TestJson.Ok(TestJson.Envelope()));
        var signingKey = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);
        var poller = new RemoteNotificationSignedPoller(settings, signingKey, new HttpClient(handler));

        var result = await poller.PullAsync("");

        Assert.Equal("auth", result.State);
        Assert.Equal(0, handler.Calls);
        Assert.False(string.IsNullOrWhiteSpace(result.Error));
    }

    [Fact]
    public async Task Invalid_response_body_is_an_error_not_a_crash()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var handler = new RecordingHttpHandler(_ => TestJson.Ok("{ not json"));
        var poller = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(handler));

        var result = await poller.PullAsync("");

        Assert.Equal("error", result.State);
        Assert.Empty(result.Notifications);
        Assert.Contains("invalid", result.Error, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Empty_envelope_is_idle()
    {
        using var key = TestSigningKey.Create();
        var settings = Settings(key);
        var handler = new RecordingHttpHandler(_ => TestJson.Ok(TestJson.Envelope()));
        var poller = new RemoteNotificationSignedPoller(settings, await AndroidKeyAsync(key), new HttpClient(handler));

        var result = await poller.PullAsync("");

        Assert.Equal("idle", result.State);
        Assert.True(result.IsSuccess);
        Assert.Empty(result.Notifications);
    }
}

internal static class TestQuery
{
    public static Dictionary<string, string> Parse(Uri uri) =>
        uri.Query.TrimStart('?')
            .Split('&', StringSplitOptions.RemoveEmptyEntries)
            .Select(part => part.Split('=', 2))
            .ToDictionary(
                part => Uri.UnescapeDataString(part[0]),
                part => Uri.UnescapeDataString(part.Length == 2 ? part[1] : ""),
                StringComparer.Ordinal);
}
