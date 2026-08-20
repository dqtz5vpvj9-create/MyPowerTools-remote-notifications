#!/usr/bin/env python3
"""Send a notification to the notification server. Used by agent hooks."""
import argparse
import hashlib
import json
import re
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_modules.simple_http_notification_conf import cloud_server_protocol, cloud_server_ip, cloud_server_port
from py_modules.simple_http_notification_sender import SimpleHttpNotificationSender
from py_modules.logging_lib import setup_logging
from py_modules.claude_stop_guard import (
    claim_stop as claim_claude_stop,
    commit_claim as commit_claude_stop,
    inspect_stop as inspect_claude_stop,
    release_claim as release_claude_stop,
)


CLAUDE_TASK_LABEL = "Claude Task"


def _canonical_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_client_msg_id(
    *,
    raw_stdin: str,
    data: dict,
    message: str,
    hook: str | None,
    client: str,
    channel: str,
    icon: str,
) -> str:
    identity = {
        "source": "codex-hook",
        "client": client,
        "hook": hook or "",
        "channel": channel,
        "icon": icon,
    }
    if raw_stdin:
        identity["payload"] = data if data else raw_stdin
    else:
        identity["message"] = message
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:32]


def client_name(client: str) -> str:
    if client == "codex":
        return "Codex"
    if client == "dsh":
        return "DeepSeek Harness"
    if client == "cursor":
        return "Cursor"
    return "Claude Code"


def _is_cursor_payload(data: dict) -> bool:
    if data.get("cursor_version"):
        return True
    return isinstance(data.get("workspace_roots"), list) and bool(data.get("conversation_id"))


def _content_text_parts(content) -> list[str]:
    if not content:
        return []
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, dict):
        text = content.get("text") or content.get("input_text")
        return [str(text)] if text else []
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text", "input_text"):
                text = block.get("text") or block.get("input_text")
                if text:
                    parts.append(str(text))
    return parts


def _event_text(event: dict) -> str:
    message = event.get("message")
    if isinstance(message, str):
        return message.strip()
    content = None
    if isinstance(message, dict):
        content = message.get("content")
        if not content:
            content = message.get("text")
    if content is None:
        content = event.get("content") or event.get("text")
    return "\n".join(_content_text_parts(content)).strip()


def _last_role_text(lines: list, roles: set[str]) -> str:
    last = ""
    for event in lines:
        role = str(event.get("role") or event.get("type") or "")
        if role not in roles:
            continue
        text = _event_text(event)
        if text:
            last = text
    return last


def _clean_user_request(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, flags=re.S)
    if match:
        return match.group(1).strip()
    cleaned = re.sub(r"<timestamp>.*?</timestamp>", "", text, flags=re.S)
    return cleaned.strip()


def _cursor_workspace_name(data: dict) -> str:
    roots = data.get("workspace_roots") or []
    if roots:
        return os.path.basename(str(roots[0]).rstrip("\\/"))
    cwd = data.get("cwd") or ""
    if cwd:
        return os.path.basename(str(cwd).rstrip("\\/"))
    return ""


