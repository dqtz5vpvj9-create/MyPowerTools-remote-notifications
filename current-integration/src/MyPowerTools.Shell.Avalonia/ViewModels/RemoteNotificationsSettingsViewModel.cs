using System.Globalization;
using System.Windows.Input;
using MyPowerTools.RemoteNotifications.Configuration;
using MyPowerTools.Shell.Avalonia.Services;

namespace MyPowerTools.Shell.Avalonia.ViewModels;

public sealed partial class RemoteNotificationsViewModel
{
    private string _protocolDraft = RemoteNotificationSettings.DefaultProtocol;
    private string _hostDraft = RemoteNotificationSettings.DefaultHost;
    private string _portDraft = RemoteNotificationSettings.DefaultPort.ToString(CultureInfo.InvariantCulture);
    private string _channelDraft = RemoteNotificationSettings.DefaultChannel;
    private string _pollIntervalDraft = RemoteNotificationSettings.DefaultPollIntervalSeconds.ToString(CultureInfo.InvariantCulture);
    private string _privateKeyPathDraft = RemoteNotificationSettings.DefaultPrivateKeyPath;
    private bool _keepWindowsBannersDraft;
    private bool _isSettingsVisible;
    private bool _isSettingsOperationRunning;
    private string _settingsFeedback = "";
    private string _settingsFeedbackState = "idle";

    public event EventHandler? PollingConfigurationChanged;

    public IReadOnlyList<string> ProtocolOptions { get; } = ["https", "http"];
    public ICommand ShowInboxCommand { get; private set; } = null!;
    public ICommand ShowSettingsCommand { get; private set; } = null!;
    public ICommand SaveSettingsCommand { get; private set; } = null!;
    public ICommand TestSettingsCommand { get; private set; } = null!;
    public ICommand ResetSettingsCommand { get; private set; } = null!;

    public int PollIntervalSeconds => _settings.PollIntervalSeconds;

    public bool IsSettingsVisible
    {
        get => _isSettingsVisible;
        private set
        {
            if (!SetProperty(ref _isSettingsVisible, value))
            {
                return;
            }

            OnPropertyChanged(nameof(IsInboxVisible));
            OnPropertyChanged(nameof(ShowSyncError));
            OnPropertyChanged(nameof(ShowInboxLabels));
        }
    }

    public bool IsInboxVisible => !IsSettingsVisible;
    public bool ShowSyncError => IsInboxVisible && HasSyncError;
    public bool ShowInboxLabels => IsInboxVisible && HasLabels;

    public bool IsSettingsOperationRunning
    {
        get => _isSettingsOperationRunning;
        private set => SetProperty(ref _isSettingsOperationRunning, value);
    }

    public string ProtocolDraft
    {
        get => _protocolDraft;
        set => SetDraft(ref _protocolDraft, value);
    }

    public string HostDraft
    {
        get => _hostDraft;
        set => SetDraft(ref _hostDraft, value);
    }

    public string PortDraft
    {
        get => _portDraft;
        set => SetDraft(ref _portDraft, value);
    }

    public string ChannelDraft
    {
        get => _channelDraft;
        set => SetDraft(ref _channelDraft, value);
    }

    public string PollIntervalDraft
    {
        get => _pollIntervalDraft;
        set => SetDraft(ref _pollIntervalDraft, value);
    }

    public string PrivateKeyPathDraft
    {
        get => _privateKeyPathDraft;
        set
        {
            if (SetDraft(ref _privateKeyPathDraft, value))
            {
                OnPropertyChanged(nameof(PrivateKeyStatus));
            }
        }
    }

    public bool KeepWindowsBannersDraft
    {
        get => _keepWindowsBannersDraft;
        set => SetDraft(ref _keepWindowsBannersDraft, value);
    }

    public string PrivateKeyStatus
    {
        get
        {
            var candidate = BuildDraftSettings();
            var validation = candidate.Validate();
            if (!validation.IsValid || validation.Settings is null)
            {
                return "Key path needs attention";
            }

            return File.Exists(validation.Settings.ExpandedPrivateKeyPath)
                ? "Ed25519 key file found; key contents stay hidden"
                : "Key file was not found at this path";
        }
    }

    public string SettingsFeedback
    {
        get => _settingsFeedback;
        private set
        {
            if (SetProperty(ref _settingsFeedback, value))
            {
                OnPropertyChanged(nameof(HasSettingsFeedback));
            }
        }
    }

    public bool HasSettingsFeedback => !string.IsNullOrWhiteSpace(SettingsFeedback);

    public string SettingsFeedbackForeground => SettingsFeedbackState switch
    {
        "success" => "#2E7D32",
        "error" => "#C62828",
        _ => "#616161"
    };

    private string SettingsFeedbackState
    {
        get => _settingsFeedbackState;
        set
        {
            if (SetProperty(ref _settingsFeedbackState, value))
            {
                OnPropertyChanged(nameof(SettingsFeedbackForeground));
            }
        }
    }

