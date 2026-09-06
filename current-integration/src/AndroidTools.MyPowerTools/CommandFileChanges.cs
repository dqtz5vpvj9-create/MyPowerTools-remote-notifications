using System.Threading.Channels;

namespace AndroidTools.MyPowerTools;

/// <summary>Wake catalog/history observers on writes; unused commands own no polling timer.</summary>
internal sealed class CommandFileChanges : IDisposable
{
    private readonly Dictionary<string, FileSystemWatcher> _watchers = new(StringComparer.Ordinal);
    private readonly ChannelWriter<byte> _signal;

    public CommandFileChanges(IEnumerable<string> paths, ChannelWriter<byte> signal)
    {
        _signal = signal;
        Observe(paths);
    }

    public void Observe(IEnumerable<string> paths)
    {
        var targets = paths.Select(Path.GetFullPath).Distinct(StringComparer.Ordinal).ToHashSet();
        foreach (var stale in _watchers.Keys.Where(path => !targets.Contains(path)).ToArray())
        {
            _watchers[stale].Dispose();
            _watchers.Remove(stale);
        }
        foreach (var path in targets)
        {
            if (_watchers.ContainsKey(path)) continue;
            var directory = Path.GetDirectoryName(path)!;
            if (!Directory.Exists(directory)) continue;
            var watcher = new FileSystemWatcher(directory, Path.GetFileName(path))
            { NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size };
            watcher.Changed += (_, _) => _signal.TryWrite(0);
            watcher.Created += (_, _) => _signal.TryWrite(0);
            watcher.Renamed += (_, _) => _signal.TryWrite(0);
            watcher.Deleted += (_, _) => _signal.TryWrite(0);
            watcher.Error += (_, _) => _signal.TryWrite(0);
            watcher.EnableRaisingEvents = true;
            _watchers.Add(path, watcher);
        }
    }

    public void Dispose()
    {
        foreach (var watcher in _watchers.Values) watcher.Dispose();
        _watchers.Clear();
    }
}
