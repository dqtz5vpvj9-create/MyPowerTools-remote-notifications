from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_guard():
    path = ROOT / "py_modules" / "claude_stop_guard.py"
    spec = importlib.util.spec_from_file_location("claude_stop_guard_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def assistant(uuid: str, message_id: str, content: list[dict], stop_reason: str):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {
            "id": message_id,
            "content": content,
            "stop_reason": stop_reason,
        },
    }


def test_tool_turn_never_replays_stale_visible_text(tmp_path, monkeypatch):
    module = load_guard()
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "state.json"))
    transcript.write_text("\n".join([
        json.dumps(assistant(
            "uuid-text",
            "msg-1",
            [{"type": "text", "text": "Now the policy side: hold delayed cold starts until their job-relative time."}],
            "tool_use",
        )),
        json.dumps(assistant(
            "uuid-tool",
            "msg-1",
            [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}],
            "tool_use",
        )),
    ]) + "\n", encoding="utf-8")

    payload = {
        "session_id": "session-1",
        "transcript_path": str(transcript),
        "last_assistant_message": "Now the policy side: hold delayed cold starts until their job-relative time.",
    }

    snapshot = module.inspect_stop(payload)
    assert snapshot is not None
    assert snapshot.content_kinds == ("text", "tool_use")
    assert snapshot.is_visible_completion is False
    assert module.claim_stop(payload) is None


def test_terminal_visible_event_is_claimed_once(tmp_path, monkeypatch):
    module = load_guard()
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "state.json"))
    transcript.write_text(json.dumps(assistant(
        "uuid-final",
        "msg-final",
        [{"type": "text", "text": "完成"}],
        "end_turn",
    )) + "\n", encoding="utf-8")
    payload = {"session_id": "session-2", "transcript_path": str(transcript)}

    first = module.claim_stop(payload)
    assert first is not None
    assert first.snapshot.text == "完成"
    assert module.claim_stop(payload) is None
    module.commit_claim(first)
    assert module.claim_stop(payload) is None


def test_partial_jsonl_line_waits_for_completion(tmp_path, monkeypatch):
    module = load_guard()
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "state.json"))
    line = json.dumps(assistant("uuid-partial", "msg-partial", [{"type": "text", "text": "稍后"}], "end_turn"))
    transcript.write_text(line[:20], encoding="utf-8")
    payload = {"session_id": "session-3", "transcript_path": str(transcript)}
    assert module.inspect_stop(payload) is None

    transcript.write_text(line + "\n", encoding="utf-8")
    claim = module.claim_stop(payload)
    assert claim is not None
    assert claim.snapshot.text == "稍后"


def test_legacy_payload_is_deduped_when_transcript_is_unavailable(tmp_path, monkeypatch):
    module = load_guard()
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "state.json"))
    payload = {
        "session_id": "session-legacy",
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "last_assistant_message": "同一条回复",
    }
    first = module.claim_stop(payload)
    assert first is not None
    module.commit_claim(first)
    assert module.claim_stop(payload) is None

