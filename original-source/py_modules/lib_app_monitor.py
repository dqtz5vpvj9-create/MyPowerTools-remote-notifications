import os, subprocess, re, threading, multiprocessing, time
from test_tools.ci_base import Ci_Base, timespec_t
from typing import Optional, List, Callable
from multiprocessing.synchronize import Event as EventClass
from datetime import datetime as datetime_class

from py_modules.lib_sh import shell_run
from py_modules.lib_aosp_base import AsOption, As, Aa
from py_modules.logging_lib import MyLogger
from py_modules.lib_aosp_testing import slugify_path
from stat_tools.android_gettimecnt import get_device_timecnt
from stat_tools.android_cpu_stat_tool import parse_proc_stat_extended
from stat_tools.android_disk_stat_tool import get_device_mapper_info, get_df_command_info, get_loop_device_info
from py_modules.lib_frame_processor import FrameRecorder
from py_modules.lib_perfetto_config import gen_frecords_perfetto_config

def parse_proc_stat(pid: int) -> Optional[str]:
    d = parse_proc_stat_extended(pid)
    tid = d.get('tid')
    tcomm = d.get('tcomm')
    min_flt = d.get('min_flt')
    maj_flt = d.get('maj_flt')
    utime = d.get('utime')
    stime = d.get('stime')
    rss = d.get('rss')
    blkio_delay = d.get('blkio_delay')
    swapin_delay = d.get('swapin_delay')
    return f"{tid},{tcomm},{utime},{stime},{min_flt},{maj_flt},{rss},{blkio_delay},{swapin_delay}"

import re

