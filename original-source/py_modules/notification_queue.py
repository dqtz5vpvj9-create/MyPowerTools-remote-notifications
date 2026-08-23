#!/usr/bin/env python3
"""Persistent async queue for hook notifications.

Hook scripts should enqueue and return quickly. A single background worker drains
the queue and retries failed deliveries with bounded exponential backoff.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None


QUEUE_ROOT = Path(os.environ.get(
    "ANDROIDTOOLS_NOTIFY_QUEUE_DIR",
    "~/.cache/androidtools/notification_queue",
)).expanduser()
PENDING_DIR = QUEUE_ROOT / "pending"
INFLIGHT_DIR = QUEUE_ROOT / "inflight"
FAILED_DIR = QUEUE_ROOT / "failed"
DEDUP_DIR = QUEUE_ROOT / "dedupe"
LOG_PATH = QUEUE_ROOT / "worker.log"
LOCK_PATH = QUEUE_ROOT / "worker.lock"
HEALTH_PATH = QUEUE_ROOT / "health.json"
PID_PATH = QUEUE_ROOT / "worker.pid"
SEND_NOTIFICATION = Path(__file__).with_name("send_notification.py")

BACKOFF_BASE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_RETRY_BASE", "5"))
BACKOFF_MAX_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_RETRY_MAX", "300"))
MAX_ITEM_AGE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_MAX_AGE", "86400"))
WORKER_POLL_MAX_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_WORKER_POLL_MAX", "5"))
DEDUP_TTL_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_DEDUP_TTL", "60"))
PERMANENT_FAILURE_ATTEMPTS = int(os.environ.get("ANDROIDTOOLS_NOTIFY_PERMANENT_FAILURE_ATTEMPTS", "3"))
MAX_ATTEMPTS = int(os.environ.get("ANDROIDTOOLS_NOTIFY_MAX_ATTEMPTS", "12"))

_SOURCE_FAILURE_MARKERS = (
    "unicodedecodeerror",
    "unicodeencodeerror",
    "jsondecodeerror",
    "invalid control character",
    "invalid continuation byte",
)
_DISK_FAILURE_MARKERS = (
    "no space left on device",
    "disk quota exceeded",
)
_TRANSPORT_FAILURE_MARKERS = (
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "temporary failure",
    "502",
    "503",
    "504",
)

SendFunc = Callable[[dict[str, Any]], subprocess.CompletedProcess[str]]


def _ensure_dirs() -> None:
    for path in (PENDING_DIR, INFLIGHT_DIR, FAILED_DIR, DEDUP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _load_health_state() -> dict[str, Any]:
    try:
        with HEALTH_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _health_update(**updates: Any) -> None:
    """Persist a small, best-effort worker health heartbeat.

    Health reporting must never turn a notification failure into a hook
    failure, especially when the filesystem is under pressure.
    """
    try:
        _ensure_dirs()
        state = _load_health_state()
        state.update(updates)
        state["updated_at"] = _now()
        _atomic_write_json(HEALTH_PATH, state)
    except Exception:
        pass


def _pid_is_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def _failure_kind(error: str) -> str:
    text = str(error or "").lower()
    if any(marker in text for marker in _SOURCE_FAILURE_MARKERS):
        return "source_parse"
    if any(marker in text for marker in _DISK_FAILURE_MARKERS):
        return "disk"
    if any(marker in text for marker in _TRANSPORT_FAILURE_MARKERS):
        return "transport"
    return "delivery"


def queue_health_snapshot(now: float | None = None) -> dict[str, Any]:
    """Return queue and worker state for the independent health monitor."""
    now = _now() if now is None else now
    _ensure_dirs()
    pending = list(PENDING_DIR.glob("*.json"))
    inflight = list(INFLIGHT_DIR.glob("*.json"))
    failed = list(FAILED_DIR.glob("*.json"))
    oldest_created_at = None
    for path in pending + inflight:
        item = _load_json(path)
        if not item:
            continue
        created = float(item.get("created_at", now))
        oldest_created_at = created if oldest_created_at is None else min(oldest_created_at, created)
    state = _load_health_state()
    pid = state.get("worker_pid")
    if not pid:
        try:
            pid = PID_PATH.read_text(encoding="ascii").strip()
        except OSError:
            pid = ""
    heartbeat = state.get("worker_heartbeat_at")
    heartbeat_age = None
    if heartbeat:
        try:
            heartbeat_age = max(0.0, now - float(heartbeat))
        except (TypeError, ValueError):
            heartbeat_age = None
    return {
        "now": now,
        "pending_count": len(pending),
        "inflight_count": len(inflight),
        "backlog_count": len(pending) + len(inflight),
        "failed_count": len(failed),
        "oldest_pending_at": oldest_created_at,
        "oldest_pending_age": (
            max(0.0, now - oldest_created_at) if oldest_created_at is not None else 0.0
        ),
        "worker_pid": pid or "",
        "worker_alive": _pid_is_alive(pid),
        "worker_state": state.get("worker_state", "unknown"),
        "worker_heartbeat_at": heartbeat,
        "worker_heartbeat_age": heartbeat_age,
        "last_enqueue_at": state.get("last_enqueue_at"),
        "last_success_at": state.get("last_success_at"),
        "last_failure_at": state.get("last_failure_at"),
        "last_error_kind": state.get("last_error_kind", ""),
        "last_error": state.get("last_error", ""),
        "last_success_id": state.get("last_success_id", ""),
        "last_failure_id": state.get("last_failure_id", ""),
        "last_quarantined_id": state.get("last_quarantined_id", ""),
    }


def _now() -> float:
    return time.time()


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _event_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _log(message: str) -> None:
    try:
        _ensure_dirs()
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{_timestamp()} {message}\n")
    except OSError:
        # A full disk must still leave the worker and its health monitor alive.
        pass


def _item_path(item: dict[str, Any], directory: Path | None = None) -> Path:
    if directory is None:
        directory = PENDING_DIR
    created_ns = int(float(item.get("created_at", _now())) * 1_000_000_000)
    return directory / f"{created_ns:020d}_{item['id']}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _client_msg_id(
    *,
    raw_stdin: str | None,
    message: str | None,
    hook: str | None,
    client: str,
    icon: str,
    channel: str,
) -> str:
    identity: dict[str, Any] = {
        "source": "codex-hook",
        "client": client,
        "hook": hook or "",
        "channel": channel,
        "icon": icon,
    }
    if raw_stdin is not None:
        try:
            parsed = json.loads(raw_stdin) if raw_stdin.strip() else {}
        except Exception:
            parsed = raw_stdin
        if (
            client == "claude"
            and str(hook or "").strip().lower() == "stop"
            and isinstance(parsed, dict)
        ):
            # Stop payloads contain changing hook bookkeeping (prompt ids,
            # timestamps, permission fields).  Those fields describe one
            # event delivery attempt, not the assistant event itself.  Keep a
            # compact semantic key in the short queue dedupe window; the
            # transcript guard performs the durable identity check later.
            identity["claude_stop"] = {
                "session_id": str(parsed.get("session_id") or parsed.get("sessionId") or ""),
                "transcript_path": str(parsed.get("transcript_path") or ""),
                "event_uuid": str(parsed.get("uuid") or parsed.get("event_id") or ""),
                "message_id": str(parsed.get("message_id") or ""),
                "text": str(
                    parsed.get("last_assistant_message")
                    or parsed.get("text")
                    or ""
                ).strip(),
            }
        else:
            identity["payload"] = parsed
    else:
        identity["message"] = message or ""
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:32]


def _dedupe_marker_path(client_msg_id: str) -> Path:
    return DEDUP_DIR / f"{client_msg_id}.json"


def _cleanup_dedupe_markers(now: float) -> None:
    for path in DEDUP_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if float(data.get("expires_at", 0.0)) <= now:
                path.unlink(missing_ok=True)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass


def _reserve_dedupe(client_msg_id: str, item_id: str, created_at: float) -> tuple[bool, str]:
    _cleanup_dedupe_markers(created_at)
    marker = _dedupe_marker_path(client_msg_id)
    payload = {
        "client_msg_id": client_msg_id,
        "item_id": item_id,
        "created_at": created_at,
        "expires_at": created_at + DEDUP_TTL_SECONDS,
    }
    try:
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
            return False, str(existing.get("item_id") or client_msg_id)
        except Exception:
            return False, client_msg_id
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    return True, item_id


def enqueue(
    *,
    raw_stdin: str | None,
    message: str | None,
    hook: str | None,
    client: str,
    icon: str,
    channel: str,
) -> str:
    _ensure_dirs()
    created_at = _now()
    client_msg_id = _client_msg_id(
        raw_stdin=raw_stdin,
        message=message,
        hook=hook,
        client=client,
        icon=icon,
        channel=channel,
    )
    reserved, existing_id = _reserve_dedupe(client_msg_id, client_msg_id, created_at)
    if not reserved:
        _log(
            f"dedupe_skip id={existing_id} client_msg_id={client_msg_id} "
            f"hook={hook or '-'} client={client} channel={channel}"
        )
        return existing_id
    item = {
        "id": client_msg_id,
        "client_msg_id": client_msg_id,
        "created_at": created_at,
        "timestamp": _event_timestamp(created_at),
        "attempts": 0,
        "next_attempt_at": 0.0,
        "client": client,
        "hook": hook,
        "icon": icon,
        "channel": channel,
        "raw_stdin": raw_stdin,
        "message": message,
    }
    try:
        _atomic_write_json(_item_path(item), item)
    except Exception as exc:
        _health_update(
            last_enqueue_failure_at=created_at,
            last_error_kind="queue_write",
            last_error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )
        raise
    _health_update(last_enqueue_at=created_at, last_enqueue_id=item["id"])
    _log(
        f"queued id={item['id']} client_msg_id={client_msg_id} "
        f"hook={hook or '-'} client={client} channel={channel}"
    )
    return str(item["id"])


def _send_command(item: dict[str, Any]) -> list[str]:
    override = os.environ.get("ANDROIDTOOLS_NOTIFY_SEND_CMD")
    if override:
        cmd = shlex.split(override)
    else:
        cmd = [sys.executable, str(SEND_NOTIFICATION)]

    cmd.extend(["--channel", str(item.get("channel", "default"))])
    cmd.extend(["--icon", str(item.get("icon", "info"))])
    cmd.extend(["--client", str(item.get("client", "claude"))])
    client_msg_id = str(item.get("client_msg_id") or item.get("id") or "")
    if client_msg_id:
        cmd.extend(["--client-msg-id", client_msg_id, "--notif-id", client_msg_id])
    timestamp = item.get("timestamp") or _event_timestamp(float(item.get("created_at", _now())))
    cmd.extend(["--timestamp", str(timestamp)])
    raw_stdin = item.get("raw_stdin")
    hook = item.get("hook")
    if raw_stdin is not None:
        cmd.append("--stdin")
        if hook:
            cmd.extend(["--hook", str(hook)])
    else:
        cmd.append(str(item.get("message") or ""))
    return cmd


def send_item(item: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    stdin = item.get("raw_stdin")
    timeout = float(os.environ.get("ANDROIDTOOLS_NOTIFY_SEND_TIMEOUT", "45"))
    return subprocess.run(
        _send_command(item),
        input=stdin if stdin is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _summarize_failure(result: subprocess.CompletedProcess[str] | None, exc: Exception | None) -> str:
    if exc is not None:
        return f"{type(exc).__name__}: {str(exc)[:240]}"
    assert result is not None
    parts = [f"exit={result.returncode}"]
    if result.stdout:
        parts.append(f"stdout={result.stdout[-240:]!r}")
    if result.stderr:
        parts.append(f"stderr={result.stderr[-240:]!r}")
    return " ".join(parts)


def _backoff_delay(attempts: int) -> float:
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


def _pending_paths() -> list[Path]:
    _ensure_dirs()
    return sorted(PENDING_DIR.glob("*.json"))


def _restore_inflight() -> None:
    _ensure_dirs()
    for path in sorted(INFLIGHT_DIR.glob("*.json")):
        target = PENDING_DIR / path.name
        try:
            os.replace(path, target)
        except OSError as exc:
            _log(f"restore_failed path={path} error={exc}")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        _log(f"load_failed path={path} error={exc}")
        return None


def _move_bad_item(path: Path, reason: str) -> None:
    target = FAILED_DIR / path.name
    try:
        os.replace(path, target)
    except OSError:
        pass
    _log(f"failed path={path.name} reason={reason}")


def process_due_items(send_func: SendFunc = send_item, now: float | None = None) -> int:
    now = _now() if now is None else now
    processed = 0
    for pending in _pending_paths():
        item = _load_json(pending)
        if item is None:
            _move_bad_item(pending, "invalid_json")
            continue
        if float(item.get("next_attempt_at", 0.0)) > now:
            continue

        inflight = INFLIGHT_DIR / pending.name
        try:
            os.replace(pending, inflight)
        except FileNotFoundError:
            continue

        processed += 1
        attempts = int(item.get("attempts", 0)) + 1
        _health_update(
            worker_heartbeat_at=now,
            current_item_id=item.get("id", ""),
            current_item_started_at=now,
            current_item_attempt=attempts,
        )
        item["attempts"] = attempts
        result: subprocess.CompletedProcess[str] | None = None
        exc: Exception | None = None
        try:
            result = send_func(item)
        except Exception as err:
            exc = err

        if exc is None and result is not None and result.returncode == 0:
            try:
                inflight.unlink()
            except FileNotFoundError:
                pass
            _log(f"sent id={item.get('id')} attempts={attempts}")
            _health_update(
                last_success_at=now,
                last_success_id=item.get("id", ""),
                last_error="",
                last_error_kind="",
                current_item_id="",
                current_item_started_at=None,
                current_item_attempt=0,
            )
            continue

        item["last_error"] = _summarize_failure(result, exc)
        item["last_attempt_at"] = now
        item["failure_kind"] = _failure_kind(item["last_error"])
        _health_update(
            last_failure_at=now,
            last_failure_id=item.get("id", ""),
            last_error_kind=item["failure_kind"],
            last_error=item["last_error"],
        )
        quarantine = (
            item["failure_kind"] == "source_parse"
            and attempts >= PERMANENT_FAILURE_ATTEMPTS
        ) or attempts >= MAX_ATTEMPTS
        if quarantine:
            _atomic_write_json(FAILED_DIR / inflight.name, item)
            try:
                inflight.unlink()
            except FileNotFoundError:
                pass
            _log(
                f"quarantine id={item.get('id')} attempts={attempts} "
                f"kind={item['failure_kind']} error={item['last_error']}"
            )
            _health_update(
                last_quarantined_at=now,
                last_quarantined_id=item.get("id", ""),
                current_item_id="",
                current_item_started_at=None,
                current_item_attempt=0,
            )
            continue
        if now - float(item.get("created_at", now)) >= MAX_ITEM_AGE_SECONDS:
            _atomic_write_json(FAILED_DIR / inflight.name, item)
            try:
                inflight.unlink()
            except FileNotFoundError:
                pass
            _log(f"expired id={item.get('id')} attempts={attempts} error={item['last_error']}")
            _health_update(
                last_quarantined_at=now,
                last_quarantined_id=item.get("id", ""),
                current_item_id="",
                current_item_started_at=None,
                current_item_attempt=0,
            )
            continue

        item["next_attempt_at"] = now + _backoff_delay(attempts)
        _atomic_write_json(PENDING_DIR / inflight.name, item)
        try:
            inflight.unlink()
        except FileNotFoundError:
            pass
        _log(
            f"retry id={item.get('id')} attempts={attempts} "
            f"next_in={item['next_attempt_at'] - now:.1f}s error={item['last_error']}"
        )
        _health_update(
            current_item_id="",
            current_item_started_at=None,
            current_item_attempt=0,
        )
    return processed


def next_due_at() -> float | None:
    due_times: list[float] = []
    for path in _pending_paths():
        item = _load_json(path)
        if item is None:
            continue
        due_times.append(float(item.get("next_attempt_at", 0.0)))
    return min(due_times) if due_times else None


@contextlib.contextmanager
def worker_lock() -> Iterator[bool]:
    _ensure_dirs()
    if fcntl is None:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            yield False
            return
        try:
            yield True
        finally:
            os.close(fd)
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
        return

    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def worker_loop(send_func: SendFunc = send_item) -> None:
    with worker_lock() as acquired:
        if not acquired:
            return
        pid = os.getpid()
        try:
            _ensure_dirs()
            PID_PATH.write_text(str(pid), encoding="ascii")
        except OSError:
            pass
        _health_update(
            worker_state="running",
            worker_pid=pid,
            worker_started_at=_now(),
            worker_heartbeat_at=_now(),
            current_item_id="",
            current_item_started_at=None,
            current_item_attempt=0,
        )
        _restore_inflight()
        _log("worker_start")
        terminal_state = "idle"
        try:
            while True:
                now = _now()
                _health_update(worker_heartbeat_at=now)
                processed = process_due_items(send_func=send_func, now=now)
                if processed:
                    continue
                due = next_due_at()
                if due is None:
                    _log("worker_idle_exit")
                    return
                sleep_for = max(0.0, min(WORKER_POLL_MAX_SECONDS, due - now))
                time.sleep(sleep_for)
        except Exception as exc:
            terminal_state = "crashed"
            _health_update(
                worker_state="crashed",
                worker_crashed_at=_now(),
                last_error_kind="worker",
                last_error=f"{type(exc).__name__}: {str(exc)[:240]}",
            )
            _log(f"worker_crash error={type(exc).__name__}: {str(exc)[:240]}")
            return
        finally:
            try:
                if PID_PATH.read_text(encoding="ascii").strip() == str(pid):
                    PID_PATH.unlink()
            except OSError:
                pass
            _health_update(
                worker_state=terminal_state,
                worker_pid="",
                worker_heartbeat_at=_now(),
                current_item_id="",
                current_item_started_at=None,
                current_item_attempt=0,
            )


def start_worker() -> None:
    _ensure_dirs()
    command = [sys.executable, str(Path(__file__).resolve()), "worker"]
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
    except OSError:
        # Keep delivery alive when the log filesystem is full. The worker
        # heartbeat remains available through health.json when it can write.
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


def requeue_failed(*, failure_kind: str = "") -> int:
    """Move quarantined items back to pending for a deliberate replay."""
    _ensure_dirs()
    moved = 0
    for path in sorted(FAILED_DIR.glob("*.json")):
        item = _load_json(path)
        if not item:
            continue
        item_kind = str(item.get("failure_kind") or _failure_kind(item.get("last_error", "")))
        if failure_kind and item_kind != failure_kind:
            continue
        item["attempts"] = 0
        item["next_attempt_at"] = 0.0
        item.pop("last_error", None)
        item.pop("last_attempt_at", None)
        item.pop("failure_kind", None)
        target = PENDING_DIR / path.name
        try:
            _atomic_write_json(target, item)
            path.unlink()
        except OSError:
            continue
        moved += 1
    if moved:
        _health_update(last_requeue_at=_now(), last_requeue_count=moved)
        start_worker()
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue and retry hook notifications")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue_parser = subparsers.add_parser("enqueue")
    enqueue_parser.add_argument("message", nargs="?", default=None)
    enqueue_parser.add_argument("--channel", default="default")
    enqueue_parser.add_argument("--icon", default="info")
    enqueue_parser.add_argument("--client", default="claude", choices=["claude", "codex", "dsh", "cursor"])
    enqueue_parser.add_argument("--stdin", action="store_true")
    enqueue_parser.add_argument("--stdin-file", default=None)
    enqueue_parser.add_argument("--hook", default=None)
    enqueue_parser.add_argument("--no-start-worker", action="store_true")

    subparsers.add_parser("worker")
    subparsers.add_parser("health")
    recover_parser = subparsers.add_parser("requeue-failed")
    recover_parser.add_argument("--failure-kind", default="")

    args = parser.parse_args()
    if args.command == "worker":
        worker_loop()
        return 0
    if args.command == "health":
        print(json.dumps(queue_health_snapshot(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "requeue-failed":
        moved = requeue_failed(failure_kind=args.failure_kind)
        print(f"requeued {moved}")
        return 0

    raw_stdin = None
    if args.stdin_file:
        try:
            with open(args.stdin_file, encoding="utf-8", errors="replace") as fh:
                raw_stdin = fh.read()
        except OSError:
            raw_stdin = None
    elif args.stdin:
        raw_stdin = sys.stdin.read()
    item_id = enqueue(
        raw_stdin=raw_stdin,
        message=args.message,
        hook=args.hook,
        client=args.client,
        icon=args.icon,
        channel=args.channel,
    )
    if not args.no_start_worker:
        start_worker()
    print(f"queued notification {item_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
