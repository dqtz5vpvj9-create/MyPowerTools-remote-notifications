from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_send_module():
    path = ROOT / "py_modules" / "send_notification.py"
    spec = importlib.util.spec_from_file_location("dsh_send_notification", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(module)
    return module


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


def test_dsh_session_name_from_projection_cache(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh-home"
    storage = dsh_home / "storages"
    storage.mkdir(parents=True)
    (storage / "session_projcache.json").write_text(json.dumps({
        "tables": {
            "sessions": {
                "session-abc": {
                    "rows": {"title": {"val": "DSH 会话标题"}}
                }
            }
        }
    }), encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    module = load_send_module()
    assert module._dsh_session_name("session-abc") == "DSH 会话标题"


def test_dsh_stop_message_uses_transcript(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir(parents=True)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "session/title", "data": {"title": "来自 transcript 的标题"}}),
        json.dumps({"type": "assistant/message", "data": {
            "message": {
                "content": [
                    {"type": "reasoning", "text": "ignore me"},
                    {"type": "text", "text": "最后一条助手消息"}
                ]
            }
        }}),
    ]), encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    module = load_send_module()
    payload = {
        "session_id": "session-abc",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": None,
    }
    message = module.format_stop_message(payload, "dsh")
    assert message.startswith("[来自 transcript 的标题]")
    assert "最后一条助手消息" in message


def test_dsh_stop_message_quotes_user_request(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir(parents=True)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user/message", "data": {
            "content": [{"type": "text", "text": "请分析 Choreo 报告"}],
            "source": {"kind": "user"},
        }}),
        json.dumps({"type": "assistant/message", "data": {
            "message": {"content": [{"type": "text", "text": "分析完成"}]}
        }}),
    ]), encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    module = load_send_module()
    payload = {
        "session_id": "session-abc",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": None,
    }
    message = module.format_stop_message(payload, "dsh")
    assert "> 请分析 Choreo 报告" in message
    assert "分析完成" in message


def test_codex_stop_message_quotes_user_request(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "16"
    session_dir.mkdir(parents=True)
    session_id = "00000000-0000-0000-0000-000000000000"
    (session_dir / f"rollout-20260816-{session_id}.jsonl").write_text(
        json.dumps({"type": "response_item", "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "把全文发给我"}],
        }}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    module = load_send_module()
    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": "这是回复",
    }
    message = module.format_stop_message(payload, "codex")
    assert "> 把全文发给我" in message
    assert "这是回复" in message


def test_cursor_stop_message_reads_transcript(tmp_path):
    conversation_id = "61e0d768-a565-4270-a0bc-d4288ef08d08"
    transcript = tmp_path / "cursor.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"role": "user", "message": {"content": [
            {"type": "text", "text": "<timestamp>Tuesday</timestamp>\n<user_query>\n修一下通知正文\n</user_query>"}
        ]}}),
        json.dumps({"role": "assistant", "message": {"content": [
            {"type": "text", "text": "已经从 transcript 取出 Cursor Remote 的回复。"}
        ]}}),
        json.dumps({"type": "turn_ended", "status": "success"}),
    ]), encoding="utf-8")
    module = load_send_module()
    payload = {
        "conversation_id": conversation_id,
        "generation_id": "gen-1",
        "cursor_version": "1.7.2",
        "workspace_roots": [str(tmp_path / "MyPowerTools")],
        "transcript_path": str(transcript),
        "hook_event_name": "stop",
        "status": "completed",
        "session_id": conversation_id,
    }
    assert module._is_cursor_payload(payload)
    message = module.format_stop_message(payload, "cursor")
    assert message.startswith("> 修一下通知正文")
    assert "[MyPowerTools] 已经从 transcript 取出 Cursor Remote 的回复。" in message


def test_cursor_payload_overrides_claude_client(tmp_path):
    transcript = tmp_path / "cursor.jsonl"
    transcript.write_text(json.dumps({
        "role": "assistant",
        "message": {"content": [{"type": "text", "text": "只看这条助手消息"}]},
    }), encoding="utf-8")
    module = load_send_module()
    payload = {
        "conversation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "generation_id": "gen-2",
        "cursor_version": "1.7.2",
        "workspace_roots": [r"C:\work\demo"],
        "transcript_path": str(transcript),
        "hook_event_name": "stop",
        "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    message = module.format_stop_message(payload, "claude")
    assert "[demo] 只看这条助手消息" in message
    assert "Task completed" not in message
    assert module.client_name("cursor") == "Cursor"


def test_banner_text_omits_quoted_user_request():
    sys.path.insert(0, str(ROOT))
    from py_modules.notification_banner import strip_leading_quoted_request

    quoted = "> 请分析 Choreo 报告\n> 第二行\n\n[来自 transcript 的标题] 分析完成"
    banner = strip_leading_quoted_request(quoted)
    assert banner == "[来自 transcript 的标题] 分析完成"
    assert "请分析" not in banner
    assert "第二行" not in banner

    plain = "[build] complete"
    assert strip_leading_quoted_request(plain) == plain
    assert strip_leading_quoted_request("> just a quote") == "> just a quote"
