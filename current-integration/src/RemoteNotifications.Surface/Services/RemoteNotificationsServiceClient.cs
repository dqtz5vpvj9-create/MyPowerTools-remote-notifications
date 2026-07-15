using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text.Json;
using MyPowerTools.Abstractions;

namespace RemoteNotifications.Surface.Services;

public interface IRemoteNotificationsServiceClient
{
    Task<RemoteNotificationsServiceState> GetStateAsync(CancellationToken cancellationToken = default);

    Task<RemoteNotificationsServiceState> PollAsync(CancellationToken cancellationToken = default);
}

public sealed record RemoteNotificationsServiceState(
    string ConnectionState,
    string LastPoll,
    string LastError,
    string Latest,
    int Fetched,
    int Shown,
    int PollIntervalSeconds);

/// <summary>
/// Product-side client for the independent Remote Notifications Service Unit. The Surface uses
/// this client for lifecycle and signed-pull operations, then renders the persisted history.
/// </summary>
public sealed class RemoteNotificationsServiceClient : IRemoteNotificationsServiceClient
{
    public const string UnitId = "remote-notifications.service";
    public const string PipeName = "remote-notifications.core";

    private readonly IServiceUnitClient _serviceUnits;

    public RemoteNotificationsServiceClient(IServiceUnitClient serviceUnits)
    {
        _serviceUnits = serviceUnits;
    }

    public async Task<RemoteNotificationsServiceState> GetStateAsync(
        CancellationToken cancellationToken = default)
    {
        var unit = await EnsureRunningAsync(cancellationToken).ConfigureAwait(false);
        using var response = await SendAsync(ResolvePipeName(unit), "state", cancellationToken).ConfigureAwait(false);
        return ParseState(response.RootElement.GetProperty("data"));
    }

    public async Task<RemoteNotificationsServiceState> PollAsync(
        CancellationToken cancellationToken = default)
    {
        var unit = await EnsureRunningAsync(cancellationToken).ConfigureAwait(false);
        using var response = await SendAsync(ResolvePipeName(unit), "poll", cancellationToken).ConfigureAwait(false);
        return ParseState(response.RootElement.GetProperty("data"));
    }

    private async Task<ServiceUnitSnapshot> EnsureRunningAsync(CancellationToken cancellationToken)
    {
        var units = await _serviceUnits.ListAsync(cancellationToken).ConfigureAwait(false);
        var unit = units.FirstOrDefault(candidate =>
            string.Equals(candidate.Id, UnitId, StringComparison.Ordinal));
        if (unit is null)
        {
            await _serviceUnits.ReloadAsync(cancellationToken).ConfigureAwait(false);
            units = await _serviceUnits.ListAsync(cancellationToken).ConfigureAwait(false);
            unit = units.FirstOrDefault(candidate =>
                string.Equals(candidate.Id, UnitId, StringComparison.Ordinal));
        }

        if (unit is null)
        {
            throw new InvalidOperationException($"Service Unit '{UnitId}' is not installed for this tool.");
        }

        if (unit.State is not ServiceUnitState.Active and not ServiceUnitState.Degraded)
        {
            unit = await _serviceUnits.StartAsync(UnitId, cancellationToken).ConfigureAwait(false);
        }

        if (unit.State is not ServiceUnitState.Active and not ServiceUnitState.Degraded)
        {
            throw new InvalidOperationException(
                unit.LastError ?? $"Service Unit '{UnitId}' did not become ready.");
        }

        return unit;
    }

    private static async Task<JsonDocument> SendAsync(
        string pipeName,
        string command,
        CancellationToken cancellationToken)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(15));

        await using var pipe = new NamedPipeClientStream(
            ".",
            pipeName,
            PipeDirection.InOut,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        await pipe.ConnectAsync(timeout.Token).ConfigureAwait(false);

        var payload = JsonSerializer.SerializeToUtf8Bytes(new { command });
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await pipe.WriteAsync(header, timeout.Token).ConfigureAwait(false);
        await pipe.WriteAsync(payload, timeout.Token).ConfigureAwait(false);
        await pipe.FlushAsync(timeout.Token).ConfigureAwait(false);

        var responseHeader = new byte[4];
        await ReadExactlyAsync(pipe, responseHeader, timeout.Token).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(responseHeader);
        if (length <= 0 || length > 1024 * 1024)
        {
            throw new InvalidDataException($"Remote Notifications Service returned invalid length {length}.");
        }

        var responsePayload = new byte[length];
        await ReadExactlyAsync(pipe, responsePayload, timeout.Token).ConfigureAwait(false);
        var response = JsonDocument.Parse(responsePayload);
        if (!response.RootElement.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
        {
            var error = response.RootElement.TryGetProperty("error", out var errorElement)
                ? errorElement.GetString()
                : null;
            response.Dispose();
            throw new InvalidOperationException(error ?? $"Service command '{command}' failed.");
        }

        return response;
    }

    private static string ResolvePipeName(ServiceUnitSnapshot unit)
    {
        var readiness = unit.Readiness;
        var address = readiness?.Address;
        return string.Equals(readiness?.Kind, "pipe", StringComparison.OrdinalIgnoreCase) &&
               !string.IsNullOrWhiteSpace(address)
            ? address
            : PipeName;
    }

    private static RemoteNotificationsServiceState ParseState(JsonElement data)
    {
        return new RemoteNotificationsServiceState(
            ReadString(data, "connectionState", "starting"),
            ReadString(data, "lastPoll", "never"),
            ReadString(data, "lastError", "none"),
            ReadString(data, "latest", "never"),
            ReadInt32(data, "fetched"),
            ReadInt32(data, "shown"),
            Math.Max(5, ReadInt32(data, "pollIntervalSeconds", 30)));
    }

    private static string ReadString(JsonElement element, string name, string fallback) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback
            : fallback;

    private static int ReadInt32(JsonElement element, string name, int fallback = 0) =>
        element.TryGetProperty(name, out var value) && value.TryGetInt32(out var result)
            ? result
            : fallback;

    private static async Task ReadExactlyAsync(
        Stream stream,
        Memory<byte> buffer,
        CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer[offset..], cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException();
            }
            offset += read;
        }
    }
}