class BaseMonitor:
    def __init__(self, ci_code: Ci_Base, serial: str, ASRCDIR: str, logger: MyLogger, do_perf: bool) -> None:
        self.ci_code = ci_code
        self.serial  = serial
        self.ASRCDIR = ASRCDIR
        self.logger  = logger

        self.do_perf = do_perf
        self.perf_details: list[BaseMonitor.PerfDetail] = []

        self.test_end = False
        self.stop_event = multiprocessing.Event()
        self.last_pid: Optional[int] = None
        self.last_reason: str = 'TestStart'
        self.timed_task_threads: list[threading.Thread] = []
        self.monitor_threads: list[threading.Thread] = []
        self.wb_enabled = self.is_wb_enabled()
        self.df_info_file = f"{ci_code.test_report_folder}/df_command_info.csv"
        self.dm_info_file = f"{ci_code.test_report_folder}/device_mapper_info.csv"
        self.loop_info_file = f"{ci_code.test_report_folder}/loop_devices_info.csv"
        self.prepare_disk_info()
        self.is_micro_benchmark = False
        self.logger.info("BaseMonitor initialized")
    
    def prepare_disk_info(self):
        get_device_mapper_info().to_csv(self.dm_info_file, index=False)
        get_df_command_info().to_csv(self.df_info_file, index=False)
        get_loop_device_info().to_csv(self.loop_info_file, index=False)


        
        
    def is_wb_enabled(self) -> bool:
        try:
            backing_dev = As("cat /sys/block/zram0/backing_dev").rstrip("\n")
            if backing_dev.startswith("/dev/block"):
                self.logger.info("Writeback is enabled")
                return True
        except Exception as e:
            self.logger.info("Writeback is disabled")
            return False


    class PerfDetail:
        """This class is used to store details about a `perf` process
        """
        def __init__(self, perf_thread: threading.Thread, target_pid: int, stop_event: EventClass) -> None:
            self.perf_thread = perf_thread
            self.target_pid = target_pid
            self.stop_event: EventClass = stop_event


    def perf_proc(self, pid: int, reason: str) -> None:
        """This function starts a `perf` process in a shell_run thread to record 
        performance data for a given process

        Args:
            pid (int): The process ID to record performance data for
            reason (str): The reason for recording performance data, used to name the output file
        """
        reason = reason.replace('.', '_')
        call_graph_option = " --call-graph fp " if self.ci_code.config.sunfish else "-g"
        # perf_cmd = f"adb -s {self.serial} shell " + "'" + f"cd /data/local/tmp/ && simpleperf-master record -e cpu-cycles,major-faults,cache-misses {call_graph_option} -p {pid} -o perf-{pid}-{reason}.data" + "'"
        perf_cmd = f"adb -s {self.serial} shell " + "'" + f"cd /data/local/tmp/ && simpleperf record -e cpu-cycles,major-faults,cache-misses {call_graph_option} -p {pid} -o perf-{pid}-{reason}.data" + "'"
        # perf_cmd = f"adb -s {self.serial} shell " + "'" + f"cd /data/local/tmp/ && simpleperf record -c 1 -e major-faults {call_graph_option} -p {pid} -o perf-{pid}-{reason}.data" + "'"
        stop_event = multiprocessing.Event()
        perf_thread = threading.Thread(target=shell_run, kwargs={"cmd": perf_cmd, "cwd": self.ASRCDIR, "stop_event": stop_event, "check_error": False, "friendly_cmd_name": f"perf {reason}", "callback": lambda _: True})
        perf_thread.start()
        self.perf_details.append(self.PerfDetail(perf_thread, pid, stop_event))
        # self.logger.warning(f"perf_proc exit")
            

    def update_last_logcat_time(self, line_time) -> None:
        is_time_line = True
        for digit in [0, 1]:
            if not line_time[digit].isdigit():
                is_time_line = False
                break
        micro_second_len = len("000000")
        micros = int(line_time[-micro_second_len:])
        micros += 1
        line_time = line_time[:-micro_second_len] + str(micros).zfill(micro_second_len)
        if is_time_line:
            self.last_logcat_time = line_time
        # print(f"Update last_logcat_time to {self.last_logcat_time}")

    # Return True if line should be filtered
    # Update last_logcat_time if line is newer
    def filter_and_update_logcat_time(self, line: str) -> bool:
        if line.startswith("---------"):
            return True
        # print(line)
        excepted_len = len("01-01 00:00:00.000000")
        line_time = line[:excepted_len]
        if len(line_time) != excepted_len:
            return True
        # parse line to python time
        line_time_spec = datetime_class.strptime(line_time, "%m-%d %H:%M:%S.%f")
        last_logcat_time_spec = datetime_class.strptime(self.last_logcat_time, "%m-%d %H:%M:%S.%f")
        if line_time_spec <= last_logcat_time_spec:
            return True
        self.update_last_logcat_time(line_time)
        return False


    def trigger_zram_swap_log(self, pid: Optional[int], reason: Optional[str], reset: bool, sequential_test: bool = True) -> None:
        timespec = get_device_timecnt()
        reason = 'TestEnd' if self.test_end else reason
        assert reason is not None
        self.ci_code.record_zram_statistics(timespec, reason, pid)
        if sequential_test:
            if not self.is_micro_benchmark:
                self.logger.notice(f"Periodically record zram and swap info, parameter: pid = {pid}, reason = {reason}, reset = {reset}, but record file name is determined by last_pid {self.last_pid} and last_reason {self.last_reason}")
            self.ci_code.record_swap_statistics(self.last_pid, self.last_reason)
            self.last_pid = pid
            self.last_reason = reason
        else:
            if not self.is_micro_benchmark:
                self.logger.notice(f"Periodically record zram and swap info, parameter: pid = {pid}, reason = {reason}, reset = {reset}")
            self.ci_code.record_swap_statistics(pid, reason)
        if reset:
            self.ci_code.reset_swap_statistics()

    def start_monitor_base(self, timed_task_functions: List[Callable], output_monitor_functions: List[Callable]) -> None:
        self.last_logcat_time = "01-01 00:00:00.000000"
        with open(self.ci_code.zram_stat_fn, "w") as zram_stat_f:
            zram_stat_f.write("mono_time,vct,ratio,reason,readIO,readTicks,writeIO,writeTicks,pid\n")
        for func in timed_task_functions:
            timed_task_thread = threading.Thread(target=func)
            timed_task_thread.start()
            self.logger.info(f"Starting timed task thread {timed_task_thread}")
            self.timed_task_threads.append(timed_task_thread)
        for func in output_monitor_functions:
            monitor_thread = threading.Thread(target=func)
            monitor_thread.start()
            self.logger.info(f"Starting monitor thread {monitor_thread}")
            self.monitor_threads.append(monitor_thread)
    
    def wait_stop(self):
        for th in self.timed_task_threads:
            self.logger.warning(f"Waiting for timed task thread {th}")
            th.join()
        for th in self.monitor_threads:
            self.logger.warning(f"Waiting for monitor thread {th}")
            th.join()


