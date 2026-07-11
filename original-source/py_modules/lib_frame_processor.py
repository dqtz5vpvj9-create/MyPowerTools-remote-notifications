import json
import select
import traceback
from typing import OrderedDict, Optional
import pandas as pd
import numpy as np
import os
import socket
import threading
import signal
from datetime import datetime as datetime_class
import importlib, sys
from os.path import dirname, pardir
from pathlib import Path

import psutil

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

from py_modules.lib_sh import shell_run
from py_modules.lib_aosp_testing import kill_process_dfs, slugify_path
from py_modules.StrRef import StrRef
from py_modules.lib_gpu_tracer import GPUMetricsCollector
from py_modules.AppTestHelper import AppTestHelper, SwipeType
from py_modules.lib_aosp_base import *
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_appium import AppiumWrapper, PreExecuteResult
from test_tools.am_pm_utils import get_foreground_activities, get_last_displayId, get_default_activity
from test_tools.ci_base import Ci_Base
from py_modules.lib_perfetto_config import gen_frecords_perfetto_config
from py_modules.lib_frame_processor_inner import process_frame_trace
from py_modules.lib_frame_result_processor import ExperimentResultProcessor

class FrameRecorder:
    def __init__(self, report_folder: str, time: int, logger: MyLogger, disable_perfetto: bool = False, device_serial: Optional[str] = None):
        self.report_folder = report_folder
        self.logger = logger
        self.app_test_helper = AppTestHelper(logger=logger, appium = None, dummy_appium = True)
        self.time = time
        self.disable_perfetto = disable_perfetto
        self.device_serial = device_serial
    def remove_recorded_traces(self):
        if self.disable_perfetto:
            self.logger.verbose("Perfetto is disabled, skipping trace file removal")
            return
        try:
            As(f"rm {self.device_trace_path}", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial)
        except Exception as e:
            self.logger.verbose(f"Failed to remove old trace file: {e}")
    def record(self, package: str, bg_packages: str | None = None):
        package_name = package
        if self.disable_perfetto:
            self.logger.info(f"Perfetto is disabled, skipping trace recording for package {package_name}")
            # Still find process for consistency even when perfetto is disabled
            if package_name != 'none':
                processes = None
                while processes is None or len(processes) == 0:
                    processes = self.app_test_helper.find_running_process(package_name, None)
                    self.logger.debug(f"Found {len(processes)} processes for package {package_name}")
                    time.sleep(0.5)
                pid = None
                for process in processes:
                    if process[4] == package_name:
                        pid = process[1]
                        break
                if pid is None:
                    self.logger.error(f"Failed to find process for package {package_name}")
                    return None
                else:
                    self.logger.debug(f"Found pid {pid} for package {package_name}")
            else:
                pid = None
            
            # Return dummy trace path when perfetto is disabled
            dummy_trace_fn = f"no-perfetto-{package_name}-{datetime_class.now().timestamp()}.trace"
            return {
                'trace_path': f"{self.report_folder}/{dummy_trace_fn}",
                'pid': pid
            }
        
        # Original perfetto recording logic
        if package_name == 'none':
            trace_fn = f"perfetto-whole_device-{datetime_class.now().timestamp()}.trace"
            pid = None
        else:
            processes = None
            while processes is None or len(processes) == 0:
                processes = self.app_test_helper.find_running_process(package_name, None)
                self.logger.debug(f"Found {len(processes)} processes for package {package_name}")
                time.sleep(0.5)
            pid = None
            for process in processes:
                if process[4] == package_name:
                    pid = process[1]
                    break
            if pid is None:
                self.logger.error(f"Failed to find process for package {package_name}")
                return None
            else:
                self.logger.debug(f"Found pid {pid} for package {package_name}")
            self.logger.info(f"Starting frame recorder for package {package_name} with pid {pid}")
            trace_fn = f"perfetto-{package_name}-{pid}-{datetime_class.now().timestamp()}.trace"
        device_trace_path = f"/data/misc/perfetto-traces/{trace_fn}"
        self.device_trace_path = device_trace_path
        device_config_path = f"/data/misc/perfetto-traces/perfetto-{package_name}.config"
        config_str = gen_frecords_perfetto_config(package if (package is not None and package != 'none') else (bg_packages.split(",") if bg_packages else None), self.time * 1000)
        try:
            As(f"echo '{config_str}' > {device_config_path}", [AsOption.STDOUT_NO_PRINT], device_serial=self.device_serial)
            pft_cmd = f"cat {device_config_path} | /system/bin/android-arm64/perfetto --txt -c - -o {device_trace_path}"
            self.logger.verbose(pft_cmd)
            As(pft_cmd, [AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial)
            Aa("pull", device_trace_path, f"{self.report_folder}/{trace_fn}", device_serial=self.device_serial)
            self.logger.verbose(f"Trace recorded: {trace_fn}")
        except Exception as e:
            self.logger.error(f"frame_recorder failed: {e}")
        # self.remove_recorded_traces()
        return {
            'trace_path': f"{self.report_folder}/{trace_fn}",
            'pid': pid
        }

class FrameProcessor:
    def __init__(self, trace_path: str, main_proc_pid: Optional[int], logger: MyLogger):
        self.trace_path = trace_path
        self.logger = logger
        self.frame_df = None
        self.main_proc_pid = main_proc_pid
        self.package_name = self.trace_path.split('-')[1].replace("_", ".")
        if len(self.trace_path.split('-')) == 4:
            # pid is in the trace file name
            self.main_proc_pid = int(self.trace_path.split('-')[2]) if self.main_proc_pid is None else self.main_proc_pid
            self.logger.debug(f"Using pid from trace file name: {self.main_proc_pid}")
    def process(self, package_name = None, main_proc_pid = None, result_dict = None) -> pd.DataFrame:
        input_dir = os.path.dirname(self.trace_path)
        if package_name is None:
            package_name = self.package_name
        else:
            self.package_name = package_name
        if main_proc_pid is None:
            main_proc_pid = self.main_proc_pid
        else:
            self.main_proc_pid = main_proc_pid
        self.logger.debug(f"package_name: {package_name}, main_proc_pid: {self.main_proc_pid}")
        decoded_csv = os.path.basename(self.trace_path).replace('.trace', '.csv')
        decoded_csv = os.path.join(input_dir, decoded_csv)

        if False: #os.path.exists(decoded_csv):
            df = pd.read_csv(decoded_csv)
        else:
            df = process_frame_trace(self.trace_path, package_name, self.main_proc_pid, None, result_dict = result_dict)
            df.to_csv(decoded_csv, index=False)
            self.logger.debug(f"Frame trace details saved to {decoded_csv}")
            print("------")
            print("df = '" + decoded_csv + "'")
            print("------")
        
        if len(df) == 0:
            ret = [package_name, 0, 0, 0, 0, 0]
        else:
            # If ts[i + 1] - (ts[i] + dur[i]) > 16.6ms, there's no drawing request => idle
            # Add up all idle time
            idle_time_ns = 0
            for i in range(len(df) - 2):
                interval = df['ts'][i + 1] - (df['ts'][i] + df['rendering_time'][i])
                if interval > 1e8: # 100ms
                    idle_time_ns += interval

            total_time_ns = df['ts'].iloc[-1] + df['rendering_time'].iloc[-1] - df['ts'].iloc[0]

            frame_cnt = len(df)
            frame_drop = len(df[df['is_drop'] == True])
            valid_sec = round((total_time_ns - idle_time_ns) / 1e9, 2)
            fps = len(df) / valid_sec
            ria = len(df[df['rendering_time'] > 16666666])

            ret = [package_name, frame_cnt, frame_drop, valid_sec, fps, ria]
        self.frame_df = pd.DataFrame([ret], columns=['comm', 'frame_cnt', 'frame_drop', 'valid_sec', 'fps', 'ria'])
        result_fn = decoded_csv.replace("perfetto-", "frame_ret-")
        self.frame_df.to_csv(result_fn, index=False)
        print(f"Frame processing result saved to {result_fn}")
        return self.frame_df


def use_phone_thread_func(helper: AppTestHelper, duration: int, stop_event: threading.Event = None, scroll_interval: float = 1, nr_scroll = None):
    helper.use_phone_swipe_with_autodroid_lock(duration=duration, period=scroll_interval, swipe_type=SwipeType.NEW, stop_event=stop_event, nr_scroll=nr_scroll)

import subprocess
import time
from datetime import datetime
import threading

def sleep_with_ratio(duration: int, multiple: float):
    time.sleep(duration / multiple)

def replay_adb_commands(agent_speedup: float):
    sleep_with_ratio(10, agent_speedup)
    # Define ADB commands list with corresponding timestamps
    commands = [
        ("2024-10-03:20:11:12,184", ['adb', '-s', 'px2:25555', 'shell', 'input', 'touchscreen', '-d', '9', 'swipe', '944', '2034', '944', '2034', '200']),
        ("2024-10-03:20:11:23,905", ['adb', '-s', 'px2:25555', 'shell', 'input', 'touchscreen', '-d', '9', 'swipe', '237', '748', '237', '748', '200']),
        ("2024-10-03:20:11:44,063", ['adb', '-s', 'px2:25555', 'shell', 'input', 'touchscreen', '-d', '9', 'swipe', '777', '400', '777', '400', '200']),
        ("2024-10-03:20:11:54,035", ['adb', '-s', 'px2:25555', 'shell', 'input', 'touchscreen', '-d', '9', 'swipe', '617', '1981', '617', '1981', '200']),
        ("2024-10-03:20:12:04,033", ['adb', '-s', 'px2:25555', 'shell', 'input', 'touchscreen', '-d', '9', 'swipe', '540', '669', '540', '669', '200']),
    ]

    # Convert timestamps to datetime objects
    timestamps = [datetime.strptime(cmd[0], "%Y-%m-%d:%H:%M:%S,%f") for cmd in commands]

    # Execute commands and simulate time intervals
    for i in range(len(commands)):
        if i > 0:
            # Calculate time difference between current command and previous command in seconds
            time_diff = (timestamps[i] - timestamps[i-1]).total_seconds()
            sleep_with_ratio(time_diff, agent_speedup)

        # Execute ADB command
        print(f"Executing command: {commands[i][1]}")
        subprocess.run(commands[i][1])

    print("Command sequence replayed successfully.")


# Frame Processor Server class
class FrameProcessorServer:
    def __init__(
        self, 
        logger, 
        package, 
        report_folder_parent, 
        host="0.0.0.0", 
        port=14862, 
        bg_name_ref=None, 
        bg_app_pid=None, 
        use_memory=False, 
        scroll_interval=1, 
        test_desc=None,
        virtual_displays = None,
        disable_perfetto=False,
        disable_perfetto_trace_parsing=False,
        handle_signal=True,
        device_serial: Optional[str] = None,
        is_cvd_device: bool = False
    ):
        self.logger: MyLogger = logger
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.recording_active : threading.Event = threading.Event()
        self.recording_complete : threading.Event = threading.Event()
        self.frame_recorder = None
        self.package = package  # Foreground app, remains constant
        self.report_folder = None  # Folder to save trace files
        self.report_folder_parent = report_folder_parent
        self.trace_path = None
        self.perfetto_process = None
        self.use_phone_thread = None
        self.app_test_helper: Optional[AppTestHelper] = None
        self.bg_name_ref: StrRef = bg_name_ref  # Background app name, may be updated by client commands
        if bg_name_ref is None:
            self.bg_name_ref = StrRef("")
        self.friendly_test_name = None
        self.bg_app_pid = bg_app_pid    # Background app PID
        self.stop_event = threading.Event()
        self.use_memory = use_memory
        self.scroll_interval = scroll_interval
        self.test_desc = test_desc
        self.background_initial_state = None
        self.nr_scroll = [0]
        self.gpu_collector = None  # GPU collector
        self.virtual_displays = virtual_displays if virtual_displays is not None else []
        self.disable_perfetto = disable_perfetto  # New option to disable perfetto tracing
        self.disable_perfetto_trace_parsing = disable_perfetto_trace_parsing  # New option to disable trace parsing while still recording
        self.handle_signal = handle_signal  # Decide whether to handle signal itself
        self.device_serial = device_serial
        self.is_cvd_device = is_cvd_device
    
    def get_experiment_folder(self):
        if not self.report_folder:
            return None
        return self.report_folder

    def start_server(self, max_retries: int = 20):
        """Start the TCP server, retrying on incrementing ports if bind fails.

        After a successful bind, ``self.port`` reflects the *actual* port in
        use, which callers should read back to stay in sync. Pass ``self.port == 0``
        on construction to ask the OS for an ephemeral port (no retry needed
        on first attempt; subsequent retries fall back to ports 1, 2, ...).
        """
        requested_port = self.port
        last_err: Optional[OSError] = None
        for attempt in range(max_retries):
            candidate_port = requested_port + attempt
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, candidate_port))
                self.server_socket = sock
                actual_port = sock.getsockname()[1]
                if requested_port == 0 and attempt == 0:
                    self.logger.verbose(
                        f"OS auto-assigned port {actual_port} for FrameProcessorServer"
                    )
                elif attempt > 0:
                    self.logger.verbose(
                        f"Port {requested_port} was busy, bound to {actual_port} "
                        f"after {attempt} retries"
                    )
                self.port = actual_port  # update to actual port
                break
            except OSError as e:
                last_err = e
                self.logger.debug(
                    f"bind({self.host}:{candidate_port}) failed: {e}, retrying..."
                )
                try:
                    sock.close()
                except Exception:
                    pass
        else:
            # All retries exhausted
            self.logger.error(
                f"Failed to bind server socket after {max_retries} attempts "
                f"(ports {requested_port}–{requested_port + max_retries - 1})"
            )
            raise last_err  # type: ignore[misc]

        self.server_socket.settimeout(1.0)  # 1 second timeout for listening
        self.server_socket.listen(5)
        self.running = True
        self.logger.verbose(f"Frame processor server started at {self.host}:{self.port}")
        
        # Register signal handlers for clean shutdown
        if self.handle_signal:
            signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        self.server_thread = threading.Thread(target=self._server_loop)
        self.server_thread.daemon = True
        self.server_thread.start()

    def _signal_handler(self, sig, frame):
        self.logger.info(f"Received signal {sig}, shutting down...")
        self._handle_stop()
        self._handle_final_state("")
        # Remove the signal handler
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def _server_loop(self):
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                self.logger.verbose(f"Connection from {addr}")
                self._handle_client(client)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:  # Only log if not shutting down
                    self.logger.error(f"Error in server loop: {e}")

        # When running=False, close the server
        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None
        self.logger.verbose("Frame processor server has stopped")

    def _handle_client(self, client):
        try:
            data = client.recv(10240).decode('utf-8', errors='ignore').strip()
            noti = data.split('\n')[0].split(' ')[0]
            
            # Parse command and parameters
            parts = data.split()
            command = parts[0]
            
            # Only log important commands (skip high-frequency CHECK_READY)
            if command not in ["CHECK_READY"]:
                self.logger.debug(f"Command: {command}")
            
            if command == "CHECK_READY":
                response = "OK: Ready for client"
                client.send(response.encode())
            elif command == "RELAUNCH_FG_APP":
                # Relaunch the foreground app
                self.logger.debug(f"Relaunching foreground app {self.package}")
                As(f"am start -n {get_default_activity(self.package)} --display 0", [AsOption.STDOUT_NO_PRINT], device_serial=self.device_serial)
                response = "OK: Foreground app relaunched"
                client.send(response.encode())
            elif command == "START":
                # Process parameters
                params = []
                if len(parts) > 1:
                    self.bg_name_ref.value = parts[1]  # First parameter is background app name
                    params.append(f"bg_apps={self.bg_name_ref.value[:30]}...")  # Truncate long names
                
                if len(parts) > 2:
                    self.use_memory = parts[2].lower() == "true"  # Second parameter is use_memory
                    params.append(f"memory={self.use_memory}")
                
                if len(parts) > 3:
                    self.bg_task = parts[3]
                    params.append(f"task={self.bg_task[:20]}")  # Truncate long task names

                if len(parts) > 4:
                    self.friendly_test_name = parts[4]
                
                # Log all parameters in one line
                if params:
                    self.logger.debug(f"START params: {', '.join(params)}")
                
                response = self._handle_start()
                client.send(response.encode())
            elif command == "STOP":
                response = "OK: Stopping recording..."
                self._handle_stop()
                response = "OK: Stopped recording"
                client.send(response.encode())
                # Server continues to run, waiting for FINAL_STATE command
            elif command == "INITIAL_STATE":
                self.logger.debug(f"Received initial device state of length {len(data)} bytes")
                self.background_initial_state = data
            elif command == "FINAL_STATE":
                # Extract device state information
                self._handle_final_state(data)
                response = "OK: Final state received, server shutting down"
                client.send(response.encode())
                # Mark server to stop after receiving FINAL_STATE
                self.running = False
            elif command == "FINAL_VIEWTREE":
                # Extract device state information
                self._handle_final_viewtree(data)
                response = "OK: Final view tree received"
                client.send(response.encode())
            else:
                response = "ERROR: Unknown command"
                client.send(response.encode())
                
        except Exception as e:
            self.logger.error(f"Error handling client: {e}")
            print(traceback.format_exc())
        finally:
            client.close()

    def _handle_start(self):
        if self.recording_active.is_set():
            return "ERROR: Recording already in progress"
        
        self.recording_active.set()
        __time_str = datetime_class.now().strftime('%Y-%m-%d-%H-%M-%S')
        # Generate new trace path for each session, including memory flag
        if self.friendly_test_name:
            report_folder = os.path.join(self.report_folder_parent, f"{self.friendly_test_name}_{slugify_path(self.bg_task[:5] if hasattr(self, 'bg_task') else '')}_{__time_str}")
        else:
            report_folder = os.path.join(self.report_folder_parent, f"{self.bg_name_ref.value}_{slugify_path(self.bg_task[:5] if hasattr(self, 'bg_task') else '')}_{__time_str}")
        self.report_folder = report_folder
        os.makedirs(report_folder, exist_ok=False)
        timestamp = datetime_class.now().timestamp()
        memory_flag = "memory" if self.use_memory else "query_llm"
        base_filename = f"perfetto-{self.package}_{memory_flag}-{timestamp}.trace"
        self.trace_path = os.path.join(report_folder, base_filename)
        self.logger.verbose(f"▶ Recording started → {self.report_folder}")
        self.test_desc['trace_path'] = self.trace_path
        self.test_desc['bg_app_name'] = self.bg_name_ref.value
        self.test_desc['use_memory'] = self.use_memory
        if hasattr(self, 'bg_task'):
            self.test_desc['bg_task'] = self.bg_task
        # Save test_desc to a YAML file
        yaml_path = os.path.join(report_folder, f"test_config.yaml")
        with open(yaml_path, 'w') as yaml_file:
            yaml.dump(self.test_desc, yaml_file, default_flow_style=False)
        self.logger.verbose(f"Config saved: {os.path.basename(yaml_path)}")
        if hasattr(self, 'background_initial_state'):
            with open(os.path.join(report_folder, f"bg_app_initial_state.txt"), 'w') as f:
                f.write(self.background_initial_state if self.background_initial_state is not None else "")
        
        # Initialize frame recorder with a fresh instance
        self.frame_recorder = FrameRecorder(report_folder=report_folder, time=3600, logger=self.logger, disable_perfetto=self.disable_perfetto, device_serial=self.device_serial)
        
        # Create a new app_test_helper instance for each session
        self.app_test_helper = self.frame_recorder.app_test_helper
        
        if not self.is_cvd_device:
            self.gpu_collector = GPUMetricsCollector(self.logger, device_serial=self.device_serial)
            self.gpu_collector.start()
        else:
            self.logger.verbose("GPU collector skipped (CVD device)")
        
        # Initialize and start the app test helper thread if scroll_interval > 0
        if self.scroll_interval >= 0:
            self.use_phone_thread = threading.Thread(
                target=use_phone_thread_func, 
                args=(self.app_test_helper, 3600, self.stop_event, self.scroll_interval, self.nr_scroll)  # Use a large timeout - will be stopped by STOP command
            )
            self.use_phone_thread.daemon = True
            self.use_phone_thread.start()
            self.logger.debug("Started user interaction thread")
        
        # Start recording in a separate thread
        self.record_thread = threading.Thread(target=self._record_thread_func)
        self.record_thread.daemon = True
        self.record_thread.start()
        self.start_time = datetime.now()
        
        return f"OK: Recording started for {self.package} with background app {self.bg_name_ref}, memory={self.use_memory}"

    def _record_thread_func(self):
        try:
            ret = self.frame_recorder.record(self.package, self.bg_name_ref.value)
            actual_trace_path = ret['trace_path']
            pid = ret['pid']
            self.foreground_pid = pid
            self.trace_path = actual_trace_path if actual_trace_path else self.trace_path
            self.logger.verbose(f"Recording complete: {os.path.basename(self.trace_path)}")
            self.recording_complete.set()
        except Exception as e:
            self.logger.error(f"Error in recording thread: {e}")
            print(traceback.format_exc())
            raise(e)
        finally:
            self.recording_active.clear()
            # self.frame_recorder.remove_recorded_traces()


    def _handle_stop(self):
        self.end_time = datetime.now()
        self.stop_event.set()
        if self.recording_active.is_set():
            # Signal perfetto to stop recording by sending SIGINT (only if perfetto is enabled)
            if not self.disable_perfetto:
                try:
                    As('killall -SIGINT perfetto', [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT], timeout=10, device_serial=self.device_serial)
                    self.logger.verbose("Stopping perfetto")
                except Exception as e:
                    self.logger.error(f"Error stopping perfetto: {e}")
                    print(traceback.format_exc())
            else:
                self.logger.verbose("Perfetto disabled, skipping stop signal")
            # Wait for recording to finish
            if hasattr(self, 'record_thread') and self.record_thread.is_alive():
                self.logger.verbose("Waiting for recording to finish...")
                self.record_thread.join(timeout=10)
            
            # Stop the user interaction thread
            if self.use_phone_thread and self.use_phone_thread.is_alive():
                self.logger.verbose("Stopping user interaction")
                self.use_phone_thread.join(timeout=5)
            
            # Stop GPU metrics collection and save results
            gpu_results = None
            if hasattr(self, 'gpu_collector') and self.gpu_collector:
                try:
                    gpu_results = self.gpu_collector.stop()
                except Exception as e:
                    self.logger.error(f"Error stopping GPU collector: {e}")
                    print(traceback.format_exc())
            
            # Now process the trace file - using the new ExperimentResultProcessor class
            # Only wait for perfetto if it's enabled
            if not self.disable_perfetto:
                wait_start = time.time()
                wait_logged = False
                while self.recording_active.is_set() and not self.recording_complete.is_set():
                    time.sleep(1)
                    elapsed = int(time.time() - wait_start)
                    if not wait_logged:
                        self.logger.verbose("Waiting for perfetto to pull trace file...")
                        wait_logged = True
                    elif elapsed > 0 and elapsed % 10 == 0:
                        self.logger.verbose(f"Still waiting for trace file ({elapsed}s elapsed)")
            else:
                self.logger.verbose("Perfetto is disabled, skipping wait for trace file")
            
            # Check if we should parse the trace file
            if self.disable_perfetto_trace_parsing:
                self.logger.verbose("Trace parsing disabled")
            elif self.trace_path and (os.path.exists(self.trace_path) or self.disable_perfetto):
                try:
                    # Create and use the result processor
                    result_processor = ExperimentResultProcessor(
                        trace_path=self.trace_path,
                        report_folder=self.report_folder,
                        logger=self.logger,
                        foreground_package=self.package,
                        foreground_pid=self.foreground_pid,
                        background_package=self.bg_name_ref.value if self.bg_name_ref else None,
                        background_pid=self.bg_app_pid,
                        use_memory=self.use_memory,
                        start_time=self.start_time,
                        end_time=self.end_time,
                        gpu_results=gpu_results
                    )
                    
                    # Process the experiment results
                    success = result_processor.process()
                    if success:
                        self.logger.debug("Experiment results processed successfully")
                    else:
                        self.logger.error("Failed to process experiment results")
                        
                except Exception as e:
                    self.logger.error(f"Error processing trace: {e}")
                    print(traceback.format_exc())
            else:
                self.logger.warning(f"No trace file found at {self.trace_path}")
            
            # Clean up resources
            self.app_test_helper = None
            self.use_phone_thread = None
            self.frame_recorder = None
            self.recording_active.clear()
        
        # Don't close the server, wait for FINAL_STATE command
        self.logger.verbose("Recording stopped, waiting for final state")

    def _handle_final_viewtree(self, state_data):
        self.final_viewtree = state_data if state_data else "empty"
        
    def _handle_final_state(self, state_data):
        # Try to parse JSON data
        try:
            # Separate command and JSON data
            json_start = state_data.find('{')
            if json_start != -1:
                json_data = state_data[json_start:]
                data = json.loads(json_data)
                
                stats = data.get("stats", {})
                
                # Save state to file
                if self.report_folder:
                    try:
                        state_dir = self.report_folder
                        # Ensure directory exists
                        os.makedirs(state_dir, exist_ok=True)
                        base_name = os.path.basename(self.trace_path) if self.trace_path else "unknown.trace"
                        # Save view tree
                        state_file = os.path.join(state_dir, 
                                                f"final_state_{base_name.replace('.trace', '.txt')}")
                        with open(state_file, 'w') as f:
                            f.write(self.final_viewtree if hasattr(self, 'final_viewtree') else "empty")
                        self.logger.verbose(f"Final state saved to {state_file}")
                        # try:
                        #     appium = AppiumWrapper(serial, self.logger, state_dir, dummy=True)
                        #     for dId in self.virtual_displays:
                        #         try:
                        #             appium.take_screenshot(nr_display=dId)
                        #         except Exception as e:
                        #             pass
                        # except Exception as e:
                        #     self.logger.warning(f"Error taking screenshot: {e}")
                        #     self.logger.warning(traceback.format_exc())
                        # Save statistics
                        stats_file = os.path.join(state_dir, 
                                                f"stats_{base_name.replace('.trace', '.yaml')}")
                        with open(stats_file, 'w') as f:
                            yaml.dump(stats, f, allow_unicode=True, sort_keys=False)
                        self.logger.verbose(f"Stats saved: {os.path.basename(stats_file)}")
                        if 'is_correct_state' in stats and not stats['is_correct_state']:
                            with open(os.path.join(self.report_folder, "TestFailFlag"), 'w') as f:
                                try:
                                    json.dump(stats, f, indent=2)
                                except Exception as e:
                                    pass
                            self.logger.error(f"Is correct state: {stats['is_correct_state']}")
                            if 'analysis' in stats:
                                self.logger.error(f"Analysis: {stats['analysis']}")
                            if 'possible_fix' in stats:
                                self.logger.error(f"Possible_fix: {stats['possible_fix']}")
                        else:
                            if True or self.nr_scroll and isinstance(self.nr_scroll, list) and len(self.nr_scroll) > 0 and self.nr_scroll[0] > 0:
                                open(os.path.join(self.report_folder, f"TestSuccessFlag"), 'w').close()
                                # self.logger.notice(f"✓ Experiment successful → {self.report_folder}")
                            else:
                                with open(os.path.join(self.report_folder, f"TestFailFlag"), 'w') as f:
                                    f.writelines(["No scroll count"])
                                self.logger.error(f"Server side error, no scroll count: {self.nr_scroll}")
                                
                    except Exception as e:
                        self.logger.error(f"Error saving final state and statistics: {e}")
                        print(traceback.format_exc())
                else:
                    self.logger.warning("No trace path available, final state not saved to file")
            else:
                # Process old format data
                self.logger.debug(f"Received final device state in old format, length {len(state_data)} bytes")
                # Save state to file
                if self.trace_path:
                    try:
                        state_dir = os.path.dirname(self.trace_path)
                        os.makedirs(state_dir, exist_ok=True)
                        
                        state_file = os.path.join(state_dir, 
                                                f"final_state_{os.path.basename(self.trace_path).replace('.trace', '.txt')}")
                        with open(state_file, 'w') as f:
                            f.write(state_data)
                        self.logger.debug(f"Final state saved to {state_file}")
                    except Exception as e:
                        self.logger.error(f"Error saving final state: {e}")
                        print(traceback.format_exc())
        except json.JSONDecodeError as e:
            print(traceback.format_exc())
            self.logger.error(f"Error parsing JSON data: {e}")
            # Process old format data
            self.logger.debug(f"Falling back to old format processing")
            if self.trace_path:
                try:
                    state_dir = os.path.dirname(self.trace_path)
                    os.makedirs(state_dir, exist_ok=True)
                    
                    state_file = os.path.join(state_dir, 
                                            f"final_state_{os.path.basename(self.trace_path).replace('.trace', '.txt')}")
                    with open(state_file, 'w') as f:
                        f.write(state_data)
                    self.logger.debug(f"Final state saved to {state_file}")
                except Exception as e:
                    self.logger.error(f"Error saving final state: {e}")
                    print(traceback.format_exc())
        
        # Mark server to stop, main loop will handle closure
        self.logger.verbose("Server stopping after final state received")
        self.running = False

