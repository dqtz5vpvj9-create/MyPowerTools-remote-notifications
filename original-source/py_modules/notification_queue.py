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
SEND_NOTIFICATION = Path(__file__).with_name("send_notification.py")

BACKOFF_BASE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_RETRY_BASE", "5"))
BACKOFF_MAX_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_RETRY_MAX", "300"))
MAX_ITEM_AGE_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_MAX_AGE", "86400"))
WORKER_POLL_MAX_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_WORKER_POLL_MAX", "5"))
DEDUP_TTL_SECONDS = float(os.environ.get("ANDROIDTOOLS_NOTIFY_DEDUP_TTL", "60"))

SendFunc = Callable[[dict[str, Any]], subprocess.CompletedProcess[str]]


def _ensure_dirs() -> None:
    for path in (PENDING_DIR, INFLIGHT_DIR, FAILED_DIR, DEDUP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _now() -> float:
    return time.time()


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _event_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _log(message: str) -> None:
    _ensure_dirs()
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{_timestamp()} {message}\n")


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
    _atomic_write_json(_item_path(item), item)
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
            continue

        item["last_error"] = _summarize_failure(result, exc)
        item["last_attempt_at"] = now
        if now - float(item.get("created_at", now)) >= MAX_ITEM_AGE_SECONDS:
            _atomic_write_json(FAILED_DIR / inflight.name, item)
            try:
                inflight.unlink()
            except FileNotFoundError:
                pass
            _log(f"expired id={item.get('id')} attempts={attempts} error={item['last_error']}")
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
        _restore_inflight()
        _log("worker_start")
        while True:
            now = _now()
            processed = process_due_items(send_func=send_func, now=now)
            if processed:
                continue
            due = next_due_at()
            if due is None:
                _log("worker_idle_exit")
                return
            sleep_for = max(0.0, min(WORKER_POLL_MAX_SECONDS, due - now))
            time.sleep(sleep_for)


def start_worker() -> None:
    _ensure_dirs()
    with LOG_PATH.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )


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

    args = parser.parse_args()
    if args.command == "worker":
        worker_loop()
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