class PCMarkMonitor(BaseMonitor):
    
    def __init__(self, ci_code: Ci_Base, serial: str, ASRCDIR: str, logger: MyLogger, do_perf: bool) -> None:
        super().__init__(ci_code, serial, ASRCDIR, logger, do_perf)
        
        self.main_proc_pattern = re.compile(r"Start proc (\d+):com.futuremark.pcmark.android.benchmark.*for.*top-activity (.*)")
        self.webview_pattern = re.compile(r"Start proc (\d+):com.android.webview:sandboxed_process.*for.*com.futuremark.pcmark.android.benchmark")
        self.main_proc_pid: Optional[int] = None
        self.workload_name: Optional[str] = None
        self.workload_pid: Optional[int] = None

        self.main_proc_stat_fn    = ci_code.test_report_folder + "/main_proc_stat.csv"
        self.proc_stat_fn         = ci_code.test_report_folder + "/proc_stat.csv"
        self.meminfo_fn           = ci_code.test_report_folder + "/meminfo.csv"
        self.main_proc_meminfo_fn = ci_code.test_report_folder + "/main_proc_meminfo.csv"

    def proc_stat_puller(self):
        with open(self.proc_stat_fn, "w") as proc_stat_f, \
             open(self.main_proc_stat_fn, "w") as main_proc_stat_f:
             
            proc_stat_f.write("mono_time,vct,ratio,pid,workload,tid,tcomm,utime,stime,min_flt,maj_flt,rss,blkio_delay,swapin_delay\n")
            main_proc_stat_f.write("mono_time,vct,ratio,pid,workload,tid,tcomm,utime,stime,min_flt,maj_flt,rss,blkio_delay,swapin_delay\n")

            print("proc_stat_puller started")

            while not self.stop_event.is_set():
                self.ci_code.record_zram_mm_stat_record()
                if self.test_end:
                    self.logger.info("proc_stat_puller ended")
                    break
                if self.workload_pid:
                    # Assuming these methods are defined elsewhere in the script
                    try:
                        mono_time, vct, ratio = get_device_timecnt()
                        try:
                            main_proc_stat_data = parse_proc_stat(self.main_proc_pid)
                        except Exception as e:
                            main_proc_stat_data = None
                            self.logger.info(f"Failed to parse main_proc_stat_data: {e}")
                        if main_proc_stat_data:
                            main_proc_stat_f.write(f"{mono_time},{vct},{ratio},{self.main_proc_pid},{self.workload_name},{main_proc_stat_data}\n")
                            main_proc_stat_f.flush()

                        try:
                            proc_stat_data = parse_proc_stat(self.workload_pid)
                        except Exception as e:
                            proc_stat_data = None
                            self.logger.info(f"Failed to parse proc_stat_data: {e}")
                        if proc_stat_data:
                            proc_stat_f.write(f"{mono_time},{vct},{ratio},{self.workload_pid},{self.workload_name},{proc_stat_data}\n")
                            proc_stat_f.flush()
                        if self.workload_pid != self.main_proc_pid:
                            # As(f"taskset -ap c0 {self.workload_pid}", [AsOption.STDOUT_NO_PRINT])
                            pass
                    except Exception as e:
                        self.logger.info(f"Failed to record proc_stat: {e}")
                        pass
                    
                    time.sleep(1)

    def ac_callback(self, line: str) -> bool:
        if self.filter_and_update_logcat_time(line):
            return True

        match = self.main_proc_pattern.search(line)
        pid: Optional[int] = None
        if match or self.test_end:
            if match:
                pid = int(match.group(1))
                grace_wait = False
                assert pid is not None
                if self.main_proc_pid is None:
                    self.main_proc_pid = pid
                    grace_wait = True
                    self.logger.notice(f"Activity Manager显示 PCMark 主进程 {pid} 开始运行")
                self.workload_pid = pid
                self.workload_name = match.group(2)
                assert self.workload_name is not None
                # remove "com.futuremark.pcmark.android.benchmark/" from workload_name
                self.workload_name = self.workload_name.removeprefix("{com.futuremark.pcmark.android.benchmark/")
                self.workload_name = self.workload_name.removesuffix("}")
                self.logger.notice(f"Activity Manager显示 PCMark Workload {self.workload_name} {pid} 开始运行")
                if pid != self.main_proc_pid:
                    # As(f"taskset -ap c0 {pid}", [AsOption.STDOUT_NO_PRINT])
                    pass
                if self.do_perf:
                    self.perf_proc(pid, self.workload_name)
            
            self.trigger_zram_swap_log(pid, self.workload_name, True)
        # webview_match = self.webview_pattern.search(line)
        # if webview_match:
        #     webview_pid = int(webview_match.group(1))
        #     print(f"Webview pid: {webview_pid}")
        #     if args.perf:
        #         self.perf_proc(webview_pid, True)
        if self.test_end:
            print("ac_callback ended")
            self.stop_event.set()
            return False
        
        return True
    
    def start_monitor(self):
        def monitor_func():
            while not self.stop_event.is_set():
                activity_manager_cmd = f"logcat -v usec -b system -d -s ActivityManager -t \"{self.last_logcat_time}\""
                activity_manager_cmd_full = f"adb -s {self.serial} wait-for-device shell '" + activity_manager_cmd + "'"
                # print current time ms
                try:
                    output = subprocess.check_output(activity_manager_cmd_full, shell=True, encoding='utf-8', text=True)
                    # measure speed of processing each line
                    lines = output.splitlines()
                    for line in lines:
                        self.ac_callback(line)
                except subprocess.CalledProcessError:
                    pass
                # Test end has to be checked later, allowing last logcat lines been parsed
                if self.test_end:
                    break
                crash_log_cmd = f"logcat -b crash -d *:F -t \"{self.last_logcat_time}\""
                crash_log_cmd_full = f"adb -s {self.serial} wait-for-device shell '" + crash_log_cmd + "'"
                try:
                    if self.workload_pid:
                        output = subprocess.check_output(crash_log_cmd_full, shell=True, encoding='utf-8', text=True)
                        if "Primitive char conversion on invalid type" in output or "crash signal" in output:
                            self.logger.fatal(f"Crash detected: {output.splitlines()[0]}")
                            self.test_end = True
                            self.stop_event.set()
                            # Wait for complete logcat output and save it to file
                            time.sleep(5)
                            with open(f"{self.ci_code.test_report_folder}/crash.log", "w") as f:
                                f.write(output)
                except subprocess.CalledProcessError:
                    pass
                time.sleep(1)
            self.logger.info("PCMark monitor_func Terminated")
        
        self.start_monitor_base([self.proc_stat_puller], [monitor_func])

