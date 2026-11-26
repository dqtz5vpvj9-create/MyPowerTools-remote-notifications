using System.Diagnostics;

namespace RemoteNotifications.Android;

/// <summary>
/// Android replacement for the named <see cref="System.Threading.Mutex"/> the shipped
/// <c>RemoteNotificationsLegacyStore</c> uses to serialize <c>history.json</c>.
///
/// On Android the PAL creates <c>/data/local/tmp/.dotnet-*</c> for a named mutex and the app sandbox
/// denies it with EACCES, which faulted the module three times and quarantined it; setting
/// <c>TMPDIR</c> in <c>Application.OnCreate</c> is too late. The shipped store source is compiled
/// unchanged and the projects bind the simple name <c>Mutex</c> to this type, so the store keeps its
/// call shape - construct unowned, <c>WaitOne(TimeSpan)</c> once, <c>ReleaseMutex()</c>,
/// <c>Dispose()</c> - while the lock becomes an app-private exclusive file lock.
/// <see cref="FileShare"/>.<c>None</c> is an OS-level exclusive lock on Android (flock-backed), so it
/// excludes other instances and other processes, and the OS drops it when a process dies.
/// </summary>
internal sealed class RemoteNotificationAndroidFileLock : IDisposable
{
    private const int RetryDelayMilliseconds = 25;
    private const string LockDirectoryName = "notification-locks";

    // A failed non-blocking flock surfaces as EAGAIN: Windows maps it to ERROR_SHARING_VIOLATION,
    // Android/Linux report errno 11 and macOS errno 35. Anything else (permissions, disk, IO) must
    // surface instead of being retried as contention.
    private const int WindowsSharingViolation = unchecked((int)0x80070020);
    private const int LinuxAgain = 11;
    private const int MacAgain = 35;

    private FileStream? _stream;

    public RemoteNotificationAndroidFileLock(bool initiallyOwned, string name)
    {
        if (initiallyOwned)
        {
            // The store always constructs the lock unowned; emulating an owned construction would
            // silently hold a lock nobody releases, so it is rejected.
            throw new ArgumentException(
                "The Android history lock does not support initially-owned construction.",
                nameof(initiallyOwned));
        }

        ArgumentException.ThrowIfNullOrWhiteSpace(name);
        LockPath = ResolveLockPath(name);
        Directory.CreateDirectory(Path.GetDirectoryName(LockPath)!);
    }

    internal string LockPath { get; }

    /// <summary>
    /// Maps the store's lock name to an app-private lock file. The name already ends in the
    /// deterministic token derived from the state path, so the same state path always resolves to
    /// the same lock file.
    /// </summary>
    internal static string ResolveLockPath(string name)
    {
        var fileName = Path.GetFileName(name.Replace('\\', '/'));
        return Path.Combine(ResolveStateRoot(), LockDirectoryName, fileName + ".lock");
    }

    private static string ResolveStateRoot()
    {
        var configured = Environment.GetEnvironmentVariable("MPT_DATA_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return Path.Combine(Path.GetFullPath(configured), "state");
        }

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
        {
            // Deliberately no temp-directory fallback: a shared, world-writable lock file would
            // weaken the boundary and is exactly the PAL behaviour that failed on device.
            throw new InvalidOperationException(
                "远程通知无法解析应用私有数据目录，已拒绝在临时目录创建历史记录锁。");
        }

        return Path.Combine(localAppData, "MyPowerTools", "state");
    }

    /// <summary>Acquires the lock, waiting up to <paramref name="timeout"/>; false means contention.</summary>
    public bool WaitOne(TimeSpan timeout)
    {
        var elapsed = Stopwatch.StartNew();
        while (true)
        {
            try
            {
                _stream = new FileStream(
                    LockPath,
                    FileMode.OpenOrCreate,
                    FileAccess.ReadWrite,
                    FileShare.None,
                    bufferSize: 1,
                    FileOptions.None);
                return true;
            }
            catch (IOException exception) when (IsSharingViolation(exception))
            {
                if (elapsed.Elapsed >= timeout)
                {
                    return false;
                }

                Thread.Sleep(RetryDelayMilliseconds);
            }
        }
    }

    public void ReleaseMutex() => Interlocked.Exchange(ref _stream, null)?.Dispose();

    public void Dispose() => ReleaseMutex();

    private static bool IsSharingViolation(IOException exception) =>
        exception.HResult is WindowsSharingViolation or LinuxAgain or MacAgain;
}
