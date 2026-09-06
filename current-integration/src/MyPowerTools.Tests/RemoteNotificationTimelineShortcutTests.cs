using Avalonia.Input;
using RemoteNotifications.Surface.Views;

namespace MyPowerTools.Tests;

public sealed class RemoteNotificationTimelineShortcutTests
{
    [Theory]
    [InlineData(KeyModifiers.Control | KeyModifiers.Shift)]
    [InlineData(KeyModifiers.Meta | KeyModifiers.Shift)]
    public void Copy_gesture_supports_windows_linux_and_macos(KeyModifiers modifiers)
    {
        Assert.True(RemoteNotificationTimelineShortcut.IsCopyGesture(Key.C, modifiers));
    }

    [Fact]
    public void Plain_copy_remains_available_to_focused_controls()
    {
        Assert.False(RemoteNotificationTimelineShortcut.IsCopyGesture(Key.C, KeyModifiers.Control));
    }
}
