using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android;

/// <summary>
/// Signed remote-feed client for Android.
///
/// The request contract is byte-for-byte the desktop contract: <c>GET {endpoint}/pull</c> with
/// <c>channel</c>, <c>sig</c> (base64url Ed25519 signature of <c>hello</c>), <c>limit</c> and an
/// optional <c>since</c> waterline; the same three attempts with the same backoff, the same
/// 15 second per-attempt timeout, the same 20 record default limit and the same response mapping.
/// The only difference is where the signature comes from: the desktop signer reads a key file,
/// Android reads the platform credential store (see <see cref="RemoteNotificationSigningKey"/>).
/// <c>RemoteNotifications.Android.Tests</c> asserts URI and record parity against the shipped
/// desktop poller so this stays true.
/// </summary>
internal sealed partial class RemoteNotificationSignedPoller : IRemoteNotificationPoller
{
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(15);
    private static HttpClient _sharedHttpClient = CreateHttpClient();

    private static HttpClient CreateHttpClient() => new(new SocketsHttpHandler
    {
        PooledConnectionIdleTimeout = TimeSpan.FromMinutes(10),
        PooledConnectionLifetime = TimeSpan.FromMinutes(30),
        MaxConnectionsPerServer = 2,
        AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate
    })
    {
        Timeout = Timeout.InfiniteTimeSpan,
        DefaultRequestVersion = HttpVersion.Version20,
        DefaultVersionPolicy = HttpVersionPolicy.RequestVersionOrLower
    };

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private readonly HttpClient? _httpClient;
    private readonly RemoteNotificationSigningKey _signingKey;
    private readonly string _endpoint;
    private readonly string _channel;

    public RemoteNotificationSignedPoller(
        RemoteNotificationSettings settings,
        RemoteNotificationSigningKey signingKey,
        HttpClient? httpClient = null)
    {
        ArgumentNullException.ThrowIfNull(settings);
        _signingKey = signingKey ?? throw new ArgumentNullException(nameof(signingKey));
        var normalized = settings.Normalize();
        _httpClient = httpClient;
        _endpoint = normalized.Endpoint.TrimEnd('/');
        _channel = normalized.Channel;
    }

    public Task<RemoteNotificationPullResult> PullAsync(string since, CancellationToken cancellationToken = default) =>
        PullAsync(since, cancellationToken, limit: null);

