using Avalonia.Controls;
using MyPowerTools.AvaloniaSdk;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;
using RemoteNotifications.Surface.Views;

namespace RemoteNotifications.Surface;

/// <summary>Creates the notification feed and its owned detail-window service.</summary>
public sealed class RemoteNotificationsSurfaceFactory : IMptAvaloniaSurfaceFactory
{
    public Control CreateSurface(MptAvaloniaSurfaceContext context)
    {
        var settingsStore = new RemoteNotificationSettingsStore(Path.Combine(context.DataDirectory, "settings.json"));
        var store = new RemoteNotificationsLegacyStore(settingsStore, context.DataDirectory);
        var serviceClient = new RemoteNotificationsServiceClient(context.ServiceUnits);
        RemoteNotificationsSnapshot snapshot;
        try { snapshot = store.Load(); }
        catch (Exception ex)
        {
            snapshot = new RemoteNotificationsSnapshot([], [], null, false);
            Info(context, ex.Message);
        }
        var viewModel = new RemoteNotificationsViewModel(
            snapshot, store: store, settingsStore: settingsStore, serviceClient: serviceClient);
        Info(context, $"Remote Notifications loaded: {viewModel.MessageCountText}.");
        var detailWindows = new RemoteNotificationDetailWindowService(
            store, OperatingSystem.IsMacOS() ? context.WebSurfaces : null);
        return new RemoteNotificationsView(detailWindows) { DataContext = viewModel };
    }
    private static void Info(MptAvaloniaSurfaceContext context, string message) =>
        context.Log(new MptSurfaceLogEntry("info", message, DateTimeOffset.Now));
}
