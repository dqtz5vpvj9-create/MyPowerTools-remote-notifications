using Avalonia;
using Avalonia.Controls;
using Avalonia.Data;
using Avalonia.Interactivity;
using Avalonia.Threading;
using MyPowerTools.UI.Controls;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationsView
{
    private const double CompactToolbarThreshold = 980;
    private MptButton? _markVisibleReadButton;

    protected override void OnInitialized()
    {
        base.OnInitialized();
        AddMarkVisibleReadButton();
        SizeChanged += OnResponsiveSizeChanged;
        AttachedToVisualTree += (_, _) =>
            Dispatcher.UIThread.Post(UpdateResponsiveLayout, DispatcherPriority.Loaded);
    }

    private void AddMarkVisibleReadButton()
    {
        if (_markVisibleReadButton is not null ||
            this.FindControl<StackPanel>("HeaderActions") is not { } headerActions)
        {
            return;
        }

        var button = new MptButton
        {
            Content = "Mark visible as read",
            VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center
        };
        button.Bind(IsVisibleProperty, new Binding(nameof(RemoteNotificationsViewModel.IsInboxVisible)));
        ToolTip.SetTip(button, "Clear unread indicators for the labels in the current results");
        button.Click += OnMarkVisibleReadClick;
        headerActions.Children.Insert(Math.Min(2, headerActions.Children.Count), button);
        _markVisibleReadButton = button;
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

    private void OnMarkVisibleReadClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationsViewModel viewModel)
        {
            viewModel.MarkVisibleMessagesAsRead();
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
            items.Add(ActionItem("Mark visible as read", () => viewModel.MarkVisibleMessagesAsRead()));
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
