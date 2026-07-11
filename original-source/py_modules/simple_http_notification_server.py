import json
import os

import sys, importlib
import time
from pathlib import Path
from typing import Optional


def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]

    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError:  # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__)  # won't be needed after that


if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from .check_interpreter import check_conda_interpreter
from .simple_http_notification_conf import add_schema, result_schema, cloud_server_port, cloud_server_ip, redis_port_offset
from .authentication import AuthenticationServer, handshake_info

if __name__ == '__main__':
    check_conda_interpreter("android_automatic")

import sys
import time
from datetime import datetime as datetime_class
from datetime import timedelta, timezone
from typing import Any, Tuple, List

import jsonschema
import logging
import redis
from flask import Flask, Response, jsonify, request
from flask_compress import Compress
import threading
import schedule
import time
import os


app = Flask(__name__)
Compress(app)
logger = app.logger
app.logger.addHandler(logging.StreamHandler(sys.stdout))
app.logger.setLevel(logging.INFO)

redis_conn = redis.Redis(host='localhost', port=cloud_server_port + redis_port_offset, db=0)

def backup_and_flush_redis()-> None:
    # Redis dump
    current_date = time.strftime("%Y%m%d", time.gmtime())
    if not os.path.exists('redis_dump'):
        os.mkdir('redis_dump')
    with open(f'redis_dump/redis_dump_{current_date}', 'wb') as dump_file:
        for key in redis_conn.keys():
            try:
                key_dump = redis_conn.dump(key)
                if key_dump:
                    dump_file.write(key_dump)
            except redis.exceptions.ResponseError:
                print(f"Cannot dump key: {key!r}")
                continue

    # Flush notification lists but preserve registered FCM tokens — otherwise
    # clients stop receiving FCM pushes and /pull until they manually reopen
    # the app to re-register.
    preserved_prefixes = (b"fcm_tokens:", b"up_endpoints:")
    for key in redis_conn.keys():
        key_bytes = key if isinstance(key, bytes) else key.encode()
        if any(key_bytes.startswith(p) for p in preserved_prefixes):
            continue
        try:
            redis_conn.delete(key)
        except redis.exceptions.ResponseError as e:
            print(f"Cannot delete key {key!r}: {e}")


# 每天凌晨1点执行backup_and_flush_redis函数
schedule.every().day.at("01:00").do(backup_and_flush_redis)

def execute_scheduled_jobs() -> None:
    while True:
        schedule.run_pending()
        time.sleep(1)

# 创建一个新的线程并启动它
scheduler_thread = threading.Thread(target=execute_scheduled_jobs)
scheduler_thread.start()

redis_notifications = f"notifications_{cloud_server_port}"
# Verify the connection
redis_connected = False
for i in range(10):
    try:
        ret = redis_conn.ping()
        redis_connected = True
        break
    except redis.exceptions.ConnectionError:
        logger.warning("Redis server not ready, retrying...")
        time.sleep(1)
        continue
assert(redis_connected)
SSH_AUTHORIZED_KEYS = os.environ.get("SSH_AUTHORIZED_KEYS", None)
assert(SSH_AUTHORIZED_KEYS is not None)
auth_server = AuthenticationServer(SSH_AUTHORIZED_KEYS, logger)

