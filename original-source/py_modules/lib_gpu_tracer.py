import re
import threading
import subprocess
import os
import json
import json
import traceback
from typing import OrderedDict, Optional
import pandas as pd
import numpy as np
import os
import socket
import os
import threading
import signal
from datetime import datetime as datetime_class
import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path


def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError: # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from py_modules.AppTestHelper import AppTestHelper, SwipeType
from py_modules.lib_aosp_base import *
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_appium import AppiumWrapper, PreExecuteResult
from test_tools.am_pm_utils import get_foreground_activities, get_last_displayId
from test_tools.ci_base import Ci_Base

from py_modules.lib_frame_processor_inner import process_frame_trace
from py_modules.lib_perfetto_config import gen_frecords_perfetto_config

class GPUMetricsCollector:
    def __init__(self, logger, device_serial: Optional[str] = None):
        self.logger = logger
        self.device_serial = device_serial
        self.busy_list = []
        self.elapsed_list = []
        self.raw_output = []
        self.collection_thread = None
        self.stop_event = threading.Event()
    
    def start(self):
        try:
            # 启动收集线程
            self.collection_thread = threading.Thread(target=self._collect_metrics)
            self.collection_thread.daemon = True
            self.collection_thread.start()
            self.logger.verbose("已启动GPU指标收集线程")
            return True
        except Exception as e:
            self.logger.error(f"启动GPU指标收集时出错: {e}")
            return False
    
    def _collect_metrics(self):
        sampling_interval = 0.5  # 采样间隔，单位秒
        try:
            while not self.stop_event.is_set():
                # 读取 gpubusy 文件 - 每次读取后会自动清零
                gpubusy_output = As('taskset 0F cat /sys/class/kgsl/kgsl-3d0/gpubusy', [AsOption.STDOUT_NO_PRINT], device_serial=self.device_serial)
                gpubusy_output = gpubusy_output.strip()
                
                if gpubusy_output:
                    timestamp = datetime_class.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                    entry = f"{timestamp}: {gpubusy_output}"
                    self.raw_output.append(entry)
                    
                    values = gpubusy_output.split()
                    if len(values) == 2:
                        busy = int(values[0])
                        elapsed = int(values[1])
                        # 直接添加到列表中，每个样本都是自上次读取后的增量
                        self.busy_list.append(busy)
                        self.elapsed_list.append(elapsed)
                
                # 等待下一次采样
                time.sleep(sampling_interval)
        except Exception as e:
            if not self.stop_event.is_set():  # 仅在非停止状态下记录
                self.logger.error(f"GPU指标收集过程中出错: {e}")
                print(traceback.format_exc())
    
    def stop(self):
        try:
            self.stop_event.set()
            
            if self.collection_thread and self.collection_thread.is_alive():
                self.collection_thread.join(timeout=5)
            
            # 返回结果
            return self.get_results()
        except Exception as e:
            self.logger.error(f"停止GPU指标收集时出错: {e}")
            return self.get_results()
    
    def get_results(self):
        # 直接汇总所有样本来计算平均使用率
        total_busy = sum(self.busy_list) if self.busy_list else 0
        total_elapsed = sum(self.elapsed_list) if self.elapsed_list else 1  # 避免除零
        
        gpu_usage_avg = total_busy / total_elapsed if total_elapsed > 0 else 0
        
        return {
            'gpu_usage_raw_output': self.raw_output,
            'gpu_busy_list': self.busy_list,
            'gpu_elapsed_list': self.elapsed_list,
            'gpu_usage_avg': gpu_usage_avg,
            'samples_count': len(self.raw_output)
        }