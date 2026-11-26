# Remote Notifications on MyPowerTools Android

This directory makes the existing Remote Notifications product usable on the MyPowerTools Android
build. It is **not** a separate file-transfer-style tool: the notification inbox, the signed pull
protocol, the reference-block message format and the settings model are the same product sources the
desktop client ships.

```
android-integration/
  package/                     static module/tool/command descriptors copied into the APK asset package
  src/RemoteNotifications.Android/
                               in-process MPT module: signed pull, inbox merge, tray delivery,
                               opt-in background polling, settings/commands/events
  tests/RemoteNotifications.Android.Tests/
                               protocol / banner / merge parity and module behaviour tests
  manifest/android-package-manifest.json
                               DLL package output manifest verified by build.ps1
  build.ps1                    stages artifacts/package/remote-notifications-android
```

## What runs where

| Concern | Desktop (unchanged) | Android (this directory) |
| --- | --- | --- |
| Signed `GET /pull` | `remote-notifications.service` Service Unit | `RemoteNotificationSignedPoller` inside the module |
| Signing key | `~/.ssh/id_ed25519` path in settings | platform `secret.store` (Android Keystore), never a file |
| Inbox merge / waterline / seen ids | `RemoteNotificationBackgroundReceiver` | `RemoteNotificationInboxSynchronizer` (parity-tested) |
| Message format / reference blocks / labels | `RemoteNotificationsLegacyStore` | same shipped source, compiled in |
| Tray banner text | `RemoteNotificationWindowsToastPublisher` | `RemoteNotificationAndroidBanner` (parity-tested) |
| Delivery | Windows toast / UserNotifications | `notification.desktop` → `AndroidNotificationService` |
| Background lifetime | Service Unit process | `background.activity` foreground-service lease |
| UI | `RemoteNotifications.Surface` (desktop layout, detail window) | `src/MyPowerTools.MobileNotifications` (phone layout, inline detail) |

No desktop Python, no `.service` sidecar and no service-unit IPC is used on Android.

## Reuse contract

The module and the phone surface compile these shipped sources directly (they are never edited from
here):

- `current-integration/src/RemoteNotifications.Surface/Services/RemoteNotificationsLegacyStore.cs`
- `current-integration/src/RemoteNotifications.Surface/Services/RemoteNotificationSettingsStore.cs`
- `current-integration/src/RemoteNotifications.Surface/Services/RemoteNotificationSeenIdRing.cs`
- `current-integration/src/RemoteNotifications.Surface/Services/RemoteNotificationSessionChain.cs`
- `current-integration/src/RemoteNotifications.Surface/ViewModels/RemoteNotificationItemViewModels.cs` (phone surface)

The protocol, the banner text and the inbox merge are re-implemented only where the platform
boundary forces it (in-memory signing, Android tray), and `RemoteNotifications.Android.Tests`
compiles the desktop implementations side by side and asserts equality, so the two clients cannot
drift apart silently.

## Android history lock (no named mutex)

The shipped `RemoteNotificationsLegacyStore` serializes `history.json` with a *named*
`System.Threading.Mutex`. On Android the CoreCLR PAL implements a named mutex by creating
`/data/local/tmp/.dotnet-*` via `mkdtemp`, which the app sandbox denies with `EACCES`; every history
lock then threw and the module host quarantined the module after three faults. Setting `TMPDIR` in
`Application.OnCreate` is too late - the PAL path is already resolved, so a clean install or cleared
app data does not help.

Fix: the shipped store source is compiled **byte-for-byte unchanged**. `RemoteNotificationAndroidFileLock`
implements the exact call shape the store uses (`new Mutex(false, name)`, `WaitOne(TimeSpan)`,
`ReleaseMutex()`, `Dispose()`), and each project that compiles the store binds the simple name
`Mutex` to it:

```xml
<Using Include="RemoteNotifications.Android.RemoteNotificationAndroidFileLock" Alias="Mutex" />
```

Applied in `src/RemoteNotifications.Android`, `src/MyPowerTools.MobileNotifications` and the test
project (the lock probe uses the same alias). The lock primitive therefore changes; nothing else
does - protocol, format, reference blocks and merge rules are still the shipped code.

Only the call contract the store uses is implemented - construct unowned, `WaitOne(TimeSpan)` once,
  `ReleaseMutex()`, `Dispose()`:

- **cross-instance and cross-process**: the lock file is `<state root>/notification-locks/<token>.lock`,
  opened with `FileShare.None`, which is an OS exclusive lock on Android (flock-backed);
- **name**: the store's own lock name already ends in the deterministic token derived from the state
  path; the file name is that token (`Path.GetFileName` after slash normalization), so the same state
  path always maps to the same lock file - no new hashing, no sanitizing or truncation fallbacks;
- **app-private and temp-free**: the state root is `MPT_DATA_ROOT/state` when the host sets it,
  otherwise the app-private `MyPowerTools/state`; there is deliberately no temp-directory fallback;
- **bounded, monotonic wait**: `WaitOne` measures the timeout with `Stopwatch` and retries only a real
  sharing violation (`ERROR_SHARING_VIOLATION` on Windows, `EAGAIN` errno 11 on Android/Linux, 35 on
  macOS); any other IO or permission error surfaces instead of being retried as contention. Returning
  `false` after the timeout preserves the store's
  `TimeoutException("Remote notification history is busy.")` contract;
- **crash safety**: the OS drops the lock when the process dies, so a killed app cannot leave the
  history permanently locked.

## Background polling rules

- The module never acquires background work at app start, module start or settings apply.
- `polling.start` is the only entry point: it acquires a `background.activity` lease and then polls.
- `polling.stop`, module disable, module dispose and signing-key removal stop the loop and release
  the lease; the loop is awaited so no pull can run after stop returns.
- The last explicit choice is remembered as an *intent* so the phone page can offer a one-tap resume.

## Verification

```bash
# module adapter and phone surface
dotnet build -c Release tools/remote-notifications/android-integration/src/RemoteNotifications.Android/RemoteNotifications.Android.csproj
dotnet build -c Release src/MyPowerTools.MobileNotifications/MyPowerTools.MobileNotifications.csproj

# protocol / parity / module behaviour suite (no socket, no device)
dotnet test -c Release tools/remote-notifications/android-integration/tests/RemoteNotifications.Android.Tests/RemoteNotifications.Android.Tests.csproj

# stage the APK-ready package (writes android-integration/artifacts/package/remote-notifications-android)
pwsh -NoLogo -NoProfile -NonInteractive -File tools/remote-notifications/android-integration/build.ps1 -MyPowerToolsRepoRoot <repo>
```

The history lock is covered by `HistoryLockTests`: real exclusion across threads and across a
**second process** (the `RemoteNotifications.Android.LockProbe` helper performs a real store write
that stays blocked until the first process releases), a real store write that blocks while the lock is
held, three shared store instances under 24 concurrent writers, and the app-private lock location. A
drift guard also fails the suite if a project stops binding the alias or the shipped store stops using
`new Mutex(false, mutexName)`.

See `PARENT_INTEGRATION.md` for the exact host/APK wiring the parent agent must own, and
`SUPPORT_MATRIX.md` for what the MPT notification product supports on Android today.