def check_foreground_package(package: str, base_dir, logger, device_serial: Optional[str] = None) -> PreExecuteResult:
    _serial = device_serial or serial
    if base_dir:
        os.makedirs(base_dir, exist_ok=True)
    appium = AppiumWrapper(_serial, logger, base_dir, dummy=True)
    fg_act = get_foreground_activities()[0]
    if package not in fg_act:
        logger.error(f"Foreground app is not {package}, it is {fg_act}")
        return PreExecuteResult(analysis="Foreground app is not the target app", is_correct_state=False, possible_fix="Switch to the correct app", screenshot_path="dummy")
    criteria = appium.get_criteria(fg_act)
    ret = appium.analyze_app_state(criteria)
    if not ret.is_correct_state:
        fix_with_ai(package, logger, ret.possible_fix, device_serial=device_serial)
        ret = appium.analyze_app_state(criteria)
    return ret

def fix_with_ai(package: str, logger: MyLogger, possible_fix: str, device_serial: Optional[str] = None) -> bool:
    """
    使用 DroidBot Agent 尝试将指定 App 恢复到其初始/主页面状态。

    Args:
        package: 目标 App 的包名。
        base_dir: 用于存放 Agent 日志的基础目录。
        logger: 日志记录器。
        display_id: 目标显示屏 ID。

    Returns:
        True 如果 Agent 成功执行 (返回码为 0)，否则 False。
    """
    BASE_DIR = os.path.join(androidtools, "AutoDroid")
    AGENT_PYTHON_PATH = os.path.join(miniconda, "envs", "autodroid", "bin", "python")
    AGENT_START_SCRIPT_PATH = os.path.join(BASE_DIR, "start.py")
    display_id = 0

    # 1. 定义 Agent 的任务
    #    一个通用的任务，让 Agent 负责启动 App 并导航到主页
    possible_fix = possible_fix.replace("'", "")
    agent_task = f"Current app is not at its desired state, please execute the proposed fix: {possible_fix}"

    # 2. 创建 Agent 输出目录
    agent_output_dir = os.path.join(BASE_DIR, "output", f"fix_{package}")
    try:
        os.makedirs(agent_output_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"无法创建 Agent 输出目录 {agent_output_dir}: {e}")
        return False

    # 3. 构建 Agent 命令
    agent_command = [
        AGENT_PYTHON_PATH,
        AGENT_START_SCRIPT_PATH,
        "-d", device_serial or serial,             # 使用全局或传入的 serial
        "-p", package,            # 目标包名
        "-o", agent_output_dir,   # Agent 的输出目录
        "-task", "'" + agent_task + "'",      # 让 Agent 执行的任务
        "-keep_env",              # 保留环境
        "-keep_app",              # 保留 App
        "-fast",                  # 使用快速模式
        "-display_id", str(display_id) # 指定显示屏
    ]

    task = ' '.join(agent_command)
    logger.info(f"执行 Agent 命令: {task}")
    shell_run(task, timeout=60, check_error=False)


