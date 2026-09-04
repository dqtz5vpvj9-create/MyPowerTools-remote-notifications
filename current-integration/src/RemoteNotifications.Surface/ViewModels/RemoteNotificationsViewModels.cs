using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.Globalization;
using System.Windows.Input;
using Avalonia.Threading;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

using MyPowerTools.AvaloniaSdk;
namespace RemoteNotifications.Surface.ViewModels;

public sealed partial class RemoteNotificationsViewModel : MyPowerTools.AvaloniaSdk.ToolSurfacePageViewModel
{
    private readonly IRemoteNotificationsStore _store;
    private readonly IRemoteNotificationsServiceClient? _serviceClient;
    private IRemoteNotificationPoller _poller;
    private readonly IRemoteNotificationSettingsStore _settingsStore;
    private readonly Func<RemoteNotificationSettings, IRemoteNotificationPoller> _pollerFactory;
    private RemoteNotificationSettings _settings;
    private readonly IRemoteNotificationToastPublisher _toastPublisher;
    private RemoteNotificationSeenIdRing _seenIds;
    private readonly HashSet<string> _unreadLabels = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _pollGate = new(1, 1);
    private readonly MptAsyncRelayCommand _retryCommand;
    private string? _filterLabel;
    private bool _persistentWindowsToasts;
    private string _connectionState = "starting";
    private string _lastPoll = "never";
    private string _fetched = "0";
    private string _shown = "0";
    private string _latest = "never";
    private string _lastError = "none";
    private bool _isErrorDetailsExpanded;
    private bool _isSearchVisible;
    private string _searchQuery = "";
    private string _waterline;
    private CancellationTokenSource? _serviceRefreshCancellation;
    private RemoteNotificationRecord? _latestSystemHealth;

    public RemoteNotificationsViewModel(
        RemoteNotificationsSnapshot snapshot,
        IRemoteNotificationsStore? store = null,
        IRemoteNotificationPoller? poller = null,
        IRemoteNotificationToastPublisher? toastPublisher = null,
        IRemoteNotificationSettingsStore? settingsStore = null,
        Func<RemoteNotificationSettings, IRemoteNotificationPoller>? pollerFactory = null,
        IRemoteNotificationsServiceClient? serviceClient = null)
        : base(
            "Remote Notifications",
            "Signed messages are synchronized automatically and kept in your local notification history.",
            MyPowerTools.AvaloniaSdk.ToolSurfaceState.Ready)
    {
        _settingsStore = settingsStore ?? new RemoteNotificationSettingsStore();
        _settings = _settingsStore.Load();
        _store = store ?? new RemoteNotificationsLegacyStore(_settingsStore);
        _serviceClient = serviceClient;
        _pollerFactory = pollerFactory ?? (settings => new RemoteNotificationHttpPoller(settings));
        _poller = poller ?? _pollerFactory(_settings);
        _toastPublisher = toastPublisher ?? RemoteNotificationToastPublisherFactory.CreateForCurrentRuntime();
        _filterLabel = snapshot.FilterLabel;
        _persistentWindowsToasts = snapshot.PersistentWindowsToasts;
        KnownLabels = new ObservableCollection<string>(snapshot.KnownLabels);
        Messages = new ObservableCollection<RemoteNotificationMessageViewModel>(
            snapshot.MessagesOldestFirst.Reverse().Select(message => new RemoteNotificationMessageViewModel(message)));
        Messages.CollectionChanged += OnMessagesCollectionChanged;
        ObserveSystemHealth(snapshot.MessagesOldestFirst);
        _seenIds = new RemoteNotificationSeenIdRing(snapshot.SeenMessageIds);
        foreach (var message in Messages.Reverse())
        {
            _seenIds.TryAccept(message.Id, message.FallbackId);
        }
        Chips = new ObservableCollection<RemoteNotificationLabelChipViewModel>();
        _retryCommand = new MptAsyncRelayCommand(RetryAsync, () => !IsPolling);
        RetryCommand = _retryCommand;
        ToggleErrorDetailsCommand = new MptAsyncRelayCommand(() =>
        {
            IsErrorDetailsExpanded = !IsErrorDetailsExpanded;
            return Task.CompletedTask;
        });
        InitializeSettingsEditor();
        _waterline = ResolveInitialWaterline(snapshot.MessagesOldestFirst);
        RebuildChips();
        NotifyMessageViewChanged();
    }

