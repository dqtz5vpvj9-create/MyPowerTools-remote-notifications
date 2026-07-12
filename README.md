# Remote Notifications for MyPowerTools

This repository is the active source for the Remote Notifications product and
currently owns the shared AndroidTools suite adapter used by the two paused
Remote Commands and Process Monitor modules.

- `original-source/` preserves the current Python/PyQt implementation.
- `current-integration/` contains the MyPowerTools adapter, package manifests,
  native product UI source, and the `powertoold` runtime.
- `build.ps1` builds the adapter and runtime into
  `artifacts/package/android-tools-suite`.
- `tool-release.json` defines the suite build output consumed by MyPowerTools.

Build from a MyPowerTools checkout:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -File .\build.ps1 `
  -MyPowerToolsRepoRoot C:\path\to\MyPowerTools
```

The adapter depends on the MyPowerTools host SDK projects through the explicit
`MyPowerToolsRepoRoot` MSBuild property. The preserved Python application remains
independently runnable from `original-source/` with its own requirements.
