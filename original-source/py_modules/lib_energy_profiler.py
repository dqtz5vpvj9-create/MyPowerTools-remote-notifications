#! /usr/bin/env python3
import json
import logging
import math

import sys, importlib
from pathlib import Path
import time

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

from typing import Tuple

from py_modules.simple_http_notification_sender import SimpleHttpNotificationSender

class EnergyProfiler:
    def __init__(self, logger: MyLogger, device_name: str | None = None):
        self.logger = logger
        self.http_sender_ = SimpleHttpNotificationSender("http", "ow.lixinrui000.cn", 18999, logger)
        self.device_name = (
            device_name
            or os.environ.get("ENERGY_DEVICE_NAME")
            or os.environ.get("ENERGY_METER_DEVICE_NAME")
        )
        self.last_create_response = None
        self.last_start_response = None
        self.last_stop_response = None
        self.last_read_response = None
        self.logger.info("EnergyProfiler initialized")
        if self.device_name:
            self.logger.info(f"EnergyProfiler device binding: {self.device_name}")
        self.status_ = "init"

    def _control(self, ctrl_msg: str):
        return self.http_sender_.control(ctrl_msg, device_name=self.device_name)

    @staticmethod
    def _extract_energy_wh(read_ret) -> float:
        if "energy_wh" in read_ret:
            value = float(read_ret["energy_wh"])
        else:
            value = float(str(read_ret["message"]).replace("Wh", "").strip())
        if math.isnan(value):
            raise ValueError("energy result is NaN")
        return value

    def check_server_online(self, timeout: float = 1.0) -> bool:
        """
        Check if the energy server is online
        
        Args:
            timeout: Timeout in seconds (default: 1.0)
            
        Returns:
            True if server is online, False otherwise
        """
        self.logger.info(f"Checking if energy server is online (timeout={timeout}s)")
        is_online = self.http_sender_.handshake(timeout)
        if is_online:
            self.logger.info("Energy server is online")
        else:
            self.logger.warning("Energy server is offline or not responding")
        return is_online

    def start(self) -> str:
        start_done = False
        self.logger.info("Starting energy profiling")
        try:
            create_ret = self._control('create')
            self.last_create_response = create_ret
            create_ok = create_ret['message']
            create_session = create_ret['session_id']
            if create_session == "not_ready":
                raise ValueError("Energy profiler is not ready")
            if create_ok == 'OK':
                time.sleep(1)
                start_ret = self._control('start')
                self.last_start_response = start_ret
                start_ok = start_ret['message']
                start_session = start_ret['session_id']
                if start_session == "not_ready":
                    raise ValueError("Energy profiler is not ready")
                if start_ok == 'OK' and create_session == start_session:
                    self.logger.info("Energy profiling started")
                    start_done = True
                else:
                    self.logger.error("Failed to start energy profiling")
        except Exception as e:
            self.logger.error(f"Failed to start energy profiling: {e}")
                
        if not start_done:
            self.logger.error("Failed to start energy profiling")
            return "[invalid_session_id_1]"
        else:
            return create_session

    def finish(self) -> Tuple[float, str]:
        self.logger.info("Finishing energy profiling")
        stop_ret = self._control('stop')
        self.last_stop_response = stop_ret
        stop_ok = stop_ret['message']
        stop_session = stop_ret['session_id']
        if stop_ok == 'OK':
            time.sleep(0.5)
            try:
                read_ret = self._control('read_nrg')
                self.last_read_response = read_ret
                return (self._extract_energy_wh(read_ret), read_ret['session_id'])
            except ValueError as e:
                self.logger.error(f"Failed to read energy profiling result: {e}")
        else:
            self.logger.error("Failed to stop energy profiling")
                
        self.logger.error("Failed to stop energy profiling")
        return (-1.0, "[invalid_session_id_2]")
        

if __name__ == '__main__':
    logger = setup_logging()
    energy_profiler = EnergyProfiler(logger)
    
    # Check if server is online before starting
    if not energy_profiler.check_server_online(timeout=1.0):
        logger.error("Energy server is not online, exiting")
        sys.exit(1)
    
    session_id = energy_profiler.start()
    if session_id == "[invalid_session_id_1]":
        logger.error("Failed to start energy profiling")
        sys.exit(1)
    time.sleep(10)
    energy, session_id = energy_profiler.finish()
    if session_id == "[invalid_session_id_2]":
        logger.error("Failed to finish energy profiling")
        sys.exit(1)
    logger.info(f"Energy: {energy} mJ")
    print(energy)
    sys.exit(0)
