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

See `PARENT_INTEGRATION.md` for the exact host/APK wiring the parent agent must own, and
`SUPPORT_MATRIX.md` for what the MPT notification product supports on Android today.
