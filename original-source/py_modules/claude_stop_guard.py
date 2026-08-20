"""Transcript-backed guard for Claude Stop hook notifications.

Claude invokes Stop for intermediate tool turns as well as for a completed
assistant turn.  The hook payload may keep an older ``last_assistant_message``
around, so using that field alone can replay a previous visible sentence while
the latest transcript entry contains only thinking or tool_use blocks.

This module keeps a small per-session cursor and the last assistant snapshot.
It accepts a Stop only when the latest complete transcript entry contains a
visible text block and has reached a terminal stop reason.  A message UUID and
the provider message id form the event identity; repeated hook invocations are
claimed once and then suppressed.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_STATE_LOCK = threading.RLock()
_MAX_INITIAL_READ_BYTES = 8 * 1024 * 1024
_MAX_SESSION_STATES = 64
_CLAIM_TTL_SECONDS = 120.0
_TERMINAL_STOP_REASONS = {"", "end_turn", "stop", "completed", "success"}
_TOOL_KINDS = {"tool_use", "tool_result", "server_tool_use"}
_TEXT_KINDS = {"text", "input_text"}
_THINKING_KINDS = {"thinking", "redacted_thinking", "reasoning"}


class ClaudeStopSnapshot:
    __slots__ = (
        "state_key", "transcript_path", "session_id", "event_uuid",
        "message_id", "text", "stop_reason", "content_kinds", "source",
    )

    def __init__(
        self,
        state_key: str,
        transcript_path: str,
        session_id: str,
        event_uuid: str,
        message_id: str,
        text: str,
        stop_reason: str,
        content_kinds: tuple[str, ...],
        source: str = "transcript",
    ) -> None:
        self.state_key = state_key
        self.transcript_path = transcript_path
        self.session_id = session_id
        self.event_uuid = event_uuid
        self.message_id = message_id
        self.text = text
        self.stop_reason = stop_reason
        self.content_kinds = content_kinds
        self.source = source

    @property
    def identity(self) -> str:
        event = self.event_uuid or self.message_id
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:20]
        return "|".join((event, self.message_id, digest))

    @property
    def is_visible_completion(self) -> bool:
        return bool(self.text.strip()) and self.stop_reason in _TERMINAL_STOP_REASONS and not (
            set(self.content_kinds) & _TOOL_KINDS
        )


class ClaudeStopClaim:
    __slots__ = ("snapshot", "token")

    def __init__(self, snapshot: ClaudeStopSnapshot, token: str) -> None:
        self.snapshot = snapshot
        self.token = token


def _state_path() -> Path:
    explicit = os.environ.get("ANDROIDTOOLS_CLAUDE_STOP_STATE")
    if explicit:
        return Path(explicit).expanduser()
    root = Path(os.environ.get(
        "ANDROIDTOOLS_NOTIFY_QUEUE_DIR",
        "~/.cache/androidtools/notification_queue",
    )).expanduser()
    return root / "claude_stop_state.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _message_dict(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = _message_dict(entry).get("content")
    if isinstance(content, dict):
        return [content]
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _classify_entry(entry: dict[str, Any]) -> dict[str, Any]:
    blocks = _content_blocks(entry)
    text_parts: list[str] = []
    normalized_blocks: list[dict[str, Any]] = []
    kinds: list[str] = []
    for index, block in enumerate(blocks):
        kind = str(block.get("type") or "").strip().lower()
        if not kind:
            kind = "text" if block.get("text") else "unknown"
        if kind not in kinds:
            kinds.append(kind)
        value = block.get("text") or block.get("input_text")
        if kind in _TEXT_KINDS and isinstance(value, str) and value.strip():
            text_parts.append(value.strip())
        normalized_blocks.append({
            "index": index,
            "type": kind,
            "id": str(block.get("id") or block.get("tool_use_id") or ""),
            "text": str(value) if isinstance(value, str) else "",
        })

    message = _message_dict(entry)
    stop_reason = str(
        message.get("stop_reason")
        or entry.get("stop_reason")
        or ""
    ).strip().lower()
    return {
        "uuid": str(entry.get("uuid") or ""),
        "message_id": str(message.get("id") or entry.get("message_id") or ""),
        "text": "\n".join(text_parts).strip(),
        "stop_reason": stop_reason,
        "kinds": kinds,
        "blocks": normalized_blocks,
    }


def _merge_text(previous: str, current: str) -> str:
    previous = previous or ""
    current = current or ""
    if not previous:
        return current
    if not current:
        return previous
    if current.startswith(previous):
        return current
    if previous.startswith(current):
        return previous
    if current == previous:
        return previous
    return f"{previous}\n{current}"


def _merge_snapshot(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Merge cumulative Claude snapshots sharing one provider message id.

    Text blocks use prefix replacement and tool blocks use their stable id (or
    position) so a streaming snapshot never creates a second visible copy.
    """
    if not previous or previous.get("message_id") != current.get("message_id"):
        return current
    old_blocks = previous.get("blocks") or []
    new_blocks = current.get("blocks") or []
    merged = [dict(block) for block in old_blocks]
    for block in new_blocks:
        block_id = (block.get("type"), block.get("id"), block.get("index"))
        match = next(
            (candidate for candidate in merged
             if (candidate.get("type"), candidate.get("id"), candidate.get("index")) == block_id),
            None,
        )
        if match is None:
            merged.append(dict(block))
        else:
            match["text"] = _merge_text(str(match.get("text") or ""), str(block.get("text") or ""))
    text = "\n".join(
        str(block.get("text") or "").strip()
        for block in merged
        if block.get("type") in _TEXT_KINDS and str(block.get("text") or "").strip()
    ).strip()
    kinds: list[str] = []
    for block in merged:
        kind = str(block.get("type") or "")
        if kind and kind not in kinds:
            kinds.append(kind)
    result = dict(current)
    result["blocks"] = merged
    result["text"] = text
    result["kinds"] = kinds
    return result


