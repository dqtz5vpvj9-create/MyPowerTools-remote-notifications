using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using MyPowerTools.RemoteNotifications.Configuration;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

namespace MyPowerTools.Shell.Avalonia.Services;

public sealed partial class RemoteNotificationHttpPoller : IRemoteNotificationPoller
{
    private const int PullLimit = 20;
    private const int MaximumAttempts = 3;
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(15);
    private static readonly HttpClient SharedHttpClient = new()
    {
        Timeout = Timeout.InfiniteTimeSpan
    };
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private readonly HttpClient _httpClient;
    private readonly string _endpoint;
    private readonly string _privateKeyPath;
    private readonly string _channel;

    public RemoteNotificationHttpPoller(
        RemoteNotificationSettings settings,
        HttpClient? httpClient = null)
        : this(
            httpClient,
            settings?.ExpandedPrivateKeyPath,
            settings?.Endpoint,
            settings?.Channel)
    {
        ArgumentNullException.ThrowIfNull(settings);
    }

    public RemoteNotificationHttpPoller(
        HttpClient? httpClient = null,
        string? privateKeyPath = null,
        string? endpoint = null,
        string? channel = null)
    {
        var defaults = new RemoteNotificationSettingsStore().Load();
        _httpClient = httpClient ?? SharedHttpClient;
        _endpoint = string.IsNullOrWhiteSpace(endpoint)
            ? defaults.Endpoint
            : endpoint.TrimEnd('/');
        _privateKeyPath = string.IsNullOrWhiteSpace(privateKeyPath)
            ? defaults.ExpandedPrivateKeyPath
            : privateKeyPath;
        _channel = string.IsNullOrWhiteSpace(channel)
            ? defaults.Channel
            : channel.Trim();
    }

    public async Task<RemoteNotificationPullResult> PullAsync(
        string since,
        CancellationToken cancellationToken = default)
    {
        try
        {
            var signature = RemoteNotificationSshSigner.SignHandshake(_privateKeyPath);
            var requestUri = BuildPullUri(signature, since ?? "");
            string lastError = "Connection failed";
            for (var attempt = 0; attempt < MaximumAttempts; attempt++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (attempt > 0)
                {
                    await Task.Delay(TimeSpan.FromMilliseconds(300 * attempt), cancellationToken)
                        .ConfigureAwait(false);
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
        catch (Exception exception)
        {
            return new RemoteNotificationPullResult("error", [], Sanitize(exception.Message));
        }
    }

    private async Task<RemoteNotificationPullResult> SendOnceAsync(
        Uri requestUri,
        CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(RequestTimeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, requestUri);
        request.Headers.UserAgent.ParseAdd("MyPowerTools/RemoteNotifications");
        using var response = await _httpClient.SendAsync(
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
                string.IsNullOrWhiteSpace(item.Channel)
                    ? _channel
                    : item.Channel,
                item.Message ?? "",
                string.IsNullOrWhiteSpace(item.Icon) ? "info" : item.Icon,
                item.Timestamp ?? "",
                item.ServerTimestamp ?? ""))
            .ToArray();
        return new RemoteNotificationPullResult(
            notifications.Length == 0 ? "idle" : "ok",
            notifications,
            "");
    }

    private Uri BuildPullUri(string signature, string since)
    {
        var parameters = new List<KeyValuePair<string, string>>
        {
            new("channel", _channel),
            new("sig", signature),
            new("limit", PullLimit.ToString(System.Globalization.CultureInfo.InvariantCulture))
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
    }
}

public static class RemoteNotificationSshSigner
{
    private static readonly byte[] Handshake = Encoding.ASCII.GetBytes("hello");

    public static string SignHandshake(string privateKeyPath)
    {
        if (!File.Exists(privateKeyPath))
        {
            throw new FileNotFoundException(
                "The SSH signing key was not found at the configured path.",
                privateKeyPath);
        }

        using var textReader = File.OpenText(privateKeyPath);
        using var pemReader = new PemReader(textReader);
        var pem = pemReader.ReadPemObject();
        if (pem is null ||
            !string.Equals(pem.Type, "OPENSSH PRIVATE KEY", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("The SSH signing key is not an OpenSSH private key.");
        }

        var keyBlob = pem.Content;
        try
        {
            var key = OpenSshPrivateKeyUtilities.ParsePrivateKeyBlob(keyBlob);
            if (key is not Ed25519PrivateKeyParameters ed25519Key)
            {
                throw new InvalidDataException("The SSH signing key must use Ed25519.");
            }

            var signer = new Ed25519Signer();
            signer.Init(forSigning: true, ed25519Key);
            signer.BlockUpdate(Handshake, 0, Handshake.Length);
            var signature = signer.GenerateSignature();
            return Convert.ToBase64String(signature)
                .Replace('+', '-')
                .Replace('/', '_');
        }
        catch (InvalidDataException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new InvalidDataException(
                "The SSH signing key could not be read. Use an unencrypted OpenSSH Ed25519 key.",
                exception);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(keyBlob);
        }
    }
}
