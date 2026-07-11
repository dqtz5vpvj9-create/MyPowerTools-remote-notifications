"""Send FCM push notifications via HTTP v1 API.

Used by send_notification.py on machines with Google connectivity.
Reads the service account key from GOOGLE_APPLICATION_CREDENTIALS or a default path.
Device tokens are synchronized from the notification server's /registered_devices
endpoint into ~/.config/androidtools/fcm_tokens.json before each send.
"""
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Find service account credentials
_CREDS_PATHS = [
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
    os.path.expanduser("~/.config/androidtools/firebase-service-account.json"),
    os.path.join(os.path.dirname(__file__), "..", "firebase-service-account.json"),
]

_credentials = None
_project_id = None

for _path in _CREDS_PATHS:
    if _path and os.path.exists(_path):
        try:
            from google.oauth2 import service_account
            _credentials = service_account.Credentials.from_service_account_file(
                _path, scopes=["https://www.googleapis.com/auth/firebase.messaging"]
            )
            with open(_path) as _f:
                _project_id = json.load(_f).get("project_id")
            break
        except Exception:
            continue

# Local token store
_TOKEN_FILE = os.path.expanduser("~/.config/androidtools/fcm_tokens.json")


def _load_tokens() -> dict[str, list[str]]:
    if os.path.exists(_TOKEN_FILE):
        try:
            with open(_TOKEN_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    str(channel): _normalize_tokens(tokens if isinstance(tokens, list) else [])
                    for channel, tokens in data.items()
                }
        except Exception:
            return {}
    return {}


def _save_tokens(tokens: dict[str, list[str]]) -> None:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def _normalize_tokens(tokens: list[Any]) -> list[str]:
    seen = set()
    normalized: list[str] = []
    for token in tokens:
        if not isinstance(token, str):
            continue
        token = token.strip()
        if not token or token.startswith("client-") or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _summarize_request_error(error: Exception) -> str:
    text = re.sub(r"sig=[^&\s)]+", "sig=<redacted>", str(error))
    return text[:240]


def _notification_server_config() -> tuple[str, str, int]:
    protocol = "https"
    host = "message.lixinrui000.cn"
    port = 8888
    user_conf = Path(__file__).with_name("simple_http_notification_conf_user.yaml")
    if user_conf.exists():
        try:
            import yaml
            data = yaml.safe_load(user_conf.read_text())
        except Exception:
            data = {}
            for line in user_conf.read_text().splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip()
        if isinstance(data, dict):
            protocol = str(data.get("cloud_server_protocol", protocol))
            host = str(data.get("cloud_server_ip", host))
            port = int(data.get("cloud_server_port", port))
    return protocol, host, port


def _notification_server_url(path: str) -> str:
    protocol, host, port = _notification_server_config()
    return f"{protocol}://{host}:{port}{path}"


def sync_tokens_from_server(channel: str = "default") -> dict[str, Any]:
    """Sync Firebase tokens from the notification server using SSH-signature auth."""
    result: dict[str, Any] = {
        "attempted": True,
        "ok": False,
        "channel": channel,
        "server_count": 0,
        "cached_count": len(_load_tokens().get(channel, [])),
        "added": 0,
    }
    try:
        from py_modules.authentication import AuthenticationClient, handshake_info

        key_path = os.path.expanduser(
            os.environ.get("ANDROIDTOOLS_NOTIFY_SSH_KEY", "~/.ssh/id_ed25519")
        )
        if not os.path.exists(key_path):
            result["error"] = f"SSH key not found: {key_path}"
            return result

        logger = logging.getLogger(__name__)
        signature = AuthenticationClient(key_path, logger).sign_request(handshake_info)
        response = requests.get(
            _notification_server_url("/registered_devices"),
            params={"channel": channel, "sig": signature},
            timeout=10,
        )
        result["status_code"] = response.status_code
        if response.status_code != 200:
            result["error"] = response.text[:200]
            return result

        data = response.json()
        server_tokens = _normalize_tokens(data.get("tokens", []))
        tokens = _load_tokens()
        existing = _normalize_tokens(tokens.get(channel, []))
        merged = _normalize_tokens(existing + server_tokens)
        tokens[channel] = merged
        _save_tokens(tokens)

        result.update({
            "ok": True,
            "server_count": len(server_tokens),
            "cached_count": len(merged),
            "added": max(0, len(set(merged)) - len(set(existing))),
        })
    except Exception as e:
        result["error"] = _summarize_request_error(e)
    return result


