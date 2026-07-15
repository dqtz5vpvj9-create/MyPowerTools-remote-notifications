using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using Avalonia.Controls;
using Avalonia.Threading;

namespace RemoteNotifications.Surface.Services;

public sealed record RemoteNotificationActivationRequest(
    string MessageId,
    string LaunchUri,
    bool FocusCommandPalette = false);

public sealed class RemoteNotificationShellInstanceLock : IDisposable
{
    public const string MutexName = @"Local\MyPowerTools.Shell";

    private readonly Mutex? _mutex;
    private readonly bool _ownsMutex;

    private RemoteNotificationShellInstanceLock(Mutex? mutex, bool ownsMutex)
    {
        _mutex = mutex;
        _ownsMutex = ownsMutex;
    }

    public bool Acquired => _ownsMutex;

    public static RemoteNotificationShellInstanceLock Acquire(string? mutexName = null)
    {
        if (!OperatingSystem.IsWindows())
        {
            return new RemoteNotificationShellInstanceLock(null, ownsMutex: true);
        }

        var mutex = new Mutex(initiallyOwned: false, mutexName ?? MutexName);
        try
        {
            return new RemoteNotificationShellInstanceLock(mutex, mutex.WaitOne(0));
        }
        catch (AbandonedMutexException)
        {
            return new RemoteNotificationShellInstanceLock(mutex, ownsMutex: true);
        }
        catch
        {
            mutex.Dispose();
            throw;
        }
    }

    public void Dispose()
    {
        if (_ownsMutex && _mutex is not null)
        {
            try
            {
                _mutex.ReleaseMutex();
            }
            catch (ApplicationException)
            {
                // Process teardown can move disposal away from the owning thread.
            }
        }

        _mutex?.Dispose();
    }
}

public static class RemoteNotificationActivationProtocol
{
    public const string ArgumentName = "--remote-notification-activation";

    public static RemoteNotificationActivationRequest? Parse(IEnumerable<string>? arguments)
    {
        var values = arguments?.ToArray() ?? [];
        for (var index = 0; index < values.Length; index++)
        {
            var value = values[index].Trim().Trim('"');
            if (string.Equals(value, ArgumentName, StringComparison.OrdinalIgnoreCase) && index + 1 < values.Length)
            {
                return ParseLaunchUri(values[index + 1]);
            }

            if (value.StartsWith($"{ArgumentName}=", StringComparison.OrdinalIgnoreCase))
            {
                return ParseLaunchUri(value[(ArgumentName.Length + 1)..]);
            }

            if (value.StartsWith("mypowertools://remote-notification", StringComparison.OrdinalIgnoreCase))
            {
                return ParseLaunchUri(value);
            }
        }

        return null;
    }

    public static RemoteNotificationActivationRequest ParseLaunchUri(string value)
    {
        var launchUri = (value ?? "").Trim().Trim('"');
        if (!Uri.TryCreate(launchUri, UriKind.Absolute, out var uri) ||
            !string.Equals(uri.Scheme, WindowsRemoteNotificationToastPlatform.ProtocolScheme, StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(uri.Host, "remote-notification", StringComparison.OrdinalIgnoreCase))
        {
            return new RemoteNotificationActivationRequest("", launchUri);
        }

        var messageId = uri.Query
            .TrimStart('?')
            .Split('&', StringSplitOptions.RemoveEmptyEntries)
            .Select(part => part.Split('=', 2))
            .Where(part => string.Equals(Uri.UnescapeDataString(part[0]), "id", StringComparison.OrdinalIgnoreCase))
            .Select(part => Uri.UnescapeDataString(part.Length == 2 ? part[1] : ""))
            .FirstOrDefault() ?? "";
        return new RemoteNotificationActivationRequest(messageId, uri.AbsoluteUri);
    }
}

public sealed class RemoteNotificationActivationPipe : IAsyncDisposable
{
    public const string PipeName = "MyPowerTools.RemoteNotificationActivation";

    private readonly Func<RemoteNotificationActivationRequest, Task> _handler;
    private readonly string _pipeName;
    private readonly CancellationTokenSource _cancellation = new();
    private Task? _serverTask;

    public RemoteNotificationActivationPipe(
        Func<RemoteNotificationActivationRequest, Task> handler,
        string? pipeName = null)
    {
        _handler = handler;
        _pipeName = string.IsNullOrWhiteSpace(pipeName) ? PipeName : pipeName;
    }

    public void Start()
    {
        if (!OperatingSystem.IsWindows() || _serverTask is not null)
        {
            return;
        }

        _serverTask = RunServerAsync(_cancellation.Token);
    }

