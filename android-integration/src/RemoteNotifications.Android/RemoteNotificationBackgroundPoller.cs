using MyPowerTools.Platform.Abstractions;

namespace RemoteNotifications.Android;

/// <summary>
/// Owns the only background polling loop on Android and the platform foreground-activity lease
/// that keeps it alive while the tool UI is hidden.
///
/// Rules this type exists to enforce:
/// <list type="bullet">
///   <item>polling never starts by itself. <see cref="StartAsync"/> is only reached from an
///   explicit user command (<c>polling.start</c>) or an explicit settings action, never from
///   module initialization or app start;</item>
///   <item>the loop runs only while a <c>background.activity</c> lease is held, so Android shows
///   the user a persistent "stop" affordance and the platform can end the work;</item>
///   <item><see cref="StopAsync"/> cancels and awaits the loop before releasing the lease, so a
///   disabled module performs no further network pulls;</item>
///   <item>a failed pull reports the error and keeps waiting for the next interval instead of
///   tearing the lease down, so a transient network drop does not silently disable sync.</item>
/// </list>
/// </summary>
internal sealed class RemoteNotificationBackgroundPoller : IAsyncDisposable
{
    private readonly IBackgroundActivityService? _background;
    private readonly Func<CancellationToken, Task> _pollOnce;
    private readonly Action<string> _onError;
    private readonly Action<bool> _onStateChanged;
    private readonly Func<TimeSpan> _intervalProvider;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private CancellationTokenSource? _lifetime;
    private Task? _loop;
    private IDisposable? _lease;
    private volatile bool _active;

    public RemoteNotificationBackgroundPoller(
        IBackgroundActivityService? background,
        Func<CancellationToken, Task> pollOnce,
        Func<TimeSpan> intervalProvider,
        Action<bool> onStateChanged,
        Action<string> onError)
    {
        _background = background;
        _pollOnce = pollOnce ?? throw new ArgumentNullException(nameof(pollOnce));
        _intervalProvider = intervalProvider ?? throw new ArgumentNullException(nameof(intervalProvider));
        _onStateChanged = onStateChanged ?? throw new ArgumentNullException(nameof(onStateChanged));
        _onError = onError ?? throw new ArgumentNullException(nameof(onError));
    }

    /// <summary>Test seam: the loop's wait between two pulls.</summary>
    internal Func<TimeSpan, CancellationToken, Task> DelayAsync { get; set; } = Task.Delay;

    public bool IsActive => _active;

    public bool IsAvailable => _background is not null;

    public async Task<bool> StartAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_active)
            {
                return false;
            }

            if (_background is null)
            {
                throw new InvalidOperationException(
                    "当前平台没有提供后台任务能力，无法在应用退到后台后继续接收通知。");
            }

            // The lease is what makes the OS treat this as user-visible background work; the
            // platform shows its own persistent notification with a stop action.
            var lease = await _background.BeginAsync(
                RemoteNotificationsAndroidOptions.ModuleId,
                RemoteNotificationsAndroidOptions.BackgroundActivityTitle,
                waitingForPeers: false,
                cancellationToken).ConfigureAwait(false);
            _lease = lease;
            // The loop captures the local, not the field: StopAsync clears the field, and the pool
            // thread may start after that.
            var lifetime = new CancellationTokenSource();
            _lifetime = lifetime;
            _active = true;
            _loop = Task.Run(() => LoopAsync(lifetime.Token), CancellationToken.None);
        }
        finally
        {
            _gate.Release();
        }

        _onStateChanged(true);
        return true;
    }

    public async Task<bool> StopAsync()
    {
        CancellationTokenSource? lifetime;
        Task? loop;
        IDisposable? lease;
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!_active)
            {
                return false;
            }

            lifetime = _lifetime;
            loop = _loop;
            lease = _lease;
            _lifetime = null;
            _loop = null;
            _lease = null;
            _active = false;
        }
        finally
        {
            _gate.Release();
        }

        if (lifetime is not null)
        {
            await lifetime.CancelAsync().ConfigureAwait(false);
        }

        if (loop is not null)
        {
            try
            {
                await loop.WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
            }
            catch (Exception exception) when (exception is TimeoutException or OperationCanceledException)
            {
                // The loop observes cancellation between pulls; a bounded wait keeps Stop honest
                // without blocking the caller on a stuck network call that already has its own
                // 15 second attempt timeout.
            }
        }

        lifetime?.Dispose();
        lease?.Dispose();
        _onStateChanged(false);
        return true;
    }

    private async Task LoopAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await _pollOnce(cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _onError(Describe(exception));
            }

            try
            {
                await DelayAsync(_intervalProvider(), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private static string Describe(Exception exception) =>
        string.IsNullOrWhiteSpace(exception.Message)
            ? exception.GetType().Name
            : exception.Message;

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
        _gate.Dispose();
    }
}
