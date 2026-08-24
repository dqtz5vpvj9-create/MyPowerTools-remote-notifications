using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows.Input;
using MyPowerTools.Shell.Avalonia.Services;

namespace MyPowerTools.Shell.Avalonia.ViewModels;

public sealed partial class RemoteNotificationsViewModel
{
    private readonly ObservableCollection<RemoteNotificationMessageViewModel> _visibleMessages = [];
    private ReadOnlyObservableCollection<RemoteNotificationMessageViewModel>? _visibleMessagesView;
    public ObservableCollection<RemoteNotificationMessageViewModel> Messages { get; }
    public ObservableCollection<string> KnownLabels { get; }
    public ObservableCollection<RemoteNotificationLabelChipViewModel> Chips { get; }
    public ICommand RetryCommand { get; }
    public ICommand ToggleErrorDetailsCommand { get; }
    public string Server => _settings.Endpoint;
    public bool HasLabels => KnownLabels.Any(label => !label.StartsWith("CHRS 健康", StringComparison.Ordinal));
    public string FilterLabel => _filterLabel ?? RemoteNotificationsLegacyStore.FilterAll;
    public int TotalCount => Messages.Count(message => !RemoteNotificationsLegacyStore.IsSystemHealthRecord(message.Source));

    /// <summary>
    /// Stable collection bound by the inbox ListBox. Keeping the same
    /// collection instance prevents an ItemsSource replacement on every poll.
    /// </summary>
    public IReadOnlyList<RemoteNotificationMessageViewModel> VisibleMessages =>
        _visibleMessagesView ??= new ReadOnlyObservableCollection<RemoteNotificationMessageViewModel>(_visibleMessages);

    private IEnumerable<RemoteNotificationMessageViewModel> EnumerateVisibleMessages() =>
        Messages.Where(message =>
            !RemoteNotificationsLegacyStore.IsSystemHealthRecord(message.Source) &&
            (_filterLabel is null || string.Equals(message.Label, _filterLabel, StringComparison.Ordinal)));

    public bool HasVisibleMessages => VisibleMessages.Count > 0;
    public bool ShowsEmptyOverlay => !HasVisibleMessages;
    public string EmptyOverlayText => _filterLabel is not null && Messages.Count > 0
        ? $"No notifications match filter “{_filterLabel}”"
        : "Waiting for notifications…";

    public string MessageCountText
    {
        get
        {
            var count = VisibleMessages.Count;
            var allCount = TotalCount;
            var suffix = count == 1 ? "" : "s";
            return _filterLabel is null
                ? $"{count.ToString(CultureInfo.InvariantCulture)} message{suffix}"
                : $"{count.ToString(CultureInfo.InvariantCulture)} of {allCount.ToString(CultureInfo.InvariantCulture)} message{suffix}";
        }
    }

    public bool HasSystemHealth => _latestSystemHealth is not null;
    public string SystemHealthText => _latestSystemHealth?.Message ?? "";
    public string SystemHealthForeground =>
        _latestSystemHealth?.Message.Contains("健康恢复", StringComparison.Ordinal) == true
            ? "#2E7D32"
            : "#9A3412";
    public string SystemHealthBackground =>
        _latestSystemHealth?.Message.Contains("健康恢复", StringComparison.Ordinal) == true
            ? "#E8F5E9"
            : "#FFF7ED";

    public bool PersistentWindowsToasts
    {
        get => _persistentWindowsToasts;
        set
        {
            if (!SetProperty(ref _persistentWindowsToasts, value))
            {
                return;
            }

            try
            {
                _store.SavePersistentWindowsToasts(value);
                _settings = _settings with { KeepWindowsBanners = value };
                KeepWindowsBannersDraft = value;
            }
            catch (Exception exception)
            {
                LastError = exception.Message;
                ConnectionState = "error";
            }
        }
    }

