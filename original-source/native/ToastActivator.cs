using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Reflection;

internal static class ToastActivator
{
    private const string PipeName = "AndroidToolsToastActivation";
    private const string ActivationPrefix = "--androidtools-toast-activation=";

    private static int Main(string[] args)
    {
        string payload = ActivationPayloadFromArgs(args);
        if (payload.Length == 0)
        {
            return 0;
        }

        if (SendToExistingInstance(payload))
        {
            return 0;
        }

        return StartMainApp(payload) ? 0 : 1;
    }

    private static string ActivationPayloadFromArgs(string[] args)
    {
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i] ?? "";
            if (arg == "--androidtools-toast-activation" && i + 1 < args.Length)
            {
                return StripQuotes(args[i + 1] ?? "");
            }
            if (arg.StartsWith(ActivationPrefix, StringComparison.Ordinal))
            {
                return StripQuotes(arg.Substring(ActivationPrefix.Length));
            }
            if (arg.IndexOf("androidtools://notification", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return StripQuotes(arg);
            }
        }
        return "";
    }

    private static string StripQuotes(string value)
    {
        value = (value ?? "").Trim();
        if (value.Length >= 2 && value[0] == '"' && value[value.Length - 1] == '"')
        {
            return value.Substring(1, value.Length - 2);
        }
        return value;
    }

    private static bool SendToExistingInstance(string payload)
    {
        try
        {
            using (var pipe = new NamedPipeClientStream(
                ".",
                PipeName,
                PipeDirection.Out,
                PipeOptions.None))
            {
                pipe.Connect(120);
                using (var writer = new StreamWriter(pipe))
                {
                    writer.Write(payload);
                    writer.Flush();
                }
            }
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static bool StartMainApp(string payload)
    {
        try
        {
            string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? "";
            string exe = Path.Combine(dir, "AndroidTools.exe");
            if (!File.Exists(exe))
            {
                return false;
            }
            ProcessStartInfo psi = new ProcessStartInfo
            {
                FileName = exe,
                Arguments = "--androidtools-toast-activation=\"" + payload.Replace("\"", "\\\"") + "\"",
                UseShellExecute = false,
                WorkingDirectory = dir,
                CreateNoWindow = true,
            };
            Process.Start(psi);
            return true;
        }
        catch
        {
            return false;
        }
    }
}
