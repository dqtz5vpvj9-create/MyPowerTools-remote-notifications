import requests
import time
import pyttsx3
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

import platform
import hashlib

class SimpleHttpNotificationReceiver:
    def __init__(self, _cloud_server_protocol: str, _cloud_server_ip: str, _cloud_server_port: int,
                 logger: MyLogger):
        self.logger = logger
        self.cloud_server_protocol = _cloud_server_protocol
        self.cloud_server_ip = _cloud_server_ip
        self.cloud_server_port = _cloud_server_port
        # authentication_client = AuthenticationClient(os.path.expanduser("~/.ssh/id_ed25519"), self.logger)
        # self.signature = authentication_client.sign_request(handshake_info)
        # use the sha256 hash of hostname as client_id
        # self.client_id = int(hashlib.md5(platform.node().encode()).hexdigest(), 16)

    def receive(self) -> str:
        try:
            response = requests.get(
                f"http://192.168.22.22:8889/get_request")
            self.logger.debug(f"Receive notification: {response.status_code}, {response.json()}")
            notification = response.json()
            if notification:
                jsonschema.validate(notification, result_schema)
                message: str = notification["message"]
                return message
            else:
                return ""
        except Exception:
            return ""

class SimpleHttpNotificationSender:
    def __init__(self, _cloud_server_protocol: str, _cloud_server_ip: str, _cloud_server_port: int,
                 logger: MyLogger) -> None:
        self.logger = logger
        self.cloud_server_protocol = _cloud_server_protocol
        self.cloud_server_ip = _cloud_server_ip
        self.cloud_server_port = _cloud_server_port

    def clear(self) -> None:
        try:
            response = requests.get(
                f'http://r743.ipads-lab.se.sjtu.edu.cn:8889/clear')
        except Exception as e:
            pass

import subprocess
import os
import time
from py_modules.lib_sh import shell_run

if __name__ == '__main__':
    seen_requests = set()
    logger = setup_logging(simple_fmt=True)
    logger.info("Starting the notification receiver service")
    
    receiver = SimpleHttpNotificationReceiver(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)
    while True:
        msg = receiver.receive()
        if msg:
            logger.info(f"Processing received message: '{msg}'")
            if msg in seen_requests:
                logger.info(f"Message '{msg}' has been seen before. Skipping.")
                continue
            else:
                logger.notice(f"New message received: '{msg}'") # 'notice' 是自定义级别
                seen_requests.add(msg)
                logger.debug(f"Added '{msg}' to seen requests. Total seen: {len(seen_requests)}")
            
            if (('vanilla' in msg) or ('oaRAM' in msg) or ('fleet' in msg)):
                env = os.environ.copy()
                env['DISPLAY'] = 'C:\\WINDOWS\\System32\\OpenSSH-Win64'
                try:
                    logger.info(f"Executing command: {msg}")
                    p = subprocess.check_output(msg, shell=True, env=env, stderr=subprocess.STDOUT)
                    logger.info(f"Command output: {p.decode('utf-8', 'ignore')}")
                except subprocess.CalledProcessError as e:
                    logger.error("Error in executing command")
                    logger.error(f"Command error output: {e.output.decode('utf-8', 'ignore')}")
                
                sender = SimpleHttpNotificationSender(cloud_server_protocol, cloud_server_ip, cloud_server_port, logger)
                sender.clear()
        
        time.sleep(2)