    private void OnMessagesCollectionChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        SyncVisibleMessages();
    }

    private void SyncVisibleMessages()
    {
        var desired = EnumerateVisibleMessages().ToArray();

        for (var index = _visibleMessages.Count - 1; index >= desired.Length; index--)
        {
            _visibleMessages.RemoveAt(index);
        }

        for (var index = 0; index < desired.Length; index++)
        {
            var wanted = desired[index];
            if (index < _visibleMessages.Count && ReferenceEquals(_visibleMessages[index], wanted))
            {
                continue;
            }

            var existingIndex = -1;
            for (var candidate = index + 1; candidate < _visibleMessages.Count; candidate++)
            {
                if (ReferenceEquals(_visibleMessages[candidate], wanted))
                {
                    existingIndex = candidate;
                    break;
                }
            }

            if (existingIndex >= 0)
            {
                _visibleMessages.Move(existingIndex, index);
            }
            else
            {
                _visibleMessages.Insert(index, wanted);
            }
        }

        for (var index = _visibleMessages.Count - 1; index >= desired.Length; index--)
        {
            _visibleMessages.RemoveAt(index);
        }
    }

    public Task RetryAsync()
    {
        return PollAsync();
    }

    public void Activate()
    {
        if (_serviceClient is null || _serviceRefreshCancellation is not null)
        {
            return;
        }

        _serviceRefreshCancellation = new CancellationTokenSource();
        _ = ObserveServiceAsync(_serviceRefreshCancellation.Token);
    }

    public void Deactivate()
    {
        var cancellation = Interlocked.Exchange(ref _serviceRefreshCancellation, null);
        if (cancellation is null)
        {
            return;
        }

        cancellation.Cancel();
        cancellation.Dispose();
    }

    public void OpenSearch()
    {
        IsSearchVisible = true;
    }

    public void CloseSearch()
    {
        SearchQuery = "";
        IsSearchVisible = false;
    }

    public async Task<int> PresentPersistedAsync(
        IEnumerable<string> messageIds,
        CancellationToken cancellationToken = default)
    {
        await _pollGate.WaitAsync(cancellationToken).ConfigureAwait(true);
        try
        {
            var requestedIds = messageIds
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Distinct(StringComparer.Ordinal)
                .ToArray();
            if (requestedIds.Length == 0)
            {
                return 0;
            }

            var persisted = _store.Load().MessagesOldestFirst
                .Select(message => new
                {
                    Message = message,
                    StableId = RemoteNotificationsLegacyStore.StableId(message),
                    FallbackId = RemoteNotificationsLegacyStore.FallbackId(message)
                })
                .ToArray();
            var visibleBefore = CaptureVisibleMessageSources();
            var shown = 0;
            foreach (var requestedId in requestedIds)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var match = persisted.LastOrDefault(item =>
                    string.Equals(item.StableId, requestedId, StringComparison.Ordinal) ||
                    string.Equals(item.FallbackId, requestedId, StringComparison.Ordinal));
                if (match is null || !_seenIds.TryAccept(match.StableId, match.FallbackId))
                {
                    continue;
                }

                if (FindMessageById(match.StableId) is null)
                {
                    var message = new RemoteNotificationMessageViewModel(match.Message);
                    Messages.Insert(0, message);
                    if (!RemoteNotificationsLegacyStore.IsSystemHealthRecord(match.Message))
                    {
                        TouchLabel(message.Label);
                    }
                }

                ObserveSystemHealth(match.Message);
                if (!RemoteNotificationsLegacyStore.IsSystemHealthRecord(match.Message))
                {
                    var result = await _toastPublisher.PublishAsync(
                        match.Message,
                        match.StableId,
                        PersistentWindowsToasts,
                        cancellationToken).ConfigureAwait(true);
                    if (result.Shown)
                    {
                        shown++;
                    }
                }
            }

            while (Messages.Count > RemoteNotificationsLegacyStore.MaximumMessages)
            {
                Messages.RemoveAt(Messages.Count - 1);
            }

            _store.SaveSeenMessageIds(_seenIds.OldestFirst);
            Shown = shown.ToString(CultureInfo.InvariantCulture);
            NotifyMessageViewChanged(HasVisibleMessageSourcesChanged(visibleBefore));
            return shown;
        }
        finally
        {
            _pollGate.Release();
        }
    }

    public async Task PollAsync(CancellationToken cancellationToken = default)
    {
        if (!await _pollGate.WaitAsync(0, cancellationToken).ConfigureAwait(true))
        {
            return;
        }

        try
        {
            if (_serviceClient is not null)
            {
                ConnectionState = "running";
                var state = await _serviceClient.PollAsync(cancellationToken).ConfigureAwait(true);
                ApplyServiceState(state);
                ReloadPersistedSnapshot();
                return;
            }

            LastError = "none";
            ConnectionState = "running";
            var pollTime = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
            var result = await _poller.PullAsync(_waterline, cancellationToken).ConfigureAwait(true);
            LastPoll = pollTime;
            Fetched = result.Notifications.Count.ToString(CultureInfo.InvariantCulture);

            var saneNotifications = result.Notifications
                .Where(IsSaneNotification)
                .OrderBy(message => ParseSortTime(message.ServerTimestamp.Length > 0 ? message.ServerTimestamp : message.Timestamp))
                .ToArray();
            var visibleBefore = CaptureVisibleMessageSources();
            ObserveSystemHealth(saneNotifications);
            var shown = 0;
            var toastCount = 0;
            var healthReceived = false;
            const int maxToasts = 3;
            foreach (var notification in saneNotifications)
            {
                var id = RemoteNotificationsLegacyStore.StableId(notification);
                var fallbackId = RemoteNotificationsLegacyStore.FallbackId(notification);
                if (!_seenIds.TryAccept(id, fallbackId))
                {
                    continue;
                }

                var isSystemHealth = RemoteNotificationsLegacyStore.IsSystemHealthRecord(notification);
                healthReceived |= isSystemHealth;
                if (!RemoteNotificationsLegacyStore.IsTaskCompletedRecord(notification))
                {
                    var message = new RemoteNotificationMessageViewModel(notification);
                    Messages.Insert(0, message);
                    if (!isSystemHealth)
                    {
                        TouchLabel(message.Label);
                    }
                }
                if (!isSystemHealth && toastCount < maxToasts)
                {
                    _ = await _toastPublisher.PublishAsync(
                        notification,
                        id,
                        PersistentWindowsToasts,
                        cancellationToken).ConfigureAwait(true);
                }
                if (!isSystemHealth)
                {
                    toastCount++;
                    shown++;
                }
            }

            if (shown > 0 || healthReceived)
            {
                ApplyMergedHistoryToView();
                var seenSnapshot = _seenIds.OldestFirst;
                var messagesSnapshot = Messages.Reverse().Select(m => m.Source).ToArray();
                await Task.Run(() =>
                {
                    _store.SaveSeenMessageIds(seenSnapshot);
                    _store.SaveMessages(messagesSnapshot);
                }).ConfigureAwait(true);
            }

            if (shown > maxToasts)
            {
                var summaryRecord = new RemoteNotificationRecord(
                    "summary",
                    "Remote Notifications",
                    $"{shown - maxToasts} more new notifications",
                    "",
                    DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture));
                _ = await _toastPublisher.PublishAsync(
                    summaryRecord,
                    "summary-batch",
                    PersistentWindowsToasts,
                    cancellationToken).ConfigureAwait(true);
            }

            while (Messages.Count > RemoteNotificationsLegacyStore.MaximumMessages)
            {
                var removed = Messages[^1];
                Messages.RemoveAt(Messages.Count - 1);
            }

            Shown = shown.ToString(CultureInfo.InvariantCulture);
            var latestNotification = saneNotifications.LastOrDefault();
            if (latestNotification is not null)
            {
                Latest = FormatDisplayTimestamp(latestNotification.Timestamp);
                var waterline = latestNotification.ServerTimestamp.Length > 0
                    ? latestNotification.ServerTimestamp
                    : latestNotification.Timestamp;
                if (RemoteNotificationMessageViewModel.TryParseServerTimestamp(waterline, out var parsed))
                {
                    _waterline = parsed.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
                }
            }

            LastError = result.Error;
            ConnectionState = result.State;
            foreach (var message in Messages)
            {
                message.RefreshRelativeTime();
            }

            NotifyMessageViewChanged(HasVisibleMessageSourcesChanged(visibleBefore));
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            LastPoll = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
            Fetched = "0";
            Shown = "0";
            LastError = exception.Message;
            ConnectionState = "error";
        }
        finally
        {
            _pollGate.Release();
        }
    }

    private async Task ObserveServiceAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                var state = await _serviceClient!.GetStateAsync(cancellationToken).ConfigureAwait(false);
                await Dispatcher.UIThread.InvokeAsync(() =>
                {
                    ApplyServiceState(state);
                    ReloadPersistedSnapshot();
                });
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                await Dispatcher.UIThread.InvokeAsync(() =>
                {
                    ConnectionState = "error";
                    LastError = exception.Message;
                });
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(1), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
        }
    }

    private void ApplyServiceState(RemoteNotificationsServiceState state)
    {
        var deliveryFailed = state.NotificationState is
            "permission-denied" or "delivery-failed" or "error";
        ConnectionState = deliveryFailed ? "error" : state.ConnectionState;
        LastPoll = state.LastPoll;
        LastError = deliveryFailed && !string.IsNullOrWhiteSpace(state.NotificationError)
            ? state.NotificationError
            : state.LastError;
        Latest = state.Latest;
        Fetched = state.Fetched.ToString(CultureInfo.InvariantCulture);
        Shown = state.Shown.ToString(CultureInfo.InvariantCulture);
    }

    private void ReloadPersistedSnapshot()
    {
        var visibleBefore = CaptureVisibleMessageSources();
        var snapshot = _store.Load();
        ObserveSystemHealth(snapshot.MessagesOldestFirst);
        var persistedNewestFirst = snapshot.MessagesOldestFirst.Reverse().ToArray();
        var currentSources = Messages.Select(message => message.Source).ToArray();
        var persistedSources = persistedNewestFirst.ToArray();
        if (!currentSources.SequenceEqual(persistedSources))
        {
            ReconcileMessages(persistedSources);
        }

        if (!KnownLabels.SequenceEqual(snapshot.KnownLabels, StringComparer.Ordinal))
        {
            KnownLabels.Clear();
            foreach (var label in snapshot.KnownLabels)
            {
                KnownLabels.Add(label);
            }
            RebuildChips();
        }

        _seenIds = new RemoteNotificationSeenIdRing(snapshot.SeenMessageIds);
        foreach (var message in Messages.Reverse())
        {
            _seenIds.TryAccept(message.Id, message.FallbackId);
        }

        foreach (var message in Messages)
        {
            message.RefreshRelativeTime();
        }
        NotifyMessageViewChanged(HasVisibleMessageSourcesChanged(visibleBefore));
    }

    private void ApplyMergedHistoryToView()
    {
        var currentOldestFirst = Messages.Reverse().Select(message => message.Source).ToArray();
        var merged = RemoteNotificationsLegacyStore.MergeTaskCompletedRecords(currentOldestFirst);
        ReconcileMessages(merged.Reverse().ToArray());
    }

    private void ReconcileMessages(IReadOnlyList<RemoteNotificationRecord> desiredNewestFirst)
    {
        var currentNewestFirst = Messages.Select(message => message.Source).ToArray();
        if (currentNewestFirst.SequenceEqual(desiredNewestFirst))
        {
            return;
        }

        var maximumOverlap = Math.Min(currentNewestFirst.Length, desiredNewestFirst.Count);
        var overlap = 0;
        for (var candidate = maximumOverlap; candidate >= 0; candidate--)
        {
            if (!currentNewestFirst
                .Take(candidate)
                .SequenceEqual(desiredNewestFirst.Skip(desiredNewestFirst.Count - candidate)))
            {
                continue;
            }

            overlap = candidate;
            break;
        }

        if (overlap == 0 && currentNewestFirst.Length > 0 && desiredNewestFirst.Count > 0)
        {
            Messages.Clear();
            foreach (var message in desiredNewestFirst)
            {
                Messages.Add(new RemoteNotificationMessageViewModel(message));
            }
            return;
        }

        for (var index = currentNewestFirst.Length - 1; index >= overlap; index--)
        {
            Messages.RemoveAt(index);
        }

        for (var index = desiredNewestFirst.Count - overlap - 1; index >= 0; index--)
        {
            Messages.Insert(0, new RemoteNotificationMessageViewModel(desiredNewestFirst[index]));
        }
    }

    public void ClearMessages()
    {
        var hadVisibleMessages = VisibleMessages.Count > 0;
        Messages.Clear();
        _latestSystemHealth = null;
        _store.ClearMessages();
        NotifyMessageViewChanged(hadVisibleMessages);
    }

    public void AcknowledgeMessage(RemoteNotificationMessageViewModel message)
    {
        if (_unreadLabels.Remove(message.Label))
        {
            RebuildChips();
        }
    }

    public RemoteNotificationMessageViewModel? FindMessageById(string messageId)
    {
        if (string.IsNullOrWhiteSpace(messageId))
        {
            return null;
        }

        return Messages.FirstOrDefault(message =>
            string.Equals(message.Id, messageId, StringComparison.Ordinal) ||
            string.Equals(message.FallbackId, messageId, StringComparison.Ordinal));
    }

    private Task SelectFilterAsync(string? label)
    {
        if (label is not null)
        {
            _unreadLabels.Remove(label);
        }

        _filterLabel = label;
        _store.SaveFilter(label);
        RebuildChips();
        NotifyMessageViewChanged();
        return Task.CompletedTask;
    }

    private void TouchLabel(string label)
    {
        var index = KnownLabels.IndexOf(label);
        if (index >= 0)
        {
            KnownLabels.RemoveAt(index);
        }

        KnownLabels.Insert(0, label);
        _unreadLabels.Add(label);
        _store.SaveKnownLabels(KnownLabels);
        RebuildChips();
    }

    private void RebuildChips()
    {
        Chips.Clear();
        // The Claude Task label has its own dedicated page — never a chip.
        var chipLabels = KnownLabels
            .Where(label =>
                !string.Equals(label, ClaudeTaskLabel, StringComparison.Ordinal) &&
                !label.StartsWith("CHRS 健康", StringComparison.Ordinal))
            .ToArray();
        if (chipLabels.Length == 0)
        {
            OnPropertyChanged(nameof(HasLabels));
            OnPropertyChanged(nameof(ShowInboxLabels));
            return;
        }

        Chips.Add(new RemoteNotificationLabelChipViewModel(
            "All",
            null,
            _filterLabel is null,
            false,
            SelectFilterAsync));
        foreach (var label in chipLabels)
        {
            Chips.Add(new RemoteNotificationLabelChipViewModel(
                label,
                label,
                string.Equals(label, _filterLabel, StringComparison.Ordinal),
                _unreadLabels.Contains(label),
                SelectFilterAsync));
        }

        OnPropertyChanged(nameof(HasLabels));
        OnPropertyChanged(nameof(ShowInboxLabels));
    }

    private void PersistMessages()
    {
        _store.SaveMessages(Messages.Reverse().Select(message => message.Source).ToArray());
    }

    private RemoteNotificationRecord[] CaptureVisibleMessageSources()
    {
        return VisibleMessages.Select(message => message.Source).ToArray();
    }

    private bool HasVisibleMessageSourcesChanged(IReadOnlyList<RemoteNotificationRecord> previous)
    {
        return !previous.SequenceEqual(CaptureVisibleMessageSources());
    }

    private void NotifyMessageViewChanged(bool visibleMessagesChanged = true)
    {
        if (visibleMessagesChanged)
        {
            SyncVisibleMessages();
        }
        OnPropertyChanged(nameof(TotalCount));
        OnPropertyChanged(nameof(HasVisibleMessages));
        OnPropertyChanged(nameof(ShowsEmptyOverlay));
        OnPropertyChanged(nameof(EmptyOverlayText));
        OnPropertyChanged(nameof(MessageCountText));
        OnPropertyChanged(nameof(SearchResultText));
        OnPropertyChanged(nameof(HasSystemHealth));
        OnPropertyChanged(nameof(SystemHealthText));
        OnPropertyChanged(nameof(SystemHealthForeground));
        OnPropertyChanged(nameof(SystemHealthBackground));
    }

    private void ObserveSystemHealth(IEnumerable<RemoteNotificationRecord> records)
    {
        foreach (var record in records)
        {
            ObserveSystemHealth(record);
        }
    }

    private void ObserveSystemHealth(RemoteNotificationRecord record)
    {
        if (!RemoteNotificationsLegacyStore.IsSystemHealthRecord(record))
        {
            return;
        }

        var currentTime = ParseSortTime(record.ServerTimestamp.Length > 0 ? record.ServerTimestamp : record.Timestamp);
        var previousTime = _latestSystemHealth is null
            ? DateTimeOffset.MinValue
            : ParseSortTime(_latestSystemHealth.ServerTimestamp.Length > 0
                ? _latestSystemHealth.ServerTimestamp
                : _latestSystemHealth.Timestamp);
        if (_latestSystemHealth is not null && currentTime < previousTime)
        {
            return;
        }

        _latestSystemHealth = record;
        OnPropertyChanged(nameof(HasSystemHealth));
        OnPropertyChanged(nameof(SystemHealthText));
        OnPropertyChanged(nameof(SystemHealthForeground));
        OnPropertyChanged(nameof(SystemHealthBackground));
    }

    private static bool IsSaneNotification(RemoteNotificationRecord notification)
    {
        var timestamp = notification.ServerTimestamp.Length > 0
            ? notification.ServerTimestamp
            : notification.Timestamp;
        return RemoteNotificationMessageViewModel.TryParseServerTimestamp(timestamp, out var parsed) &&
               parsed <= DateTimeOffset.UtcNow.AddMinutes(2);
    }

    private static DateTimeOffset ParseSortTime(string timestamp)
    {
        return RemoteNotificationMessageViewModel.TryParseServerTimestamp(timestamp, out var parsed)
            ? parsed
            : DateTimeOffset.MinValue;
    }

    private static string ResolveInitialWaterline(
        IReadOnlyList<RemoteNotificationRecord> persistedMessages)
    {
        DateTimeOffset? newest = null;
        foreach (var message in persistedMessages)
        {
            if (string.IsNullOrWhiteSpace(message.ServerTimestamp) ||
                !RemoteNotificationMessageViewModel.TryParseServerTimestamp(message.ServerTimestamp, out var parsed) ||
                parsed > DateTimeOffset.UtcNow.AddMinutes(2))
            {
                continue;
            }

            if (newest is null || parsed > newest.Value)
            {
                newest = parsed;
            }
        }

        // Older builds did not persist the server cursor. An empty cursor asks
        // the server for its recent window; the persisted seen-id ring removes
        // replays while allowing messages missed during page navigation through.
        return newest?.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture) ?? "";
    }

    private static string FormatDisplayTimestamp(string timestamp)
    {
        return RemoteNotificationMessageViewModel.TryParseServerTimestamp(timestamp, out var parsed)
            ? parsed.ToLocalTime().ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture)
            : string.IsNullOrWhiteSpace(timestamp) ? "never" : timestamp;
    }

    private static string BuildErrorSummary(string state, string error)
    {
        if (string.Equals(state, "auth", StringComparison.OrdinalIgnoreCase))
        {
            return "Check the device signing key, then retry synchronization.";
        }

        var meaningfulLine = error
            .Replace('\r', '\n')
            .Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Reverse()
            .FirstOrDefault(line =>
                !line.StartsWith("Traceback", StringComparison.OrdinalIgnoreCase) &&
                !line.StartsWith("File ", StringComparison.OrdinalIgnoreCase) &&
                !line.StartsWith("^", StringComparison.Ordinal));
        if (string.IsNullOrWhiteSpace(meaningfulLine) ||
            string.Equals(meaningfulLine, "none", StringComparison.OrdinalIgnoreCase))
        {
            return "Check the connection and try again.";
        }

        const int maximumLength = 180;
        return meaningfulLine.Length <= maximumLength
            ? meaningfulLine
            : $"{meaningfulLine[..(maximumLength - 1)]}…";
    }
}
