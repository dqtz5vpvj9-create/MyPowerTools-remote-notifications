using System.Collections.Specialized;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MyPowerTools.Shell.Avalonia.ViewModels;

namespace MyPowerTools.Shell.Avalonia.Views;

public sealed partial class RemoteNotificationsView : UserControl
{
    private readonly DispatcherTimer _pollTimer;
    private CancellationTokenSource? _pollCancellation;
    private ScrollViewer? _hostScroller;
    private RemoteNotificationsViewModel? _subscribedViewModel;
    private RemoteNotificationScrollAnchorState? _pendingScrollAnchor;
    private bool _scrollRestoreQueued;

    public RemoteNotificationsView()
    {
        AvaloniaXamlLoader.Load(this);
        _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        AttachedToVisualTree += OnAttachedToVisualTree;
        DetachedFromVisualTree += OnDetachedFromVisualTree;
    }

    private void OnAttachedToVisualTree(object? sender, VisualTreeAttachmentEventArgs e)
    {
        _pollTimer.Tick -= OnPollTimerTick;
        _pollTimer.Tick += OnPollTimerTick;
        _hostScroller = this.FindAncestorOfType<ScrollViewer>();
        if (_hostScroller is not null)
        {
            _hostScroller.SizeChanged += OnHostScrollerSizeChanged;
            MatchHostHeight();
        }

        _pollCancellation = new CancellationTokenSource();
        SubscribeToMessageChanges();
        _pollTimer.Start();
        ShellCommandFaultBoundary.Run(this, "Initial remote notification poll", PollOnceAsync);
    }

    private void OnDetachedFromVisualTree(object? sender, VisualTreeAttachmentEventArgs e)
    {
        _pollTimer.Stop();
        _pollTimer.Tick -= OnPollTimerTick;
        _pollCancellation?.Cancel();
        _pollCancellation?.Dispose();
        _pollCancellation = null;
        UnsubscribeFromMessageChanges();
        _pendingScrollAnchor = null;
        _scrollRestoreQueued = false;
        if (_hostScroller is not null)
        {
            _hostScroller.SizeChanged -= OnHostScrollerSizeChanged;
            _hostScroller = null;
        }
    }

    private void OnPollTimerTick(object? sender, EventArgs args)
    {
        ShellCommandFaultBoundary.Run(this, "Poll remote notifications", PollOnceAsync);
    }

    private void SubscribeToMessageChanges()
    {
        UnsubscribeFromMessageChanges();
        if (DataContext is not RemoteNotificationsViewModel viewModel)
        {
            return;
        }

        _subscribedViewModel = viewModel;
        viewModel.Messages.CollectionChanged += OnMessagesCollectionChanged;
    }

    private void UnsubscribeFromMessageChanges()
    {
        if (_subscribedViewModel is not null)
        {
            _subscribedViewModel.Messages.CollectionChanged -= OnMessagesCollectionChanged;
            _subscribedViewModel = null;
        }
    }

