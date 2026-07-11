import subprocess
import configparser
import os, sys
from typing import List
import importlib
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

from . check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
if __name__ == '__main__':
    check_conda_interpreter(CONDA_ENV_NAME)

aosp_config = configparser.RawConfigParser()
aosp_config.optionxform = lambda option: option # type: ignore
aosp_config.read(os.path.expanduser("~/aosp_config.ini"))

def apply_env_override(config, section, key, env_prefix="OVERRIDE_ANDROID_TOOLS_"):
    env_key = env_prefix + key
    if env_key in os.environ:
        config[section][key] = os.environ[env_key]

apply_env_override(aosp_config, 'benchmark', 'app')
apply_env_override(aosp_config, 'serial', 'serial')
apply_env_override(aosp_config, 'serial', 'serial_l')
apply_env_override(aosp_config, 'path', 'androidtools')
apply_env_override(aosp_config, 'path', 'aosp_host_working_dir')
apply_env_override(aosp_config, 'path', 'ASRCDIR')
apply_env_override(aosp_config, 'path', 'SUNFISH_KSRCDIR')
apply_env_override(aosp_config, 'path', 'GOLDFISH_KSRCDIR')
apply_env_override(aosp_config, 'path', 'miniconda')
apply_env_override(aosp_config, 'path', 'ndkapps')
apply_env_override(aosp_config, 'custom', 'sunfish_env_command')
apply_env_override(aosp_config, 'custom', 'sunfish_out_dir_name')
apply_env_override(aosp_config, 'custom', 'goldfish_env_command')
apply_env_override(aosp_config, 'custom', 'goldfish_out_dir_name')
apply_env_override(aosp_config, 'server', 'KSERVER')
apply_env_override(aosp_config, 'server', 'ASERVER')

benchmark_app = aosp_config['benchmark']['app']
serial = aosp_config['serial']['serial']
serial_l = aosp_config['serial']['serial_l']
androidtools = os.path.expanduser(aosp_config['path']['androidtools'].replace("\\", "/"))
aosp_host_working_dir = os.path.expanduser(aosp_config['path']['aosp_host_working_dir'].replace("\\", "/"))
ASRCDIR = os.path.expanduser(aosp_config['path']['ASRCDIR'].replace("\\", "/"))
SUNFISH_KSRCDIR = os.path.expanduser(aosp_config['path']['SUNFISH_KSRCDIR'].replace("\\", "/"))
GOLDFISH_KSRCDIR = os.path.expanduser(aosp_config['path']['GOLDFISH_KSRCDIR'].replace("\\", "/"))
miniconda = os.path.expanduser(aosp_config['path']['miniconda'].replace("\\", "/"))
ndkapps = os.path.expanduser(aosp_config['path']['ndkapps'].replace("\\", "/"))
sunfish_env_command = aosp_config['custom']['sunfish_env_command']
sunfish_out_dir_name = aosp_config['custom']['sunfish_out_dir_name']
goldfish_env_command = aosp_config['custom']['goldfish_env_command']
goldfish_out_dir_name = aosp_config['custom']['goldfish_out_dir_name']
android_input_client_port = aosp_config.getint('ports', 'android_input_client_port',fallback=27797)
android_fps_client_port = aosp_config.getint('ports', 'android_fps_client_port',fallback=27800)
droid_bot_port = aosp_config.getint('ports', 'droid_bot_config', fallback=7336)
accessibility_service_port = aosp_config.getint('ports', 'accessibility_service_port', fallback=12345)
KSERVER = aosp_config['server']['KSERVER']
ASERVER = aosp_config['server']['ASERVER']
LIB_AOSP_BASE_INITED = True

class ohvar():
    apply_env_override(aosp_config, 'path', 'ohsrcdir')
    ohsrcdir = aosp_config['path']['ohsrcdir']
    
import subprocess
from enum import Enum
from typing import Optional, Union, List
import os
import shutil

# Global variable to cache the ADB path
cached_adb_path: Optional[str] = None

def _find_android_sdk() -> Optional[str]:
    """Guess the Android SDK location from env vars and common paths."""
    for env in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        val = os.environ.get(env)
        if val and os.path.isdir(val):
            return val
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'Android', 'Sdk'),
        os.path.join(home, 'android-sdk'),
        '/android/sdk',
        '/opt/android-sdk',
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None

