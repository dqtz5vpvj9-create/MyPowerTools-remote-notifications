namespace RemoteNotifications.Surface;

internal static class ShellCommandFaultBoundary
{
    public static void Run(object? source, string operationName, Action action)
    {
        try { action(); }
        catch (Exception ex) { System.Diagnostics.Debug.WriteLine($"[{operationName}] {ex.Message}"); }
    }

    public static void Run(object? source, string operationName, Func<Task> action)
    {
        try { action().GetAwaiter().GetResult(); }
        catch (Exception ex) { System.Diagnostics.Debug.WriteLine($"[{operationName}] {ex.Message}"); }
    }
}