    public static async Task<bool> TryForwardToRunningShellAsync(
        RemoteNotificationActivationRequest request,
        TimeSpan? totalTimeout = null,
        CancellationToken cancellationToken = default,
        string? pipeName = null)
    {
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        var deadline = DateTimeOffset.UtcNow.Add(totalTimeout ?? TimeSpan.FromSeconds(2));
        while (DateTimeOffset.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                await using var client = new NamedPipeClientStream(
                    ".",
                    string.IsNullOrWhiteSpace(pipeName) ? PipeName : pipeName,
                    PipeDirection.Out,
                    PipeOptions.Asynchronous);
                using var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                connectTimeout.CancelAfter(TimeSpan.FromMilliseconds(200));
                await client.ConnectAsync(connectTimeout.Token).ConfigureAwait(false);
                var payload = JsonSerializer.Serialize(request);
                var bytes = Encoding.UTF8.GetBytes(payload);
                await client.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
                await client.FlushAsync(cancellationToken).ConfigureAwait(false);
                return true;
            }
            catch (Exception exception) when (
                exception is IOException or TimeoutException or OperationCanceledException &&
                !cancellationToken.IsCancellationRequested)
            {
                await Task.Delay(100, cancellationToken).ConfigureAwait(false);
            }
        }

        return false;
    }

    private async Task RunServerAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await using var server = new NamedPipeServerStream(
                    _pipeName,
                    PipeDirection.In,
                    1,
                    PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous);
                await server.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);
                using var memory = new MemoryStream();
                await server.CopyToAsync(memory, cancellationToken).ConfigureAwait(false);
                var request = JsonSerializer.Deserialize<RemoteNotificationActivationRequest>(memory.ToArray());
                if (request is not null)
                {
                    await _handler(request).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (IOException)
            {
                // Recreate the single-client pipe after a disconnected activation client.
            }
            catch (JsonException)
            {
                // Ignore malformed external activation payloads.
            }
            catch (Exception)
            {
                // Keep the activation endpoint available after a handler failure.
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _cancellation.Cancel();
        if (_serverTask is not null)
        {
            try
            {
                await _serverTask.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
        }
        _cancellation.Dispose();
    }
}

public sealed class RemoteNotificationActivationCoordinator : IAsyncDisposable
{
    private readonly Window? _shellWindow;
    private readonly Func<Task>? _focusCommandPalette;
    private readonly RemoteNotificationDetailWindowService _detailWindows;
    private readonly RemoteNotificationActivationPipe _pipe;

    public RemoteNotificationActivationCoordinator(
        Window? shellWindow = null,
        Func<Task>? focusCommandPalette = null,
        RemoteNotificationDetailWindowService? detailWindows = null)
    {
        _shellWindow = shellWindow;
        _focusCommandPalette = focusCommandPalette;
        _detailWindows = detailWindows ?? RemoteNotificationDetailWindowService.Shared;
        _pipe = new RemoteNotificationActivationPipe(OpenAsync);
    }

    public void Start(RemoteNotificationActivationRequest? initialRequest = null)
    {
        _pipe.Start();
        if (initialRequest is null)
        {
            return;
        }

        if (!string.IsNullOrWhiteSpace(initialRequest.MessageId) || _shellWindow is null || _shellWindow.IsVisible)
        {
            _ = OpenAsync(initialRequest);
            return;
        }

        void OpenInitial(object? sender, EventArgs eventArgs)
        {
            _shellWindow.Opened -= OpenInitial;
            _ = OpenAsync(initialRequest);
        }
        _shellWindow.Opened += OpenInitial;
    }

    public Task OpenAsync(RemoteNotificationActivationRequest request)
    {
        if (Dispatcher.UIThread.CheckAccess())
        {
            return OpenOnUiThreadAsync(request);
        }

        return Dispatcher.UIThread.InvokeAsync(() => OpenOnUiThreadAsync(request));
    }

    private async Task OpenOnUiThreadAsync(RemoteNotificationActivationRequest request)
    {
        if (!string.IsNullOrWhiteSpace(request.MessageId))
        {
            _detailWindows.TryOpen(request.MessageId);
            return;
        }

        if (_shellWindow is null)
        {
            return;
        }

        _shellWindow.Show();
        _shellWindow.WindowState = WindowState.Normal;
        _shellWindow.Activate();

        if (request.FocusCommandPalette && _focusCommandPalette is not null)
        {
            await _focusCommandPalette().ConfigureAwait(true);
            return;
        }

    }

    public ValueTask DisposeAsync() => _pipe.DisposeAsync();
}
