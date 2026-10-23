using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text.Json;
using MyPowerTools.Abstractions;
using RemoteNotifications.Service;

namespace RemoteNotifications.Configuration.Tests;

public sealed class NotificationClickActivationTests
{
    [Theory]
    [InlineData(0x06, true)]
    [InlineData(0x00, false)]
    public async Task Running_shell_receives_the_exact_detail_target_and_must_acknowledge(int reply, bool expected)
    {
        if (!OperatingSystem.IsWindows()) return;
        var name = $"MptToastTest.{Guid.NewGuid():N}";
        await using var server = new NamedPipeServerStream(
            name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var request = new ToolActivationRequest("remote-notifications", "inbox",
            "mypowertools://remote-notification?id=message%20%26%20%E6%B5%8B%E8%AF%95")
        {
            SuppressShellWindow = true
        };
        var forwarding = ProductActivationLauncher.TryActivateRunningShellAsync(request, name);
        await server.WaitForConnectionAsync(timeout.Token);
        var header = new byte[4];
        await server.ReadExactlyAsync(header, timeout.Token);
        var payload = new byte[BinaryPrimitives.ReadInt32LittleEndian(header)];
        await server.ReadExactlyAsync(payload, timeout.Token);
        using var document = JsonDocument.Parse(payload);
        Assert.Equal(request, document.RootElement.GetProperty("ToolActivation").Deserialize<ToolActivationRequest>());
        Assert.False(document.RootElement.GetProperty("ShowShell").GetBoolean());
        Assert.False(document.RootElement.GetProperty("ShutdownShell").GetBoolean());
        await server.WriteAsync(new byte[] { (byte)reply }, timeout.Token);
        await server.FlushAsync(timeout.Token);
        Assert.Equal(expected, await forwarding);
    }

    [Fact]
    public async Task Missing_shell_leaves_activation_for_the_product_cold_start()
    {
        var request = new ToolActivationRequest("remote-notifications", "inbox",
            "mypowertools://remote-notification?id=missing");
        Assert.False(await ProductActivationLauncher.TryActivateRunningShellAsync(
            request, $"MptToastTest.{Guid.NewGuid():N}", TimeSpan.FromMilliseconds(100)));
    }

    [Fact]
    public async Task Silent_shell_cannot_hold_the_protocol_process_open()
    {
        if (!OperatingSystem.IsWindows()) return;
        var name = $"MptToastTest.{Guid.NewGuid():N}";
        await using var server = new NamedPipeServerStream(
            name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
        var request = new ToolActivationRequest("remote-notifications", "inbox",
            "mypowertools://remote-notification?id=silent");
        var forwarding = ProductActivationLauncher.TryActivateRunningShellAsync(
            request, name, TimeSpan.FromMilliseconds(300));
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await server.WaitForConnectionAsync(timeout.Token);
        Assert.False(await forwarding.WaitAsync(timeout.Token));
    }
}
