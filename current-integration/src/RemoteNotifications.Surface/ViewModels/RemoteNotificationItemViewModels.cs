using System.Globalization;
using System.Windows.Input;
using RemoteNotifications.Surface.Services;

using MyPowerTools.AvaloniaSdk;
namespace RemoteNotifications.Surface.ViewModels;

public sealed class RemoteNotificationLabelChipViewModel : MyPowerTools.AvaloniaSdk.MptObservableViewModel
{
    private bool _isSelected;
    private bool _isUnread;

    public RemoteNotificationLabelChipViewModel(
        string label,
        string? filterValue,
        bool isSelected,
        bool isUnread,
        Func<string?, Task> select)
    {
        Label = label;
        FilterValue = filterValue;
        _isSelected = isSelected;
        _isUnread = isUnread;
        SelectCommand = new MptAsyncRelayCommand(() => select(FilterValue));
    }

    public string Label { get; }
    public string? FilterValue { get; }
    public ICommand SelectCommand { get; }

    public bool IsSelected
    {
        get => _isSelected;
        internal set => SetProperty(ref _isSelected, value);
    }

    public bool IsUnread
    {
        get => _isUnread;
        internal set => SetProperty(ref _isUnread, value);
    }
}

public sealed class RemoteNotificationMessageViewModel : MyPowerTools.AvaloniaSdk.MptObservableViewModel
{
    private string _relativeTime;

    public RemoteNotificationMessageViewModel(RemoteNotificationRecord source)
    {
        Source = source;
        _relativeTime = FormatRelativeTimestamp(source.Timestamp).Display;
        AbsoluteTime = FormatRelativeTimestamp(source.Timestamp).Tooltip;
    }

    public RemoteNotificationRecord Source { get; }
    public string Id => RemoteNotificationsLegacyStore.StableId(Source);
    public string FallbackId => RemoteNotificationsLegacyStore.FallbackId(Source);
    public string Channel => string.IsNullOrWhiteSpace(Source.Channel) ? "default" : Source.Channel;
    public string Message => Source.Message;
    public string DisplayMessage => RemoveLeadingLabel(Source.Message, Label);
    public string Icon => string.IsNullOrWhiteSpace(Source.Icon) ? "info" : Source.Icon.ToLowerInvariant();
    public string Timestamp => Source.Timestamp;
    public string Label => RemoteNotificationsLegacyStore.ExtractLabel(Message);
    public bool HasCustomChannel => !string.Equals(Channel, "default", StringComparison.OrdinalIgnoreCase);
    public string DetailWindowTitle => HasCustomChannel ? $"{Channel} notification" : "Remote notification";
    public string AccessibleLabel => $"{RelativeTime}, {Channel}, {Message}";

    public string IconBackground => Icon switch
    {
        "warning" => "#FF9800",
        "error" => "#E53935",
        "success" => "#43A047",
        "claude" => "#FFF7ED",
        "codex" => "#101828",
        _ => "#2979FF"
    };

    public string IconForeground => Icon switch
    {
        "claude" => "#D97706",
        _ => "#FFFFFF"
    };

    public string IconGlyph => Icon switch
    {
        "warning" => "!",
        "error" => "×",
        "success" => "✓",
        "claude" => "✦",
        "codex" => "C",
        _ => "i"
    };

    public string RelativeTime
    {
        get => _relativeTime;
        private set => SetProperty(ref _relativeTime, value);
    }

    public string AbsoluteTime { get; }

    public void RefreshRelativeTime()
    {
        RelativeTime = FormatRelativeTimestamp(Timestamp).Display;
    }

    public static (string Display, string Tooltip) FormatRelativeTimestamp(string timestamp)
    {
        var now = DateTimeOffset.Now;
        if (!TryParseServerTimestamp(timestamp, out var parsed))
        {
            return string.IsNullOrWhiteSpace(timestamp)
                ? (now.ToString("HH:mm", CultureInfo.InvariantCulture), now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture))
                : (timestamp, timestamp);
        }

        var local = parsed.ToLocalTime();
        var tooltip = local.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
        var delta = now - local;
        if (delta < TimeSpan.Zero)
        {
            return (local.ToString("HH:mm", CultureInfo.InvariantCulture), tooltip);
        }

        if (delta < TimeSpan.FromMinutes(1))
        {
            return ("just now", tooltip);
        }

        if (delta < TimeSpan.FromHours(1))
        {
            return ($"{(int)delta.TotalMinutes}m ago", tooltip);
        }

        if (local.Date == now.Date)
        {
            return (local.ToString("HH:mm", CultureInfo.InvariantCulture), tooltip);
        }

        return local.Year == now.Year
            ? (local.ToString("MM/dd HH:mm", CultureInfo.InvariantCulture), tooltip)
            : (local.ToString("yyyy/MM/dd", CultureInfo.InvariantCulture), tooltip);
    }

    public static bool TryParseServerTimestamp(string timestamp, out DateTimeOffset parsed)
    {
        if (DateTimeOffset.TryParse(
                timestamp,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AllowWhiteSpaces,
                out parsed))
        {
            return true;
        }

        parsed = default;
        return false;
    }

    private static string RemoveLeadingLabel(string message, string label)
    {
        var prefix = $"[{label}]";
        return message.StartsWith(prefix, StringComparison.Ordinal)
            ? message[prefix.Length..].TrimStart()
            : message;
    }
}
