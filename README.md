# Remote Notifications for MyPowerTools

This repository is the active source for the Remote Notifications product and
currently owns the shared AndroidTools suite adapter used by the two paused
Remote Commands and Process Monitor modules.

- `original-source/` preserves the current Python/PyQt implementation.
- `current-integration/` contains the MyPowerTools adapter, package manifests,
  native product UI source, and the `AndroidTools.Runtime`.
- `build.ps1` builds the adapter and publishes a self-contained Windows x64
  `AndroidTools.Runtime` into `artifacts/package/android-tools-suite`.
- `tool-release.json` defines the suite build output consumed by MyPowerTools.

Build from a MyPowerTools checkout:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -File .\build.ps1 `
  -MyPowerToolsRepoRoot C:\path\to\MyPowerTools
```

The adapter depends on the MyPowerTools host SDK projects through the explicit
`MyPowerToolsRepoRoot` MSBuild property. The preserved Python application remains
independently runnable from `original-source/` with its own requirements.

## CHRS notification self-health

The Python hook path has an independent health monitor:

- `original-source/py_modules/notification_queue.py` writes queue/worker
  heartbeats, isolates repeated source-parsing failures, and exposes `health`
  and `requeue-failed` commands.
- `original-source/py_modules/notification_health_monitor.py` checks queue age,
  worker liveness, failed items, hook completion, and disk pressure.
- The monitor sends alerts through direct HTTP and direct FCM, outside the
  event queue and transcript formatter.
- `original-source/systemd/user/androidtools-notification-health.timer` runs
  the monitor every minute on CHRS.

A direct probe can be sent with:

```bash
python3 /android/androidtools/py_modules/notification_health_monitor.py --probe --json
```