import pandas as pd
import os
class RunningAppInfo:
    def __init__(self, uid: int, comm: str) -> None:
        self.uid = uid
        self.comm = comm

class MultiAppMonitor(BaseMonitor):
    def __init__(self, ci_code: Ci_Base, serial: str, ASRCDIR: str, logger: MyLogger, do_perf: bool, test_BGC: bool = False, test_BGC_bind_big: bool = False, is_fg_marvin = False) -> None:
        super().__init__(ci_code, serial, ASRCDIR, logger, do_perf)
        self.proc_stat_fn = f"{ci_code.test_report_folder}/proc_stat.csv"
        self.running_app_pid_comm_map: dict[int, RunningAppInfo] = {}
        self.proc_start_pattern = re.compile(r"SET_MEMCG: pid = (\d+), comm = (.*), uid = (.*)")
        self.proc_start_pattern_old = re.compile(r"SET_MEMCG: pid = (\d+), comm = (.*)")
        self.gc_remap_cnt_pattern = re.compile(
            r"CompactGCRemapCnt, (RequestCMSTransition|DoCMSTransition|DoneCMSTransition|RequestCompactTransition|DoCompactTransition|DoneCompactTransition), ([-]?\d+), (\d+), (FG|BG), (IGNORED|EXECUTED|Dummy), (.+)")
        self.gc_remap_cnt_report_df = pd.DataFrame(columns=["type", "remap_cnt", "pid", "processName", "idx"])
        self.compact_gc_idx_map: dict[int, int] = {}
        self.do_proc_stat_ASAP_pids: List[int] = []
        self.do_proc_stat_ASAP_event = threading.Event()
        self.frame_recorder_threads: List[threading.Thread] = []
        self.test_BGC = test_BGC
        self.test_BGC_bind_big = test_BGC_bind_big
        self.marvin_app_stop_event = threading.Event()
        self.marvin_app_stable_allocation_start_event = threading.Event()
        self.marvin_thread_started_event = threading.Event()
        self.marvin_process_id = None
        self.marvin_thread_tid = None
        self.marvin_gc_thread_tid = None
        self.marvin_main_thread_tid = None
        self.is_fg_marvin = is_fg_marvin
        if self.is_fg_marvin:
            self.fg_marvin_result_file_fn = f"{ci_code.test_report_folder}/test-marvin.log"
            self.fg_marvin_result_file = open(self.fg_marvin_result_file_fn, "w")
            self.last_marvin_result = None

        
    
    def do_proc_stat_inner(self, proc_stat_f, running_app_pids: List[int]):
        dead_app_pids = []
        for app_pid in running_app_pids:
            app_comm = self.running_app_pid_comm_map[app_pid]
            try:
                mono_time, vct, ratio = get_device_timecnt()
                try:
                    proc_stat_data = parse_proc_stat(app_pid)
                    # logger.warning(f"MULTIAPP app {app_comm} pid {app_pid} is running")
                except:
                    # app is not running
                    dead_app_pids.append(app_pid)
                    # logger.warning(f"MULTIAPP app {app_comm} pid {app_pid} is not running")
                    proc_stat_data = None
                if proc_stat_data:
                    proc_stat_f.write(f"{mono_time},{vct},{ratio},{app_pid},{app_comm.comm},{proc_stat_data},{app_comm.uid}\n")
                    proc_stat_f.flush()
            except Exception as e:
                self.logger.error(f"MULTIAPP failed to record proc_stat: {e}")
                pass
        return dead_app_pids

    def proc_stat_monitor(self):
        with open(self.proc_stat_fn, 'w') as proc_stat_f:
            proc_stat_f.write("mono_time,vct,ratio,pid,comm,tid,tcomm,utime,stime,min_flt,maj_flt,rss,blkio_delay,swapin_delay,uid\n")
            self.logger.info("MULTIAPP start proc_stat_monitor")
            epoch_wait = 1.0
            inner_wait = 0.05
            sum_inner_wait = 0.0
            while True:
                if self.test_end:
                    self.ci_code.record_zram_mm_stat_record()
                    if self.wb_enabled:
                        self.ci_code.record_zram_bd_stat()
                    self.logger.info("MULTIAPP test ended, stop proc_stat_monitor")
                    break
                
                self.ci_code.record_zram_mm_stat_record()
                if self.wb_enabled:
                    self.ci_code.record_zram_bd_stat()
                while True:
                    time.sleep(inner_wait)
                    sum_inner_wait += inner_wait
                    do_proc_stat_now = False
                    if self.do_proc_stat_ASAP_event.is_set():
                        self.do_proc_stat_ASAP_event.clear()
                        do_proc_stat_now = True
                    if sum_inner_wait >= epoch_wait or do_proc_stat_now:
                        dead_app_pids = self.do_proc_stat_inner(proc_stat_f, list(self.running_app_pid_comm_map.keys()))
                        # remove dead apps from running_app_pid_comm_map
                        for dead_app_pid in dead_app_pids:
                            # print(f"MULTIAPP remove dead app {dead_app_pid}")
                            del self.running_app_pid_comm_map[dead_app_pid]
                        sum_inner_wait = 0.0
                        break


    def start_frame_recorder(self, activity: str):
        package = activity.split('/')[0]
        frame_record_app_pkgs: list[str] = [
            "com.skype.raider",
        ]
        if package in frame_record_app_pkgs:
            # enable frame record
            frame_recorder = FrameRecorder(self.ci_code.test_report_folder, self.logger)
            th = threading.Thread(target=frame_recorder.record, kwargs={"package": package})
            th.start()
            self.frame_recorder_threads.append(th)

    def join_frame_recorder_threads(self):
        for th in self.frame_recorder_threads:
            th.join()
    
    def activity_manager_output_callback(self, line: str) -> bool:
        # if self.filter_and_update_logcat_time(line):
        #     return True

        start_match = self.proc_start_pattern.search(line)
        start_match_old = self.proc_start_pattern_old.search(line)
        if start_match or start_match_old or self.test_end:
            
            app_pid: Optional[int] = None
            app_comm: Optional[str] = None
            if start_match_old is not None:
                app_pid = int(start_match_old.group(1))
                if start_match is not None:
                    app_comm = start_match.group(2) # Must use the new format otherwise the comm will contain the uid
                    uid = start_match.group(3)
                else:
                    app_comm = start_match_old.group(2)
                    uid = -1
                self.logger.info(f"Application {app_comm} (uid = {uid}) pid {app_pid} forked")
                self.running_app_pid_comm_map[app_pid] = RunningAppInfo(int(uid), app_comm)
                As(f"echo {app_pid} > /proc/lxr_set_app_main_thread_tid", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT])
                if self.test_BGC:
                    self.marvin_process_id = app_pid
                else:
                    self.do_proc_stat_ASAP_pids.append(app_pid)
                    self.do_proc_stat_ASAP_event.set()
                if self.do_perf:
                    self.perf_proc(app_pid, app_comm)

            self.trigger_zram_swap_log(app_pid, app_comm, False)

        
        return True
    
    def bgc_logcat_output_callback(self, line: str) -> bool:
        if self.test_BGC:
            with open(os.path.join(self.ci_code.test_report_folder, "marvin_app.log"), "a") as f:
                f.write(line + "\n")
        if self.test_BGC and "Marvin" in line:
            if "Marvin Allocator thread started" in line:
                assert self.marvin_process_id is not None
                marvin_pid = self.marvin_process_id
                while True:
                    try:
                        ps_ret = As(f"ps -AT -o pid,tid,CMD | grep {marvin_pid}", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT]).splitlines()
                        for line in ps_ret:
                            if str(marvin_pid) in line:
                                if 'HeapTaskDaemon' in line:
                                    _, tid, _ = line.split()
                                    self.marvin_gc_thread_tid = int(tid)
                                elif 'MarvinThread' in line:
                                    _, tid, _ = line.split()
                                    self.marvin_main_thread_tid = int(tid)
                                else:
                                    splits = line.split()
                                    As(f"echo {splits[1]} > /dev/cpuset/tasks", [AsOption.STDOUT_NO_PRINT])
                        assert self.marvin_gc_thread_tid is not None
                        assert self.marvin_main_thread_tid is not None
                        if self.test_BGC_bind_big:
                            As(f"echo {self.marvin_gc_thread_tid} > /dev/cpuset/tasks", [AsOption.STDOUT_NO_PRINT])
                            As(f"echo {self.marvin_main_thread_tid} > /dev/cpuset/tasks", [AsOption.STDOUT_NO_PRINT])
                            As(f"taskset -p 40 {self.marvin_gc_thread_tid}", [AsOption.STDOUT_NO_PRINT])
                            As(f"taskset -p 80 {self.marvin_main_thread_tid}", [AsOption.STDOUT_NO_PRINT])
                            self.logger.info(f"First time Marvin GC thread {self.marvin_gc_thread_tid} and main thread {self.marvin_main_thread_tid} taskset binded")
                        break
                    except Exception as e:
                        self.logger.error(f"Failed to get ps info: {e}")
                self.marvin_thread_started_event.set()
            if "Marvin Allocator end" in line:
                self.marvin_app_stop_event.set()
            if 'Marvin Allocator Stable Run start' in line:
                self.marvin_app_stable_allocation_start_event.set()
                assert self.marvin_process_id is not None
                self.do_proc_stat_ASAP_pids.append(self.marvin_process_id)
                self.do_proc_stat_ASAP_event.set()

    def get_compact_gc_swap_stat(self, pid, reason):
        swapin_cmd = f"adb -s {self.serial} shell 'cat /proc/lxr_swapin_statistics_launching'"
        swapin_output = subprocess.check_output(swapin_cmd, shell=True, encoding='utf-8')

        swapout_cmd = f"adb -s {self.serial} shell 'cat /proc/lxr_swapout_statistics_launching'"
        swapout_output = subprocess.check_output(swapout_cmd, shell=True, encoding='utf-8')

        # 将输出保存到响应文件中
        launching_fn = slugify_path(f"swap-{pid}-{reason}.txt")
        folder = os.path.join(self.ci_code.test_report_folder, "compact_gc_swap_stat")
        os.makedirs(folder, exist_ok=True)
        with open(f"{folder}/{launching_fn}", "w") as swap_file:
            swap_file.write(swapin_output)
            swap_file.write(swapout_output)
    
    def compact_gc_logcat_output_callback(self, line: str) -> bool:
        match = self.gc_remap_cnt_pattern.search(line)
        if match:
            transition_type = match.group(1)
            remap_count = match.group(2)
            pid = int(match.group(3))
            fg_type = match.group(4)
            exec_type = match.group(5)
            process_name = match.group(6)
            if "CMS" in transition_type:
                gc_type = "CMS"
                transition_type = transition_type.replace("CMS", "")
            elif "Compact" in transition_type:
                gc_type = "Compact"
                transition_type = transition_type.replace("Compact", "")
            self.logger.info(f"Transition: {transition_type}, Remap Count: {remap_count}, Pid: {pid}, Process Name: {process_name}")
            if transition_type == "RequstTransition":
                if pid in self.compact_gc_idx_map:
                    self.compact_gc_idx_map[pid] += 1
                else:
                    self.compact_gc_idx_map[pid] = 0
            compact_gc_idx = self.compact_gc_idx_map.get(pid, 0)
            if transition_type == "DoTransition":
                self.get_compact_gc_swap_stat(pid, f"{process_name}-{compact_gc_idx}-Start")
            elif transition_type == "DoneTransition":
                self.get_compact_gc_swap_stat(pid, f"{process_name}-{compact_gc_idx}-End")
            self.gc_remap_cnt_report_df = pd.concat([self.gc_remap_cnt_report_df if not self.gc_remap_cnt_report_df.empty else None, pd.DataFrame([{
                "type": transition_type,
                "remap_cnt": remap_count,
                "pid": pid,
                "processName": process_name,
                "idx": compact_gc_idx,
                "gc_type": gc_type,
                "fg_type": fg_type,
                "exec_type": exec_type,
            }])])
            self.gc_remap_cnt_report_df.to_csv(f"{self.ci_code.test_report_folder}/compact_gc_remap_cnt.csv", index=False)
            return True
        return False
    
    def logcat_output_monitor_func(self):
        while True:
            am_cmd = f"logcat -v usec -d -t \"{self.last_logcat_time}\""
            am_cmd_full = f"adb -s {self.serial} wait-for-device shell '{am_cmd}'"
            try:
                output = subprocess.check_output(am_cmd_full, shell=True, text=True, encoding='utf-8', errors='backslashreplace')
                lines = output.splitlines()
                if len(lines) == 0:
                    continue
                idx = 0
                while idx < len(lines) and self.filter_and_update_logcat_time(lines[idx]):
                    idx += 1
                while idx < len(lines):
                    if "SET_MEMCG" in lines[idx]:
                        self.activity_manager_output_callback(lines[idx])
                    if self.is_fg_marvin and "MICRO_BENCHMARK" in lines[idx]:
                        self.micro_benchmark_output_callback(lines[idx])
                    if self.test_BGC:
                        self.bgc_logcat_output_callback(lines[idx])
                    if "CompactGCRemapCnt" in lines[idx]:
                        self.compact_gc_logcat_output_callback(lines[idx])
                    idx += 1
                self.filter_and_update_logcat_time(lines[-1])
            except subprocess.CalledProcessError:
                pass
            if self.test_end:
                self.stop_event.set()
                break
            time.sleep(1)
        self.logger.info("MULTIAPP test ended, stop logcat_output_monitor_func")
    
    def micro_benchmark_output_callback(self, line: str) -> bool:
        self.fg_marvin_result_file.write(line + "\n")
        self.fg_marvin_result_file.flush()
        self.last_marvin_result = line
        if "Marvin Allocator thread started" in line:
            self.marvin_thread_started_event.set()
        return True

    
    def marvin_bind_core_thread(self):
        while self.test_BGC and not self.marvin_app_stop_event.is_set():
            try:
                if self.marvin_gc_thread_tid and self.marvin_main_thread_tid and self.marvin_thread_started_event.is_set():
                    As(f"echo {self.marvin_gc_thread_tid} > /dev/cpuset/tasks", [AsOption.STDOUT_NO_PRINT])
                    As(f"echo {self.marvin_main_thread_tid} > /dev/cpuset/tasks", [AsOption.STDOUT_NO_PRINT])
                    As(f"taskset -p 40 {self.marvin_gc_thread_tid}", [AsOption.STDOUT_NO_PRINT])
                    As(f"taskset -p 80 {self.marvin_main_thread_tid}", [AsOption.STDOUT_NO_PRINT])
                    self.logger.info(f"Periodic Marvin GC thread {self.marvin_gc_thread_tid} and main thread {self.marvin_main_thread_tid} taskset binded")
                    As(f"ps -AT -o pid,tid,NAME,CMD,%cpu,psr | grep {self.marvin_process_id}", [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT])
                if self.test_end:
                    break
            except Exception as e:
                self.logger.error(f"Failed to bind Marvin threads: {e}")
            finally:
                time.sleep(1)
        self.logger.info("Marvin bind core thread terminated")
    
    def start_monitor(self):
        if self.test_BGC:
            self.start_monitor_base([self.proc_stat_monitor, self.marvin_bind_core_thread], [self.logcat_output_monitor_func])
        else:
            self.start_monitor_base([self.proc_stat_monitor], [self.logcat_output_monitor_func])

