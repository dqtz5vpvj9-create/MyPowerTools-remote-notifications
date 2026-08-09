from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_sender_module():
    path = ROOT / "py_modules" / "simple_http_notification_sender.py"
    spec = importlib.util.spec_from_file_location("session_metadata_sender", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Logger:
    def notice(self, *_args, **_kwargs):
        pass


def test_sender_posts_session_metadata(monkeypatch):
    module = load_sender_module()
    captured = {}

    class Response:
        def json(self):
            return {"status": "ok"}

    def post(url, payload, timeout=None):
        captured.update({"url": url, "payload": payload, "timeout": timeout})
        return Response()

    monkeypatch.setattr(module, "_http_post_json", post)
    sender = module.SimpleHttpNotificationSender("https", "example.test", 8888, Logger())
    sender.send(
        "default",
        "done",
        "codex",
        session_id="00000000-0000-0000-0000-000000000000",
        session_name="Session metadata test",
        source_client="codex",
    )

    assert captured["payload"]["schema_version"] == 2
    assert captured["payload"]["session_id"] == "00000000-0000-0000-0000-000000000000"
    assert captured["payload"]["session_name"] == "Session metadata test"
    assert captured["payload"]["source_client"] == "codex"


def test_server_source_keeps_legacy_records_readable():
    source = (ROOT / "py_modules" / "simple_http_notification_server.py").read_text(encoding="utf-8")
    assert "def _notification_record(raw: Any, channel: str)" in source
    assert "isinstance(raw, (list, tuple))" in source
    assert '"session_id": ""' in source
    assert "results.append(_public_notification(record))" in source
