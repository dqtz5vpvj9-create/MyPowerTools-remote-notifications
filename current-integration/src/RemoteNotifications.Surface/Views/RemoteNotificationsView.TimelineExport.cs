using System.Text;
using Avalonia.Input;
using MyPowerTools.AvaloniaSdk;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationsView
{
    protected override void OnKeyDown(KeyEventArgs eventArgs)
    {
        base.OnKeyDown(eventArgs);
        if (eventArgs.Handled ||
            !RemoteNotificationTimelineShortcut.IsCopyGesture(eventArgs.Key, eventArgs.KeyModifiers) ||
            DataContext is not RemoteNotificationsViewModel viewModel ||
            viewModel.VisibleMessages.Count == 0)
        {
            return;
        }

        eventArgs.Handled = true;
        var timeline = RemoteNotificationTimelineFormatter.Format(
            viewModel.VisibleMessages,
            viewModel.FilterLabel,
            viewModel.SearchQuery);
        MptCommandFaultBoundary.Run(
            this,
            "Copy filtered notification timeline",
            () => CopyMessageAsync(timeline));
    }
}

public static class RemoteNotificationTimelineShortcut
{
    public static bool IsCopyGesture(Key key, KeyModifiers modifiers)
    {
        if (key != Key.C)
        {
            return false;
        }

        return modifiers == (KeyModifiers.Control | KeyModifiers.Shift) ||
               modifiers == (KeyModifiers.Meta | KeyModifiers.Shift);
    }
}

public static class RemoteNotificationTimelineFormatter
{
    public static string Format(
        IReadOnlyList<RemoteNotificationMessageViewModel> messages,
        string filterLabel,
        string searchQuery)
    {
        ArgumentNullException.ThrowIfNull(messages);

        var builder = new StringBuilder();
        builder.Append("Remote notifications · ")
            .Append(messages.Count)
            .Append(messages.Count == 1 ? " message" : " messages")
            .AppendLine();

        if (!string.IsNullOrWhiteSpace(filterLabel))
        {
            builder.Append("Filter: ").AppendLine(filterLabel.Trim());
        }

        if (!string.IsNullOrWhiteSpace(searchQuery))
        {
            builder.Append("Search: ").AppendLine(searchQuery.Trim());
        }

        builder.AppendLine();
        foreach (var message in messages)
        {
            builder.Append("- [")
                .Append(message.AbsoluteTime)
                .Append("] [")
                .Append(message.Channel)
                .Append(']');
            if (message.HasSession)
            {
                builder.Append(" [").Append(message.SessionDisplay).Append(']');
            }

            builder.AppendLine();
            AppendIndentedMessage(builder, message.DisplayMessage);
        }

        return builder.ToString().TrimEnd();
    }

    private static void AppendIndentedMessage(StringBuilder builder, string message)
    {
        var normalized = (message ?? "")
            .Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Trim();
        foreach (var line in normalized.Split('\n'))
        {
            builder.Append("  ").AppendLine(line.TrimEnd());
        }
    }
}
