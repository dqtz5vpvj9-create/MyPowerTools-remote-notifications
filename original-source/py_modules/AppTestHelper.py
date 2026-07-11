import os
from io import TextIOWrapper
import random
import time
import subprocess
import re
from typing import NewType, Optional, Tuple
import importlib, sys
from pathlib import Path
import os
import select
import subprocess
import time
from typing import Callable, Optional, Tuple, Any
import numpy as np
import psutil
from queue import Empty
import multiprocessing
import tempfile

from stat_tools.android_gettimecnt import get_device_timecnt
from py_modules.android_input_client import AndroidInputClient, InputClientError
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from . check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
if __name__ == '__main__':
    check_conda_interpreter(CONDA_ENV_NAME)

from . lib_aosp_base import *
from . lib_ref import Ref
from . logging_lib import MyLogger, setup_logging
import logging
from datetime import datetime as datetime_class
import psutil
from stat_tools.log_parser import parse_diskstats_to_dataframe_with_index
from test_tools.ci_base import mono_mills_range_t, vtc_range_t
from py_modules.lib_appium import AppiumWrapper, PreExecuteResult

# Util functions

from typing import Dict, Union
import os
import platform

uid_t = NewType('uid_t', int)
pid_t = NewType('pid_t', int)
ppid_t = NewType('ppid_t', int)
cmd_t = NewType('cmd_t', str)
name_t = NewType('name_t', str)

class SwipeType(Enum):
    UP = "UP"
    UP_FAST = "UP_FAST"
    DOWN_FAST = "DOWN_FAST"
    NEW = "NEW"

class dummyAppium:
    def __init__(self):
        self.should_stop = False
        pass
    def pre_execute_app(self, activity: str) -> PreExecuteResult:
        return PreExecuteResult(analysis="dummy", is_correct_state=True, possible_fix="dummy", screenshot_path="dummy")
    def close(self):
        pass

