using Avalonia.Controls;
using MyPowerTools.AvaloniaSdk;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;
using RemoteNotifications.Surface.Views;

namespace RemoteNotifications.Surface;

/// <summary>
/// Dotnet-surface factory for the Remote Notifications tool. Loaded by the Shell's DotnetSurfaceLoader
/// from this assembly via the route's <c>assembly</c>+<c>type</c> manifest fields. Builds the
/// RemoteNotificationsViewModel from the persisted legacy store snapshot, mirroring the Shell
/// controller's load path but operating independently through
/// <see cref="MptAvaloniaSurfaceContext"/>.
/// </summary>
public sealed class RemoteNotificationsSurfaceFactory : IMptAvaloniaSurfaceFactory
{
    public Control CreateSurface(MptAvaloniaSurfaceContext context)
    {
        return CreateAsync(context).GetAwaiter().GetResult();
    }

    private static Task<UserControl> CreateAsync(MptAvaloniaSurfaceContext context)
    {
        RemoteNotificationsViewModel viewModel;
        try
        {
            var store = new RemoteNotificationsLegacyStore();
            var snapshot = store.Load();
            // The ViewModel constructor wires its own poller, toast publisher and settings editor
            // from the snapshot; the Shell controller passes the same single-argument overload.
            viewModel = new RemoteNotificationsViewModel(snapshot);
            Info(context, $"Remote Notifications loaded: {viewModel.MessageCountText}.");
        }
        catch (Exception ex)
        {
            viewModel = new RemoteNotificationsViewModel(
                new RemoteNotificationsSnapshot([], [], null, false));
            Info(context, ex.Message);
        }

        return Task.FromResult<UserControl>(new RemoteNotificationsView { DataContext = viewModel });
    }

    private static void Info(MptAvaloniaSurfaceContext context, string message)
    {
        context.Log(new MptSurfaceLogEntry("info", message, DateTimeOffset.Now));
    }
}
