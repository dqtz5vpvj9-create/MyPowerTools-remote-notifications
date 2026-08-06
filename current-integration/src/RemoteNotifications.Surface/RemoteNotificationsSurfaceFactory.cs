using Avalonia.Controls;
using MyPowerTools.AvaloniaSdk;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;
using RemoteNotifications.Surface.Views;

namespace RemoteNotifications.Surface;

/// <summary>
/// Dotnet-surface factory for the Remote Notifications tool. Loaded by the Shell's DotnetSurfaceLoader
/// from this assembly via the route's <c>assembly</c>+<c>type</c> manifest fields. Builds the
/// RemoteNotificationsViewModel from the persisted legacy store snapshot, mirroring the Shell
/// controller's load path and delegates synchronization to the independently supervised
/// Remote Notifications Service Unit.
/// </summary>
public sealed class RemoteNotificationsSurfaceFactory : IMptAvaloniaSurfaceFactory
{
    public Control CreateSurface(MptAvaloniaSurfaceContext context)
    {
        var settingsStore = new RemoteNotificationSettingsStore(
            Path.Combine(context.DataDirectory, "settings.json"));
        var store = new RemoteNotificationsLegacyStore(settingsStore, context.DataDirectory);
        var serviceClient = new RemoteNotificationsServiceClient(context.ServiceUnits);
        RemoteNotificationsSnapshot snapshot;
        try
        {
            snapshot = store.Load();
        }
        catch (Exception ex)
        {
            snapshot = new RemoteNotificationsSnapshot([], [], null, false);
            Info(context, ex.Message);
        }

        var viewModel = new RemoteNotificationsViewModel(
            snapshot,
            store: store,
            settingsStore: settingsStore,
            serviceClient: serviceClient);
        Info(context, $"Remote Notifications loaded: {viewModel.MessageCountText}.");

        return new RemoteNotificationsView
        {
            DataContext = viewModel
        };
    }

    private static void Info(MptAvaloniaSurfaceContext context, string message)
    {
        context.Log(new MptSurfaceLogEntry("info", message, DateTimeOffset.Now));
    }
}