def _cursor_fallback_transcript_path(data: dict) -> str:
    conversation_id = str(data.get("conversation_id") or data.get("session_id") or "")
    if not conversation_id:
        return ""
    projects = os.path.expanduser("~/.cursor/projects")
    if not os.path.isdir(projects):
        return ""
    import glob
    patterns = [
        os.path.join(projects, "*", "agent-transcripts", conversation_id, f"{conversation_id}.jsonl"),
        os.path.join(projects, "*", "agent-transcripts", f"{conversation_id}.jsonl"),
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    if not matches:
        return ""
    return max(matches, key=os.path.getmtime)


def _cursor_transcript_lines(data: dict) -> list:
    path = str(data.get("transcript_path") or "")
    lines = _read_transcript_lines(path)
    if lines:
        return lines
    return _read_transcript_lines(_cursor_fallback_transcript_path(data))


def _cursor_session_name(data: dict, transcript_path: str = "") -> str:
    path = transcript_path or str(data.get("transcript_path") or "") or _cursor_fallback_transcript_path(data)
    for event in _read_transcript_lines(path):
        if event.get("type") == "session/title":
            title = (event.get("data") or {}).get("title")
            if title:
                return str(title)
        if event.get("title"):
            return str(event.get("title"))
    return _cursor_workspace_name(data)


def _dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _read_transcript_lines(transcript_path: str) -> list:
    if not transcript_path:
        return []
    try:
        if transcript_path.endswith(".zstd"):
            import zstandard
            with open(transcript_path, "rb") as fh:
                raw = zstandard.ZstdDecompressor().stream_reader(fh).read()
            text = raw.decode("utf-8", errors="replace")
        else:
            with open(transcript_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    except Exception:
        return []
    lines = []
    for line in text.splitlines():
        try:
            lines.append(json.loads(line))
        except Exception:
            continue
    return lines


def _dsh_session_name(session_id: str, transcript_path: str = "") -> str:
    """Resolve a DSH session title from the projection cache, then transcript."""
    if not session_id:
        return ""
    cache_path = os.path.join(_dsh_home(), "storages", "session_projcache.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
        row = (cache.get("tables", {}).get("sessions", {}).get(session_id) or {}).get("rows") or {}
        title = (row.get("title") or {}).get("val")
        if title:
            return str(title)
    except Exception:
        pass
    title = ""
    for event in _read_transcript_lines(transcript_path):
        if event.get("type") == "session/title":
            candidate = (event.get("data") or {}).get("title")
            if candidate:
                title = str(candidate)
    return title


def _dsh_last_assistant_message(transcript_path: str) -> str:
    """Return the last assistant text message from a DSH transcript."""
    last = ""
    for event in _read_transcript_lines(transcript_path):
        if event.get("type") != "assistant/message":
            continue
        message = (event.get("data") or {}).get("message") or {}
        parts = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(str(text))
        if parts:
            last = "\n".join(parts).strip()
    return last


def get_session_name(session_id: str, client: str = "claude", transcript_path: str = "", data: dict | None = None) -> str:
    """Look up the session name when the client stores session metadata."""
    if client == "codex":
        return _codex_thread_name(session_id)
    if client == "dsh":
        return _dsh_session_name(session_id, transcript_path)
    if client == "cursor":
        return _cursor_session_name(data or {}, transcript_path)
    if client != "claude":
        return ""
    import glob
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    for f in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(f) as fh:
                meta = json.load(fh)
            if meta.get("sessionId") == session_id:
                return meta.get("name", "")
        except Exception:
            continue
    return ""


def _codex_thread_name(session_id: str) -> str:
    """Latest thread_name from ~/.codex/session_index.jsonl matching session_id.

    Codex appends one record per /rename; later records override earlier ones,
    so we keep the last match in file order.
    """
    if not session_id:
        return ""
    path = os.path.expanduser("~/.codex/session_index.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    name = ""
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("id") == session_id:
            candidate = record.get("thread_name") or ""
            if candidate:
                name = candidate
    return name


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _codex_last_user_message(session_id: str, transcript_path: str = "") -> str:
    """Latest user prompt from the Codex rollout for a session."""
    if not session_id and not transcript_path:
        return ""
    if transcript_path:
        path = transcript_path
    else:
        import glob
        pattern = os.path.join(_codex_home(), "sessions", "**", f"rollout-*-{session_id}.jsonl")
        matches = glob.glob(pattern, recursive=True)
        if not matches:
            return ""
        path = max(matches, key=os.path.getmtime)
    last = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("type") != "response_item":
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                parts = []
                for block in payload.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                        text = block.get("text")
                        if text:
                            parts.append(str(text))
                if parts:
                    last = "\n".join(parts).strip()
    except OSError:
        return ""
    return last


def _claude_entry_text(entry: dict) -> str:
    """Text of one Claude transcript entry, mirroring agentsview's
    ExtractTextContent: message.content text blocks first, then flat
    message/text/body fallbacks."""
    message = entry.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    if isinstance(message, dict):
        content = message.get("content")
        parts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in (None, "text", "input_text"):
                    text = block.get("text") or block.get("input_text")
                    if text:
                        parts.append(str(text))
                elif isinstance(block, str) and block.strip():
                    parts.append(block)
        elif isinstance(content, str) and content.strip():
            parts.append(content)
        if parts:
            return "\n".join(parts).strip()
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    for key in ("text", "body", "content"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tail_transcript_entries(transcript_path: str, max_lines: int = 512) -> list:
    """Last JSONL entries of a Claude transcript, read from the tail so a
    Stop hook never pays for a full re-read of a long session."""
    if not transcript_path:
        return []
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            buf = b""
            while end > 0 and buf.count(b"\n") <= max_lines:
                chunk = min(4096, end)
                end -= chunk
                fh.seek(end)
                buf = fh.read(chunk) + buf
    except OSError:
        return []
    entries = []
    for line in buf.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _claude_last_user_entry(data: dict) -> dict | None:
    """Read the latest declared user record when the hook has no event UUID.

    Production Stop hooks carry an accepted event UUID and use the anchored
    parent-chain path. This compatibility path still relies on Claude's
    explicit metadata; it never examines the record text.
    """
    for entry in reversed(_tail_transcript_entries(data.get("transcript_path") or "")):
        if entry.get("type") == "user":
            return entry
    return None


def _claude_transcript_index(data: dict, max_lines: int = 512) -> dict[str, dict]:
    """Index the recent Claude JSONL entries by their explicit UUID.

    Claude's transcript is the source of origin metadata.  This index is only
    used for ancestry traversal; message text never participates in the
    automated/human decision.
    """
    path = str(data.get("transcript_path") or "")
    entries = _tail_transcript_entries(path, max_lines=max_lines)
    indexed: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uuid = str(entry.get("uuid") or entry.get("id") or "").strip()
        if uuid:
            indexed[uuid] = entry
    return indexed


def _claude_parent_uuid(entry: dict) -> str:
    return str(
        entry.get("parentUuid")
        or entry.get("parent_uuid")
        or entry.get("parentId")
        or entry.get("parent_id")
        or ""
    ).strip()


def _claude_ancestry_result(entries: dict[str, dict], event_uuid: str) -> tuple[bool, bool, str]:
    """Return (complete, synthetic, human_request) for one UUID chain."""
    current = entries.get(event_uuid)
    if current is None:
        return False, False, ""
    visited: set[str] = set()
    for _ in range(512):
        parent_uuid = _claude_parent_uuid(current)
        if not parent_uuid or parent_uuid in visited:
            return True, False, ""
        visited.add(parent_uuid)
        parent = entries.get(parent_uuid)
        if parent is None:
            return False, False, ""
        if _is_explicit_claude_automatic_entry(parent):
            return True, True, ""
        if str(parent.get("type") or "").strip().lower() == "user":
            return True, False, _claude_entry_text(parent).strip()
        current = parent
    return True, False, ""


def _is_explicit_claude_automatic_entry(entry: dict) -> bool:
    """Recognize only provider-declared synthetic transcript entries."""
    if _has_synthetic_claude_origin(entry):
        return True
    if str(entry.get("type") or "").strip().lower() != "system":
        return False
    subtype = str(entry.get("subtype") or "").strip().lower()
    return subtype in {
        "scheduled_task_fire",
        "task_notification",
        "monitor",
        "background",
        "auto",
    }


def _claude_stop_has_synthetic_ancestor(data: dict, snapshot=None) -> bool:
    """Follow the accepted assistant event's UUID ancestry to its origin.

    AgentsView uses the same explicit Claude fields (`isMeta`, `promptSource`,
    and `origin.kind`).  A missing UUID or an incomplete tail fails open so a
    normal human reply remains visible; no report text is inspected.
    """
    event_uuid = str(
        getattr(snapshot, "event_uuid", "")
        or data.get("_mpt_claude_event_uuid")
        or data.get("event_uuid")
        or data.get("eventUuid")
        or ""
    ).strip()
    if not event_uuid:
        return False
    for max_lines in (512, 2048, 8192):
        complete, synthetic, _ = _claude_ancestry_result(
            _claude_transcript_index(data, max_lines=max_lines), event_uuid
        )
        if complete:
            return synthetic
    return False


def _claude_parent_user_request(data: dict) -> str | None:
    """Return the explicit human prompt attached to the accepted event.

    ``None`` means the transcript could not be indexed.  An empty string means
    the ancestry is known and has no human prompt, so callers must not fall
    back to a later synthetic user entry.
    """
    event_uuid = str(
        data.get("_mpt_claude_event_uuid")
        or data.get("event_uuid")
        or data.get("eventUuid")
        or ""
    ).strip()
    if not event_uuid:
        return None
    for max_lines in (512, 2048, 8192):
        complete, synthetic, request = _claude_ancestry_result(
            _claude_transcript_index(data, max_lines=max_lines), event_uuid
        )
        if complete:
            return "" if synthetic else request
    return ""


def _has_synthetic_claude_origin(entry: dict) -> bool:
    if entry.get("isMeta") is True or str(entry.get("isMeta") or "").lower() in ("true", "1"):
        return True
    for origin_key in ("origin", "user_origin"):
        origin = entry.get(origin_key)
        if isinstance(origin, dict):
            kind = str(origin.get("kind") or "").strip().lower()
            if kind in {"task-notification", "system", "monitor", "background", "auto"}:
                return True
    prompt_source = str(entry.get("promptSource") or entry.get("prompt_source") or "").strip().lower()
    return prompt_source in {"system", "task-notification", "monitor", "background", "auto"}


def is_claude_task_notification(data: dict, snapshot=None) -> bool:
    """True only when Claude declares an automatic/system-origin turn."""
    if _has_synthetic_claude_origin(data):
        return True
    entry_type = str(data.get("type") or "").strip()
    if entry_type and entry_type not in ("user", "assistant"):
        return True
    if _claude_stop_has_synthetic_ancestor(data, snapshot):
        return True
    event_uuid = str(
        getattr(snapshot, "event_uuid", "")
        or data.get("_mpt_claude_event_uuid")
        or data.get("event_uuid")
        or data.get("eventUuid")
        or ""
    ).strip()
    if event_uuid:
        return False
    entry = _claude_last_user_entry(data)
    return entry is not None and _has_synthetic_claude_origin(entry)


def label_for_payload(data: dict, client: str) -> str:
    if client == "claude" and is_claude_task_notification(data):
        return CLAUDE_TASK_LABEL
    session_id = data.get("session_id") or data.get("conversation_id") or ""
    session_name = get_session_name(session_id, client, data.get("transcript_path", ""), data)
    cwd = data.get("cwd") or ""
    if not cwd:
        roots = data.get("workspace_roots") or []
        cwd = roots[0] if roots else ""
    return session_name or os.path.basename(str(cwd).rstrip("\\/")) or client_name(client)


def _dsh_last_user_message(transcript_path: str) -> str:
    """Last user prompt from a DSH transcript."""
    last = ""
    for event in _read_transcript_lines(transcript_path):
        if event.get("type") != "user/message":
            continue
        data = event.get("data") or {}
        source = data.get("source") or {}
        if source.get("kind") not in (None, "user"):
            continue
        parts = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(str(text))
        if parts:
            last = "\n".join(parts).strip()
    return last


def _quote_request(request: str) -> str:
    return "\n".join(f"> {line}" for line in request.splitlines())


def format_stop_message(data: dict, client: str = "claude") -> str:
    """Format a Stop hook payload.
    Input fields: session_id, transcript_path, cwd, permission_mode,
                  hook_event_name, stop_hook_active, last_assistant_message
    """
    label = label_for_payload(data, client)
    claude_task = label == CLAUDE_TASK_LABEL

    last_msg = ""
    if client == "claude":
        # A Stop hook payload can retain an older assistant sentence after the
        # transcript has moved on to thinking/tool_use.  The guard places the
        # accepted transcript text in this private field for the exact event
        # that was claimed by main().
        accepted = data.get("_mpt_claude_visible_text")
        if isinstance(accepted, str):
            last_msg = accepted.strip()
        else:
            snapshot = inspect_claude_stop(data)
            if snapshot is not None and snapshot.is_visible_completion:
                data["_mpt_claude_event_uuid"] = snapshot.event_uuid
                last_msg = snapshot.text.strip()
    if not last_msg:
        last_msg = data.get("last_assistant_message") or data.get("text") or ""
        if not isinstance(last_msg, str):
            last_msg = ""
        last_msg = last_msg.strip()
    if not last_msg and client == "dsh":
        last_msg = _dsh_last_assistant_message(data.get("transcript_path", ""))
    if not last_msg and client in ("cursor", "claude"):
        last_msg = _last_role_text(_cursor_transcript_lines(data), {"assistant", "assistant/message"})
    request = data.get("user_prompt") or data.get("prompt") or ""
    if not request:
        if client == "dsh":
            request = _dsh_last_user_message(data.get("transcript_path", ""))
        elif client == "codex":
            request = _codex_last_user_message(
                data.get("session_id", ""), data.get("transcript_path", "")
            )
        elif client == "claude":
            request = _claude_parent_user_request(data)
            if request is None:
                request = _last_role_text(_cursor_transcript_lines(data), {"user", "user/message"})
        elif client == "cursor":
            request = _last_role_text(_cursor_transcript_lines(data), {"user", "user/message"})
    request = _clean_user_request(str(request).strip())
    if request and not claude_task:
        quote = _quote_request(request)
        body = f"{quote}\n\n[{label}] {last_msg or 'Task completed'}"
        return body
    if last_msg:
        return f"[{label}] {last_msg}"
    return f"[{label}] Task completed"


def format_plan_message(data: dict, client: str = "claude") -> str:
    """Format an ExitPlanMode PreToolUse payload.
    Plan mode doesn't fire the Notification hook — Claude calls the
    ExitPlanMode tool with the plan in tool_input.plan and waits for
    user approval. We surface the full plan so the user can decide
    from the phone without opening the laptop."""
    label = label_for_payload(data, client)
    tool_input = data.get("tool_input") or {}
    plan = (tool_input.get("plan") or "").strip()
    body = f"Plan ready for approval:\n{plan}" if plan else "Plan ready for approval"
    return f"[{label}] {body}"


def format_notification_message(data: dict, client: str = "claude") -> str:
    """Format a Notification hook payload.
    Input fields: session_id, transcript_path, cwd, hook_event_name,
                  message, title, notification_type
    """
    label = label_for_payload(data, client)

    title = data.get("title", "")
    message = data.get("message", "")
    body = f"{title}: {message}" if title and message else (message or title or "")

    if not body and data.get("hook_event_name") == "PermissionRequest":
        tool_name = data.get("tool_name") or ""
        tool_input = data.get("tool_input") or {}
        description = tool_input.get("description") or ""
        command = tool_input.get("command") or ""
        detail = description or command
        if tool_name and detail:
            detail = f"{tool_name}: {detail}"
        elif tool_name:
            detail = tool_name
        body = f"Permission request: {detail}" if detail else "Permission request"

    if not body:
        return ""
    return f"[{label}] {body}"


def normalize_hook_name(hook: str | None) -> str | None:
    if not hook:
        return None
    normalized = hook.strip().replace("_", "-").lower()
    aliases = {
        "stop": "stop",
        "notification": "notification",
        "permission": "permission",
        "permissionrequest": "permission",
        "permission-request": "permission",
        "plan": "plan",
        "exitplanmode": "plan",
        "exit-plan-mode": "plan",
        "afteragentresponse": "stop",
        "agentresponse": "stop",
        "agent-response": "stop",
    }
    return aliases.get(normalized, normalized)


def normalize_timestamp(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    text = str(value).strip()
    try:
        return datetime.fromtimestamp(float(text), timezone.utc).isoformat()
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc).isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description='Send a notification')
    parser.add_argument('message', nargs='?', default=None, help='Notification message')
    parser.add_argument('--channel', default='default', help='Channel name (default: default)')
    parser.add_argument('--icon', default='info', help='Icon name (default: info)')
    parser.add_argument('--client', default='claude', choices=['claude', 'codex', 'dsh', 'cursor'],
                        help='Hook client for payload formatting')
    parser.add_argument('--stdin', action='store_true', help='Read message from stdin JSON from an agent hook')
    parser.add_argument('--hook', default=None,
                        help='Hook type for smarter formatting')
    parser.add_argument('--timestamp', default=None,
                        help='Canonical notification event timestamp; async hooks pass queue time')
    parser.add_argument('--client-msg-id', default=None)
    parser.add_argument('--notif-id', default=None)
    args = parser.parse_args()
    hook = normalize_hook_name(args.hook)
    timestamp = normalize_timestamp(args.timestamp)
    data = {}
    raw = ""
    client = args.client
    claude_claim = None

    if args.stdin:
        try:
            raw = sys.stdin.read()
            if not raw.strip():
                data = {}
            else:
                data = json.loads(raw)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        if _is_cursor_payload(data):
            client = "cursor"

        if hook == 'stop' and client == "claude":
            claude_claim = claim_claude_stop(data)
            if claude_claim is None:
                # Intermediate tool turns, thinking-only turns, incomplete
                # transcript lines, and repeated event identities stay out of
                # the notification stream.
                return
            data["_mpt_claude_event_uuid"] = claude_claim.snapshot.event_uuid
            if is_claude_task_notification(data, claude_claim.snapshot):
                # Claude emits Stop hooks for automatic turns as well as for
                # real human conversations. System/task-notification turns
                # are background progress and must stay out of the stream;
                # human-origin turns continue through the normal route.
                release_claude_stop(claude_claim)
                return
            data["_mpt_claude_visible_text"] = claude_claim.snapshot.text

        if hook == 'stop':
            message = format_stop_message(data, client)
        elif hook in ('notification', 'permission'):
            message = format_notification_message(data, client)
            if not message:
                return
        elif hook == 'plan':
            message = format_plan_message(data, client)
        else:
            message = data.get('message', data.get('title', str(data)))

    elif args.message:
        message = args.message
    else:
        parser.error('message is required unless --stdin is used')
        return

    icon = args.icon
    if client == "cursor" and icon in ("info", "claude"):
        icon = "cursor"

    session_id = str(data.get("session_id") or data.get("conversation_id") or "") if args.stdin else ""
    session_name = (
        get_session_name(session_id, client, data.get("transcript_path", ""), data)
        if args.stdin else ""
    )

    # Debug: log what we're sending
    debug_log = f"/tmp/{client}_hook_debug.log"
    try:
        with open(debug_log, 'w') as f:
            f.write(f"hook={args.hook}\n")
            f.write(f"client={client}\n")
            if args.stdin:
                f.write(f"raw_stdin_len={len(raw)}\n")
                f.write(f"raw_stdin={raw[:500]}\n")
                f.write(f"data_keys={list(data.keys()) if data else 'empty'}\n")
            f.write(f"message={message}\n")
            f.write(f"timestamp={timestamp}\n")
    except OSError:
        debug_log = ""

    client_msg_id = args.client_msg_id or stable_client_msg_id(
        raw_stdin=raw if args.stdin else "",
        data=data if args.stdin else {},
        message=message,
        hook=hook,
        client=client,
        channel=args.channel,
        icon=icon,
    )
    notif_id = args.notif_id or client_msg_id
    source_event_id = ""
    source_message_id = ""
    content_kind = ""
    stop_reason = ""
    if claude_claim is not None:
        source_event_id = claude_claim.snapshot.event_uuid
        source_message_id = claude_claim.snapshot.message_id
        content_kind = "text"
        stop_reason = claude_claim.snapshot.stop_reason

    logger = setup_logging()
    sender = SimpleHttpNotificationSender(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)
    http_timeout = float(os.environ.get("ANDROIDTOOLS_NOTIFY_HTTP_TIMEOUT", "10"))
    try:
        sender.send(
            args.channel,
            message,
            icon,
            notif_id=notif_id,
            client_msg_id=client_msg_id,
            timestamp=timestamp,
            session_id=session_id,
            session_name=session_name,
            source_client=client,
            source_event_id=source_event_id,
            source_message_id=source_message_id,
            content_kind=content_kind,
            stop_reason=stop_reason,
            schema_version=2,
            timeout=http_timeout,
        )
    except Exception:
        if claude_claim is not None:
            release_claude_stop(claude_claim)
        raise
    else:
        if claude_claim is not None:
            commit_claude_stop(claude_claim)
    if debug_log:
        try:
            with open(debug_log, 'a') as f:
                f.write(f"client_msg_id={client_msg_id}\n")
                f.write(f"notif_id={notif_id}\n")
        except OSError:
            pass

    # Also send FCM push directly from this machine (server may not have Google connectivity)
    try:
        from py_modules.fcm_push import send_fcm_push
        fcm_result = send_fcm_push(
            args.channel,
            message,
            icon,
            timestamp=timestamp,
            notif_id=notif_id,
            session_id=session_id,
            session_name=session_name,
            source_client=client,
            source_event_id=source_event_id,
            source_message_id=source_message_id,
            content_kind=content_kind,
            stop_reason=stop_reason,
        )
        if debug_log:
            with open(debug_log, 'a') as f:
                f.write(f"fcm_result={json.dumps(fcm_result, sort_keys=True)}\n")
    except Exception as e:
        if debug_log:
            try:
                with open(debug_log, 'a') as f:
                    f.write(f"fcm_error={type(e).__name__}: {str(e)[:240]}\n")
            except OSError:
                pass

if __name__ == '__main__':
    main()
