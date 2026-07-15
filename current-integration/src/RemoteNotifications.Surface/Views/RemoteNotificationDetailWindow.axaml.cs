using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Platform;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationDetailWindow : Window
{
    public RemoteNotificationDetailWindow()
    {
        AvaloniaXamlLoader.Load(this);
        Icon = new WindowIcon(AssetLoader.Open(new Uri("avares://MyPowerTools.Shell.Avalonia/Assets/MyPowerTools.ico")));
    }

    public RemoteNotificationDetailWindow(RemoteNotificationMessageViewModel message)
        : this()
    {
        DataContext = message;
        Title = message.DetailWindowTitle;
    }

    private void OnCopyClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not RemoteNotificationMessageViewModel message || Clipboard is null)
        {
            return;
        }

        ShellCommandFaultBoundary.Run(
            this,
            "Copy remote notification details",
            async () =>
            {
                var transfer = new DataTransfer();
                transfer.Add(DataTransferItem.CreateText(message.Message));
                await Clipboard.SetDataAsync(transfer);
                await Clipboard.FlushAsync();
            });
    }

    private void OnCloseClick(object? sender, RoutedEventArgs e)
    {
        Close();
    }
}
