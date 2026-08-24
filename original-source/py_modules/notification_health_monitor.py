#!/usr/bin/env python3
"""Independent health monitor for the hook notification path.

The monitor intentionally runs outside the notification queue worker. It can
report a poisoned queue, a dead worker, hook/backend failures, and disk
pressure through direct HTTP and direct FCM paths even when normal event
formatting is broken.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR.parent))

from py_modules import notification_queue


MONITOR_STATE_PATH = Path(os.environ.get(
    "ANDROIDTOOLS_NOTIFY_HEALTH_STATE",
    "~/.cache/androidtools/notification_queue/monitor.json",
)).expanduser()
HOOK_LOGS = (
    Path(os.environ.get(
        "ANDROIDTOOLS_NOTIFY_CODEX_HOOK_LOG",
        "~/.cache/codex-tmux-integration/codex_hook.log",
    )).expanduser(),
    Path(os.environ.get(
        "ANDROIDTOOLS_NOTIFY_CLAUDE_HOOK_LOG",
        "~/.cache/codex-tmux-integration/claude_hook.log",
    )).expanduser(),
)
RAW_HOOK_LOGS = (
    Path(os.environ.get(
        "ANDROIDTOOLS_NOTIFY_CODEX_RAW_LOG",
        "~/.cache/codex-tmux-integration/codex_hook_raw_stdin.json",
    )).expanduser(),
    Path(os.environ.get(
        "ANDROIDTOOLS_NOTIFY_CLAUDE_RAW_LOG",
        "~/.cache/codex-tmux-integration/claude_hook_raw_stdin.json",
    )).expanduser(),
)

PENDING_WARN_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_PENDING_WARN", "300"))
WORKER_STALE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_WORKER_STALE", "180"))
HOOK_STALE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_HOOK_STALE", "180"))
MIN_FREE_GB = float(os.environ.get("ANDROIDTOOLS_NOTIFY_MIN_FREE_GB", "5"))
MIN_FREE_PERCENT = float(os.environ.get("ANDROIDTOOLS_NOTIFY_MIN_FREE_PERCENT", "3"))


def _now() -> float:
    return time.time()


def _timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or _now(), timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _disk_checks() -> list[dict[str, Any]]:
    checks = []
    seen: set[str] = set()
    for candidate in (notification_queue.QUEUE_ROOT, Path.home(), Path("/android")):
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        try:
            usage = shutil.disk_usage(candidate)
        except OSError as exc:
            checks.append({"path": key, "ok": False, "error": str(exc)[:160]})
            continue
        free_gb = usage.free / (1024 ** 3)
        free_percent = (usage.free / usage.total * 100.0) if usage.total else 0.0
        checks.append({
            "path": key,
            "ok": free_gb >= MIN_FREE_GB and free_percent >= MIN_FREE_PERCENT,
            "free_gb": round(free_gb, 2),
            "free_percent": round(free_percent, 2),
        })
    return checks


def _hook_checks(now: float) -> list[str]:
    issues: list[str] = []
    for raw_path, hook_path in zip(RAW_HOOK_LOGS, HOOK_LOGS):
        try:
            raw_mtime = raw_path.stat().st_mtime
        except OSError:
            continue
        if now - raw_mtime > HOOK_STALE_SECONDS:
            continue
        try:
            hook_mtime = hook_path.stat().st_mtime
            hook_text = hook_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            issues.append(f"hook_log_missing:{hook_path}")
            continue
        if raw_mtime - hook_mtime > 30:
            issues.append(f"hook_not_completed:{hook_path.name}")
        if any(marker in hook_text.lower() for marker in (
            "backend unavailable",
            "traceback",
            "error",
            "exception",
            "failed",
        )):
            issues.append(f"hook_error:{hook_path.name}")
    return issues


def collect_health() -> dict[str, Any]:
    now = _now()
    queue = notification_queue.queue_health_snapshot(now)
    backlog_count = int(queue.get("backlog_count", queue.get("pending_count", 0) + queue.get("inflight_count", 0)))
    disks = _disk_checks()
    issues: list[dict[str, Any]] = []

    if backlog_count and queue["oldest_pending_age"] >= PENDING_WARN_SECONDS:
        issues.append({
            "code": "queue_backlog",
            "detail": f"backlog={backlog_count} oldest={int(queue['oldest_pending_age'])}s",
        })
    if queue["failed_count"]:
        issues.append({"code": "failed_items", "detail": f"failed={queue['failed_count']}"})
    if backlog_count and not queue["worker_alive"]:
        issues.append({"code": "worker_dead", "detail": f"state={queue['worker_state']}"})
    heartbeat_age = queue.get("worker_heartbeat_age")
    if backlog_count and queue["worker_alive"] and heartbeat_age is not None:
        if heartbeat_age >= WORKER_STALE_SECONDS:
            issues.append({"code": "worker_stale", "detail": f"heartbeat_age={int(heartbeat_age)}s"})
    if queue.get("last_error_kind"):
        last_failure = queue.get("last_failure_at")
        try:
            recent_failure = last_failure is not None and now - float(last_failure) <= WORKER_STALE_SECONDS
        except (TypeError, ValueError):
            recent_failure = True
        if recent_failure:
            issues.append({
                "code": f"send_{queue['last_error_kind']}",
                "detail": str(queue.get("last_error") or "")[-220:],
            })
    for check in disks:
        if not check.get("ok", False):
            issues.append({
                "code": "disk_pressure",
                "detail": f"{check.get('path')} free={check.get('free_gb', '?')}GB/{check.get('free_percent', '?')}%",
            })
    for detail in _hook_checks(now):
        issues.append({"code": "hook", "detail": detail})

    # Keep changing measurements in the report, while keeping the alert
    # identity stable.  Disk free space changes every minute under normal
    # operation; it must not create a fresh alert for the same pressure state.
    signature_parts: list[str] = []
    for item in issues:
        code = str(item.get("code") or "")
        detail = str(item.get("detail") or "")
        if code == "disk_pressure":
            detail = detail.split(" free=", 1)[0]
        elif code in {
            "queue_backlog",
            "failed_items",
            "worker_dead",
            "worker_stale",
        } or code.startswith("send_"):
            detail = ""
        signature_parts.append(f"{code}:{detail}" if detail else code)
    signature = "|".join(signature_parts)
    return {
        "schema": "androidtools.notification-health.v1",
        "checked_at": now,
        "checked_at_iso": _timestamp(now),
        "healthy": not issues,
        "issues": issues,
        "signature": signature,
        "queue": queue,
        "disks": disks,
    }


def _attempt_worker_recovery(report: dict[str, Any]) -> dict[str, Any]:
    """Wake the queue worker when work is stranded without a live worker."""
    queue = report.get("queue") or {}
    backlog_count = int(queue.get("backlog_count", queue.get("pending_count", 0) + queue.get("inflight_count", 0)))
    if not backlog_count:
        return {}
    heartbeat_age = queue.get("worker_heartbeat_age")
    try:
        stale = bool(
            queue.get("worker_alive")
            and heartbeat_age is not None
            and float(heartbeat_age) >= WORKER_STALE_SECONDS
        )
    except (TypeError, ValueError):
        stale = False
    if queue.get("worker_alive") and not stale:
        return {}
    try:
        notification_queue.start_worker()
    except Exception as exc:
        return {
            "attempted": True,
            "started": False,
            "reason": "worker_stale" if stale else "worker_dead",
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    return {
        "attempted": True,
        "started": True,
        "reason": "worker_stale" if stale else "worker_dead",
    }


def _health_message(report: dict[str, Any], recovery: bool = False, probe: bool = False) -> str:
    if probe:
        return (
            f"[CHRS 健康自检] 通知链路可用\n"
            f"queue pending={report['queue']['pending_count']} "
            f"failed={report['queue']['failed_count']} "
            f"checked={report['checked_at_iso']}"
        )
    if recovery:
        return f"[CHRS 健康恢复] 通知链路已恢复\nchecked={report['checked_at_iso']}"
    details = "\n".join(f"- {item['code']}: {item['detail']}" for item in report["issues"][:8])
    return f"[CHRS 健康告警] 通知链路异常\n{details}\nchecked={report['checked_at_iso']}"


def _notification_id(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:32]


def _send_direct(message: str, *, severity: str = "warning") -> dict[str, Any]:
    """Store the health state for UI polling without creating a push alert."""
    result: dict[str, Any] = {
        "http": {},
        "fcm": {"suppressed": True, "reason": "system_health_is_ui_only"},
    }
    notif_id = _notification_id(message)
    timestamp = _timestamp()
    try:
        from py_modules.simple_http_notification_conf import (
            cloud_server_ip,
            cloud_server_port,
            cloud_server_protocol,
        )
        from py_modules.simple_http_notification_sender import SimpleHttpNotificationSender
        from py_modules.logging_lib import setup_logging

        sender = SimpleHttpNotificationSender(
            cloud_server_protocol,
            cloud_server_ip,
            cloud_server_port,
            setup_logging(),
        )
        result["http"] = sender.send(
            "default",
            message,
            severity,
            notif_id=notif_id,
            client_msg_id=notif_id,
            timestamp=timestamp,
            session_name="CHRS notification health",
            source_client="chris-health",
            content_kind="system_health",
            schema_version=2,
            timeout=10,
        )
    except Exception as exc:
        result["http"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    result["delivered"] = bool(
        result["http"].get("accepted", False)
        or result["http"].get("ok", False)
        or result["http"].get("status") == "ok"
    )
    return result


def run_once(*, force: bool = False, probe: bool = False) -> dict[str, Any]:
    report = collect_health()
    worker_recovery = _attempt_worker_recovery(report)
    if worker_recovery:
        report["worker_recovery"] = worker_recovery
    state = _load_json(MONITOR_STATE_PATH)
    now = report["checked_at"]
    previous_signature = str(state.get("active_signature") or "")
    signature = report["signature"]
    should_send = force or probe or (bool(signature) and signature != previous_signature)
    recovery = not signature and bool(previous_signature)
    if recovery:
        should_send = True
    delivery = {}
    if should_send:
        delivery = _send_direct(
            _health_message(report, recovery=recovery, probe=probe),
            severity="error" if signature and not probe else "warning",
        )
        state["last_alert_at"] = now
        state["last_alert_signature"] = signature or "recovery"
        state["last_delivery"] = delivery
    state["active_signature"] = signature
    state["last_run_at"] = now
    state["last_report"] = report
    try:
        _write_json(MONITOR_STATE_PATH, state)
    except OSError:
        pass
    report["delivery"] = delivery
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="CHRS notification health monitor")
    parser.add_argument("--force", action="store_true", help="send an alert even when the signature is unchanged")
    parser.add_argument("--probe", action="store_true", help="send a direct end-to-end health probe")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    report = run_once(force=args.force, probe=args.probe)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print("healthy" if report["healthy"] else "degraded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