class AppTestHelper:
    def __init__(self, logger: MyLogger, appium: Optional[AppiumWrapper] = None, dummy_appium = False, device_serial: Optional[str] = None) -> None:
        self.logger: MyLogger = logger
        self.app_idx: int = 0
        self.device_serial = device_serial
        if dummy_appium:
            self.appium_wrapper: AppiumWrapper = dummyAppium()
            self.own_appium = False
        else:
            if not appium:
                self.appium_wrapper: AppiumWrapper = AppiumWrapper(device_serial or serial, logger, tempfile.gettempdir())
                self.own_appium = True
            else:
                self.appium_wrapper = appium
                self.own_appium = False

    def get_foreground_app(self, candidates: List[str] = []) -> Optional[str]:
        dumpsys_output: str = subprocess.check_output(
            ["adb", "-s", self.device_serial or serial, "shell", "/system/bin/sh", "-c", "'dumpsys window windows | grep mSurface=Surface'"],
            text=True)
        pattern: str = "mSurface=Surface\(name=(.*?)/"
        print(re.findall(pattern, dumpsys_output))
        matches: List[str] = [app for app in re.findall(pattern, dumpsys_output) if app in candidates]
        return matches[0] if len(matches) > 0 else None

    def am_start_app(self, activity: str) -> Tuple[str, str, int, int, mono_mills_range_t, vtc_range_t]:
        # diskstat_start_result_file: str = f"{self.test_setup.output_dir}/launch-start-app-{self.app_idx}-diskstats"
        # Aa("pull", "/proc/diskstats", diskstat_start_result_file)
        start_mono_mills, start_vtc, _ = get_device_timecnt(set_app_launching=None, device_serial=self.device_serial)
        # diskstat_end_result_file: str = f"{self.test_setup.output_dir}/launch-complete-app-{self.app_idx}-diskstats"
        self.app_idx += 1
        # Aa("pull", "/proc/diskstats", diskstat_end_result_file)
        # f_start: TextIOWrapper = open(diskstat_start_result_file)
        # f_end: TextIOWrapper = open(diskstat_end_result_file)
        # dstat_bg = parse_diskstats_to_dataframe_with_index(f_start.read())
        # dstat_ed = parse_diskstats_to_dataframe_with_index(f_end.read())
        # f_start.close()
        # f_end.close()
        # diff_inf_mem = dstat_ed - dstat_bg
        # diskstat_diff = diff_inf_mem[diff_inf_mem != 0].dropna(how='all')
        # diskstat_diff.to_csv(f"{self.test_setup.output_dir}/launch-diff-app-{self.app_idx}-diskstats")

        brief: List[str] = As(f"am start -W -n '{activity}'", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial).splitlines()
        end_mono_mills, end_vtc, _ = get_device_timecnt(set_app_launching=None, device_serial=self.device_serial)

        launch_state: str = 'UNKNOWN'
        started_activity: str = 'UNKNOWN'
        total_time: int = 0
        wait_time: int = 0

        for line in brief:
            if 'LaunchState' in line:
                launch_state = (line.split(': ')[1]).split(' ')[0]
            elif 'Activity' in line:
                started_activity = line.split(': ')[1]
            elif 'TotalTime' in line:
                total_time = int(line.split(': ')[1])
            elif 'WaitTime' in line:
                wait_time = int(line.split(': ')[1])

        return launch_state, started_activity, total_time, wait_time, (start_mono_mills, end_mono_mills), (start_vtc, end_vtc)

    def am_stop_app(self, package: str) -> str:
        return As(f"am force-stop {package}", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial)


    def stop_activities(self, activities: List[str]) -> None:
        for activity in activities:
            self.am_stop_app(activity.split('/')[0])
            self.logger.info(f"stopped {activity.split('/')[0]}")
        self.am_stop_app("com.android.managedprovisioning")
        self.am_stop_app("com.android.settings")
        self.am_stop_app("com.android.dialer")
        self.am_stop_app("com.android.dynsystem")
        self.am_stop_app("com.android.printspooler")
        self.am_stop_app("com.android.packageinstaller")
        self.am_stop_app("com.android.localtransport")
        As("killall bootanimition", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial)
        time.sleep(3)
        self.logger.info(f"stoped all apps")

    def find_running_processes(self, activity_uid_map: dict[str, int], main_process_only = False, include_zygote64_process = False) -> List[Tuple[uid_t, pid_t, ppid_t, cmd_t, name_t]]:
        zygote64_pid = As("pidof zygote64", AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
        # check zygote64 pid is valid
        if not zygote64_pid:
            self.logger.error("Cannot find zygote64 pid")
            return []

        cmd = f"adb -s {self.device_serial or serial} shell \"ps -o uid,pid,ppid,CMD,name\""
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        ps_ret = out.decode("utf-8").splitlines()

        # Parse ps_ret and filter the results
        valid_processes = []
        if include_zygote64_process:
            valid_processes.append((0, zygote64_pid, 1, 'main', 'zygote64'))
        for line in ps_ret:
            splits = line.split()
            if len(splits) >= 5:
                try:
                    uid = int(splits[0])
                    pid = int(splits[1])
                    ppid = int(splits[2])
                    cmd = str(splits[3])
                    name = str(' '.join(splits[4:]))

                    if int(ppid) == int(zygote64_pid) and uid in activity_uid_map.values():
                        valid_processes.append((uid, pid, ppid, cmd, name))
                except ValueError:
                    pass

        # Convert valid_processes to a NumPy array
        valid_processes_arr = np.array(valid_processes, dtype=[('uid', int), ('pid', int), ('ppid', int), ('cmd', object), ('name', object)])

        ret = []
        for activity, uid in activity_uid_map.items():
            pkg = activity.split('/')[0]
            # Search for processes with matching UID and package name
            if main_process_only:
                pkgname_predictor = np.char.equal(valid_processes_arr['name'].astype(str), pkg)
            else:
                pkgname_predictor = (np.char.find(valid_processes_arr['name'].astype(str), pkg) != -1)
            matching_processes = valid_processes_arr[(valid_processes_arr['uid'] == uid) & pkgname_predictor]
            for proc in matching_processes:
                ret.append((uid_t(proc['uid']), pid_t(proc['pid']), ppid_t(proc['ppid']), cmd_t(proc['cmd']), name_t(proc['name'])))
        return ret
    
    def find_running_process(self, pkg: str, uid: Optional[int]) -> List[Tuple[uid_t, pid_t, ppid_t, cmd_t, name_t]]:
        zygote64_pid = As("pidof zygote64", AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
        # check zygote64 pid is valid
        if not zygote64_pid:
            self.logger.error("Can not find zygote64 pid")
            return []
        cmd = f"adb -s {self.device_serial or serial} shell \"ps -o uid,pid,ppid,CMD,name\""
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        ps_ret = out.decode("utf-8").splitlines()

        ret = []
        for line in ps_ret:
            if pkg in line:
                splits = line.split()
                uid = int(splits[0])
                pid = int(splits[1])
                ppid = int(splits[2])
                cmd = splits[3]
                name = ' '.join(splits[4:])

                if int(ppid) == int(zygote64_pid) and int(uid) == uid:
                    ret.append((uid_t(uid), pid_t(pid), ppid_t(ppid), cmd_t(cmd), name_t(name)))
        return ret

    def use_phone_swipe(self, duration=30, period=0, swipe_type=SwipeType.UP):
        start = time.time()
        flag = True
        while True:
            if period > 0:
                time.sleep(period)
            
            if swipe_type == SwipeType.UP:
                As('input touchscreen swipe 930 880 930 480', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            elif swipe_type == SwipeType.UP_FAST:
                As('input touchscreen swipe 930 880 930 380 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            elif swipe_type == SwipeType.NEW:
                As('input touchscreen swipe 678 1607 943 1158 100', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=30, device_serial=self.device_serial)
            else:  # swipe_type == SwipeType.DOWN_FAST
                if flag:
                    As('input touchscreen swipe 930 880 930 380 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = False
                else:
                    As('input touchscreen swipe 930 380 930 880 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = True
            
            if start + duration < time.time():
                break

    def use_phone_swipe_with_autodroid_lock(self, duration=30, period=0, swipe_type=SwipeType.UP, stop_event = None, nr_scroll = None):
        start = time.time()
        flag = True
        nr_swipes = 0
        
        self.logger.notice(f"每 {period} 秒滑动屏幕一次")
        while ((stop_event is None) or (not stop_event.is_set())):
            if period > 0:
                time.sleep(period)
            
            nr_swipes += 1

            if swipe_type == SwipeType.UP:
                adb_shell("input touchscreen swipe 930 880 930 480", device_serial=self.device_serial)
            elif swipe_type == SwipeType.UP_FAST:
                adb_shell("input touchscreen swipe 930 880 930 380 80", device_serial=self.device_serial)
            elif swipe_type == SwipeType.NEW:
                adb_shell("input touchscreen swipe 678 1607 943 1158 100", device_serial=self.device_serial)
            else:  # swipe_type == SwipeType.DOWN_FAST
                if flag:
                    adb_shell("input touchscreen swipe 930 880 930 380 80", device_serial=self.device_serial)
                    flag = False
                else:
                    adb_shell("input touchscreen swipe 930 380 930 880 80", device_serial=self.device_serial)
                    flag = True

            # 检查持续时间是否已超过
            if start + duration < time.time():
                break
                
        if nr_scroll is not None and isinstance(nr_scroll, list) and len(nr_scroll) > 0:
            nr_scroll[0] = nr_swipes


    def use_phone_swipe_MIDDLE(self, duration=30, period=0, swipe_type=SwipeType.UP):
        start = time.time()
        flag = True
        while True:
            if period > 0:
                time.sleep(period)
            
            if swipe_type == SwipeType.UP:
                As('input touchscreen swipe 500 2000 500 600', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            elif swipe_type == SwipeType.UP_FAST:
                As('input touchscreen swipe 500 2000 500 600 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            else:  # swipe_type == SwipeType.DOWN_FAST
                if flag:
                    As('input touchscreen swipe 500 2000 500 600 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = False
                else:
                    As('input touchscreen swipe 500 600 500 2000 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = True
            
            if start + duration < time.time():
                break

    def use_phone_swipe_middle2(self, duration=30, period=0, swipe_type=SwipeType.UP):
        start = time.time()
        flag = True
        while True:
            if period > 0:
                time.sleep(period)
            
            if swipe_type == SwipeType.UP:
                As('input touchscreen swipe 500 1000 500 600', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            elif swipe_type == SwipeType.UP_FAST:
                As('input touchscreen swipe 500 1000 500 500 100', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            else:  # swipe_type == SwipeType.DOWN_FAST
                if flag:
                    As('input touchscreen swipe 500 1000 500 500 100', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = False
                else:
                    As('input touchscreen swipe 500 500 500 1000 100', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = True
            
            if start + duration < time.time():
                break

    def use_phone_swipe_left(self, duration=30, period=0, swipe_type=SwipeType.UP):
        start = time.time()
        flag = True
        while True:
            if period > 0:
                time.sleep(period)
            
            if swipe_type == SwipeType.UP:
                As('input touchscreen swipe 142 880 142 480', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            elif swipe_type == SwipeType.UP_FAST:
                As('input touchscreen swipe 142 880 142 480 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
            else:  # swipe_type == SwipeType.DOWN_FAST
                if flag:
                    As('input touchscreen swipe 142 880 142 480 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = False
                else:
                    As('input touchscreen swipe 142 480 142 880 80', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    flag = True
            
            if start + duration < time.time():
                break

    def use_app(self, duration, period=0):
        self.use_phone_swipe(duration=duration/3, period=period, swipe_type=SwipeType.UP_FAST)
        self.use_phone_swipe(duration=duration/3, period=period, swipe_type=SwipeType.DOWN_FAST)
        self.use_phone_swipe(duration=duration/3, period=period, swipe_type=SwipeType.UP)
            
    def use_app_qqdownloader(self, duration, period=0):
        self.use_phone_swipe_MIDDLE(duration=duration/3, period=period, swipe_type=SwipeType.UP_FAST)
        self.use_phone_swipe_MIDDLE(duration=duration/3, period=period, swipe_type=SwipeType.DOWN_FAST)
        self.use_phone_swipe_MIDDLE(duration=duration/3, period=period, swipe_type=SwipeType.UP)

    # def use_app_qqmusic(self, duration, period=0):
    #     self.use_phone_swipe_middle2(duration=duration/3, period=period, swipe_type=SwipeType.UP_FAST)
    #     self.use_phone_swipe_middle2(duration=duration/3, period=period, swipe_type=SwipeType.DOWN_FAST)
    #     self.use_phone_swipe_middle2(duration=duration/3, period=period, swipe_type=SwipeType.UP)
    
    def use_app_easybike(self, duration, period=0):
        self.use_phone_swipe_left(duration=duration/3, period=period, swipe_type=SwipeType.UP_FAST)
        self.use_phone_swipe_left(duration=duration/3, period=period, swipe_type=SwipeType.DOWN_FAST)
        self.use_phone_swipe_left(duration=duration/3, period=period, swipe_type=SwipeType.UP)

    def use_app_slack(self, duration, period=0):
        self.use_phone_swipe(duration=duration/2, period=period, swipe_type=SwipeType.UP_FAST)
        self.use_phone_swipe(duration=duration/2, period=period, swipe_type=SwipeType.UP)

    def swipe_up(self) -> None:
        As("input swipe 300 300 500 1000", AsOption.STDERR_TO_STDOUT, device_serial=self.device_serial)

    def swipe_down(self) -> None:
        print("swipe_down")
        As("input swipe 500 1000 300 300", AsOption.STDERR_TO_STDOUT, device_serial=self.device_serial)
