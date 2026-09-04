using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Threading;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationsView
{
    private const double CompactToolbarThreshold = 980;

    protected override void OnInitialized()
    {
        base.OnInitialized();
        SizeChanged += OnResponsiveSizeChanged;
        AttachedToVisualTree += (_, _) =>
            Dispatcher.UIThread.Post(UpdateResponsiveLayout, DispatcherPriority.Loaded);
    }

    private void OnResponsiveSizeChanged(object? sender, SizeChangedEventArgs e)
    {
        UpdateResponsiveLayout();
    }

    private void UpdateResponsiveLayout()
    {
        var compact = Bounds.Width > 0 && Bounds.Width < CompactToolbarThreshold;
        if (this.FindControl<Control>("HeaderActions") is { } headerActions)
        {
            headerActions.IsVisible = !compact;
        }
        if (this.FindControl<Control>("ConnectionExpander") is { } connection)
        {
            connection.IsVisible = !compact;
        }
        if (this.FindControl<Control>("OverflowMenuButton") is { } menuButton)
        {
            menuButton.IsVisible = compact;
        }
    }

    private void OnOverflowMenuClick(object? sender, RoutedEventArgs e)
    {
        if (sender is not Control anchor || DataContext is not RemoteNotificationsViewModel viewModel)
        {
            return;
        }

        var items = new List<MenuItem>();
        if (viewModel.IsInboxVisible)
        {
            items.Add(ActionItem("Search", () => OnSearchClick(anchor, e)));
            items.Add(ActionItem("Clear all", () => OnClearClick(anchor, e)));
            items.Add(ActionItem("Claude Task", () => Execute(viewModel.ShowClaudeTaskCommand)));
            items.Add(ActionItem("Settings", () => Execute(viewModel.ShowSettingsCommand)));
        }
        else
        {
            items.Add(ActionItem("Back to inbox", () => Execute(viewModel.ShowInboxCommand)));
        }

        var menu = new ContextMenu { ItemsSource = items };
        menu.Open(anchor);
    }

    private static MenuItem ActionItem(string header, Action action)
    {
        var item = new MenuItem { Header = header };
        item.Click += (_, _) => action();
        return item;
    }

    private static void Execute(System.Windows.Input.ICommand command)
    {
        if (command.CanExecute(null))
        {
            command.Execute(null);
        }
    }
}