    public async Task<RemoteNotificationPullResult> PullAsync(
        string since,
        CancellationToken cancellationToken,
        int? limit)
    {
        try
        {
            var signature = await _signingKey.GetHandshakeSignatureAsync(cancellationToken).ConfigureAwait(false);
            var requestUri = BuildPullUri(signature, since ?? "", limit ?? RemoteNotificationsAndroidOptions.DefaultPullLimit);
            string lastError = "Connection failed";
            for (var attempt = 0; attempt < 3; attempt++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (attempt > 0)
                {
                    await Task.Delay(TimeSpan.FromMilliseconds(300 * attempt), cancellationToken).ConfigureAwait(false);
                }

                try
                {
                    return await SendOnceAsync(requestUri, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    lastError = "Request timed out";
                }
                catch (HttpRequestException exception)
                {
                    lastError = SummarizeRequestError(exception.Message);
                }
            }

            return new RemoteNotificationPullResult("error", [], lastError);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (InvalidOperationException exception)
        {
            // Missing or unusable signing key: report it as an authentication problem instead of
            // a transport error so the UI points at the credential, not the network.
            return new RemoteNotificationPullResult("auth", [], Sanitize(exception.Message));
        }
        catch (Exception exception)
        {
            return new RemoteNotificationPullResult("error", [], Sanitize(exception.Message));
        }
    }

    /// <summary>Disposes pooled sockets after a network change; DNS and TLS resolve on the new route.</summary>
    public static void ResetConnectionsAfterNetworkChange()
    {
        Interlocked.Exchange(ref _sharedHttpClient, CreateHttpClient()).Dispose();
    }

    internal Uri BuildPullUri(string signature, string since, int limit)
    {
        var parameters = new List<KeyValuePair<string, string>>
        {
            new("channel", _channel),
            new("sig", signature),
            new("limit", Math.Clamp(limit, 1, 2000).ToString(System.Globalization.CultureInfo.InvariantCulture))
        };
        if (!string.IsNullOrWhiteSpace(since))
        {
            parameters.Add(new KeyValuePair<string, string>("since", since));
        }

        var query = string.Join(
            '&',
            parameters.Select(parameter =>
                $"{Uri.EscapeDataString(parameter.Key)}={Uri.EscapeDataString(parameter.Value)}"));
        return new Uri($"{_endpoint}/pull?{query}", UriKind.Absolute);
    }

    private async Task<RemoteNotificationPullResult> SendOnceAsync(
        Uri requestUri,
        CancellationToken cancellationToken)
    {
        var client = _httpClient ?? Volatile.Read(ref _sharedHttpClient);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(RequestTimeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, requestUri)
        {
            Version = client.DefaultRequestVersion,
            VersionPolicy = client.DefaultVersionPolicy
        };
        request.Headers.UserAgent.ParseAdd("MyPowerTools/RemoteNotifications");
        using var response = await client.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                timeout.Token)
            .ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(timeout.Token).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var detail = body.Length <= 200 ? body : body[..200];
            return new RemoteNotificationPullResult(
                response.StatusCode == HttpStatusCode.Unauthorized ? "auth" : "error",
                [],
                Sanitize($"HTTP {(int)response.StatusCode}: {detail}"));
        }

        PullEnvelope? envelope;
        try
        {
            envelope = JsonSerializer.Deserialize<PullEnvelope>(body, JsonOptions);
        }
        catch (JsonException exception)
        {
            return new RemoteNotificationPullResult(
                "error",
                [],
                $"Notification response was invalid: {exception.Message}");
        }

        if (envelope is null)
        {
            return new RemoteNotificationPullResult("error", [], "Notification response was empty.");
        }

        var notifications = (envelope.Notifications ?? [])
            .Where(item => !string.IsNullOrWhiteSpace(item.Message))
            .Select(item => new RemoteNotificationRecord(
                item.Id ?? item.MessageId ?? "",
                string.IsNullOrWhiteSpace(item.Channel) ? _channel : item.Channel,
                item.Message ?? "",
                string.IsNullOrWhiteSpace(item.Icon) ? "info" : item.Icon,
                item.Timestamp ?? "",
                item.ServerTimestamp ?? "",
                item.SessionId ?? "",
                item.SessionName ?? "",
                item.SourceClient ?? "",
                item.SourceEventId ?? "",
                item.SourceMessageId ?? "",
                item.ContentKind ?? "",
                item.StopReason ?? ""))
            .ToArray();
        return new RemoteNotificationPullResult(
            notifications.Length == 0 ? "idle" : "ok",
            notifications,
            "");
    }

    private static string SummarizeRequestError(string value)
    {
        var sanitized = Sanitize(value);
        return sanitized.Contains("UNEXPECTED_EOF_WHILE_READING", StringComparison.OrdinalIgnoreCase)
            ? "TLS connection closed early by server"
            : sanitized.Length <= 300 ? sanitized : sanitized[..300];
    }

    private static string Sanitize(string value)
    {
        return SignaturePattern().Replace(value ?? "", "sig=<redacted>").Trim();
    }

    [GeneratedRegex(@"sig=[^&\s)]+", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex SignaturePattern();

    private sealed class PullEnvelope
    {
        [JsonPropertyName("notifications")]
        public List<PullNotification>? Notifications { get; init; }
    }

    private sealed class PullNotification
    {
        [JsonPropertyName("id")]
        public string? Id { get; init; }

        [JsonPropertyName("message_id")]
        public string? MessageId { get; init; }

        [JsonPropertyName("channel")]
        public string? Channel { get; init; }

        [JsonPropertyName("message")]
        public string? Message { get; init; }

        [JsonPropertyName("icon")]
        public string? Icon { get; init; }

        [JsonPropertyName("timestamp")]
        public string? Timestamp { get; init; }

        [JsonPropertyName("server_timestamp")]
        public string? ServerTimestamp { get; init; }

        [JsonPropertyName("session_id")]
        public string? SessionId { get; init; }

        [JsonPropertyName("session_name")]
        public string? SessionName { get; init; }

        [JsonPropertyName("source_client")]
        public string? SourceClient { get; init; }

        [JsonPropertyName("source_event_id")]
        public string? SourceEventId { get; init; }

        [JsonPropertyName("source_message_id")]
        public string? SourceMessageId { get; init; }

        [JsonPropertyName("content_kind")]
        public string? ContentKind { get; init; }

        [JsonPropertyName("stop_reason")]
        public string? StopReason { get; init; }
    }
}
