from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_qt_toast_uses_page1_accepted_signal_only():
    qt = read_text("qt.py")

    assert "connect_safe(self.page1.accepted_msg_signal, self.on_received_msg)" in qt
    assert "received_msg_signal" not in qt
    assert 'notification_id = str(msg[4]) if len(msg) > 4 and msg[4] else ""' in qt
    assert "strip_leading_quoted_request(content)" in qt


def test_page1_carries_server_message_id_to_acceptance_boundary():
    page1 = read_text("powertool/page1.py")

    assert 'notification_id = str(n.get("id") or n.get("message_id") or "")' in page1
    assert "channel, message, icon, ts, notification_id, True, content_kind" in page1
    assert "def is_explicit_agent_internal(record: dict) -> bool:" in page1
    assert "notification_id or notification_message_id(channel, content, icon_name, timestamp)" in page1
    assert "if should_notify:" in page1
    assert "self.accepted_msg_signal.emit((channel, content, icon_name, timestamp, h))" in page1


def test_page1_seen_ids_cover_full_pull_replay():
    page1 = read_text("powertool/page1.py")

    assert "MAX_SEEN_MESSAGE_IDS = 5000" in page1
    assert 'seen_ids = json.loads(self._settings.value("seen_message_ids", "[]"))' in page1
    assert "if h in self._seen_message_ids or fallback_id in self._seen_message_ids:" in page1
    assert 'self._settings.setValue("seen_message_ids", json.dumps(list(self._seen_message_order)))' in page1


def test_page1_uses_in_memory_pull_waterline_for_traffic_only():
    page1 = read_text("powertool/page1.py")
    receiver = read_text("py_modules/simple_http_notification_receiver.py")

    assert "self._since = datetime_class.now(timezone.utc).isoformat()" in page1
    assert "since=self._since" in page1
    assert "PULL_INCREMENTAL_LIMIT = 20" in page1
    assert "self._settings.setValue(\"last_since\"" not in page1
    assert 'self._settings.value("last_since"' not in page1
    assert "def sane_pull_waterline(ts: str, max_future_seconds: int = 120) -> str:" in page1
    assert "if parsed > now + timedelta(seconds=max_future_seconds):" in page1
    assert 'def notification_cursor_ts(notification: dict) -> str:' in page1
    assert 'notification.get("server_timestamp") or notification.get("timestamp")' in page1
    assert "def newest_sane_pull_waterline(notifications: list[dict]) -> str:" in page1
    assert "latest_waterline = newest_sane_pull_waterline(sane_notifications)" in page1
    assert "def is_sane_server_ts(ts: str, max_future_seconds: int = 120) -> bool:" in page1
    assert "sane_notifications = [" in page1
    assert "if is_sane_server_ts(notification_cursor_ts(n))" in page1
    assert "def pull(self, channel: str, since: str = \"\", limit: int | None = None)" in receiver
    assert 'params["limit"] = str(limit)' in receiver
