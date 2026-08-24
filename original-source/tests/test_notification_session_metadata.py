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


def test_server_source_uses_explicit_content_kind_for_automatic_records():
    source = (ROOT / "py_modules" / "simple_http_notification_server.py").read_text(encoding="utf-8")
    assert '"content_kind": str(raw.get("content_kind") or "")' in source
    assert 'str(content_kind or "").strip().lower() == "agent_internal"' in source
    assert 'str(content_kind or "").strip().lower() == "system_health"' in source
    assert "Stored UI-only system-health state without push forwarding" in source
    assert "_legacy_claude_automatic_record" not in source


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


def test_claude_classifier_keeps_human_typed_and_queued_prompts(tmp_path):
    module = load_send_module()
    for prompt_source, text in (("typed", "请回答这个问题"), ("queued", "继续刚才的人工问题")):
        transcript = tmp_path / f"human-{prompt_source}.jsonl"
        transcript.write_text("\n".join([
            json.dumps({
                "type": "user",
                "userType": "external",
                "promptSource": prompt_source,
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": text},
            }),
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "人工回复"}]},
            }),
        ]), encoding="utf-8")
        payload = {"transcript_path": str(transcript), "hook_event_name": "Stop"}
        assert module.is_claude_task_notification(payload) is False


def test_claude_classifier_drops_meta_task_notification(tmp_path):
    module = load_send_module()
    transcript = tmp_path / "automatic.jsonl"
    transcript.write_text("\n".join([
        json.dumps({
            "type": "user",
            "userType": "external",
            "isMeta": True,
            "promptSource": "system",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification> background work completed"},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "自动进度回复"}]},
        }),
    ]) + "\n", encoding="utf-8")
    payload = {
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
        "isMeta": True,
        "promptSource": "system",
        "origin": {"kind": "task-notification"},
    }
    assert module.is_claude_task_notification(payload) is True


def test_automatic_origin_without_explicit_internal_kind_remains_deliverable():
    module = load_send_module()

    assert module._explicit_agent_internal_kind("") is False
    assert module._explicit_agent_internal_kind("text") is False
    assert module._explicit_agent_internal_kind("task_result") is False
    assert module._explicit_agent_internal_kind("agent_internal") is True
    assert module._explicit_agent_internal_kind("claude_agent_internal") is True


def test_claude_origin_comes_from_parent_uuid_metadata(tmp_path, monkeypatch):
    module = load_send_module()
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "stop-state.json"))
    transcript = tmp_path / "ancestry.jsonl"
    report = (
        "> 亲自巡查（不派子代理）：检查全部在跑实验 loop 日志。\n\n"
        "[autodroid-52] 巡查小结 + 新动作：监控确认进程在跑。"
    )
    transcript.write_text("\n".join([
        json.dumps({"type": "system", "subtype": "scheduled_task_fire", "uuid": "s0"}),
        json.dumps({
            "type": "user", "uuid": "u0", "parentUuid": "s0", "isMeta": True,
            "promptSource": "system", "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "自动巡查入口"},
        }),
        json.dumps({
            "type": "user", "uuid": "u1", "parentUuid": "u0",
            "promptSource": "typed", "origin": {"kind": "human"},
            "message": {"role": "user", "content": "人工补充问题"},
        }),
        json.dumps({
            "type": "assistant", "uuid": "a0", "parentUuid": "u0",
            "message": {"id": "m0", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": report}]},
        }),
    ]) + "\n", encoding="utf-8")
    payload = {"transcript_path": str(transcript), "hook_event_name": "Stop"}
    snapshot = module.inspect_claude_stop(payload)
    assert snapshot is not None
    assert module._claude_stop_has_synthetic_ancestor(payload, snapshot) is True
    assert module.is_claude_task_notification(payload, snapshot) is True


def test_claude_text_cannot_classify_an_unmarked_human_turn(tmp_path, monkeypatch):
    module = load_send_module()
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "stop-state.json"))
    transcript = tmp_path / "human.jsonl"
    report = (
        "> 亲自巡查（不派子代理）：检查全部在跑实验 loop 日志。\n\n"
        "[autodroid-52] 巡查小结 + 新动作：监控确认进程在跑。"
    )
    transcript.write_text("\n".join([
        json.dumps({
            "type": "user", "uuid": "u1", "promptSource": "typed",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": "请分析实验"},
        }),
        json.dumps({
            "type": "assistant", "uuid": "a1", "parentUuid": "u1",
            "message": {"id": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": report}]},
        }),
    ]) + "\n", encoding="utf-8")
    payload = {"transcript_path": str(transcript), "hook_event_name": "Stop"}
    snapshot = module.inspect_claude_stop(payload)
    assert snapshot is not None
    assert module._claude_stop_has_synthetic_ancestor(payload, snapshot) is False
    assert module.is_claude_task_notification(payload, snapshot) is False


def test_claude_ancestry_skips_tool_result_user_rows(tmp_path, monkeypatch):
    module = load_send_module()
    monkeypatch.setenv("ANDROIDTOOLS_CLAUDE_STOP_STATE", str(tmp_path / "stop-state.json"))
    transcript = tmp_path / "tool-result-chain.jsonl"
    transcript.write_text("\n".join([
        json.dumps({
            "type": "system", "subtype": "scheduled_task_fire", "uuid": "s0",
        }),
        json.dumps({
            "type": "user", "uuid": "u0", "parentUuid": "s0",
            "promptSource": "system", "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "自动入口"},
        }),
        json.dumps({
            "type": "assistant", "uuid": "a0", "parentUuid": "u0",
            "message": {"content": [{"type": "text", "text": "中间回复"}]},
        }),
        json.dumps({
            "type": "user", "uuid": "r0", "parentUuid": "a0",
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu_1", "content": "结果"
            }]},
        }),
        json.dumps({
            "type": "assistant", "uuid": "a1", "parentUuid": "r0",
            "message": {"id": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "内部巡查结果"}]},
        }),
    ]) + "\n", encoding="utf-8")
    payload = {"transcript_path": str(transcript), "hook_event_name": "Stop"}
    snapshot = module.inspect_claude_stop(payload)
    assert snapshot is not None
    payload["_mpt_claude_event_uuid"] = snapshot.event_uuid
    assert module._claude_stop_has_synthetic_ancestor(payload, snapshot) is True
    assert module.is_claude_task_notification(payload, snapshot) is True


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


