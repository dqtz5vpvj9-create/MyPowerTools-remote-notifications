using System.Collections.Specialized;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using Avalonia.VisualTree;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationsView : UserControl
{
    private ScrollViewer? _hostScroller;
    private RemoteNotificationsViewModel? _subscribedViewModel;
    private RemoteNotificationScrollAnchorState? _pendingScrollAnchor;
    private bool _scrollRestoreQueued;
    private readonly RemoteNotificationDetailWindowService _detailWindows;
    private Point? _labelDragStart;
    private double _labelDragStartOffset;
    private bool _labelDragActive;

    public RemoteNotificationsView()
        : this(RemoteNotificationDetailWindowService.Shared)
    {
    }

    public RemoteNotificationsView(RemoteNotificationDetailWindowService detailWindows)
    {
        _detailWindows = detailWindows;
        AvaloniaXamlLoader.Load(this);
        AttachedToVisualTree += OnAttachedToVisualTree;
        DetachedFromVisualTree += OnDetachedFromVisualTree;
    }

    private void OnAttachedToVisualTree(object? sender, VisualTreeAttachmentEventArgs e)
    {
        _hostScroller = this.FindAncestorOfType<ScrollViewer>();
        if (_hostScroller is not null)
        {
            _hostScroller.SizeChanged += OnHostScrollerSizeChanged;
            MatchHostHeight();
        }

        SubscribeToMessageChanges();
    }

    private void OnDetachedFromVisualTree(object? sender, VisualTreeAttachmentEventArgs e)
    {
        UnsubscribeFromMessageChanges();
        _pendingScrollAnchor = null;
        _scrollRestoreQueued = false;
        if (_hostScroller is not null)
        {
            _hostScroller.SizeChanged -= OnHostScrollerSizeChanged;
            _hostScroller = null;
        }
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
        if (anchor is null || VisualRoot is null)
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

    private void OnLabelScrollerPointerPressed(object? sender, PointerPressedEventArgs e)
    {
        if (sender is not ScrollViewer scroller ||
            !e.GetCurrentPoint(scroller).Properties.IsLeftButtonPressed)
        {
            return;
        }

        _labelDragStart = e.GetPosition(scroller);
        _labelDragStartOffset = scroller.Offset.X;
        _labelDragActive = false;
    }

    private void OnLabelScrollerPointerMoved(object? sender, PointerEventArgs e)
    {
        if (sender is not ScrollViewer scroller ||
            _labelDragStart is not { } dragStart ||
            !e.GetCurrentPoint(scroller).Properties.IsLeftButtonPressed)
        {
            return;
        }

        var delta = e.GetPosition(scroller).X - dragStart.X;
        if (!_labelDragActive && Math.Abs(delta) < 4)
        {
            return;
        }

        if (!_labelDragActive)
        {
            _labelDragActive = true;
            e.Pointer.Capture(scroller);
        }

        var horizontalOffset = RemoteNotificationLabelDrag.CalculateOffset(
            _labelDragStartOffset,
            delta,
            scroller.Extent.Width,
            scroller.Viewport.Width);
        scroller.Offset = new Vector(horizontalOffset, scroller.Offset.Y);
        e.Handled = true;
    }

    private void OnLabelScrollerPointerReleased(object? sender, PointerReleasedEventArgs e)
    {
        if (_labelDragActive)
        {
            e.Handled = true;
            e.Pointer.Capture(null);
        }

        ResetLabelDrag();
    }

    private void OnLabelScrollerPointerCaptureLost(object? sender, PointerCaptureLostEventArgs e)
    {
        ResetLabelDrag();
    }

    private void ResetLabelDrag()
    {
        _labelDragStart = null;
        _labelDragStartOffset = 0;
        _labelDragActive = false;
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
            DataContext is not RemoteNotificationsViewModel viewModel)
        {
            return;
        }

        e.Handled = true;
        ShellCommandFaultBoundary.Run(
            this,
            "Open remote notification details",
            () =>
            {
                viewModel.AcknowledgeMessage(message);
                _detailWindows.Open(message);
                return Task.CompletedTask;
            });
    }

    public bool TryOpenMessageById(string messageId)
    {
        if (DataContext is not RemoteNotificationsViewModel viewModel ||
            viewModel.FindMessageById(messageId) is not { } message)
        {
            return false;
        }

        viewModel.AcknowledgeMessage(message);
        return _detailWindows.Open(message);
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

public static class RemoteNotificationLabelDrag
{
    public static double CalculateOffset(
        double startOffset,
        double pointerDelta,
        double extent,
        double viewport)
    {
        var maximum = Math.Max(0, extent - viewport);
        return Math.Clamp(startOffset - pointerDelta, 0, maximum);
    }
}
