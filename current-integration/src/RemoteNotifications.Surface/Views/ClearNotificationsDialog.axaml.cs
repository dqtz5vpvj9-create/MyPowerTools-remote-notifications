using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;

namespace RemoteNotifications.Surface.Views;

public sealed partial class ClearNotificationsDialog : Window, IMptSurfaceSheetDialog
{
    public ClearNotificationsDialog()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public ClearNotificationsDialog(int notificationCount)
        : this()
    {
        var prompt = this.FindControl<TextBlock>("PromptText")
            ?? throw new InvalidOperationException("Clear notifications prompt was not found.");
        prompt.Text = $"Remove all {notificationCount} stored notifications? This action cannot be undone.";
    }

    /// <summary>
    /// Set by the owning surface when this dialog is presented inside an in-surface sheet.
    /// Single-view hosts (Android) have no platform window, so the dialog reports its result
    /// through this completion source instead of <see cref="Window.Close(object?)"/>.
    /// </summary>
    public TaskCompletionSource<object?>? SheetCompletion { get; set; }

    private void OnClearClick(object? sender, RoutedEventArgs e)
    {
        Complete(true);
    }

    private void OnCancelClick(object? sender, RoutedEventArgs e)
    {
        Complete(false);
    }

    private void Complete(bool result)
    {
        if (SheetCompletion is { } completion)
        {
            completion.TrySetResult(result);
            return;
        }

        Close(result);
    }
}
