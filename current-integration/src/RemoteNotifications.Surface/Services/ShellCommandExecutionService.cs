using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using MyPowerTools.HostControl;
using HostProto = MyPowerTools.Protocol.HostControl.V1;

namespace RemoteNotifications.Surface.Services;

public sealed class ShellCommandExecutionService
{
    public async Task<ShellCommandExecutionResult> ExecuteAsync(string commandId, JsonObject? args = null, CancellationToken cancellationToken = default)
        => await ExecuteAsync(Guid.NewGuid().ToString("N"), commandId, args, cancellationToken);

    public async Task<ShellCommandExecutionResult> ExecuteAsync(string invocationId, string commandId, JsonObject? args = null, CancellationToken cancellationToken = default)
    {
        using var client = HostControlClient.ForDefaultEndpoint();
        var response = args is null
            ? await client.ExecuteCommandAsync(invocationId, commandId, new JsonObject(), cancellationToken)
            : await client.ExecuteCommandAsync(invocationId, commandId, args, cancellationToken);
        return new ShellCommandExecutionResult(
            $"{response.State}: {response.Summary}",
            response,
            string.Equals(response.State, "permission-required", StringComparison.OrdinalIgnoreCase));
    }
}

public sealed record ShellCommandExecutionResult(string StatusText, HostProto.CommandExecutionResponse Response, bool RequiresPermissionPrompt);
