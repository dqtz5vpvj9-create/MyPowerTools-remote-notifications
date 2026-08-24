using System.Collections.Specialized;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MyPowerTools.Shell.Avalonia.Services;
using MyPowerTools.Shell.Avalonia.ViewModels;

namespace MyPowerTools.Shell.Avalonia.Views;

public sealed partial class RemoteNotificationsView : UserControl
{
    private ScrollViewer? _hostScroller;
    private RemoteNotificationsViewModel? _subscribedViewModel;
    private RemoteNotificationScrollAnchorState? _pendingScrollAnchor;
    private bool _scrollRestoreQueued;
    private int _scrollAnchorLayoutPasses;
    private int _scrollAnchorStableLayoutPasses;
    private double _scrollAnchorLastExtent;
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
        ClearPendingScrollAnchor();
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
        if (e.Action == NotifyCollectionChangedAction.Reset)
        {
            QueueMessageScrollRestore();
            return;
        }

        if (e.Action != NotifyCollectionChangedAction.Add || e.NewStartingIndex != 0)
        {
            return;
        }

        QueueMessageScrollRestore();
    }

    private void QueueMessageScrollRestore()
    {
        var list = this.FindControl<ListBox>("NotificationList");
        var scroller = list?.GetVisualDescendants().OfType<ScrollViewer>().FirstOrDefault();
        if (list is null || scroller is null)
        {
            return;
        }

        if (_pendingScrollAnchor is null)
        {
            var itemAnchor = CaptureReadingAnchor(list, scroller);
            _pendingScrollAnchor = new RemoteNotificationScrollAnchorState(
                scroller,
                scroller.Offset.Y,
                scroller.Extent.Height,
                _hostScroller,
                _hostScroller?.Offset.Y ?? 0,
                _hostScroller?.Extent.Height ?? 0,
                list,
                itemAnchor.Item,
                itemAnchor.Top);
            _scrollAnchorLastExtent = scroller.Extent.Height;
            _scrollAnchorLayoutPasses = 0;
            _scrollAnchorStableLayoutPasses = 0;
            scroller.LayoutUpdated += OnMessageListLayoutUpdated;
        }
        if (_scrollRestoreQueued)
        {
            return;
        }

        _scrollRestoreQueued = true;
        Dispatcher.UIThread.Post(RestoreMessageScrollAnchor, DispatcherPriority.Render);
    }

    private void RestoreMessageScrollAnchor()
    {
        _scrollRestoreQueued = false;
        var anchor = _pendingScrollAnchor;
        if (anchor is null || VisualRoot is null)
        {
            ClearPendingScrollAnchor();
            return;
        }

        ApplyScrollAnchor(anchor);
    }

    private void OnMessageListLayoutUpdated(object? sender, EventArgs e)
    {
        if (_pendingScrollAnchor is not { } anchor)
        {
            return;
        }

        ApplyScrollAnchor(anchor);
        var currentExtent = anchor.Scroller.Extent.Height;
        _scrollAnchorLayoutPasses++;
        if (Math.Abs(currentExtent - _scrollAnchorLastExtent) <= 0.5)
        {
            _scrollAnchorStableLayoutPasses++;
        }
        else
        {
            _scrollAnchorLastExtent = currentExtent;
            _scrollAnchorStableLayoutPasses = 0;
        }

        if (_scrollAnchorStableLayoutPasses >= 2 || _scrollAnchorLayoutPasses >= 8)
        {
            ClearPendingScrollAnchor();
        }
    }

    private void ApplyScrollAnchor(RemoteNotificationScrollAnchorState anchor)
    {
        var scroller = anchor.Scroller;
        var offset = CalculateCurrentListOffset(anchor);
        if (Math.Abs(scroller.Offset.Y - offset) > 0.1)
        {
            scroller.Offset = new Vector(scroller.Offset.X, offset);
        }

        if (anchor.HostScroller is not { } hostScroller)
        {
            return;
        }

        var hostOffset = RemoteNotificationScrollAnchor.CalculateOffset(
            anchor.HostOffset,
            anchor.HostExtent,
            hostScroller.Extent.Height,
            hostScroller.Viewport.Height);
        if (Math.Abs(hostScroller.Offset.Y - hostOffset) > 0.1)
        {
            hostScroller.Offset = new Vector(hostScroller.Offset.X, hostOffset);
        }
    }

    private static (RemoteNotificationMessageViewModel? Item, double Top) CaptureReadingAnchor(
        ListBox list,
        ScrollViewer scroller)
    {
        foreach (var item in list.GetVisualDescendants().OfType<ListBoxItem>())
        {
            if (item.DataContext is not RemoteNotificationMessageViewModel message)
            {
                continue;
            }

            var origin = item.TranslatePoint(default, scroller);
            if (origin is not { } point ||
                point.Y + item.Bounds.Height <= 0 ||
                point.Y >= scroller.Viewport.Height)
            {
                continue;
            }

            return (message, point.Y);
        }

        return (null, 0);
    }

    private static double CalculateCurrentListOffset(RemoteNotificationScrollAnchorState anchor)
    {
        var scroller = anchor.Scroller;
        if (anchor.Offset > RemoteNotificationScrollAnchor.TopThreshold &&
            anchor.Item is not null)
        {
            var currentItem = anchor.List
                .GetVisualDescendants()
                .OfType<ListBoxItem>()
                .FirstOrDefault(item => ReferenceEquals(item.DataContext, anchor.Item));
            var origin = currentItem?.TranslatePoint(default, scroller);
            if (origin is { } point)
            {
                var maximum = Math.Max(0, scroller.Extent.Height - scroller.Viewport.Height);
                return Math.Clamp(scroller.Offset.Y + point.Y - anchor.ItemTop, 0, maximum);
            }
        }

        return RemoteNotificationScrollAnchor.CalculateOffset(
            anchor.Offset,
            anchor.Extent,
            scroller.Extent.Height,
            scroller.Viewport.Height);
    }

    private void ClearPendingScrollAnchor()
    {
        if (_pendingScrollAnchor is { } anchor)
        {
            anchor.Scroller.LayoutUpdated -= OnMessageListLayoutUpdated;
        }

        _pendingScrollAnchor = null;
        _scrollAnchorLayoutPasses = 0;
        _scrollAnchorStableLayoutPasses = 0;
        _scrollAnchorLastExtent = 0;
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
    double Extent,
    ScrollViewer? HostScroller,
    double HostOffset,
    double HostExtent,
    ListBox List,
    RemoteNotificationMessageViewModel? Item,
    double ItemTop);

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