import pandas as pd
import os
class MicroBenchmark(BaseMonitor):
    def __init__(self, ci_code: Ci_Base, serial: str, ASRCDIR: str, logger: MyLogger, do_perf: bool, stdout_fn: str) -> None:
        super().__init__(ci_code, serial, ASRCDIR, logger, do_perf)
        self.proc_stat_fn = f"{ci_code.test_report_folder}/proc_stat.csv"
        # TEST_MICRO_BENCHMARK is printed in aosp/art heap.cc 
        self.proc_start_pattern = re.compile(r"TEST_MICRO_BENCHMARK: pid = (\d+), comm = (.*)")
        self.app_pid: Optional[int] = None
        self.app_comm: Optional[str] = None
        self.stdout_fn = stdout_fn
        self.lines_printed = 0  # Track the number of lines already printed
        self.is_micro_benchmark = True
        self.error = False

    def print_new_stdout_lines(self):
        """
        Prints newly printed lines from the stdout of the MicroBenchmark process
        """
        try:
            with open(self.stdout_fn, 'r') as file:
                all_lines = file.readlines()
                new_lines = all_lines[self.lines_printed:]  # Get only new lines

                for line in new_lines:
                    if "[MonoTime]" in line:
                        self.ci_code.record_swap_statistics(self.app_pid, "TestRoundStart")
                    print(line, end='')  # Print each new line

                self.lines_printed += len(new_lines)  # Update the count of printed lines
        except FileNotFoundError:
            pass
        except IOError as e:
            pass

    def stdout_monitor_func(self):
        while True:
            self.print_new_stdout_lines()
            if self.test_end:
                break
            time.sleep(0.05)

    def zram_stat_monitor(self):
        while True:
            self.ci_code.record_zram_mm_stat_record()
            if self.wb_enabled:
                self.ci_code.record_zram_bd_stat()
            app_comm_str = self.app_comm if self.app_comm else "TestStart"
            self.trigger_zram_swap_log(self.app_pid, app_comm_str, False)
            if self.test_end:
                break
            time.sleep(1)
    
    def activity_manager_output_callback(self, line: str) -> bool:
        if self.filter_and_update_logcat_time(line):
            return True
        
        start_match = self.proc_start_pattern.search(line)
        if start_match:
            app_pid: Optional[int] = None
            app_comm: Optional[str] = None
            app_pid = int(start_match.group(1))
            app_comm = start_match.group(2)
            if app_pid is not None:
                self.app_pid = app_pid
                self.app_comm = app_comm
                self.logger.info(f"MicroBenchmark {app_comm} pid {app_pid} started")
                if self.do_perf:
                    self.perf_proc(app_pid, app_comm)
        if self.test_end:
            self.logger.info("MicroBenchmark test ended, stop activity_manager_output_callback")
            self.stop_event.set()
            return False
        
        return True

    def logcat_output_monitor_func(self):
        while True:
            am_cmd = f"logcat -v usec -d -t \"{self.last_logcat_time}\" | grep TEST_MICRO_BENCHMARK"
            am_cmd_full = f"adb -s {self.serial} wait-for-device shell '{am_cmd}'"
            try:
                output = subprocess.check_output(am_cmd_full, shell=True, text=True, encoding='utf-8')
                lines = output.splitlines()
                for line in lines:
                    self.activity_manager_output_callback(line)
            except subprocess.CalledProcessError:
                pass
            if self.test_end:
                break
            time.sleep(1)
        self.logger.info("MicroBenchmark monitor_func Terminated")

    def do_proc_stat_inner(self, proc_stat_f, running_app_pids: List[int]):
        dead_app_pids = []
        for app_pid in running_app_pids:
            app_comm = self.app_comm
            try:
                mono_time, vct, ratio = get_device_timecnt()
                try:
                    proc_stat_data = parse_proc_stat(app_pid)
                    # logger.warning(f"MULTIAPP app {app_comm} pid {app_pid} is running")
                except:
                    # app is not running
                    dead_app_pids.append(app_pid)
                    # logger.warning(f"MULTIAPP app {app_comm} pid {app_pid} is not running")
                    proc_stat_data = None
                if proc_stat_data:
                    proc_stat_f.write(f"{mono_time},{vct},{ratio},{app_pid},{app_comm},{proc_stat_data}\n")
                    proc_stat_f.flush()
            except Exception as e:
                self.logger.error(f"MULTIAPP failed to record proc_stat: {e}")
                pass
        return dead_app_pids

    def proc_stat_monitor(self):
        with open(self.proc_stat_fn, 'w') as proc_stat_f:
            proc_stat_f.write("mono_time,vct,ratio,pid,comm,tid,tcomm,utime,stime,min_flt,maj_flt,rss,blkio_delay,swapin_delay\n")
            self.logger.info("MULTIAPP start proc_stat_monitor")
            epoch_wait = 1.0
            inner_wait = 0.05
            sum_inner_wait = 0.0
            while True:
                if self.test_end:
                    try:
                        self.ci_code.record_zram_mm_stat_record()
                    except Exception as e:
                        self.error = True
                        self.logger.error(f"Failed to record zram_mm_stat: {e}")
                        self.test_end = True
                    if self.wb_enabled:
                        try:
                            self.ci_code.record_zram_bd_stat()
                        except Exception as e:
                            self.error = True
                            self.logger.error(f"Failed to record zram_bd_stat: {e}")
                            self.test_end = True
                    self.logger.info("MULTIAPP test ended, stop proc_stat_monitor")
                    break
                
                try:
                    self.ci_code.record_zram_mm_stat_record()
                except Exception as e:
                    self.error = True
                    self.logger.error(f"Failed to record zram_mm_stat: {e}")
                    self.test_end = True
                if self.wb_enabled:
                    try:
                        self.ci_code.record_zram_bd_stat()
                    except Exception as e:
                        self.error = True
                        self.logger.error(f"Failed to record zram_bd_stat: {e}")
                        self.test_end = True
                while True:
                    time.sleep(inner_wait)
                    sum_inner_wait += inner_wait
                    do_proc_stat_now = False
                    if (sum_inner_wait >= epoch_wait or do_proc_stat_now) and self.app_pid:
                        self.do_proc_stat_inner(proc_stat_f, [self.app_pid])
                        sum_inner_wait = 0.0
                        break
    
    def start_monitor(self):
        self.start_monitor_base([self.proc_stat_monitor, self.zram_stat_monitor], [self.logcat_output_monitor_func, self.stdout_monitor_func])
