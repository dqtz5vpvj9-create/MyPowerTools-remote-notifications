# MPT Remote Notifications on Android — support matrix

Scope: what the Remote Notifications product does on the MyPowerTools Android build after this
workstream. "Supported" means implemented and covered by the host-side suite in
`tests/RemoteNotifications.Android.Tests`; it does not claim on-device acceptance (see the open
items at the end).

## Supported

| Capability | Android status | Evidence |
| --- | --- | --- |
| Signed `GET /pull?channel&sig&limit&since` handshake | Supported | `RemoteNotificationSignedPoller`; parity test compares it byte-for-byte with the shipped desktop poller |
| Ed25519 signature of `hello` from an OpenSSH private key | Supported | `RemoteNotificationSigningKey`; signed/verified against a generated key in tests |
| Key storage in the platform credential store | Supported | `secret.store` capability; test asserts nothing is written to app-private files |
| Waterline (`since`) from the newest `server_timestamp` with clock-skew clamp | Supported | `RemoteNotificationInboxSynchronizer`; parity test against `RemoteNotificationBackgroundReceiver` |
| Deduplication across restarts (stable + fallback id ring) | Supported | same store file the desktop client uses; parity + dedup tests |
| History, labels, filter, seen ids in app-private data | Supported | `<state>/tools/remote-notifications-android/{settings,history}.json` |
| Mutual exclusion for `history.json` across instances/processes | Supported | `RemoteNotificationAndroidFileLock` via the `Mutex` alias; `HistoryLockTests` (cross-thread, cross-process, 24 concurrent writers, app-private location) |
| `[label] reply` extraction | Supported | shipped `RemoteNotificationsLegacyStore.ExtractLabel` |
| Quoted-request handling (leading `>` block, trailing `For reference` / `原文仅供参考`) | Supported | shipped `SplitQuotedRequest`/`AttachQuotedRequest`; parity test on persisted history |
| Claude Stop duplicate collapse, agent-internal filtering, task-completed merge | Supported | shipped store source, same code path as desktop |
| Banner title/body rules and truncation (140/900) | Supported | `RemoteNotificationAndroidBanner`; compared with `RemoteNotificationWindowsToastPublisher.BuildEnvelope` |
| Suppression of `Claude Task` bursts and `system_health` records | Supported | parity test against the desktop publisher |
| Tray delivery through `notification.desktop` | Supported | `AndroidNotificationService` via the capability; fake service test asserts one banner per new message |
| Tap-to-open activation (`mypowertools://remote-notification?id=…`) | Supported | tool manifest `activationUriPrefixes` + `MobileNotificationsView.ActivateAsync` |
| Opt-in background polling with a foreground-service lease | Supported | `RemoteNotificationBackgroundPoller` + `background.activity`; lifecycle tests |
| Stop on user request, module disable, dispose, or key removal | Supported | lease released and loop awaited before stop returns |
| MPT settings contract (schema/validate/apply) | Supported | non-secret fields only; secret patches are rejected |
| MPT command contract | Supported | 10 commands listed in `package/commands.index.json` |
| MPT event contract | Supported | `module.running`, `message.received`, `polling.started/stopped`, `server.connected/disconnected`, `inbox.cleared`, `signing-key.*` |
| Phone UI: list, search, label chips, Claude Task page, inline detail, settings, background switch, key import | Supported | `src/MyPowerTools.MobileNotifications` (code-built surface) |

## Partially supported / different from desktop

| Item | Android behaviour | Reason |
| --- | --- | --- |
| Detail view | Expands inline on the card instead of a separate window | Android surfaces do not open desktop windows (`allowWindow: false`) |
| Persistent banners (`KeepWindowsBanners`) | Not applied; every banner is dismissible | `AndroidNotificationService` does not expose an ongoing/reminder flag (platform owner's file) |
| Auto-resume of background polling after an app restart | Not automatic: the page offers a one-tap resume | Required by the "no background at startup" rule |
| Notification permission | Requested by the platform service on first background enable | Provided by `AndroidBackgroundActivityService.NotificationPermissionRequest` |
| Health records | Shown as a thin strip, not as inbox cards | Same as the desktop inbox |

## Not supported on Android today

| Item | Status |
| --- | --- |
| Sending notifications (this client is receive-only, same as desktop) | Out of scope |
| Service-unit based desktop background service (`remote-notifications.service`) | Not applicable on Android |
| UnifiedPush / FCM push transport used by NotifyApp | Not part of the MPT tool; NotifyApp remains a separate app |
| Desktop-only shell commands / time-line export from the desktop Surface | Not ported (no desktop window, no clipboard-file export) |
| Notification action buttons (reply, mark read from the tray) | Not implemented |
| Multiple channels side by side | Single configured channel, same as the desktop settings model |
| Encrypted OpenSSH keys | Rejected with a clear message; the desktop client also requires an unencrypted key |
| Named-mutex based history lock (desktop implementation) | Not used on Android: the PAL path `/data/local/tmp/.dotnet-*` fails with `EACCES`. The Android build binds the same call shape to an app-private exclusive file lock instead |

## Open items (not verified here)

1. On-device acceptance on a real Android build: permission prompt, lease notification + stop action,
   KeyStore import, HTTPS/Tailscale reachability, background continuation and battery behaviour.
2. APK packaging wiring (asset glob + build order) is owned by the parent agent — see
   `PARENT_INTEGRATION.md`.
3. The desktop AndroidTools suite's own `remote-notifications` tool card stays unsupported on
   Android; optional exclusion from the APK is described in `PARENT_INTEGRATION.md`.
