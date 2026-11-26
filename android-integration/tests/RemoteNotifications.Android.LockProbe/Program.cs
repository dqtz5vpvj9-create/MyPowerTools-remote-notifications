using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

// Writes one record through the real RemoteNotificationsLegacyStore from a second process, so the
// test process can prove that the Android history lock excludes another process and that releasing
// it lets that process proceed.
//
// Usage: LockProbe <data-root>
if (args.Length < 1)
{
    Console.Error.WriteLine("usage: LockProbe <data-root>");
    return 2;
}

var dataRoot = args[0];
var store = new RemoteNotificationsLegacyStore(
    new RemoteNotificationSettingsStore(Path.Combine(dataRoot, "settings.json")),
    dataRoot);

Console.WriteLine("STARTED");
Console.Out.Flush();
store.SaveMessages(
[
    new RemoteNotificationRecord(
        "probe",
        "default",
        "[Codex] probe write",
        "codex",
        DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture))
]);
Console.WriteLine("WRITE-OK");
return 0;
