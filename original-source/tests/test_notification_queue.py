from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "notification_queue",
    ROOT / "py_modules" / "notification_queue.py",
)
notification_queue = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(notification_queue)


def configure_queue(tmp_path, monkeypatch):
    queue_root = tmp_path / "queue"
    monkeypatch.setattr(notification_queue, "QUEUE_ROOT", queue_root)
    monkeypatch.setattr(notification_queue, "PENDING_DIR", queue_root / "pending")
    monkeypatch.setattr(notification_queue, "INFLIGHT_DIR", queue_root / "inflight")
    monkeypatch.setattr(notification_queue, "FAILED_DIR", queue_root / "failed")
    monkeypatch.setattr(notification_queue, "DEDUP_DIR", queue_root / "dedupe")
    monkeypatch.setattr(notification_queue, "LOG_PATH", queue_root / "worker.log")
    monkeypatch.setattr(notification_queue, "LOCK_PATH", queue_root / "worker.lock")
    monkeypatch.setattr(notification_queue, "HEALTH_PATH", queue_root / "health.json")
    monkeypatch.setattr(notification_queue, "PID_PATH", queue_root / "worker.pid")
    return queue_root


def test_enqueue_persists_hook_payload(tmp_path, monkeypatch):
    configure_queue(tmp_path, monkeypatch)

    item_id = notification_queue.enqueue(
        raw_stdin='{"cwd": "/android", "last_assistant_message": "done"}',
        message=None,
        hook="stop",
        client="claude",
        icon="claude",
        channel="default",
    )

    queued = list(notification_queue.PENDING_DIR.glob("*.json"))
    assert len(queued) == 1
    data = json.loads(queued[0].read_text())
    assert data["id"] == item_id
    assert data["hook"] == "stop"
    assert re.match(r"20\d\d-\d\d-\d\dT", data["timestamp"])
    assert data["raw_stdin"].startswith('{"cwd"')


def test_worker_retries_then_sends_in_order(tmp_path, monkeypatch):
    configure_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(notification_queue, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(notification_queue, "BACKOFF_MAX_SECONDS", 0.0)

    first = notification_queue.enqueue(
        raw_stdin="{}",
        message=None,
        hook="stop",
        client="claude",
        icon="claude",
        channel="default",
    )
    second = notification_queue.enqueue(
        raw_stdin=None,
        message="plain",
        hook=None,
        client="claude",
        icon="info",
        channel="default",
    )
    calls = []

    def send_func(item):
        calls.append(item["id"])
        if item["id"] == first and item["attempts"] == 1:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="down")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    notification_queue.process_due_items(send_func=send_func, now=100.0)
    assert calls == [first, second]
    assert len(list(notification_queue.PENDING_DIR.glob("*.json"))) == 1

    notification_queue.process_due_items(send_func=send_func, now=100.0)
    assert calls == [first, second, first]
    assert list(notification_queue.PENDING_DIR.glob("*.json")) == []
    assert list(notification_queue.FAILED_DIR.glob("*.json")) == []


def test_queue_accepts_codex_schema_hook_name(tmp_path, monkeypatch):
    configure_queue(tmp_path, monkeypatch)

    item_id = notification_queue.enqueue(
        raw_stdin='{"hook_event_name": "PermissionRequest", "tool_name": "Bash"}',
        message=None,
        hook="PermissionRequest",
        client="codex",
        icon="codex",
        channel="default",
    )

    data = json.loads(next(notification_queue.PENDING_DIR.glob("*.json")).read_text())
    assert data["id"] == item_id
    assert data["hook"] == "PermissionRequest"


def test_send_command_uses_queue_timestamp(tmp_path, monkeypatch):
    configure_queue(tmp_path, monkeypatch)

    item_id = notification_queue.enqueue(
        raw_stdin="{}",
        message=None,
        hook="Stop",
        client="codex",
        icon="codex",
        channel="default",
    )
    item = json.loads(next(notification_queue.PENDING_DIR.glob("*.json")).read_text())

    cmd = notification_queue._send_command(item)

    assert "--timestamp" in cmd
    assert cmd[cmd.index("--timestamp") + 1] == item["timestamp"]
    assert item["id"] == item_id


def test_source_parse_failure_is_quarantined_and_reported(tmp_path, monkeypatch):
    configure_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(notification_queue, "PERMANENT_FAILURE_ATTEMPTS", 2)
    monkeypatch.setattr(notification_queue, "MAX_ATTEMPTS", 99)
    monkeypatch.setattr(notification_queue, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(notification_queue, "BACKOFF_MAX_SECONDS", 0.0)
    item_id = notification_queue.enqueue(
        raw_stdin="{}",
        message=None,
        hook="Stop",
        client="codex",
        icon="codex",
        channel="default",
    )

    def send_func(_item):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="UnicodeDecodeError: invalid continuation byte",
        )

    notification_queue.process_due_items(send_func=send_func, now=100.0)
    notification_queue.process_due_items(send_func=send_func, now=100.0)

    assert list(notification_queue.PENDING_DIR.glob("*.json")) == []
    failed = list(notification_queue.FAILED_DIR.glob("*.json"))
    assert len(failed) == 1
    failed_data = json.loads(failed[0].read_text())
    assert failed_data["id"] == item_id
    assert failed_data["failure_kind"] == "source_parse"
    health = notification_queue.queue_health_snapshot(now=100.0)
    assert health["failed_count"] == 1
    assert health["last_error_kind"] == "source_parse"
