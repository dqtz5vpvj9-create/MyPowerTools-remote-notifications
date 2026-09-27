# Parent integration checklist (Remote Notifications on Android)

This file lists exactly what belongs to the shared host/APK build (owned by other agents) and is
**not** changed by this workstream. Everything below was verified by reading the host sources; no
host file was modified.

## 1. Bundle the module package into the APK

`android-integration/build.ps1` stages a complete, self-describing package at
`tools/remote-notifications/android-integration/artifacts/package/remote-notifications-android/`.
Its parameters (`-MyPowerToolsRepoRoot`, `-Configuration`) already match what
`scripts/build-android.ps1` passes to every entry in `$moduleStages`, so the wiring is two small
patches.

**1a. `scripts/build-android.ps1` — append to `$moduleStages`:**

```powershell
    [pscustomobject]@{
        ToolId      = 'remote-notifications-android'
        BuildScript = 'tools/remote-notifications/android-integration/build.ps1'
        Stage       = 'tools/remote-notifications/android-integration/artifacts/package/remote-notifications-android'
        Manifest    = 'module.json'
        Optional    = $false
    }
```

**1b. `src/MyPowerTools.Android/MyPowerTools.Android.csproj` — add next to the file-transfer group:**

```xml
<!-- Remote Notifications (Android): module adapter, descriptors and the phone surface. -->
<AndroidAsset Include="../../tools/remote-notifications/android-integration/artifacts/package/remote-notifications-android/**/*"
              Exclude="../../tools/remote-notifications/android-integration/artifacts/package/remote-notifications-android/**/*.pdb;../../tools/remote-notifications/android-integration/artifacts/package/remote-notifications-android/**/*.deps.json">
  <Link>modules/remote-notifications-android/%(RecursiveDir)%(Filename)%(Extension)</Link>
</AndroidAsset>
```

Do **not** use an `MyPowerTools.*` wildcard in `Exclude`: the phone surface itself is
`ui/surface/MyPowerTools.MobileNotifications.dll`, and a `**/MyPowerTools.*` pattern silently drops
it (the tool then fails to load its route). The shared host contracts that must not be bundled
(`MyPowerTools.Abstractions.dll`, `MyPowerTools.Platform.Abstractions.dll`,
`MyPowerTools.AvaloniaSdk.dll`) are already removed from the package root by `build.ps1`, and
`build.ps1` verifies them plus the surface DLL against `manifest/android-package-manifest.json`
afterwards. If you still want a belt-and-braces exclusion, name those three files exactly, never with
a wildcard:

```xml
              Exclude=".../**/*.pdb;.../**/*.deps.json;.../MyPowerTools.Abstractions.dll;.../MyPowerTools.Platform.Abstractions.dll;.../MyPowerTools.AvaloniaSdk.dll"
```

- Run `build.ps1` **before** the Android app build (it also builds `src/MyPowerTools.MobileNotifications`).
- `build.ps1` removes the linked host contracts (`MyPowerTools.Abstractions.dll`,
  `MyPowerTools.Platform.Abstractions.dll`, `MyPowerTools.AvaloniaSdk.dll`) and the module's `*.pdb`
  **at the package root only** — `ui/surface/**` is never touched, so
  `ui/surface/MyPowerTools.MobileNotifications.dll` always survives. The manifest verification that
  fails the build when a required file is missing includes that surface DLL, so a botched cleanup
  breaks the build instead of the APK.
- The package currently contains `RemoteNotifications.Android.deps.json`; it is harmless and is the
  only other file the sample `Exclude` above drops.
- Unlike file-transfer, this package's JSON descriptors are **not** mirrored into `modules/`, so they
  must come from the package root (do not copy the file-transfer Exclude list verbatim: it drops
  `module.json`, `commands.index.json` and `ui/*.json` on the assumption that the `modules/**`
  whitelist glob already supplies them).
- Alternative layout (what file-transfer does): copy the package to `modules/remote-notifications-android`
  and reuse the file-transfer exclusion pattern. Either layout works; do not do both, or the same
  tool manifest enters the APK twice.

**1c. `MyPowerTools.Android.slnx`** (the file already documents this slot): add

