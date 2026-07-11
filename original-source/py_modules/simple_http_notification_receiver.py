import requests
import time

import os
import yaml
import logging
import jsonschema

import sys, importlib
from pathlib import Path


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


if __name__ == '__main__' and __package__ is None:
    import_parents()

from .simple_http_notification_conf import result_schema, cloud_server_ip, cloud_server_port, cloud_server_protocol
from .logging_lib import setup_logging, MyLogger
from .authentication import AuthenticationClient, handshake_info

import platform
import hashlib
import re


class NotificationAuthError(RuntimeError):
    pass


class NotificationPullError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _summarize_request_error(error: Exception) -> str:
    text = re.sub(r"sig=[^&\s)]+", "sig=<redacted>", str(error))
    if "UNEXPECTED_EOF_WHILE_READING" in text:
        return "TLS connection closed early by server"
    if isinstance(error, requests.exceptions.Timeout):
        return "Request timed out"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "Connection failed"
    return text[:300]


class SimpleHttpNotificationReceiver:
    def __init__(self, _cloud_server_protocol: str, _cloud_server_ip: str, _cloud_server_port: int,
                 logger: MyLogger):
        self.logger = logger
        self.cloud_server_protocol = _cloud_server_protocol
        self.cloud_server_ip = _cloud_server_ip
        self.cloud_server_port = _cloud_server_port
        authentication_client = AuthenticationClient(os.path.expanduser("~/.ssh/id_ed25519"), self.logger)
        self.signature = authentication_client.sign_request(handshake_info)
        # use a truncated md5 hash of hostname as client_id (fits in 64-bit int)
        self.client_id = int(hashlib.md5(platform.node().encode()).hexdigest()[:16], 16)

    def receive(self, channel: str) -> tuple[str, str]:
        try:
            response = requests.get(
                f"{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/notification",
                params={"channel": channel, "client_id": str(self.client_id), "sig": self.signature})
            self.logger.debug(f"Receive notification: {response.status_code}, {response.json()}")
            if response.status_code == 200:
                notification = response.json()
                jsonschema.validate(notification, result_schema)
                message: str = notification["message"]
                icon: str = notification.get("icon", "info")
                return message, icon
            else:
                return "", ""
        except Exception:
            return "", ""

    def pull(self, channel: str, since: str = "", limit: int | None = None) -> list[dict]:
        """Pull all recent notifications for a channel, optionally only those
        newer than `since` (ISO timestamp). Uses SSH signature auth.
        Returns list of {"message", "icon", "channel", "timestamp"} dicts."""
        params = {"channel": channel, "sig": self.signature}
        if since:
            params["since"] = since
        if limit is not None:
            params["limit"] = str(limit)
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                if attempt:
                    time.sleep(0.3 * attempt)
                response = requests.get(
                    f"{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/pull",
                    params=params, timeout=15)
                self.logger.debug(f"Pull notifications: {response.status_code}")
                if response.status_code == 200:
                    data = response.json()
                    return data.get("notifications", [])
                if response.status_code == 401:
                    raise NotificationAuthError(response.text[:200])
                self.logger.debug(f"Pull failed: {response.status_code} {response.text[:200]}")
                raise NotificationPullError(response.status_code, response.text[:200])
            except NotificationAuthError:
                raise
            except NotificationPullError:
                raise
            except requests.exceptions.RequestException as e:
                summary = _summarize_request_error(e)
                self.logger.debug(f"Pull exception attempt {attempt + 1}/{max_attempts}: {summary}")
                if attempt == max_attempts - 1:
                    raise NotificationPullError(0, summary)
                continue
            except Exception as e:
                summary = _summarize_request_error(e)
                self.logger.debug(f"Pull exception: {summary}")
                raise NotificationPullError(0, summary)
        raise NotificationPullError(0, "Pull failed")

import subprocess
from py_modules.lib_sh import shell_run
from py_modules.simple_http_notification_sender import SimpleHttpNotificationSender
if __name__ == '__main__':
    seen_requests = set()
    logger = setup_logging(simple_fmt=True)
    receiver = SimpleHttpNotificationReceiver(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)

    has_print = False
    while True:
        msg, icon = receiver.receive("default")
        if not has_print:
            logger.info("Here")
            has_print = True
        if msg:
            logger.info(str(msg))
            if msg in seen_requests:
                logger.info("seen" + str(msg))
                continue
            else:
                logger.notice("new" + str(msg))
                seen_requests.add(msg)
            if (('vanilla' in msg) or ('oaRAM' in msg) or ('fleet' in msg)):
                env = os.environ.copy()
                try:
                    subprocess.run(msg, shell=True, env=env)
                except Exception as e:
                    logger.error(f"Error: {e}")
                sender = SimpleHttpNotificationSender(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)
                sender.clear("default")
                has_print = False
        time.sleep(2)