    private void OnMessagesCollectionChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action != NotifyCollectionChangedAction.Add || e.NewStartingIndex != 0)
        {
            return;
        }

        var list = this.FindControl<ListBox>("NotificationList");
        var scroller = list?.GetVisualDescendants().OfType<ScrollViewer>().FirstOrDefault();
        if (scroller is null)
        {
            return;
        }

        _pendingScrollAnchor ??= new RemoteNotificationScrollAnchorState(
            scroller,
            scroller.Offset.Y,
            scroller.Extent.Height);
        if (_scrollRestoreQueued)
        {
            return;
        }

        _scrollRestoreQueued = true;
        Dispatcher.UIThread.Post(RestoreMessageScrollAnchor, DispatcherPriority.Loaded);
    }

    private void RestoreMessageScrollAnchor()
    {
        _scrollRestoreQueued = false;
        var anchor = _pendingScrollAnchor;
        _pendingScrollAnchor = null;
        if (anchor is null || _pollCancellation?.IsCancellationRequested != false)
        {
            return;
        }

        var scroller = anchor.Scroller;
        var offset = RemoteNotificationScrollAnchor.CalculateOffset(
            anchor.Offset,
            anchor.Extent,
            scroller.Extent.Height,
            scroller.Viewport.Height);
        scroller.Offset = new Vector(scroller.Offset.X, offset);
    }

    private void OnHostScrollerSizeChanged(object? sender, SizeChangedEventArgs e)
    {
        MatchHostHeight();
    }

    private void MatchHostHeight()
    {
        if (_hostScroller is null || _hostScroller.Bounds.Height <= 0)
        {
            return;
        }

        Height = Math.Max(520, _hostScroller.Bounds.Height);
    }

    private async Task PollOnceAsync()
    {
        if (DataContext is not RemoteNotificationsViewModel viewModel || _pollCancellation is null)
        {
            return;
        }

        try
        {
            await viewModel.PollAsync(_pollCancellation.Token);
        }
        catch (OperationCanceledException) when (_pollCancellation.IsCancellationRequested)
        {
            // The page was closed while a pull was in flight.
        }
    }

    private void OnClearClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not RemoteNotificationsViewModel viewModel)
        {
            return;
        }

        ShellCommandFaultBoundary.Run(
            this,
            "Clear remote notifications",
            () => ClearMessagesAsync(viewModel));
    }

    private async Task ClearMessagesAsync(RemoteNotificationsViewModel viewModel)
    {
        if (viewModel.TotalCount > 0 && !await ConfirmClearAsync(viewModel.TotalCount))
        {
            return;
        }

        viewModel.ClearMessages();
    }

    private async Task<bool> ConfirmClearAsync(int count)
    {
        if (TopLevel.GetTopLevel(this) is not Window owner)
        {
            return false;
        }

        var dialog = new ClearNotificationsDialog(count);
        return await dialog.ShowDialog<bool>(owner);
    }

    private void OnMessageDoubleTapped(object? sender, TappedEventArgs e)
    {
        if (sender is not Control { DataContext: RemoteNotificationMessageViewModel message } ||
            DataContext is not RemoteNotificationsViewModel viewModel ||
            TopLevel.GetTopLevel(this) is not Window owner)
        {
            return;
        }

        e.Handled = true;
        ShellCommandFaultBoundary.Run(
            this,
            "Open remote notification details",
            async () =>
            {
        viewModel.AcknowledgeMessage(message);
        var detail = new RemoteNotificationDetailWindow(message);
        await detail.ShowDialog(owner);
            });
    }

    public bool TryOpenMessageById(string messageId, Window? ownerOverride = null)
    {
        var owner = TopLevel.GetTopLevel(this) as Window ?? ownerOverride;
        if (DataContext is not RemoteNotificationsViewModel viewModel ||
            owner is null ||
            viewModel.FindMessageById(messageId) is not { } message)
        {
            return false;
        }

        viewModel.AcknowledgeMessage(message);
        var detail = new RemoteNotificationDetailWindow(message);
        detail.Show(owner);
        detail.Activate();
        return true;
    }

    private void OnMessageContextRequested(object? sender, ContextRequestedEventArgs e)
    {
        if (sender is not Control { DataContext: RemoteNotificationMessageViewModel message } control)
        {
            return;
        }

        var copy = new MenuItem { Header = "Copy message" };
        copy.Click += (_, _) => ShellCommandFaultBoundary.Run(
            this,
            "Copy remote notification",
            () => CopyMessageAsync(message.Message));
        var menu = new ContextMenu
        {
            ItemsSource = new[] { copy }
        };
        menu.Open(control);
        e.Handled = true;
    }

    private async Task CopyMessageAsync(string message)
    {
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard;
        if (clipboard is not null)
        {
            var transfer = new DataTransfer();
            transfer.Add(DataTransferItem.CreateText(message));
            await clipboard.SetDataAsync(transfer);
            await clipboard.FlushAsync();
        }
    }
}

public sealed record RemoteNotificationScrollAnchorState(
    ScrollViewer Scroller,
    double Offset,
    double Extent);

public static class RemoteNotificationScrollAnchor
{
    public const double TopThreshold = 40;

    public static double CalculateOffset(
        double previousOffset,
        double previousExtent,
        double currentExtent,
        double viewport)
    {
        if (previousOffset <= TopThreshold)
        {
            return 0;
        }

        var insertedHeight = Math.Max(0, currentExtent - previousExtent);
        var maximum = Math.Max(0, currentExtent - viewport);
        return Math.Clamp(previousOffset + insertedHeight, 0, maximum);
    }
}
