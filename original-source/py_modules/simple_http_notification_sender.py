#! /usr/bin/env python3
import json
import logging
import urllib.error
import urllib.request

try:
    import requests
except ModuleNotFoundError:
    requests = None

import sys, importlib
from pathlib import Path

import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
#    try:
#        sys.path.remove(str(parent))
#    except ValueError: # already removed
#        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()
sys.path.append(dirname(__file__) + os.sep + pardir)
from py_modules.logging_lib import setup_logging, LogLevel, MyLogger


class _UrllibResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body

    def json(self):
        if not self._body:
            return {}
        return json.loads(self._body.decode("utf-8"))


def _http_get(url: str, timeout: float | None = None):
    if requests is not None:
        return requests.get(url, timeout=timeout)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return _UrllibResponse(response.status, response.read())


def _http_post_json(url: str, payload: dict, timeout: float | None = None):
    headers = {'Content-type': 'application/json'}
    if requests is not None:
        return requests.post(url, data=json.dumps(payload), headers=headers, timeout=timeout)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return _UrllibResponse(response.status, response.read())


_TIMEOUT_EXCEPTIONS = (TimeoutError,)
_CONNECTION_EXCEPTIONS = (OSError, urllib.error.URLError)
if requests is not None:
    _TIMEOUT_EXCEPTIONS = _TIMEOUT_EXCEPTIONS + (requests.exceptions.Timeout,)
    _CONNECTION_EXCEPTIONS = _CONNECTION_EXCEPTIONS + (requests.exceptions.ConnectionError,)


class SimpleHttpNotificationSender:
    def __init__(self, _cloud_server_protocol: str, _cloud_server_ip: str, _cloud_server_port: int,
                 logger: MyLogger) -> None:
        self.logger = logger
        self.cloud_server_protocol = _cloud_server_protocol
        self.cloud_server_ip = _cloud_server_ip
        self.cloud_server_port = _cloud_server_port

    def clear(self, channel: str, timeout: float | None = None) -> None:
        response = _http_get(
            f'{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/clear?channel={channel}',
            timeout=timeout)

    def send(
        self,
        channel: str,
        message: str,
        icon: str = "info",
        notif_id: str = "",
        client_msg_id: str = "",
        timestamp: str = "",
        session_id: str = "",
        session_name: str = "",
        source_client: str = "",
        source_event_id: str = "",
        source_message_id: str = "",
        content_kind: str = "",
        stop_reason: str = "",
        schema_version: int = 2,
        timeout: float | None = None,
    ):
        data = {"message": message, "channel": channel, "icon": icon}
        if notif_id:
            data["id"] = notif_id
        if client_msg_id:
            data["client_msg_id"] = client_msg_id
        if timestamp:
            data["timestamp"] = timestamp
        if session_id:
            data["session_id"] = session_id
        if session_name:
            data["session_name"] = session_name
        if source_client:
            data["source_client"] = source_client
        if source_event_id:
            data["source_event_id"] = source_event_id
        if source_message_id:
            data["source_message_id"] = source_message_id
        if content_kind:
            data["content_kind"] = content_kind
        if stop_reason:
            data["stop_reason"] = stop_reason
        data["schema_version"] = schema_version
        response = _http_post_json(
            f'{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/notification',
            data,
            timeout=timeout)
        logging.debug(f"Send notification: {message} with response: {response.json()}")
        return response.json()

    def control(self, ctrl_msg: str, device_name: str | None = None, timeout: float | None = None):
        choices = ['create', 'start', 'pause', 'stop', 'read_nrg']
        if ctrl_msg not in choices:
            raise ValueError(f"Invalid control message: {ctrl_msg}")
        data = {"energy_ctrl_msg": ctrl_msg}
        if device_name:
            data["device_name"] = device_name
        response = _http_post_json(
            f'{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/energy_control',
            data,
            timeout=timeout)
        self.logger.notice(f"Send control: {ctrl_msg} with response: {response.json()}")
        return response.json()

    def handshake(self, timeout: float = 1.0) -> bool:
        """
        Check if the energy server is online

        Args:
            timeout: Timeout in seconds (default: 1.0)

        Returns:
            True if server is online, False otherwise
        """
        try:
            response = _http_get(
                f'{self.cloud_server_protocol}://{self.cloud_server_ip}:{self.cloud_server_port}/handshake',
                timeout=timeout)
            if response.status_code == 200:
                result = response.json()
                self.logger.info(f"Handshake successful: {result}")
                return True
            else:
                self.logger.warning(f"Handshake failed with status code: {response.status_code}")
                return False
        except _TIMEOUT_EXCEPTIONS:
            self.logger.warning(f"Handshake timeout after {timeout}s")
            return False
        except _CONNECTION_EXCEPTIONS:
            self.logger.warning(f"Connection error during handshake")
            return False
        except Exception as e:
            self.logger.warning(f"Handshake failed with exception: {e}")
            return False


class MockSimpleHttpNotificationSender:
    def __init__(self, _cloud_server_protocol: str, _cloud_server_ip: str, _cloud_server_port: int,
                 logger: MyLogger) -> None:
        self.logger = logger

    def clear(self, channel: str) -> None:
        pass

    def send(self, channel: str, message: str, icon: str = "info",
             notif_id: str = "", timestamp: str = "",
             client_msg_id: str = "",
             session_id: str = "", session_name: str = "",
             source_client: str = "", source_event_id: str = "",
             source_message_id: str = "", content_kind: str = "",
             stop_reason: str = "", schema_version: int = 2,
             timeout: float | None = None) -> None:
        self.logger.notice(message)

if __name__ == '__main__':
    logger = setup_logging()
    sender = SimpleHttpNotificationSender("http", "127.0.0.1", "18999", logger)
    if len(sys.argv) == 2:
        msg = sys.argv[1]
    else:
        msg = "hello"
    sender.control("start")
