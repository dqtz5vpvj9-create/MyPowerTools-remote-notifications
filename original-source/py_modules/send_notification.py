#!/usr/bin/env python3
"""Send a notification to the notification server. Used by agent hooks."""
import argparse
import hashlib
import json
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_modules.simple_http_notification_conf import cloud_server_protocol, cloud_server_ip, cloud_server_port
from py_modules.simple_http_notification_sender import SimpleHttpNotificationSender
from py_modules.logging_lib import setup_logging


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
    return "Claude Code"


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


def get_session_name(session_id: str, client: str = "claude", transcript_path: str = "") -> str:
    """Look up the session name when the client stores session metadata."""
    if client == "codex":
        return _codex_thread_name(session_id)
    if client == "dsh":
        return _dsh_session_name(session_id, transcript_path)
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


def label_for_payload(data: dict, client: str) -> str:
    session_id = data.get("session_id", "")
    session_name = get_session_name(session_id, client, data.get("transcript_path", ""))
    cwd = data.get("cwd", "")
    return session_name or os.path.basename(cwd) or client_name(client)


def format_stop_message(data: dict, client: str = "claude") -> str:
    """Format a Stop hook payload.
    Input fields: session_id, transcript_path, cwd, permission_mode,
                  hook_event_name, stop_hook_active, last_assistant_message
    """
    label = label_for_payload(data, client)

    last_msg = data.get("last_assistant_message", "")
    if not last_msg and client == "dsh":
        last_msg = _dsh_last_assistant_message(data.get("transcript_path", ""))
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
    parser.add_argument('--client', default='claude', choices=['claude', 'codex', 'dsh'],
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

    if args.stdin:
        try:
            raw = sys.stdin.read()
            if not raw.strip():
                data = {}
            else:
                data = json.loads(raw)
        except Exception:
            data = {}

        if hook == 'stop':
            message = format_stop_message(data, args.client)
        elif hook in ('notification', 'permission'):
            message = format_notification_message(data, args.client)
            if not message:
                return
        elif hook == 'plan':
            message = format_plan_message(data, args.client)
        else:
            message = data.get('message', data.get('title', str(data)))
    elif args.message:
        message = args.message
    else:
        parser.error('message is required unless --stdin is used')
        return

    session_id = str(data.get("session_id") or "") if args.stdin else ""
    session_name = (
        get_session_name(session_id, args.client, data.get("transcript_path", ""))
        if session_id else ""
    )

    # Debug: log what we're sending
    debug_log = f"/tmp/{args.client}_hook_debug.log"
    with open(debug_log, 'w') as f:
        f.write(f"hook={args.hook}\n")
        if args.stdin:
            f.write(f"raw_stdin_len={len(raw)}\n")
            f.write(f"raw_stdin={raw[:500]}\n")
            f.write(f"data_keys={list(data.keys()) if data else 'empty'}\n")
        f.write(f"message={message}\n")
        f.write(f"timestamp={timestamp}\n")

    client_msg_id = args.client_msg_id or stable_client_msg_id(
        raw_stdin=raw if args.stdin else "",
        data=data if args.stdin else {},
        message=message,
        hook=hook,
        client=args.client,
        channel=args.channel,
        icon=args.icon,
    )
    notif_id = args.notif_id or client_msg_id

    logger = setup_logging()
    sender = SimpleHttpNotificationSender(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)
    http_timeout = float(os.environ.get("ANDROIDTOOLS_NOTIFY_HTTP_TIMEOUT", "10"))
    sender.send(
        args.channel,
        message,
        args.icon,
        notif_id=notif_id,
        client_msg_id=client_msg_id,
        timestamp=timestamp,
        session_id=session_id,
        session_name=session_name,
        source_client=args.client,
        schema_version=2,
        timeout=http_timeout,
    )
    with open(debug_log, 'a') as f:
        f.write(f"client_msg_id={client_msg_id}\n")
        f.write(f"notif_id={notif_id}\n")

    # Also send FCM push directly from this machine (server may not have Google connectivity)
    try:
        from py_modules.fcm_push import send_fcm_push
        fcm_result = send_fcm_push(
            args.channel,
            message,
            args.icon,
            timestamp=timestamp,
            notif_id=notif_id,
            session_id=session_id,
            session_name=session_name,
            source_client=args.client,
        )
        with open(debug_log, 'a') as f:
            f.write(f"fcm_result={json.dumps(fcm_result, sort_keys=True)}\n")
    except Exception as e:
        with open(debug_log, 'a') as f:
            f.write(f"fcm_error={type(e).__name__}: {str(e)[:240]}\n")

if __name__ == '__main__':
    main()
