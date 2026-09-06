using MyPowerTools.AvaloniaSdk;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public partial class RemoteNotificationsView : IMptShortcutCommandSource
{
    public string ShortcutToolId => "remote-notifications";
    public string ShortcutContext => DataContext is RemoteNotificationsViewModel vm ? vm.IsSettingsVisible ? "settings" : vm.IsClaudeTaskVisible ? "tasks" : "inbox" : "";

    public IReadOnlyList<MptShortcutCommand> GetShortcutCommands()
    {
        if (DataContext is not RemoteNotificationsViewModel vm) return [];
        return
        [
            new("remote-notifications.ui.copy-timeline", CopyFilteredTimelineAsync, () => vm.VisibleMessages.Count > 0 && !vm.IsSettingsVisible),
            new("remote-notifications.ui.mark-visible-read", () => { vm.MarkVisibleMessagesAsRead(); return Task.CompletedTask; }, () => vm.VisibleMessages.Count > 0 && !vm.IsSettingsVisible),
            MptShortcutCommand.FromCommand("remote-notifications.ui.retry", vm.RetryCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.show-inbox", vm.ShowInboxCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.show-settings", vm.ShowSettingsCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.show-claude-task", vm.ShowClaudeTaskCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.save-settings", vm.SaveSettingsCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.test-settings", vm.TestSettingsCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.reset-settings", vm.ResetSettingsCommand),
            MptShortcutCommand.FromCommand("remote-notifications.ui.toggle-error-details", vm.ToggleErrorDetailsCommand),
            new("remote-notifications.ui.search", () => { OnSearchClick(this, new Avalonia.Interactivity.RoutedEventArgs()); return Task.CompletedTask; }, () => !vm.IsSettingsVisible),
        ];
    }
}
