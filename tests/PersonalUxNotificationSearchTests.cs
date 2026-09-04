using Avalonia.Headless.XUnit;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;
using Xunit;

namespace PersonalUx.Tests;

public sealed class PersonalUxNotificationSearchTests
{
    [AvaloniaFact]
    public void Global_search_includes_other_labels_and_tasks_without_losing_the_original_filter()
    {
        var snapshot = Snapshot();
        var store = new MemoryStore(snapshot);
        var vm = Create(snapshot, store);
        var collection = vm.VisibleMessages;
        vm.OpenSearch();
        vm.SearchQuery = "build";
        Assert.Single(vm.VisibleMessages);
        vm.SearchAllNotifications = true;
        Assert.Equal(3, vm.VisibleMessages.Count);
        Assert.Same(collection, vm.VisibleMessages);
        Assert.Equal("alpha", vm.FilterLabel);
        Assert.Equal("All labels and Claude Task", vm.SearchScopeText);
        vm.CloseSearch();
        Assert.Single(vm.VisibleMessages);
        Assert.Equal("alpha", vm.FilterLabel);
        Assert.False(vm.SearchAllNotifications);
        Assert.Equal(0, store.FilterWrites);
    }

    [AvaloniaFact]
    public void Closing_global_search_returns_to_the_task_page_and_blank_search_keeps_scope()
    {
        var snapshot = Snapshot();
        var vm = Create(snapshot, new MemoryStore(snapshot));
        vm.ShowClaudeTaskCommand.Execute(null);
        Assert.Single(vm.VisibleMessages);
        vm.SearchAllNotifications = true;
        Assert.Single(vm.VisibleMessages);
        vm.SearchQuery = "build";
        Assert.Equal(3, vm.VisibleMessages.Count);
        vm.CloseSearch();
        Assert.True(vm.IsClaudeTaskVisible);
        Assert.Single(vm.VisibleMessages);
        Assert.Equal(RemoteNotificationsViewModel.ClaudeTaskLabel, vm.VisibleMessages[0].Label);
    }

    private static RemoteNotificationsSnapshot Snapshot() => new(
        [new("1", "default", "[alpha] build passed", "", "2026-09-05 10:00:00"),
         new("2", "default", "[beta] build passed", "", "2026-09-05 10:01:00"),
         new("3", "default", "[Claude Task] build passed", "", "2026-09-05 10:02:00"),
         new("4", "default", "[CHRS 健康] build passed", "", "2026-09-05 10:03:00", ContentKind: "system_health")],
        ["alpha", "beta", "Claude Task"], "alpha", false);

    private static RemoteNotificationsViewModel Create(RemoteNotificationsSnapshot snapshot, MemoryStore store) => new(
        snapshot, store, new RemoteNotificationNoopPoller(), new RemoteNotificationNoopToastPublisher(), new SettingsStore());

    private sealed class SettingsStore : IRemoteNotificationSettingsStore
    {
        public string SettingsPath => "unused";
        public RemoteNotificationSettings Load() => RemoteNotificationSettings.Default;
        public void Save(RemoteNotificationSettings settings) { }
    }

    private sealed class MemoryStore(RemoteNotificationsSnapshot snapshot) : IRemoteNotificationsStore
    {
        public int FilterWrites { get; private set; }
        public RemoteNotificationsSnapshot Load() => snapshot;
        public void SaveMessages(IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst) { }
        public void SaveFilter(string? label) => FilterWrites++;
        public void SaveKnownLabels(IReadOnlyList<string> labels) { }
        public void SavePersistentWindowsToasts(bool enabled) { }
        public void ClearMessages() { }
    }
}
