using System.Buffers.Binary;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AndroidTools.MyPowerTools;

internal sealed record RemoteNotificationServiceControlResult(
    bool Connected,
    string Endpoint,
    JsonObject? Data,
    string Error);

internal static class RemoteNotificationServiceControl
{
    private const string PipeName = "remote-notifications.core";
    private const int MaximumFrameBytes = 1024 * 1024;
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(8);

    public static async Task<RemoteNotificationServiceControlResult> TrySendAsync(
        string command,
        JsonObject? arguments,
        CancellationToken cancellationToken)
    {
        var endpoint = OperatingSystem.IsWindows()
            ? PipeName
            : ResolveSocketPath();
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
            timeout.CancelAfter(Timeout);
            await using var stream = await ConnectAsync(endpoint, timeout.Token)
                .ConfigureAwait(false);
            var request = new JsonObject { ["command"] = command };
            if (arguments is not null)
            {
                foreach (var item in arguments)
                {
                    request[item.Key] = item.Value?.DeepClone();
                }
            }

            await WriteFrameAsync(stream, request, timeout.Token).ConfigureAwait(false);
            var response = await ReadFrameAsync(stream, timeout.Token).ConfigureAwait(false);
            var ok = response["ok"] is JsonValue okValue &&
                okValue.TryGetValue<bool>(out var accepted) &&
                accepted;
            if (!ok)
            {
                return new RemoteNotificationServiceControlResult(
                    true,
                    endpoint,
                    null,
                    ReadString(response, "error", $"Service rejected '{command}'."));
            }

            var data = response["data"] as JsonObject ?? new JsonObject();
            return new RemoteNotificationServiceControlResult(
                true,
                endpoint,
                (JsonObject)data.DeepClone(),
                "");
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return new RemoteNotificationServiceControlResult(
                false,
                endpoint,
                null,
                $"Remote Notifications Service timed out at {endpoint}.");
        }
        catch (Exception exception) when (
            exception is IOException or SocketException or UnauthorizedAccessException or
                JsonException or InvalidDataException)
        {
            return new RemoteNotificationServiceControlResult(
                false,
                endpoint,
                null,
                $"Remote Notifications Service is unavailable at {endpoint}: " +
                exception.Message);
        }
    }

    private static async Task<Stream> ConnectAsync(
        string endpoint,
        CancellationToken cancellationToken)
    {
        if (OperatingSystem.IsWindows())
        {
            var pipe = new NamedPipeClientStream(
                ".",
                endpoint,
                PipeDirection.InOut,
                PipeOptions.Asynchronous);
            try
            {
                await pipe.ConnectAsync(cancellationToken).ConfigureAwait(false);
                return pipe;
            }
            catch
            {
                await pipe.DisposeAsync().ConfigureAwait(false);
                throw;
            }
        }

        var socket = new Socket(
            AddressFamily.Unix,
            SocketType.Stream,
            ProtocolType.Unspecified);
        try
        {
            await socket.ConnectAsync(
                new UnixDomainSocketEndPoint(endpoint),
                cancellationToken).ConfigureAwait(false);
            return new NetworkStream(socket, ownsSocket: true);
        }
        catch
        {
            socket.Dispose();
            throw;
        }
    }

    private static string ResolveSocketPath()
    {
        var configured = Environment.GetEnvironmentVariable(
            "MPT_REMOTE_NOTIFICATIONS_SOCKET");
        return string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(
                Path.GetTempPath(),
                "mypowertools",
                "remote-notifications.core.sock")
            : Path.GetFullPath(Environment.ExpandEnvironmentVariables(configured));
    }

    private static async Task WriteFrameAsync(
        Stream stream,
        JsonObject payload,
        CancellationToken cancellationToken)
    {
        var body = JsonSerializer.SerializeToUtf8Bytes(payload);
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, body.Length);
        await stream.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync(body, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task<JsonObject> ReadFrameAsync(
        Stream stream,
        CancellationToken cancellationToken)
    {
        var header = new byte[4];
        await ReadExactlyAsync(stream, header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > MaximumFrameBytes)
        {
            throw new InvalidDataException(
                $"Remote Notifications Service returned invalid frame length {length}.");
        }

        var body = new byte[length];
        await ReadExactlyAsync(stream, body, cancellationToken).ConfigureAwait(false);
        return JsonNode.Parse(body) as JsonObject
            ?? throw new InvalidDataException(
                "Remote Notifications Service returned a non-object response.");
    }

    private static async Task ReadExactlyAsync(
        Stream stream,
        Memory<byte> buffer,
        CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var count = await stream.ReadAsync(
                buffer[offset..],
                cancellationToken).ConfigureAwait(false);
            if (count == 0)
            {
                throw new EndOfStreamException();
            }
            offset += count;
        }
    }

    private static string ReadString(
        JsonObject values,
        string key,
        string fallback)
    {
        return values[key] is JsonValue value &&
            value.TryGetValue<string>(out var result) &&
            !string.IsNullOrWhiteSpace(result)
                ? result
                : fallback;
    }
}