    private void InitializeSettingsEditor()
    {
        LoadDraft(_settings);
        ShowInboxCommand = new AsyncRelayCommand(() =>
        {
            IsSettingsVisible = false;
            return Task.CompletedTask;
        });
        ShowSettingsCommand = new AsyncRelayCommand(() =>
        {
            IsSettingsVisible = true;
            return Task.CompletedTask;
        });
        SaveSettingsCommand = new AsyncRelayCommand(SaveSettingsAsync);
        TestSettingsCommand = new AsyncRelayCommand(TestSettingsAsync);
        ResetSettingsCommand = new AsyncRelayCommand(() =>
        {
            LoadDraft(_settings);
            SetSettingsFeedback("Changes reverted to the saved configuration.", "idle");
            return Task.CompletedTask;
        });
    }

    private async Task SaveSettingsAsync()
    {
        var validation = BuildDraftSettings().Validate();
        if (!validation.IsValid || validation.Settings is null)
        {
            SetSettingsFeedback(validation.Error, "error");
            return;
        }

        IsSettingsOperationRunning = true;
        try
        {
            await _pollGate.WaitAsync().ConfigureAwait(true);
            try
            {
                _settingsStore.Save(validation.Settings);
                _settings = validation.Settings;
                _poller = _pollerFactory(_settings);
                _persistentWindowsToasts = _settings.KeepWindowsBanners;
                OnPropertyChanged(nameof(PersistentWindowsToasts));
                OnPropertyChanged(nameof(Server));
                OnPropertyChanged(nameof(PollIntervalSeconds));
                PollingConfigurationChanged?.Invoke(this, EventArgs.Empty);
                LoadDraft(_settings);
                SetSettingsFeedback("Settings saved. Signed synchronization restarted with the new configuration.", "success");
            }
            finally
            {
                _pollGate.Release();
            }

            await PollAsync().ConfigureAwait(true);
        }
        catch (Exception exception)
        {
            SetSettingsFeedback(exception.Message, "error");
        }
        finally
        {
            IsSettingsOperationRunning = false;
        }
    }

    private async Task TestSettingsAsync()
    {
        var validation = BuildDraftSettings().Validate();
        if (!validation.IsValid || validation.Settings is null)
        {
            SetSettingsFeedback(validation.Error, "error");
            return;
        }

        IsSettingsOperationRunning = true;
        SetSettingsFeedback("Checking the Ed25519 signing key and remote endpoint…", "idle");
        try
        {
            _ = RemoteNotificationSshSigner.SignHandshake(validation.Settings.ExpandedPrivateKeyPath);
            var result = await _pollerFactory(validation.Settings).PullAsync("").ConfigureAwait(true);
            if (result.IsSuccess)
            {
                SetSettingsFeedback(
                    $"Signing succeeded and {validation.Settings.Endpoint} accepted the signed request.",
                    "success");
            }
            else
            {
                SetSettingsFeedback($"Signing succeeded; endpoint check failed: {result.Error}", "error");
            }
        }
        catch (Exception exception)
        {
            SetSettingsFeedback(exception.Message, "error");
        }
        finally
        {
            IsSettingsOperationRunning = false;
            OnPropertyChanged(nameof(PrivateKeyStatus));
        }
    }

    private RemoteNotificationSettings BuildDraftSettings()
    {
        _ = int.TryParse(PortDraft, NumberStyles.Integer, CultureInfo.InvariantCulture, out var port);
        _ = int.TryParse(PollIntervalDraft, NumberStyles.Integer, CultureInfo.InvariantCulture, out var pollInterval);
        return new RemoteNotificationSettings(
            ProtocolDraft,
            HostDraft,
            port,
            ChannelDraft,
            pollInterval,
            PrivateKeyPathDraft,
            KeepWindowsBannersDraft);
    }

    private void LoadDraft(RemoteNotificationSettings settings)
    {
        _protocolDraft = settings.Protocol;
        _hostDraft = settings.Host;
        _portDraft = settings.Port.ToString(CultureInfo.InvariantCulture);
        _channelDraft = settings.Channel;
        _pollIntervalDraft = settings.PollIntervalSeconds.ToString(CultureInfo.InvariantCulture);
        _privateKeyPathDraft = settings.PrivateKeyPath;
        _keepWindowsBannersDraft = settings.KeepWindowsBanners;
        OnPropertyChanged(nameof(ProtocolDraft));
        OnPropertyChanged(nameof(HostDraft));
        OnPropertyChanged(nameof(PortDraft));
        OnPropertyChanged(nameof(ChannelDraft));
        OnPropertyChanged(nameof(PollIntervalDraft));
        OnPropertyChanged(nameof(PrivateKeyPathDraft));
        OnPropertyChanged(nameof(KeepWindowsBannersDraft));
        OnPropertyChanged(nameof(PrivateKeyStatus));
    }

    private bool SetDraft(ref string field, string value)
    {
        var changed = SetProperty(ref field, value ?? "");
        if (changed)
        {
            ClearSettingsFeedback();
        }
        return changed;
    }

    private bool SetDraft(ref bool field, bool value)
    {
        var changed = SetProperty(ref field, value);
        if (changed)
        {
            ClearSettingsFeedback();
        }
        return changed;
    }

    private void ClearSettingsFeedback()
    {
        SettingsFeedback = "";
        SettingsFeedbackState = "idle";
    }

    private void SetSettingsFeedback(string message, string state)
    {
        SettingsFeedbackState = state;
        SettingsFeedback = message;
    }
}