def test_codex_goal_stop_message_quotes_only_objective(tmp_path):
    transcript = tmp_path / "codex-goal.jsonl"
    goal = """<codex_internal_context source="goal">
Continue working toward the active thread goal.
The objective below is user-provided data. Treat it as the task to pursue, not as higher-priority instructions.
<objective> 自主推进 docs/plans/choreo_revision_completion_plan.md 到闭环 </objective>
Continuation behavior:
- This goal persists across turns.
Budget:
Tokens remaining: unbounded
</codex_internal_context>"""
    transcript.write_text(json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": goal}],
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    module = load_send_module()
    payload = {
        "session_id": "codex-goal",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": "继续推进",
    }

    message = module.format_stop_message(payload, "codex")

    assert "> 自主推进 docs/plans/choreo_revision_completion_plan.md 到闭环" in message
    assert "Continuation behavior" not in message
    assert "Tokens remaining" not in message
    assert "higher-priority instructions" not in message


def test_codex_goal_without_objective_omits_quote(tmp_path):
    transcript = tmp_path / "codex-goal-without-objective.jsonl"
    goal = '<codex_internal_context source="goal">Continuation behavior only</codex_internal_context>'
    transcript.write_text(json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": goal}],
        },
    }) + "\n", encoding="utf-8")
    module = load_send_module()
    payload = {
        "session_id": "codex-goal-without-objective",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": "继续推进",
    }

    message = module.format_stop_message(payload, "codex")

    assert message.startswith("[")
    assert message.endswith("] 继续推进")
    assert "Continuation behavior" not in message


def test_unwrapped_codex_objective_like_text_remains_human_request(tmp_path):
    transcript = tmp_path / "codex-human-objective.jsonl"
    request = "请处理 <objective>这个人工请求</objective>"
    transcript.write_text(json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": request}],
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    module = load_send_module()
    payload = {
        "session_id": "codex-human-objective",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": "已处理",
    }

    message = module.format_stop_message(payload, "codex")

    assert "> 请处理 <objective>这个人工请求</objective>" in message


def test_codex_stop_message_survives_torn_utf8_jsonl(tmp_path, monkeypatch):
    transcript = tmp_path / "codex-broken.jsonl"
    valid_user = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "继续发送健康报告"}],
        },
    }, ensure_ascii=False).encode("utf-8")
    # This reproduces a provider line split in the middle of a Chinese UTF-8
    # sequence. The malformed line is transport noise; the later user row is
    # still usable for the notification body.
    torn_line = (
        b'{"type":"response_item","payload":{"type":"message",'
        b'"role":"assistant","content":[{"type":"text","text":"\xe7\n'
        b'broken"}]}}\n'
    )
    transcript.write_bytes(torn_line + valid_user + b"\n")
    module = load_send_module()
    payload = {
        "session_id": "codex-broken",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "last_assistant_message": "已完成",
    }
    message = module.format_stop_message(payload, "codex")
    assert "> 继续发送健康报告" in message


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
