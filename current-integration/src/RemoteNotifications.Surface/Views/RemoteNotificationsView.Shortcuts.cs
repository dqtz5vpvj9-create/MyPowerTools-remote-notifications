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
