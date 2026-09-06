using RemoteNotifications.Surface.ViewModels;

namespace MyPowerTools.Tests;

public sealed class RemoteNotificationReadSelectionTests
{
    [Fact]
    public void Selects_only_unread_labels_that_are_currently_visible()
    {
        var result = RemoteNotificationReadSelection.FindUnreadLabels(
            ["Session A", "Session A", "Session B"],
            ["Session A", "Session C"]);

        Assert.Equal(["Session A"], result);
    }

    [Fact]
    public void Preserves_case_sensitive_label_identity()
    {
        var result = RemoteNotificationReadSelection.FindUnreadLabels(
            ["session a"],
            ["Session A"]);

        Assert.Empty(result);
    }
}
