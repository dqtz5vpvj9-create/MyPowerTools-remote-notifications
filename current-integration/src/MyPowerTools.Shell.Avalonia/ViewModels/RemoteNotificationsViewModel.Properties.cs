using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows.Input;
using MyPowerTools.Shell.Avalonia.Services;

namespace MyPowerTools.Shell.Avalonia.ViewModels;

public sealed partial class RemoteNotificationsViewModel
{
    public ObservableCollection<RemoteNotificationMessageViewModel> Messages { get; }
    public ObservableCollection<string> KnownLabels { get; }
    public ObservableCollection<RemoteNotificationLabelChipViewModel> Chips { get; }
    public ICommand RetryCommand { get; }
    public ICommand ToggleErrorDetailsCommand { get; }
    public string Server => RemoteNotificationsLegacyStore.ServerEndpoint;
    public bool HasLabels => KnownLabels.Count > 0;
    public string FilterLabel => _filterLabel ?? RemoteNotificationsLegacyStore.FilterAll;
    public int TotalCount => Messages.Count;

    public IReadOnlyList<RemoteNotificationMessageViewModel> VisibleMessages => _filterLabel is null
        ? Messages
        : Messages.Where(message => string.Equals(message.Label, _filterLabel, StringComparison.Ordinal)).ToArray();

    public bool HasVisibleMessages => VisibleMessages.Count > 0;
    public bool ShowsEmptyOverlay => !HasVisibleMessages;
    public string EmptyOverlayText => _filterLabel is not null && Messages.Count > 0
        ? $"No notifications match filter “{_filterLabel}”"
        : "Waiting for notifications…";

    public string MessageCountText
    {
        get
        {
            var count = _filterLabel is null ? Messages.Count : VisibleMessages.Count;
            var suffix = count == 1 ? "" : "s";
            return _filterLabel is null
                ? $"{count.ToString(CultureInfo.InvariantCulture)} message{suffix}"
                : $"{count.ToString(CultureInfo.InvariantCulture)} of {Messages.Count.ToString(CultureInfo.InvariantCulture)} message{suffix}";
        }
    }

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
            OnPropertyChanged(nameof(ErrorSummary));
            OnPropertyChanged(nameof(TechnicalErrorDetails));
            if (!HasSyncError)
            {
                IsErrorDetailsExpanded = false;
            }
        }
    }
}