def _read_complete_entries(path: Path, offset: int, initial: bool) -> tuple[list[dict[str, Any]], int, int]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, 0

    if initial:
        start = max(0, size - _MAX_INITIAL_READ_BYTES)
    elif offset > size:
        start = max(0, size - _MAX_INITIAL_READ_BYTES)
    else:
        start = offset

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            if start > 0:
                handle.seek(start - 1)
                previous = handle.read(1)
                handle.seek(start)
            else:
                previous = b"\n"
            if start > 0 and previous not in (b"\n", b"\r"):
                handle.readline()  # discard a possible partial first line
                start = handle.tell()
            raw = handle.read()
    except OSError:
        return [], offset, size

    entries: list[dict[str, Any]] = []
    consumed = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith((b"\n", b"\r")):
            break  # keep the partial JSON line for the next invocation
        consumed += len(line)
        try:
            value = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries, start + consumed, size


def _session_key(data: dict[str, Any], transcript_path: str) -> str:
    session_id = str(data.get("session_id") or data.get("sessionId") or "")
    return f"{session_id}|{transcript_path}" if session_id else transcript_path


def _snapshot_from_summary(state_key: str, transcript_path: str, session_id: str, summary: dict[str, Any]) -> ClaudeStopSnapshot:
    return ClaudeStopSnapshot(
        state_key=state_key,
        transcript_path=transcript_path,
        session_id=session_id,
        event_uuid=str(summary.get("uuid") or ""),
        message_id=str(summary.get("message_id") or ""),
        text=str(summary.get("text") or "").strip(),
        stop_reason=str(summary.get("stop_reason") or "").strip().lower(),
        content_kinds=tuple(str(kind) for kind in summary.get("kinds") or []),
    )


