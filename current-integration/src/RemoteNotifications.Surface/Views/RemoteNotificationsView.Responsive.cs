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
        var headerActions = this.FindControl<Control>("HeaderActions");
        var connection = this.FindControl<Control>("ConnectionExpander");
        var menuButton = this.FindControl<Control>("OverflowMenuButton");
        var splitView = this.FindControl<SplitView>("ResponsiveSplitView");

        if (headerActions is not null)
        {
            headerActions.IsVisible = !compact;
        }
        if (connection is not null)
        {
            connection.IsVisible = !compact;
        }
        if (menuButton is not null)
        {
            menuButton.IsVisible = compact;
        }
        if (!compact && splitView is not null)
        {
            splitView.IsPaneOpen = false;
        }
    }

    private void OnOverflowMenuClick(object? sender, RoutedEventArgs e)
    {
        if (this.FindControl<SplitView>("ResponsiveSplitView") is { } splitView)
        {
            splitView.IsPaneOpen = !splitView.IsPaneOpen;
        }
    }

    private void OnCloseOverflowClick(object? sender, RoutedEventArgs e)
    {
        CloseResponsivePane();
    }

    private void OnPaneSearchClick(object? sender, RoutedEventArgs e)
    {
        CloseResponsivePane();
        OnSearchClick(sender, e);
    }

    private void OnPaneClearClick(object? sender, RoutedEventArgs e)
    {
        CloseResponsivePane();
        OnClearClick(sender, e);
    }

    private void OnPaneClaudeTaskClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationsViewModel viewModel &&
            viewModel.ShowClaudeTaskCommand.CanExecute(null))
        {
            viewModel.ShowClaudeTaskCommand.Execute(null);
        }
        CloseResponsivePane();
    }

    private void OnPaneSettingsClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationsViewModel viewModel &&
            viewModel.ShowSettingsCommand.CanExecute(null))
        {
            viewModel.ShowSettingsCommand.Execute(null);
        }
        CloseResponsivePane();
    }

    private void OnPaneInboxClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationsViewModel viewModel &&
            viewModel.ShowInboxCommand.CanExecute(null))
        {
            viewModel.ShowInboxCommand.Execute(null);
        }
        CloseResponsivePane();
    }

    private void CloseResponsivePane()
    {
        if (this.FindControl<SplitView>("ResponsiveSplitView") is { } splitView)
        {
            splitView.IsPaneOpen = false;
        }
    }
}