def unregister_tokens_from_server(channel: str, tokens: list[str]) -> dict[str, Any]:
    tokens = _normalize_tokens(tokens)
    result: dict[str, Any] = {
        "attempted": len(tokens),
        "ok": 0,
        "failed": 0,
    }
    for token in tokens:
        try:
            response = requests.delete(
                _notification_server_url("/register_device"),
                json={"channel": channel, "token": token},
                timeout=10,
            )
            if response.status_code == 200:
                result["ok"] += 1
            else:
                result["failed"] += 1
        except Exception:
            result["failed"] += 1
    return result


def register_token(token: str, channel: str = "default") -> None:
    """Register a device token locally."""
    tokens = _load_tokens()
    channel_tokens = _normalize_tokens(tokens.get(channel, []))
    if token not in channel_tokens:
        channel_tokens.append(token)
    tokens[channel] = channel_tokens
    _save_tokens(tokens)


def _default_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def send_fcm_push(
    channel: str,
    message: str,
    icon: str = "info",
    timestamp: str = "",
    notif_id: str = "",
) -> dict[str, Any]:
    """Send FCM data message to all registered tokens for a channel."""
    if not timestamp:
        timestamp = _default_timestamp()
    result: dict[str, Any] = {
        "enabled": _credentials is not None and _project_id is not None,
        "channel": channel,
        "timestamp": timestamp,
        "token_count": 0,
        "sent": 0,
        "failed": 0,
        "removed": 0,
    }
    result["sync"] = sync_tokens_from_server(channel)

    if _credentials is None or _project_id is None:
        result["error"] = "Firebase service account credentials not available"
        return result

    tokens = _load_tokens()
    channel_tokens = _normalize_tokens(tokens.get(channel, []))
    result["token_count"] = len(channel_tokens)
    if not channel_tokens:
        result["error"] = "No registered FCM tokens for channel"
        return result

    # Get access token
    from google.auth.transport.requests import Request
    try:
        _credentials.refresh(Request())
    except Exception as e:
        result["error"] = f"Failed to refresh Firebase access token: {_summarize_request_error(e)}"
        return result
    access_token = _credentials.token

    url = f"https://fcm.googleapis.com/v1/projects/{_project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    removed = []
    status_codes: dict[str, int] = {}
    for token in channel_tokens:
        payload = {
            "message": {
                "token": token,
                "data": {
                    "title": channel,
                    "message": message,
                    "channel": channel,
                    "icon": icon,
                    "timestamp": timestamp,
                    "id": notif_id,
                },
            }
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            status_codes[str(resp.status_code)] = status_codes.get(str(resp.status_code), 0) + 1
            if resp.status_code == 200:
                result["sent"] += 1
                continue
            if resp.status_code == 404 or (resp.status_code == 400 and "UNREGISTERED" in resp.text):
                removed.append(token)
            result["failed"] += 1
        except Exception:
            result["failed"] += 1

    # Clean up invalid tokens
    if removed:
        tokens[channel] = [t for t in channel_tokens if t not in removed]
        _save_tokens(tokens)
        result["server_unregistered"] = unregister_tokens_from_server(channel, removed)
    result["removed"] = len(removed)
    if status_codes:
        result["status_codes"] = status_codes
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "register":
        token = sys.argv[2]
        channel = sys.argv[3] if len(sys.argv) > 3 else "default"
        register_token(token, channel)
        print(f"Registered token {token[:20]}... for channel {channel}")
    elif len(sys.argv) >= 2 and sys.argv[1] == "sync":
        channel = sys.argv[2] if len(sys.argv) > 2 else "default"
        print(json.dumps(sync_tokens_from_server(channel), indent=2, sort_keys=True))
    elif len(sys.argv) >= 2 and sys.argv[1] == "test":
        print(json.dumps(send_fcm_push("default", "FCM push test from local machine", "claude"), indent=2, sort_keys=True))
    else:
        print("Usage:")
        print("  python fcm_push.py register <token> [channel]")
        print("  python fcm_push.py sync [channel]")
        print("  python fcm_push.py test")