```xml
<Project Path="src/MyPowerTools.MobileNotifications/MyPowerTools.MobileNotifications.csproj" />
<Project Path="tools/remote-notifications/android-integration/src/RemoteNotifications.Android/RemoteNotifications.Android.csproj" />
```

and, if the Android solution should run this module's suite, the test project as well:

```xml
<Project Path="tools/remote-notifications/android-integration/tests/RemoteNotifications.Android.Tests/RemoteNotifications.Android.Tests.csproj" />
```

## 2. No host code change is required

- `AndroidPlatformPack` already registers `secret.store`, `background.activity`,
  `notification.desktop` (provider `AndroidNotificationService`) and `files.downloads`.
- `AndroidHost` already maps `notification.desktop → platform.Notifications` and
  `background.activity → platform.Background` into the module capability providers.
- `MyPowerTools.Android/Properties/AndroidManifest.xml` already declares `POST_NOTIFICATIONS`,
  `FOREGROUND_SERVICE` and the data-sync/connected-device service types used by the lease.
- `AndroidBackgroundActivityService` already cancels a module (`SetModuleEnabledAsync(false)`) when
  the user taps "stop" on the foreground notification, which reaches this module through
  `DisableAsync`.
- `MyPowerTools.Shell.Avalonia` needs no change: the tool is a standard dynamic dotnet surface and
  the module is a standard in-proc module.

## 3. Tool/module id and the desktop suite

- New ids, deliberately distinct from the desktop suite to avoid the runtime's duplicate-tool-id
  hard failure (`ToolRegistry` throws `InvalidDataException` for a duplicate tool id):
  `packageId = moduleId = toolId = remote-notifications-android`.
- The desktop AndroidTools suite declares tool id `remote-notifications` whose module requires
  `service.user`; on Android it can never become usable. If it stays in the APK it renders as an
  unsupported card next to the working tool. Optional cleanup (owned by the parent):
  exclude `modules/android-tools-suite/modules/notifications/**` from the Android asset glob.
- If the parent later prefers a single canonical tool id (`remote-notifications`), rename it in
  `package/module.json`, `package/ui/tool.json`, `package/commands.index.json`,
  `RemoteNotificationsAndroidOptions` and the phone surface command literals; the manifest tests
  fail until all of them agree.

## 4. Signing key provisioning (product decision, no code needed)

The phone cannot read `~/.ssh/id_ed25519`, and the module refuses to write private key material to a
file. The key is imported once from the tool page (`导入密钥`, command
`remote-notifications-android.signing-key.import`) and stored via the platform `secret.store`
(Android Keystore). The user must paste the same OpenSSH Ed25519 key the desktop client uses,
otherwise the server will not recognize the `sig` for that channel.

## 5. Artifacts governance

- `src/MyPowerTools.MobileNotifications` builds into the already-declared `artifacts/build` class.
- The staged package lives inside the tool submodule (`tools/remote-notifications/android-integration/artifacts/`),
  matching `tools/file-transfer/artifacts/`, so no new `scripts/artifacts-policy.json` entry is
  required. If the parent starts writing a new path under the top-level `artifacts/`, that entry
  must be declared there.

## 6. Not done here (needs a human/device or another owner)

- No device or emulator run: the module and surface were compiled and unit/parity tested on the host
  only. On-device checks still owed: notification permission prompt, foreground-service lease and
  its stop action, KeyStore import, HTTPS/Tailscale reachability, background continuation.
- No changes to `external/NotifyApp`, `tools/remote-notifications/current-integration/**`,
  `RemoteNotifications.Surface`, `MyPowerTools.Android`, `MyPowerTools.Platform.Android` or any
  script. Message semantics, quoting/`For reference` blocks and banner rules are unchanged, so the
  desktop/NotifyApp dual-release rule is not triggered by this workstream.
- `MyPowerTools.slnx` and `MyPowerTools.Android.slnx` were not touched; add
  `src/MyPowerTools.MobileNotifications/MyPowerTools.MobileNotifications.csproj` to whichever
  solution should build it by default.
- The desktop `RemoteNotificationsSurfaceFactory` still hardwires `RemoteNotificationsServiceClient`
  and a file-path signing key. A future refactor (owned by the Surface owner) could inject a poller
  and let Android reuse the desktop Views verbatim; today the phone surface is separate but shares
  the store/format/item sources.