def inspect_stop(data: dict[str, Any]) -> ClaudeStopSnapshot | None:
    """Read and classify the latest Claude assistant event.

    The function updates the cursor even when the latest event is thinking or
    tool_use.  Returning ``None`` for an unreadable transcript preserves the
    legacy payload path; a readable transcript always wins over stale fields.
    """
    transcript_path = str(data.get("transcript_path") or "")
    session_id = str(data.get("session_id") or data.get("sessionId") or "")
    path = Path(transcript_path).expanduser() if transcript_path else None
    if path is None or not path.exists() or not path.is_file():
        return _legacy_snapshot(data)

    state_file = _state_path()
    with _STATE_LOCK:
        state = _read_state(state_file)
        sessions = state.setdefault("sessions", {})
        key = _session_key(data, transcript_path)
        entry_state = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
        try:
            size = path.stat().st_size
        except OSError:
            return _legacy_snapshot(data)
        old_offset = int(entry_state.get("offset", 0) or 0)
        initial = not entry_state or old_offset <= 0
        entries, new_offset, _ = _read_complete_entries(path, old_offset, initial)
        latest = entry_state.get("last_assistant") if isinstance(entry_state.get("last_assistant"), dict) else None
        for entry in entries:
            if str(entry.get("type") or "") != "assistant":
                continue
            current = _classify_entry(entry)
            latest = _merge_snapshot(latest, current)

        entry_state["offset"] = new_offset
        entry_state["file_size"] = size
        entry_state["updated_at"] = time.time()
        if latest is not None:
            entry_state["last_assistant"] = latest
        sessions[key] = entry_state
        if len(sessions) > _MAX_SESSION_STATES:
            oldest = sorted(
                sessions.items(),
                key=lambda pair: float((pair[1] or {}).get("updated_at", 0.0)),
            )
            for old_key, _ in oldest[:len(sessions) - _MAX_SESSION_STATES]:
                sessions.pop(old_key, None)
        _write_state(state_file, state)

    if latest is None:
        return None
    return _snapshot_from_summary(_session_key(data, transcript_path), transcript_path, session_id, latest)


def _legacy_snapshot(data: dict[str, Any]) -> ClaudeStopSnapshot | None:
    message = data.get("last_assistant_message") or data.get("text") or ""
    if not isinstance(message, str) or not message.strip():
        return None
    stop_reason = str(data.get("stop_reason") or "").strip().lower()
    session_id = str(data.get("session_id") or data.get("sessionId") or "")
    key = _session_key(data, "legacy")
    return ClaudeStopSnapshot(
        state_key=key,
        transcript_path="",
        session_id=session_id,
        event_uuid="",
        message_id="",
        text=message.strip(),
        stop_reason=stop_reason,
        content_kinds=("text",),
        source="payload",
    )


def claim_stop(data: dict[str, Any]) -> ClaudeStopClaim | None:
    """Atomically claim one visible Claude Stop event for delivery."""
    snapshot = inspect_stop(data)
    if snapshot is None or not snapshot.is_visible_completion:
        return None
    state_file = _state_path()
    with _STATE_LOCK:
        state = _read_state(state_file)
        sessions = state.setdefault("sessions", {})
        entry = sessions.setdefault(snapshot.state_key, {})
        identity = snapshot.identity
        if str(entry.get("notified_identity") or "") == identity:
            return None
        inflight = entry.get("inflight") if isinstance(entry.get("inflight"), dict) else None
        if inflight:
            if (
                str(inflight.get("identity") or "") == identity
                and time.time() - float(inflight.get("at", 0.0) or 0.0) < _CLAIM_TTL_SECONDS
            ):
                return None
        token = uuid.uuid4().hex
        entry["inflight"] = {"identity": identity, "token": token, "at": time.time()}
        entry["last_identity"] = identity
        _write_state(state_file, state)
        return ClaudeStopClaim(snapshot, token)


def commit_claim(claim: ClaudeStopClaim) -> None:
    state_file = _state_path()
    with _STATE_LOCK:
        state = _read_state(state_file)
        entry = (state.setdefault("sessions", {})).setdefault(claim.snapshot.state_key, {})
        inflight = entry.get("inflight") if isinstance(entry.get("inflight"), dict) else {}
        if str(inflight.get("token") or "") != claim.token:
            return
        entry["notified_identity"] = claim.snapshot.identity
        entry.pop("inflight", None)
        _write_state(state_file, state)


def release_claim(claim: ClaudeStopClaim) -> None:
    state_file = _state_path()
    with _STATE_LOCK:
        state = _read_state(state_file)
        entry = (state.setdefault("sessions", {})).setdefault(claim.snapshot.state_key, {})
        inflight = entry.get("inflight") if isinstance(entry.get("inflight"), dict) else {}
        if str(inflight.get("token") or "") == claim.token:
            entry.pop("inflight", None)
            _write_state(state_file, state)