    public string ConnectionState
    {
        get => _connectionState;
        private set
        {
            if (!SetProperty(ref _connectionState, value))
            {
                return;
            }

            OnPropertyChanged(nameof(StatusText));
            OnPropertyChanged(nameof(StatusForeground));
            OnPropertyChanged(nameof(StatusBackground));
            OnPropertyChanged(nameof(ConnectionSummary));
            OnPropertyChanged(nameof(IsPolling));
            OnPropertyChanged(nameof(HasSyncError));
            OnPropertyChanged(nameof(ShowSyncError));
            OnPropertyChanged(nameof(ErrorTitle));
            OnPropertyChanged(nameof(ErrorSummary));
            OnPropertyChanged(nameof(TechnicalErrorDetails));
            _retryCommand.NotifyCanExecuteChanged();
            if (!HasSyncError)
            {
                IsErrorDetailsExpanded = false;
            }
        }
    }

    public string StatusText => ConnectionState switch
    {
        "running" => "Syncing",
        "ok" => "Connected",
        "idle" => "Idle",
        "auth" => "Sign-in required",
        "error" => "Sync needs attention",
        _ => "Starting"
    };

    public string ConnectionSummary => ConnectionState switch
    {
        "running" => "Checking the signed remote feed for new messages…",
        "ok" => "Automatic synchronization is active.",
        "idle" => "Automatic synchronization is active; no new messages were found.",
        "auth" => "The remote feed could not verify this device.",
        "error" => "The latest synchronization attempt failed.",
        _ => "Preparing automatic synchronization…"
    };

    public bool IsPolling => string.Equals(ConnectionState, "running", StringComparison.OrdinalIgnoreCase);
    public bool HasSyncError => ConnectionState is "error" or "auth" ||
        !string.Equals(LastError, "none", StringComparison.OrdinalIgnoreCase);
    public string ErrorTitle => ConnectionState == "auth" ? "Authentication failed" : "Notifications could not sync";
    public string ErrorSummary => BuildErrorSummary(ConnectionState, LastError);
    public string TechnicalErrorDetails =>
        $"Endpoint: {Server}\nLast attempt: {LastPoll}\n\n{LastError}";
    public string ErrorDetailsActionLabel => IsErrorDetailsExpanded ? "Hide details" : "Show details";

    public bool IsErrorDetailsExpanded
    {
        get => _isErrorDetailsExpanded;
        private set
        {
            if (SetProperty(ref _isErrorDetailsExpanded, value))
            {
                OnPropertyChanged(nameof(ErrorDetailsActionLabel));
            }
        }
    }

    public string StatusForeground => ConnectionState switch
    {
        "running" => "#2979FF",
        "ok" => "#43A047",
        "idle" => "#9E9E9E",
        "error" or "auth" => "#E53935",
        _ => "#9E9E9E"
    };

    public string StatusBackground => ConnectionState switch
    {
        "running" => "#E3F2FD",
        "ok" => "#E8F5E9",
        "idle" => "#F0F0F0",
        "error" or "auth" => "#FFEBEE",
        _ => "#F0F0F0"
    };

    public string LastPoll
    {
        get => _lastPoll;
        private set
        {
            if (SetProperty(ref _lastPoll, value))
            {
                OnPropertyChanged(nameof(LastSyncText));
                OnPropertyChanged(nameof(TechnicalErrorDetails));
            }
        }
    }

    public string LastSyncText => string.Equals(LastPoll, "never", StringComparison.OrdinalIgnoreCase)
        ? "Not synced yet"
        : LastPoll;

    public string Fetched
    {
        get => _fetched;
        private set
        {
            if (SetProperty(ref _fetched, value))
            {
                OnPropertyChanged(nameof(SyncResultText));
            }
        }
    }

    public string Shown
    {
        get => _shown;
        private set
        {
            if (SetProperty(ref _shown, value))
            {
                OnPropertyChanged(nameof(SyncResultText));
            }
        }
    }

    public string SyncResultText => Shown == "0"
        ? "No new messages"
        : $"{Shown} new · {Fetched} received";

    public string Latest
    {
        get => _latest;
        private set => SetProperty(ref _latest, value);
    }

    public string LastError
    {
        get => _lastError;
        private set
        {
            if (!SetProperty(ref _lastError, string.IsNullOrWhiteSpace(value) ? "none" : value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSyncError));
            OnPropertyChanged(nameof(ShowSyncError));
            OnPropertyChanged(nameof(ErrorSummary));
            OnPropertyChanged(nameof(TechnicalErrorDetails));
            if (!HasSyncError)
            {
                IsErrorDetailsExpanded = false;
            }
        }
    }
}
