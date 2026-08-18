param()

# Cursor stop hook -> Remote Notifications queue (Windows deployment wrapper).
# Cursor Remote / Claude Code compatibility fires a Cursor stop payload without
# last_assistant_message. Enqueue stays short; the worker is started detached.

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logRoot = Join-Path $env:TEMP 'codex-hooks'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logPath = Join-Path $logRoot 'cursor_hook.log'
$rawPath = Join-Path $logRoot 'cursor_hook_raw_stdin.json'

$raw = [Console]::In.ReadToEnd()
Set-Content -LiteralPath $rawPath -Value $raw -NoNewline -Encoding UTF8

$python = $env:ANDROIDTOOLS_NOTIFY_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) {
    $python = 'C:\Users\lixinrui\miniconda3\envs\android_automatic_314\python.exe'
}
$queue = $env:ANDROIDTOOLS_NOTIFY_QUEUE
if ([string]::IsNullOrWhiteSpace($queue)) {
    $queue = Join-Path $scriptDir 'notification_queue.py'
}

$argList = @(
    'enqueue'
    '--stdin'
    '--hook'
    'stop'
    '--client'
    'cursor'
    '--icon'
    'cursor'
    '--no-start-worker'
)

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $python
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$psi.WorkingDirectory = $scriptDir
$psi.ArgumentList.Add($queue)
foreach ($arg in $argList) {
    $psi.ArgumentList.Add($arg)
}

try {
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Write($raw)
    $proc.StandardInput.Close()
    if (-not $proc.WaitForExit(15000)) {
        try { $proc.Kill($true) } catch {}
        Set-Content -LiteralPath $logPath -Value 'enqueue timed out' -Encoding UTF8
        exit 0
    }
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    Set-Content -LiteralPath $logPath -Value "exit=$($proc.ExitCode)`nstdout=$stdout`nstderr=$stderr`nraw_len=$($raw.Length)" -Encoding UTF8
} catch {
    Set-Content -LiteralPath $logPath -Value $_.Exception.ToString() -Encoding UTF8
    exit 0
}

if ($proc.ExitCode -eq 0) {
    $workerPsi = [System.Diagnostics.ProcessStartInfo]::new()
    $workerPsi.FileName = $python
    $workerPsi.ArgumentList.Add($queue)
    $workerPsi.ArgumentList.Add('worker')
    $workerPsi.UseShellExecute = $true
    $workerPsi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    [void][System.Diagnostics.Process]::Start($workerPsi)
}

exit 0