def get_base_dir(root):
    test_time_spec = Ci_Base.get_test_time_spec(aosp_host_working_dir)
    date_str = test_time_spec['date_str']
    time_str = test_time_spec['time_str']
    meridian = test_time_spec['meridian']
    test_num = test_time_spec['test_num']
    test_name = f"Agent-{date_str}-{time_str}-{meridian}-test{test_num}"
    return os.path.join(root, test_name)

import threading
import click
import yaml

@click.command()
@click.option('--base_dir', type=str, required=False, help="Path to the trace file", default='')
@click.option('--record', is_flag=True, help="Record new trace")
@click.option('--package', type=str, default="com.whatsapp", help="Package name for recording")
@click.option('--pid', default=None, help="PID for recording")
@click.option('--time_', type=int, default=20, help="Duration for recording")
@click.option('--use_mock_agent', is_flag=True, help="Use agent to replay adb commands")
@click.option('--agent_speedup', type=float, default=1.0, help="Speedup ratio for agent")
@click.option('--bg_app_name', type=str, default=None, help="Background (virtual screen) app package name")
@click.option('--bg_app_pid', type=int, default=None, help="Background app PID")
@click.option('--server_mode', is_flag=True, help="Run in server mode waiting for commands")
@click.option('--host', type=str, default="0.0.0.0", help="Host to bind server to")
@click.option('--port', type=int, default=14862, help="Port to bind server to")
@click.option('--use_memory', is_flag=True, help="Whether to use memory optimization")
@click.option('--scroll_interval', type=int, default=1, help="Interval for scrolling")
@click.option('--disable_perfetto', is_flag=True, help="Disable perfetto tracing and run other functions normally")
@click.option('--disable_perfetto_trace_parsing', is_flag=True, help="Disable trace parsing while still recording with perfetto")
@click.option('--device_serial', type=str, default=None, help="Target device serial for ADB commands")
def main(base_dir, record, package, pid, time_, use_mock_agent, agent_speedup, bg_app_name, bg_app_pid, server_mode, host, port, use_memory, scroll_interval, disable_perfetto, disable_perfetto_trace_parsing, device_serial):
    print(f"base_dir: {base_dir}, record: {record}, package: {package}, pid: {pid}, time: {time_}")
    print(f"use_mock_agent: {use_mock_agent}, agent_speedup: {agent_speedup}, bg_app_name: {bg_app_name}, bg_app_pid: {bg_app_pid}")
    print(f"server_mode: {server_mode}, host: {host}, port: {port}, use_memory: {use_memory}")
    print(f"disable_perfetto: {disable_perfetto}, disable_perfetto_trace_parsing: {disable_perfetto_trace_parsing}")
    
    logger = setup_logging()

    fg_app_name = package
    if not (fg_app_name == 'com.levelinfinite.sgameGlobal' or fg_app_name == 'com.garena.game.codm'):
        _serial = device_serial or serial
        default_act = As(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {fg_app_name}", AsOption.STDOUT_NO_PRINT, device_serial=_serial)
        default_act = default_act.strip().split("\n")[-1].strip()
        As(f"am force-stop {fg_app_name}", device_serial=_serial)
        time.sleep(2)
        As(f"am start -n {default_act} --display 0", device_serial=_serial)
        time.sleep(5)
        if _serial != "px2:25555":
            As(f"/system/bin/fps render {fg_app_name} 1", device_serial=_serial)

    if not base_dir:
        base_dir = get_base_dir(aosp_host_working_dir)
    os.makedirs(base_dir, exist_ok=True)
    
    test_desc = {
        'foreground_package': package,
        'foreground_scroll_interval': scroll_interval,
    }

    if server_mode:
        # Run in server mode
        server = FrameProcessorServer(
            logger=logger, 
            package=package, 
            report_folder_parent=base_dir,
            host=host, 
            port=port,
            bg_name_ref=StrRef(bg_app_name) if bg_app_name else StrRef(""),
            bg_app_pid=bg_app_pid,
            use_memory=use_memory,
            scroll_interval=scroll_interval,
            test_desc=test_desc,
            disable_perfetto=disable_perfetto,
            disable_perfetto_trace_parsing=disable_perfetto_trace_parsing
        )
        server.start_server()
        try:
            # Keep main thread alive until interrupted
            while server.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down server...")
            server._handle_stop()
    else:
        # ----------------------------------------------------------
        # (2) "Direct execution mode": Script connects to itself via Socket commands
        # ----------------------------------------------------------
        logger.notice("Running in direct-execution mode (self-socket).")
        base_dir = os.path.join(base_dir, f"{package}_1")

        # 1) Start server (similar to server_mode=True) in background thread
        server = FrameProcessorServer(
            logger=logger,
            package=package,
            report_folder_parent=base_dir,
            host="127.0.0.1", 
            port=port, 
            bg_name_ref=StrRef(bg_app_name) if bg_app_name else StrRef(""),
            bg_app_pid=bg_app_pid,
            use_memory=use_memory, 
            scroll_interval=scroll_interval,
            test_desc=test_desc,
            disable_perfetto=disable_perfetto,
            disable_perfetto_trace_parsing=disable_perfetto_trace_parsing
        )
        server.start_server()

        # 2) Simple function to send commands to server
        def send_cmd(cmd_str):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
                logger.debug(f"[self-client] Send: {cmd_str}")
                s.sendall(cmd_str.encode("utf-8"))
                resp = s.recv(4096).decode("utf-8", errors="ignore")
                logger.debug(f"[self-client] Resp: {resp}")

        # 3) Sequentially send Socket commands
        time.sleep(1)
        send_cmd("CHECK_READY")
        use_mem_str = "true" if use_memory else "false"
        bg_app_str = bg_app_name if bg_app_name else "com.example.bg"

        # Example: START background_package_name memory_flag extra_info
        send_cmd(f"START {bg_app_str} {use_mem_str} direct_mode_task")

        # Simulate operation for 30 seconds
        input_cmd = None
        while input_cmd != "y":
            input_cmd = input("Press 'y' to stop recording")
            print("You pressed:", input_cmd)
        send_cmd("STOP")

        # Finally send FINAL_STATE with JSON
        fake_stats = {
            "stats": {
                "is_correct_state": True,
                "analysis": "Direct mode completed!",
                "possible_fix": ""
            }
        }
        send_cmd("FINAL_STATE " + json.dumps(fake_stats))

        # 4) Wait for server to exit
        while server.running:
            time.sleep(0.5)
        logger.notice("Direct-execution mode complete.")

if __name__ == '__main__':
    main()