def find_adb() -> str:
    global cached_adb_path
    if cached_adb_path is None:
        # Try to find ADB in PATH
        adb_in_path = shutil.which("adb")
        if adb_in_path:
            cached_adb_path = adb_in_path
        else:
            # Try to locate via SDK
            sdk_dir = _find_android_sdk()
            if sdk_dir:
                specific_path = os.path.join(sdk_dir, "platform-tools", "adb")
                if os.path.exists(specific_path):
                    cached_adb_path = specific_path
                    os.environ["PATH"] += os.pathsep + os.path.dirname(specific_path)
            if cached_adb_path is None:
                raise FileNotFoundError("ADB executable not found")
    return cached_adb_path

def Aa(*args: str, device_serial: Optional[str] = None) -> str:
    _serial = device_serial or serial
    adb_path = find_adb()
    cmd: List[str] = [adb_path, "-s", _serial]
    # cmd: List[str] = ["adb", "-s", _serial]
    cmd.extend(args)
    output: bytes = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    output_s = output.decode("utf-8")
    if len(output_s.strip()) > 0:
        print(output_s.strip())
    return output_s

from enum import Enum
from typing import Optional, Union
class AsOption(Enum):
    STDOUT_NO_PRINT = 1
    STDERR_TO_STDOUT = 2

def As(args: str, options: Optional[Union[AsOption, List[AsOption]]] = None, timeout = None, device_serial: Optional[str] = None) -> str:
    """Adb shell, should only accept a single string as command since android shell will parse the commandline

    Args:
        stdout (Optional[AsOption], optional): if equals STDOUT_NO_PRINT, As will not print the output. Defaults to None.
        stderr (Optional[AsOption], optional): if equals STDERR_TO_STDOUT, As will use error output as output. Defaults to None.
        device_serial (Optional[str], optional): target device serial. Defaults to None (uses global serial).

    Raises:
        e: if stderr is not STDERR_TO_STDOUT, As will raise the CalledProcessError

    Returns:
        str: output from device 
    """
    _serial = device_serial or serial
    if options is None:
        options = []
    # if options is instance of AsOption, convert to list
    if isinstance(options, AsOption):
        options = [options]
    adb_path = find_adb()
    cmd: List[str] = [adb_path, "-s", _serial, "shell", args]
    # cmd: List[str] = ["adb", "-s", _serial, "shell", args]
    try:
        output: bytes = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.CalledProcessError as e:
        if options and AsOption.STDERR_TO_STDOUT in options:
            output = e.output
        else:
            raise e
    output_s = output.decode("utf-8")
    if options and AsOption.STDOUT_NO_PRINT in options:
        pass
    elif len(output_s.strip()) > 0:
        print(output_s.strip())
    return output_s

adb_shell = As

import re
import time
from datetime import datetime as datetime_class

def wait_boot_complete(timeout=300) -> bool:
    start_time = time.time()
    boot_complete = False
    log_match_count = 0
    first_match_time = None
    error_log_pattern = re.compile(r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Unable to set property "ctl.interface_start" to "android.frameworks.sensorservice@1.0::ISensorManager/default"')

    while not boot_complete and (time.time() - start_time) < timeout:
        try:
            # Simulate getting boot completed status
            boot_completed_output = int(As('getprop sys.boot_completed').strip())
            if boot_completed_output == 1:
                boot_complete = True
                break
            else:
                # Check the logcat for the specific error message with timestamp
                log_output = As('logcat -d *:S libc:W', options=[AsOption.STDOUT_NO_PRINT])
                for match in error_log_pattern.finditer(log_output):
                    timestamp_str = match.group(1)
                    timestamp = datetime_class.strptime(timestamp_str, "%m-%d %H:%M:%S")
                    # Convert to epoch time
                    epoch_timestamp = int(timestamp.replace(year=datetime_class.now().year).timestamp())

                    if log_match_count == 0:
                        first_match_time = epoch_timestamp

                    log_match_count += 1
                    assert first_match_time is not None
                    if log_match_count >= 10 and (epoch_timestamp - first_match_time) >= 120:
                        # Restart the device after detecting 10 logs and 120 seconds have passed since the first log
                        As('reboot')
                        log_match_count = 0
                        first_match_time = None
                        break

        except Exception as e:
            pass
        finally:
            time.sleep(1)

    return boot_complete