# Firebase Cloud Messaging (FCM) initialization — uses HTTP v1 API directly
# (firebase-admin SDK uses gRPC which conflicts with gevent worker)
import requests as http_requests
fcm_enabled = False
fcm_project_id: Optional[str] = None
fcm_credentials = None
try:
    from google.oauth2 import service_account as google_service_account
    google_creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google_creds_path:
        fcm_credentials = google_service_account.Credentials.from_service_account_file(
            google_creds_path,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        with open(google_creds_path) as f:
            fcm_project_id = json.load(f).get("project_id")
        fcm_enabled = True
        logger.info("FCM push notifications enabled (HTTP v1 API)")
    else:
        logger.info("FCM disabled: GOOGLE_APPLICATION_CREDENTIALS not set")
except ImportError:
    logger.info("FCM disabled: google-auth package not installed")
except Exception as e:
    logger.warning(f"FCM disabled: initialization failed: {e}")


def _get_fcm_access_token() -> str:
    """Get a valid OAuth2 access token for FCM HTTP v1 API."""
    assert fcm_credentials is not None
    from google.auth.transport.requests import Request as GoogleAuthRequest
    fcm_credentials.refresh(GoogleAuthRequest())
    return fcm_credentials.token


def _ssh_signature_authorized(sig: str) -> bool:
    if not sig:
        return False
    try:
        return auth_server.validate_request(handshake_info, sig.encode("utf-8"))
    except Exception as e:
        logger.debug(f"SSH signature validation failed: {e}")
        return False


def _fcm_tokens_for_channel(channel: str) -> list[str]:
    token_key = f"fcm_tokens:{cloud_server_port}:{channel}"
    tokens: list[str] = []
    for raw_token in redis_conn.smembers(token_key):
        token = raw_token.decode("utf-8") if isinstance(raw_token, bytes) else raw_token
        # client-* IDs are local bearer tokens for /pull, not Firebase tokens.
        if token.startswith("client-"):
            continue
        tokens.append(token)
    return sorted(tokens)


# --- FCM kill switch -----------------------------------------------------
# FCM is paused (unreliable on Chinese OEMs). We migrated to UnifiedPush /
# self-hosted ntfy. Set ENABLE_FCM=1 in env to re-enable as a fallback.
fcm_paused = os.environ.get("ENABLE_FCM", "0") != "1"
if fcm_paused:
    logger.info("FCM push disabled (paused). Set ENABLE_FCM=1 to re-enable.")


def send_unifiedpush(channel: str, message: str, icon: str = "info",
                     timestamp: str = "", notif_id: str = "") -> None:
    """POST notification to every registered UnifiedPush endpoint URL for a channel.

    UnifiedPush endpoints are opaque URLs supplied by the phone-side distributor
    (e.g. ntfy). The server just forwards the JSON body; the distributor's
    server is responsible for waking the device.
    """
    endpoint_key = f"up_endpoints:{cloud_server_port}:{channel}"
    endpoints = redis_conn.smembers(endpoint_key)
    if not endpoints:
        return
    payload = {
        "id": notif_id,
        "channel": channel,
        "message": message,
        "icon": icon,
        "timestamp": timestamp,
    }
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    # Optional Basic Auth for endpoints behind a locked-down ntfy. Format:
    # NTFY_BASIC_AUTH=user:password (single account, applied to every UP POST).
    ntfy_basic = os.environ.get("NTFY_BASIC_AUTH", "")
    if ntfy_basic and ":" in ntfy_basic:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(ntfy_basic.encode()).decode()
    for raw in endpoints:
        url = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            resp = http_requests.post(url, data=body, headers=headers, timeout=10)
            if resp.status_code in (404, 410):
                logger.info(f"UP: removing dead endpoint {url[:48]}... ({resp.status_code})")
                redis_conn.srem(endpoint_key, url)
            elif resp.status_code >= 400:
                logger.warning(f"UP send failed {resp.status_code} for {url[:48]}...: {resp.text[:120]}")
        except Exception as e:
            logger.warning(f"UP send failed for {url[:48]}...: {e}")


def send_fcm_push(channel: str, message: str, icon: str = "info", timestamp: str = "", notif_id: str = "") -> None:
    """Send FCM data messages via HTTP v1 API to all registered tokens for a channel."""
    if not fcm_enabled:
        return
    token_key = f"fcm_tokens:{cloud_server_port}:{channel}"
    tokens = _fcm_tokens_for_channel(channel)
    if not tokens:
        return
    try:
        access_token = _get_fcm_access_token()
    except Exception as e:
        logger.warning(f"FCM: failed to get access token: {e}")
        return
    url = f"https://fcm.googleapis.com/v1/projects/{fcm_project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    for token in tokens:
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
            resp = http_requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 404 or (resp.status_code == 400 and "UNREGISTERED" in resp.text):
                logger.info(f"FCM: removing unregistered token {token[:12]}...")
                redis_conn.srem(token_key, token)
            elif resp.status_code != 200:
                logger.warning(f"FCM send failed for token {token[:12]}...: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"FCM send failed for token {token[:12]}...: {e}")


def parse_utc(ts: str) -> datetime_class:
    """Parse a stored ISO timestamp and return an aware UTC datetime.
    Handles Z suffix, +00:00 suffix, and legacy naive timestamps."""
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    d = datetime_class.fromisoformat(ts)
    if d.tzinfo is None:
        # Legacy naive data was produced by datetime.now() in local server tz.
        # But the server's local tz is UTC so naive means UTC for our purposes.
        d = d.replace(tzinfo=timezone.utc)
    return d


def normalize_notification_timestamp(timestamp: str = "") -> str:
    if timestamp:
        try:
            return parse_utc(timestamp).astimezone(timezone.utc).isoformat()
        except Exception:
            logger.warning(f"Invalid notification timestamp {timestamp!r}; using server time")
    return datetime_class.now(timezone.utc).isoformat()


def _dedupe_key(channel: str, client_msg_id: str) -> str:
    return f"notification_dedupe:{cloud_server_port}:{channel}:{client_msg_id}"


def add_notification(message: str, channel: str, icon: str = "info",
                     notif_id: str = "", timestamp: str = "", client_msg_id: str = "") -> dict[str, Any]:
    import uuid
    event_timestamp = normalize_notification_timestamp(timestamp)
    server_timestamp = datetime_class.now(timezone.utc).isoformat()
    # Prefer client-provided id so FCM (possibly sent by the same client
    # out-of-band) and server-generated /pull use the same id.
    if not notif_id:
        notif_id = uuid.uuid4().hex
    if client_msg_id:
        key = _dedupe_key(channel, client_msg_id)
        dedupe_record = {
            "message_id": notif_id,
            "timestamp": event_timestamp,
            "server_timestamp": server_timestamp,
            "client_msg_id": client_msg_id,
        }
        inserted = redis_conn.set(key, json.dumps(dedupe_record), nx=True, ex=86400)
        if not inserted:
            existing_raw = redis_conn.get(key)
            try:
                existing = json.loads(
                    existing_raw.decode("utf-8") if isinstance(existing_raw, bytes) else str(existing_raw)
                )
            except Exception:
                existing = {}
            existing_id = str(existing.get("message_id") or notif_id)
            logger.info(
                f"Duplicate notification skipped client_msg_id={client_msg_id[:12]} "
                f"message_id={existing_id[:12]} channel={channel}"
            )
            return {
                "status": "duplicate",
                "stored_new": False,
                "message_id": existing_id,
                "client_msg_id": client_msg_id,
            }
    # 6-tuple: event timestamp for display/dedup, server timestamp for pull
    # cursors. Legacy 4/5-tuples remain readable.
    notification = (event_timestamp, message, [], icon, notif_id, server_timestamp)
    redis_conn.lpush(f"{redis_notifications}:{channel}", json.dumps(notification))
    logger.debug(f"Added notification id={notif_id[:8]} to channel: {channel}")
    # Fire-and-forget UnifiedPush forward in a background thread
    threading.Thread(
        target=send_unifiedpush,
        args=(channel, message, icon, event_timestamp, notif_id),
        daemon=True,
    ).start()
    # FCM is paused — keep code path for rollback via ENABLE_FCM=1.
    if fcm_enabled and not fcm_paused:
        threading.Thread(
            target=send_fcm_push,
            args=(channel, message, icon, event_timestamp, notif_id),
            daemon=True,
        ).start()
    return {
        "status": "ok",
        "stored_new": True,
        "message_id": notif_id,
        "client_msg_id": client_msg_id,
    }


def get_notification(channel: str, client_id: int) -> Tuple[datetime_class, str, List[int], str, str] | None:
    notification_key = f"{redis_notifications}:{channel}"
    lock_key = f"lock:{notification_key}"
    with redis_conn.lock(lock_key, timeout=5):
        for i in range(redis_conn.llen(notification_key)):
            notification_json = redis_conn.lindex(notification_key, i)
            if notification_json:
                notification = json.loads(notification_json)
                msg_date = parse_utc(notification[0])
                msg = notification[1]
                sent_list = notification[2]
                icon = notification[3] if len(notification) > 3 else "info"
                notif_id = notification[4] if len(notification) > 4 else ""
                server_ts = notification[5] if len(notification) > 5 else notification[0]
                server_date = parse_utc(server_ts)
                logger.debug(f"Checking notification: {msg} {msg_date} {sent_list} since now: {datetime_class.now(timezone.utc) - msg_date} to channel: {channel}")
                if datetime_class.now(timezone.utc) - server_date <= timedelta(seconds=60) and client_id not in sent_list:
                    sent_list.append(client_id)
                    updated_notification = (msg_date.isoformat(), msg, sent_list, icon, notif_id, server_ts)
                    redis_conn.lset(notification_key, i, json.dumps(updated_notification))
                    logger.debug(f"Returning notification: {msg} to channel: {channel}")
                    return msg_date, msg, sent_list, icon, notif_id
    return None


@app.route('/clear', methods=['GET'])
def clear_notifications() -> Tuple[Response, int]:
    channel = request.args.get('channel')
    logger.debug(f"Clearing notifications in channel {channel}")
    if not channel:
        return jsonify({"error": "Channel not specified"}), 400
    redis_conn.delete(f"{redis_notifications}:{channel}")
    return jsonify({"status": "ok"}), 200

@app.route('/register_device', methods=['POST'])
def register_device() -> Tuple[Response, int]:
    data: Any | None = request.get_json()
    if not data or "token" not in data:
        return jsonify({"error": "token is required"}), 400
    token = data["token"]
    channel = data.get("channel", "default")
    client_id = data.get("client_id", "")
    token_key = f"fcm_tokens:{cloud_server_port}:{channel}"
    redis_conn.sadd(token_key, token)
    logger.info(f"Registered FCM device token {token[:12]}... for channel {channel} (client_id={client_id})")
    return jsonify({"status": "ok"}), 200


@app.route('/register_device', methods=['DELETE'])
def unregister_device() -> Tuple[Response, int]:
    data: Any | None = request.get_json()
    if not data or "token" not in data:
        return jsonify({"error": "token is required"}), 400
    token = data["token"]
    channel = data.get("channel", "default")
    token_key = f"fcm_tokens:{cloud_server_port}:{channel}"
    redis_conn.srem(token_key, token)
    logger.info(f"Unregistered FCM device token {token[:12]}... from channel {channel}")
    return jsonify({"status": "ok"}), 200


@app.route('/registered_devices', methods=['GET'])
def registered_devices() -> Tuple[Response, int]:
    """Return registered Firebase tokens for trusted desktop senders.

    The proxy cannot reliably reach Google from its network. Local desktop
    clients use this authenticated endpoint to sync tokens, then send FCM from
    the local machine.
    """
    channel = request.args.get('channel', 'default')
    sig = request.args.get('sig', '').strip()
    if not _ssh_signature_authorized(sig):
        return jsonify({"error": "Authorization required"}), 401
    tokens = _fcm_tokens_for_channel(channel)
    logger.info(f"Returned {len(tokens)} registered FCM token(s) for channel {channel} to {request.remote_addr}")
    return jsonify({"channel": channel, "tokens": tokens, "count": len(tokens)}), 200


@app.route('/register_unifiedpush_endpoint', methods=['POST'])
def register_up_endpoint() -> Tuple[Response, int]:
    """Register a UnifiedPush endpoint URL for a channel.
    Body: {"endpoint": "https://ntfy.../UP?up=1", "channel": "default", "client_id": "..."}
    """
    data: Any | None = request.get_json()
    if not data or "endpoint" not in data:
        return jsonify({"error": "endpoint is required"}), 400
    endpoint = data["endpoint"]
    channel = data.get("channel", "default")
    client_id = data.get("client_id", "")
    endpoint_key = f"up_endpoints:{cloud_server_port}:{channel}"
    redis_conn.sadd(endpoint_key, endpoint)
    logger.info(f"Registered UP endpoint {endpoint[:48]}... for channel {channel} (client_id={client_id})")
    return jsonify({"status": "ok"}), 200


@app.route('/register_unifiedpush_endpoint', methods=['DELETE'])
def unregister_up_endpoint() -> Tuple[Response, int]:
    data: Any | None = request.get_json()
    if not data or "endpoint" not in data:
        return jsonify({"error": "endpoint is required"}), 400
    endpoint = data["endpoint"]
    channel = data.get("channel", "default")
    endpoint_key = f"up_endpoints:{cloud_server_port}:{channel}"
    redis_conn.srem(endpoint_key, endpoint)
    logger.info(f"Unregistered UP endpoint {endpoint[:48]}... from channel {channel}")
    return jsonify({"status": "ok"}), 200


@app.route('/notification', methods=['POST'])
def receive_notification() -> Tuple[Response, int]:
    data: Any | None = request.get_json()
    result: dict[str, Any] = {"status": "ok"}
    if data:
        try:
            jsonschema.validate(data, add_schema)
            channel = data["channel"]
            msg = data["message"]
            icon = data.get("icon", "info")
            client_id = data.get("id", "")  # optional client-generated UUID
            timestamp = data.get("timestamp", "")
            client_msg_id = data.get("client_msg_id", "")
            logger.info(
                f"Received notification from ip {request.remote_addr} "
                f"id={client_id[:12] if client_id else 'none'} "
                f"client_msg_id={client_msg_id[:12] if client_msg_id else 'none'} "
                f"channel={channel}"
            )
            result = add_notification(
                msg,
                channel,
                icon,
                notif_id=client_id,
                timestamp=timestamp,
                client_msg_id=client_msg_id,
            )
        except jsonschema.exceptions.ValidationError as e:
            return jsonify({"error": e.message}), 400
    return jsonify(result), 200


@app.route('/sms', methods=['POST'])
def receive_sms() -> Tuple[Response, int]:
    msg = request.form["payload"]
    if msg:
        channel = "sms"
        logger.debug(f"Received sms: {msg} from ip {request.remote_addr}")
        add_notification(msg, channel)
    else:
        return jsonify({"error": "Wrong request"}), 400
    return jsonify({"status": "ok"}), 200


@app.route('/version', methods=['GET'])
def get_version() -> Tuple[Response, int]:
    """Return the latest app version info for self-update."""
    version_file = os.path.join(os.path.dirname(__file__), '..', 'notifyapp_version.json')
    if os.path.exists(version_file):
        with open(version_file) as f:
            return jsonify(json.load(f)), 200
    return jsonify({"error": "No version info"}), 404


@app.route('/update.apk', methods=['GET'])
def download_apk() -> Tuple[Response, int]:
    """Serve the latest APK for self-update."""
    apk_path = os.path.join(os.path.dirname(__file__), '..', 'notifyapp_update.apk')
    if os.path.exists(apk_path):
        from flask import send_file
        return send_file(apk_path, mimetype='application/vnd.android.package-archive',
                         as_attachment=True, download_name='NotifyApp.apk')
    return jsonify({"error": "No APK available"}), 404


@app.route('/pull', methods=['GET'])
def pull_notifications() -> Tuple[Response, int]:
    """Return recent notifications for a channel.
    Auth: either FCM device token (Bearer header) or SSH signature (?sig= param).
    Used by the mobile app's polling service and desktop clients like powertool Page1."""
    channel = request.args.get('channel', 'default')
    since = request.args.get('since')  # ISO timestamp, optional
    try:
        limit = max(1, min(int(request.args.get('limit', '500')), 2000))
    except ValueError:
        limit = 500
    # Accept FCM Bearer token (mobile app) OR SSH signature (desktop clients)
    token = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    sig = request.args.get('sig', '').strip()
    authorized = False
    if token:
        token_key = f"fcm_tokens:{cloud_server_port}:{channel}"
        if redis_conn.sismember(token_key, token):
            authorized = True
    if not authorized and sig:
        authorized = _ssh_signature_authorized(sig)
    if not authorized:
        return jsonify({"error": "Authorization required"}), 401
    notification_key = f"{redis_notifications}:{channel}"
    results = []
    for i in range(redis_conn.llen(notification_key)):
        if len(results) >= limit:
            break
        raw = redis_conn.lindex(notification_key, i)
        if not raw:
            continue
        notification = json.loads(raw)
        msg_date = parse_utc(notification[0])
        msg = notification[1]
        icon = notification[3] if len(notification) > 3 else "info"
        notif_id = notification[4] if len(notification) > 4 else ""
        server_ts = notification[5] if len(notification) > 5 else notification[0]
        server_dt = parse_utc(server_ts)
        if since:
            try:
                since_dt = parse_utc(since)
                if server_dt <= since_dt:
                    continue
            except ValueError:
                pass
        results.append({
            "id": notif_id,
            "message": msg,
            "icon": icon,
            "channel": channel,
            "timestamp": msg_date.isoformat(),
            "server_timestamp": server_dt.isoformat(),
        })
    return jsonify({"notifications": results}), 200


@app.route('/notification', methods=['GET'])
def get_notifications() -> Tuple[Response, int]:
    channel = request.args.get('channel')
    _client_id = request.args.get('client_id')
    signature = request.args.get('sig')
    try:
        if signature:
            vaild = auth_server.validate_request(handshake_info, signature.encode("utf-8"))
            if not vaild:
                logger.debug(f"Signature validation failed")
                return jsonify({"error": "Signature validation failed"}), 400
        else:
            logger.debug(f"Signature not specified")
            return jsonify({"error": "Signature not specified"}), 400
    except Exception as e:
        logger.debug(f"Signature validation failed: {e}")
        return jsonify({"error": f"Signature validation failed {str(e)}"}), 400
    if not channel or not _client_id or len(_client_id) == 0 or len(channel) == 0 or not _client_id.isdigit():
        return jsonify({"error": "Channel or client_id not specified"}), 400
    client_id = int(_client_id)
    max_wait = 3
    logger.debug(f"Waiting for notification in channel {channel} for client {client_id} from ip {request.remote_addr}")
    for attempt in range(max_wait):
        logger.debug(f"Check for the {attempt + 1} time(s) for notification in channel {channel} for client {client_id} from ip {request.remote_addr}")
        notification = get_notification(channel, client_id)
        if notification:
            msg_date, msg, _, icon, notif_id = notification
            ret = {
                "id": notif_id,
                "message": msg,
                "icon": icon,
                "channel": channel,
                "timestamp": msg_date.isoformat(),
            }
            jsonschema.validate(ret, result_schema)
            logger.debug(f"Return {msg} in channel {channel} for client {client_id} from ip {request.remote_addr}")
            return jsonify(ret), 200
        time.sleep(1)
    logger.debug(f"Dropped connection in channel {channel} for client {client_id} from ip {request.remote_addr}")
    return jsonify({"error": "No notification"}), 204



if __name__ == '__main__':
    check_conda_interpreter("android_automatic")
    SSL_CERT_FILE = os.environ.get("SSL_CERT_FILE")
    SSL_KEY_FILE = os.environ.get("SSL_KEY_FILE")
    if SSL_CERT_FILE is None or SSL_KEY_FILE is None:
        raise ValueError("SSL_CERT_FILE or SSL_KEY_FILE is not set in environment variables")
    app.run(host='0.0.0.0', port=cloud_server_port, ssl_context=(SSL_CERT_FILE, SSL_KEY_FILE))
