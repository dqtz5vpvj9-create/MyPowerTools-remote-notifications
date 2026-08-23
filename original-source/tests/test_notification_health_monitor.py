from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "notification_health_monitor",
    ROOT / "py_modules" / "notification_health_monitor.py",
)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


def test_probe_uses_independent_delivery_and_persists_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MONITOR_STATE_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(monitor.notification_queue, "queue_health_snapshot", lambda now: {
        "pending_count": 0,
        "inflight_count": 0,
        "failed_count": 0,
        "oldest_pending_age": 0.0,
        "worker_alive": False,
        "worker_state": "idle",
        "worker_heartbeat_age": None,
        "last_error_kind": "",
        "last_error": "",
    })
    monkeypatch.setattr(monitor, "_disk_checks", lambda: [{"path": "/", "ok": True}])
    monkeypatch.setattr(monitor, "_hook_checks", lambda _now: [])
    captured = {}

    def send_direct(message, *, severity):
        captured.update(message=message, severity=severity)
        return {"delivered": True}

    monkeypatch.setattr(monitor, "_send_direct", send_direct)
    report = monitor.run_once(probe=True)

    assert report["healthy"] is True
    assert "健康自检" in captured["message"]
    assert captured["severity"] == "warning"
    assert (tmp_path / "monitor.json").exists()


def test_degraded_signature_is_reported_once_until_it_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MONITOR_STATE_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(monitor.notification_queue, "queue_health_snapshot", lambda now: {
        "pending_count": 3,
        "inflight_count": 0,
        "failed_count": 1,
        "oldest_pending_age": 600.0,
        "worker_alive": False,
        "worker_state": "idle",
        "worker_heartbeat_age": None,
        "last_error_kind": "source_parse",
        "last_error": "UnicodeDecodeError",
    })
    monkeypatch.setattr(monitor, "_disk_checks", lambda: [{"path": "/", "ok": True}])
    monkeypatch.setattr(monitor, "_hook_checks", lambda _now: [])
    deliveries = []
    monkeypatch.setattr(monitor, "_send_direct", lambda message, **kwargs: deliveries.append(message) or {"delivered": True})

    first = monitor.run_once()
    second = monitor.run_once()

    assert first["healthy"] is False
    assert second["healthy"] is False
    assert len(deliveries) == 1


def test_backlog_restarts_dead_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MONITOR_STATE_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(monitor.notification_queue, "queue_health_snapshot", lambda now: {
        "pending_count": 1,
        "inflight_count": 1,
        "backlog_count": 2,
        "failed_count": 0,
        "oldest_pending_age": 600.0,
        "worker_alive": False,
        "worker_state": "crashed",
        "worker_heartbeat_age": None,
        "last_error_kind": "",
        "last_error": "",
    })
    monkeypatch.setattr(monitor, "_disk_checks", lambda: [{"path": "/", "ok": True}])
    monkeypatch.setattr(monitor, "_hook_checks", lambda _now: [])
    monkeypatch.setattr(monitor, "_send_direct", lambda _message, **_kwargs: {"delivered": True})
    starts = []
    monkeypatch.setattr(monitor.notification_queue, "start_worker", lambda: starts.append(True))

    report = monitor.run_once()

    assert starts == [True]
    assert report["worker_recovery"] == {
        "attempted": True,
        "started": True,
        "reason": "worker_dead",
    }
