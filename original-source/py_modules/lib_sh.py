import os
import time
import subprocess
import re
from typing import Optional, Tuple
import importlib, sys
from pathlib import Path
import os
import select
import subprocess
import time
from typing import Callable, Optional, Tuple, Any
import psutil
from queue import Empty
import multiprocessing
import argparse
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

from . lib_aosp_base import *
from . lib_ref import Ref
from . logging_lib import setup_logging
import logging
from datetime import datetime as datetime_class
import psutil

# Util functions

from typing import Dict, Union
import os
import platform

def ssh_command(ssh_host: str, cmd: str, login: bool = False) -> str:
    if os.name == 'nt':
        if os.path.exists("C:\\WINDOWS\\System32\\OpenSSH-Win64\\ssh.exe"):
            return f'C:\\WINDOWS\\System32\\OpenSSH-Win64\\ssh.exe {ssh_host} "{cmd}"'
        elif os.path.exists("C:\\WINDOWS\\System32\\OpenSSH\\ssh.exe"):
            return f'C:\\WINDOWS\\System32\\OpenSSH\\ssh.exe {ssh_host} "{cmd}"'
        else:
            return f'ssh {ssh_host} "{cmd}"'
    else:
        return f'ssh {ssh_host} "{cmd}"'

def scp_command(cmd: str) -> str:
    if os.name == 'nt':
        if os.path.exists("C:\\WINDOWS\\System32\\OpenSSH-Win64\\scp.exe"):
            return f'C:\\WINDOWS\\System32\\OpenSSH-Win64\\scp.exe {cmd}'
        elif os.path.exists("C:\\WINDOWS\\System32\\OpenSSH\\scp.exe"):
            return f'C:\\WINDOWS\\System32\\OpenSSH\\scp.exe {cmd}'
        else:
            return f'scp {cmd}'
    else:
        return f'scp "{cmd}"'

def detect_shell_name() -> str:
    if platform.system() == "Windows":
        comspec = os.environ.get("ComSpec", "").lower()
        if "powershell" in comspec:
            return "powershell"
        else:
            return "cmd"
    else:
        shell_path = os.environ.get("SHELL", "").lower()
        if "zsh" in shell_path:
            return "zsh"
        elif "bash" in shell_path:
            return "bash"
        elif "fish" in shell_path:
            return "fish"
        else:
            return "bash"  # Default to bash if the shell cannot be determined

shell_name = detect_shell_name()


def get_env_prefix(env: Dict[str, Union[str, int, float]], shell_name: str) -> str:
    if shell_name == "cmd":
        return " ".join(f"set \"{key}={value}\" &&" for key, value in env.items()) + " "
    elif shell_name == "powershell":
        return " ".join(f"$env:{key}='{value}';" for key, value in env.items()) + " "
    elif shell_name in ["zsh", "bash"]:
        return " ".join(f"export {key}={value};" for key, value in env.items()) + " "
    elif shell_name == "fish":
        return " ".join(f"set -x {key} {value};" for key, value in env.items()) + " "
    else:
        raise ValueError(f"Unsupported shell: {shell_name}")

import subprocess
import threading
from typing import Optional, Tuple
logger = setup_logging()

def blocking_streamer(pipe, output_queue, stop_event: threading.Event) -> None:
    try:
        for line in iter(pipe.readline, ''):
            decoded_line = line.strip()
            output_queue.put(decoded_line)
        pipe.close()
    except ValueError:
        stop_event.set()

def print_output(line: str) -> bool:
    if len(line) > 0:
        print(line)
    return True

def color_print(line: str) -> bool:
    if len(line) > 0:
        WARNING_COLOR = "\033[93m"  # Yellow
        ERROR_COLOR = "\033[31m"  # Red
        DEFAULT_COLOR = "\033[0m"  # Reset to default

        if "WARNING" in line:
            print(f"{WARNING_COLOR}{line}{DEFAULT_COLOR}")
        elif "ERROR" in line:
            print(f"{ERROR_COLOR}{line}{DEFAULT_COLOR}")
        else:
            print(line)
    return True



import ctypes
class ShellError(Exception):
    def __init__(self, return_code: Optional[int], stdout: Any, stderr: Any) -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"Command exited with code: {return_code}\n{stderr}")

from multiprocessing.synchronize import Event as EventClass
import shlex
from typing import Union, List

def shell_run_inner(cmd: Union[str, List[str]], cwd: Optional[str], env_override: Optional[Dict[str, str]],
                    stdout_queue: multiprocessing.Queue, stderr_queue: multiprocessing.Queue,
                    pstop_event: EventClass, code_page: str,
                    retcode: multiprocessing.Queue, shell: bool = True) -> None:
    with subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, cwd=cwd, env=env_override) as process:
        # Stream stdout and stderr
        stop_event = threading.Event()
        stdout_thread = threading.Thread(target=blocking_streamer,
                                         args=(process.stdout, stdout_queue, stop_event),
                                         name="ShellRunStdout")
        stderr_thread = threading.Thread(target=blocking_streamer,
                                         args=(process.stderr, stderr_queue, stop_event),
                                         name="ShellRunStderr")

        # Start the threads
        stdout_thread.start()
        stderr_thread.start()

        ps = psutil.Process(process.pid)
            
        voluntarily_exit: Callable[[psutil.Process], bool]
        voluntarily_exit = lambda ps: ps.status() in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
        process_running = True

        stdout_i = 0
        stderr_i = 0
        while process_running:
            try:
                if voluntarily_exit(ps):
                    process_running = False
            except psutil.NoSuchProcess:
                process_running = False
            # stop_event is set because of an pipe readline error
            if stop_event.wait(timeout=0.01):
                process.kill()
                pstop_event.set()
                break
            # pstop_event is set because of the parent process decided to stop
            if pstop_event.wait(timeout=0.01):
                process.kill()
                break
        # Ensure the subprocess is terminated
        try:
            process.kill()
            ps.terminate()
        except Exception:
            pass
        process.poll()
        if retcode is not None:
            retcode.put(process.returncode)

def get_code_page() -> str:
    # return 'cp936'
    # si = subprocess.STARTUPINFO()
    # si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    result = subprocess.run('chcp.com', capture_output=True, shell=True)
    assert result.stdout is not None
    sc = re.search(rb'\d+', result.stdout)
    if sc is None:
        return 'cp936'
    assert sc is not None
    code_page = sc.group().decode('utf-8')
    return 'cp' + code_page


import select
import signal

class Unbuffered(object):
    def __init__(self, stream):
        self.stream = stream
    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
    def writelines(self, datas):
        self.stream.writelines(datas)
        self.stream.flush()
    def __getattr__(self, attr):
        return getattr(self.stream, attr)
import sys, typing, errno

def shell_run(cmd: Union[str, List[str]], timeout: Optional[float] = None, retry: bool = False,
              check_error: bool = True, callback: Callable[[str], Any] = print_output,
              cwd: Optional[str] = None, env_override: Optional[dict] = None,
              stop_event: Optional[multiprocessing.synchronize.Event] = None, silent: bool = False,
              kill_signal = signal.SIGTERM, grace_wait: bool = False, friendly_cmd_name: Optional[str] = "",
              retcode_out: Optional[Ref[int]] = None, pid_out: Optional[Ref[int]] = None,
              shell: bool = True) -> Tuple[str, str]:
    # Avoid wrapping stdout multiple times - causes recursive calls and BrokenPipeError
    if not isinstance(sys.stdout, Unbuffered):
        sys.stdout = typing.cast(typing.TextIO, Unbuffered(sys.stdout))
    # Manual type checking
    if not isinstance(cmd, (str, list)):
        raise TypeError(f"cmd must be of type str or list, but got {type(cmd)}")
    
    # Convert cmd to appropriate format based on shell parameter
    if shell:
        # shell=True expects a string
        if isinstance(cmd, list):
            cmd = shlex.join(cmd)
        cmd_for_display = cmd
    else:
        # shell=False expects a list
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        cmd_for_display = shlex.join(cmd)
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise TypeError(f"timeout must be of type float or int or None, but got {type(timeout)}")
    if not isinstance(retry, bool):
        raise TypeError(f"retry must be of type bool, but got {type(retry)}")
    if not isinstance(check_error, bool):
        raise TypeError(f"check_error must be of type bool, but got {type(check_error)}")
    if callback is not None and not callable(callback):
        raise TypeError(f"callback must be callable or None, but got {type(callback)}")
    if cwd is not None and not isinstance(cwd, str):
        raise TypeError(f"cwd must be of type str or None, but got {type(cwd)}")
    if env_override is not None and not isinstance(env_override, dict):
        raise TypeError(f"env_override must be of type dict or None, but got {type(env_override)}")

    # Determine if we're on Windows
    is_windows = os.name == 'nt'
    friendly_cmd_name = f" {friendly_cmd_name} " if ((friendly_cmd_name is not None) and (len(friendly_cmd_name) > 0)) else ""
    prompt = ''
    if is_windows:
        code_page = get_code_page()
        prompt = f"Running command{friendly_cmd_name}(" + code_page + "):" + "\x1b[92m\"" + cmd_for_display + "__pid__" + "\"\x1b[0m"
    else:
        prompt = f"Running command{friendly_cmd_name}:" + "\x1b[92m\"" + cmd_for_display + "\"\x1b[0m" + "__pid__"
        import fcntl
    # print the env overridden
    # if env_override is not None and not silent:
    #     for key, value in env_override.items():
    #         print(f"\x1b[92m\"{key}={value}\" \x1b[0m")
    # build the env by combining the env_override and the current env
    env = os.environ.copy()
    if env_override is not None:
        env.update(env_override)
    # if cwd is not None and not silent:
    #     print(f"\x1b[92m\"cwd={cwd}\" \x1b[0m")

    end_retry = False
    keyboard_interrupt = False

    stdout_lines = []
    stderr_lines = []
    retcode = 0
    start_time = time.time()
    if stop_event is None:
        if os.name == 'nt':
            stop_event = multiprocessing.Event()
        else:
            stop_event = threading.Event()  # type: ignore[assignment]
    while not end_retry:
        end_retry = True
        voluntarily_exit: Callable[[psutil.Process], bool]
        voluntarily_exit = lambda ps: ps.status() in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
        if is_windows:
            stdout_queue: multiprocessing.Queue = multiprocessing.Queue()
            stderr_queue: multiprocessing.Queue = multiprocessing.Queue()
            _retcode: multiprocessing.Queue = multiprocessing.Queue()
            process_holder = multiprocessing.Process(target=shell_run_inner,
                                                     args=(cmd, cwd, env, stdout_queue,
                                                           stderr_queue, stop_event,
                                                           code_page, _retcode, shell))
            process_holder.start()
            ps = psutil.Process(process_holder.pid)
            if pid_out is not None:
                pid_out.value = process_holder.pid
            prompt = prompt.replace("__pid__", f", pid={process_holder.pid}")
            if not silent:
                print(prompt)
                pass
            while True:
                try:
                    try:
                        while True:
                            line = stdout_queue.get(timeout=0.01)
                            ret = callback(line)
                            stdout_lines.append(line)
                            if ret is not None and ret == False:
                                stop_event.set()
                    except Empty:
                        pass
                    try:
                        while True:
                            line = stderr_queue.get(timeout=0.01)
                            ret = callback(line)
                            stderr_lines.append(line)
                            if ret is not None and ret == False:
                                stop_event.set()
                    except Empty:
                        pass

                    try:
                        if voluntarily_exit(ps):
                            break
                    except psutil.NoSuchProcess:
                        break
                    if stop_event.wait(0.01):
                        # Wait 1 second for the process_holder to exit
                        process_holder.join(1)
                        # If the process_holder is still alive, kill it
                        if process_holder.is_alive():
                            process_holder.kill()
                        break
                    if timeout is not None and time.time() - start_time > timeout:
                        end_retry = False
                        print(f"Command timed out after {timeout}s")
                        process_holder.terminate()
                        start_time = time.time()
                        break

                except KeyboardInterrupt:
                    print("Received keyboard interrupt. Terminating process_holder.")
                    keyboard_interrupt = True
                    process_holder.terminate()

            try:
                while True:
                    line = stdout_queue.get(timeout=0.01)
                    ret = callback(line)
                    stdout_lines.append(line)
                    if ret is not None and ret == False:
                        stop_event.set()
            except Empty:
                pass
            try:
                while True:
                    line = stderr_queue.get(timeout=0.01)
                    ret = callback(line)
                    stderr_lines.append(line)
                    if ret is not None and ret == False:
                        stop_event.set()
            except Empty:
                pass

            try:
                retcode = _retcode.get(timeout=0.01)
            except Empty:
                pass

        else:
            def make_async(file: typing.IO[bytes]):
                fcntl.fcntl(file, fcntl.F_SETFL, fcntl.fcntl(file, fcntl.F_GETFL) | os.O_NONBLOCK)

            def read_async(file: typing.IO[bytes]) -> bytes:
                try:
                    return file.read()
                except IOError as e:
                    if e.errno != errno.EAGAIN:
                        raise e
                    else:
                        return b''
            
            def split_buffer_lines(buffer: bytes) -> tuple[list[bytes], bytes]:
                lines = buffer.split(b'\n')
                if buffer.endswith(b'\n'):
                    output = lines[:-1]
                    buffer = b''
                else:
                    output = lines[:-1]
                    buffer = lines[-1]
                return output, buffer
                
            
            def readlines_timeout(file: typing.IO[bytes], timeout: float, buffer: bytes):
                start_time = time.time()
                while time.time() - start_time < timeout:
                    buffer += read_async(file)
                    if b'\n' in buffer:
                        return split_buffer_lines(buffer)
                    time.sleep(0.1)  # Sleep briefly to avoid high CPU usage
                return [], buffer

            process = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=False, cwd=cwd, env=env)
            ps = psutil.Process(process.pid)
            if pid_out is not None:
                pid_out.value = process.pid
            prompt = prompt.replace("__pid__", f", pid={process.pid}")
            if not silent:
                print(prompt)
            assert process.stdout is not None
            assert process.stderr is not None
            make_async(process.stdout)
            make_async(process.stderr)
            stdout_buffer: bytes = b''
            stderr_buffer: bytes = b''
            break_next_loop = False
            while True:
                try:
                    reads = [process.stdout.fileno(), process.stderr.fileno()]
                    select_ret = select.select(reads, [], [], 0.1)
                    for fd in select_ret[0]:
                        if fd == process.stdout.fileno():
                            bytes_lines, stdout_buffer = readlines_timeout(process.stdout, 0.1, stdout_buffer)
                            ret = None
                            for bytes_line in bytes_lines:
                                str_line = bytes_line.decode("utf-8", errors="backslashreplace")
                                ret = callback(str_line)
                                stdout_lines.append(str_line)
                            try:
                                if ret is not None and ret == False:
                                    stop_event.set()
                            except UnboundLocalError:
                                pass
                        if fd == process.stderr.fileno():
                            bytes_lines, stdout_buffer = readlines_timeout(process.stderr, 0.1, stdout_buffer)
                            for bytes_line in bytes_lines:
                                str_line = bytes_line.decode("utf-8", errors="backslashreplace")
                                ret = callback(str_line)
                                stderr_lines.append(str_line)
                            try:
                                if ret is not None and ret == False:
                                    stop_event.set()
                            except UnboundLocalError:
                                pass

                    if break_next_loop:
                        if b'\n' in stdout_buffer:
                            bytes_lines, stdout_buffer = split_buffer_lines(stdout_buffer)
                            for bytes_line in bytes_lines:
                                str_line = bytes_line.decode("utf-8", errors="backslashreplace")
                                callback(str_line)
                                stdout_lines.append(str_line)
                        else:
                            str_line = stdout_buffer.decode("utf-8", errors="backslashreplace")
                            callback(str_line)
                            stdout_lines.append(str_line)

                        if b'\n' in stderr_buffer:
                            bytes_lines, stderr_buffer = split_buffer_lines(stderr_buffer)
                            for bytes_line in bytes_lines:
                                str_line = bytes_line.decode("utf-8", errors="backslashreplace")
                                callback(str_line)
                                stderr_lines.append(str_line)
                        else:
                            str_line = stderr_buffer.decode("utf-8", errors="backslashreplace")
                            callback(str_line)
                            stderr_lines.append(str_line)
                        break

                    try:
                        if voluntarily_exit(ps):
                            break_next_loop = True
                    except psutil.NoSuchProcess:
                        break_next_loop = True

                    if timeout is not None and time.time() - start_time > timeout:
                        end_retry = False
                        process.send_signal(int(kill_signal))
                        print(f"Command timed out after {timeout}s")
                        break_next_loop = True
                    if stop_event.is_set():
                        process.send_signal(int(kill_signal))
                        if not silent:
                            print(f"Received stop event from caller. Terminating process using signal {int(kill_signal)}")
                        break_next_loop = True
                except KeyboardInterrupt:
                    process.send_signal(int(kill_signal))
                    break_next_loop = True

            # if grace_wait:
            #     while not process.poll():
            #         reads = [process.stdout.fileno(), process.stderr.fileno()]
            #         ret = select.select(reads, [], [], 1)
            #         for fd in ret[0]:
            #             if fd == process.stdout.fileno():
            #                 print("Exit 2.1")
            #                 readlns = process.stdout.readlines()
            #                 print("Exit 2.2")
            #                 for readb in readlns:
            #                     read = readb.decode("utf-8", errors="backslashreplace")
            #                     read = read.strip()
            #                     callback(read)
            #                     stdout_lines.append(read)
            #             if fd == process.stderr.fileno():
            #                 readlns = process.stderr.readlines()
            #                 for readb in readlns:
            #                     read = readb.decode("utf-8", errors="backslashreplace")
            #                     read = read.strip()
            #                     callback(read)
            #                     stderr_lines.append(read)
            process.poll()
            retcode = process.returncode
                


        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)

        if (retcode is not None and retcode == 0 and not silent) or (stop_event.is_set() and not silent):
            print("Command \x1b[92m\"" + cmd_for_display + "\"\x1b[0m" + f" exited with returncode {retcode}")
        elif not silent:
            print("Command \x1b[91m\"" + cmd_for_display + "\"\x1b[0m" + " exited or killed with returncode " + str(retcode) + " , retry: " + ("0" if end_retry else "1") )

        if not retry:
            end_retry = True

        if end_retry and check_error:
            if retcode and retcode != 0:
                raise subprocess.CalledProcessError(retcode, cmd, output=stdout, stderr=stderr)
            if keyboard_interrupt:
                raise subprocess.CalledProcessError(retcode, cmd, output=stdout, stderr=stderr)
                
    if retcode_out is not None:
        retcode_out.value = retcode if retcode is not None else 0
    return stdout, stderr


def get_clean_env() -> Dict[str, str]:
    # Set your required environment variables
    ret = {}
    ret['LANG'] = 'en_US.UTF-8'
    ret['TERM'] = 'screen-256color'
    ret['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    ret['HOME'] = os.environ['HOME']
    return ret

class dotdict(dict):
    """dot.notation access to dictionary attributes"""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


def get_argparse_defaults(parser):
    defaults = {}
    for action in parser._actions:
        if not action.required and action.dest != "help":
            defaults[action.dest] = action.default
    return defaults

import glob
def glob_path(d, pattern) -> str:
    d = os.path.expanduser(d)
    m = list(glob.glob(os.path.join(d, pattern)))
    if len(m) == 0:
        raise(FileNotFoundError(f'No file found for {pattern} in {d}'))
    if len(m) > 1:
        logger.error(f'Multiple files found for {pattern} in {d}')
        for i in m:
            logger.error(i)
        raise(RuntimeError(f'Multiple files found for {pattern} in {d}'))
    return m[0]

def is_notebook() -> bool:
    try:
        shell = get_ipython().__class__.__name__ # type: ignore
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter

def parse_known_args_notebook_compatible(parser: argparse.ArgumentParser):
    if is_notebook():
        return dotdict(get_argparse_defaults(parser)), None
    else:
        return parser.parse_known_args()

def parse_dir_list(input_dirs):
    # if input_dirs is string:
    if isinstance(input_dirs, str):
        # 将输入目录从字符串转换为列表
        input_dirs = input_dirs.strip()
        input_dirs = input_dirs.split(',')
        input_dirs = [s.split('\n') for s in input_dirs]
        input_dirs = [item for sublist in input_dirs for item in sublist]
        input_dirs = [item.strip() for item in input_dirs if len(item.strip()) > 0]
        input_dirs = [os.path.realpath(os.path.expanduser(d)) for d in input_dirs]
        return input_dirs
    elif isinstance(input_dirs, list):
        return [os.path.realpath(os.path.expanduser(d)) for d in input_dirs]

def add_pwtool_args(parser: argparse.ArgumentParser):
    parser.add_argument("-f1", "--file1", type=str, nargs='?', help="The file to be processed")
    parser.add_argument("-f2", "--file2", type=str, nargs='?', help="The file to be processed")

def handle_pwtool_args(args, f1_replace: str, f2_replace: str):
    if args.file1:
        with open(args.file1, "r") as f:
            content = f.read()
            setattr(args, f1_replace, content.strip())
        with open(args.file2, "r") as f:
            content = f.read()
            setattr(args, f2_replace, content.strip())
        return True
    else:
        return False
    if not getattr(args, f1_replace):
        raise ValueError(f"Please specify the {f1_replace}")
    if not getattr(args, f2_replace):
        raise ValueError(f"Please specify the {f2_replace}")

import os
from typing import Optional

def read_string_from_file(fn: str) -> Optional[str]:
    """
    Reads the entire content of a file and returns it as a string.

    Args:
        fn (str): The path to the file to be read.

    Returns:
        Optional[str]: The content of the file as a string, or None if an error occurs.
    """
    try:
        # Expand the '~' in the path
        expanded_fn = os.path.expanduser(fn)
        # Ensure the file exists
        if not os.path.isfile(expanded_fn):
            print(f"Error: The file '{expanded_fn}' does not exist.")
            return None
        # Read the file content
        with open(expanded_fn, 'r', encoding='utf-8') as file:
            return file.read()
    except Exception as e:
        print(f"Error reading file '{fn}': {e}")
        return None

def write_string_to_file(content: str, fn: str) -> bool:
    """
    Writes a string to a file, overwriting the file if it already exists.

    Args:
        content (str): The string content to write to the file.
        fn (str): The path to the file to write.

    Returns:
        bool: True if the write operation was successful, False otherwise.
    """
    try:
        # Expand the '~' in the path
        expanded_fn = os.path.expanduser(fn)
        # Write the content to the file
        with open(expanded_fn, 'w', encoding='utf-8') as file:
            file.write(content)
        return True
    except Exception as e:
        print(f"Error writing to file '{fn}': {e}")
        return False

def write_and_check(file_path: str, content: str, max_retries: int = 5, delay: float = 1.0, logger = None):
    """
    写入内容到文件并检查最后一行是否写入成功。如果失败则重试，直到成功或达到最大重试次数。

    :param file_path: 文件路径
    :param content: 写入的内容
    :param max_retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    """
    if not logger:
        logger = logging.getLogger(__name__)
    retries = 0
    while retries < max_retries:
        try:
            with open(file_path, 'a+') as file:
                # 写入内容并刷新
                file.write(content)
                file.flush()

                # 移动到文件的开始位置
                file.seek(0, 0)

                # 读取所有行并检查最后一行
                lines = file.readlines()
                if lines and lines[-1] == content:
                    logger.debug("Successfully wrote the last line to the file")
                    return True
                else:
                    logger.error("Failed to write the last line to the file, retrying...")
        except Exception as e:
            logger.error(f"Error writing to file: {e}, retrying...")

        retries += 1
        time.sleep(delay)

    logger.error("Exceeded maximum retries, failed to write the last line to the file")
    return False

def remove_argument(parser, arg):
    for action in parser._actions:
        opts = action.option_strings
        if (opts and opts[0] == arg) or action.dest == arg:
            parser._remove_action(action)
            break

    for action in parser._action_groups:
        for group_action in action._group_actions:
            opts = group_action.option_strings
            if (opts and opts[0] == arg) or group_action.dest == arg:
                action._group_actions.remove(group_action)
                return

def use_single_dash_argument(parser, arg, **kwargs):
    remove_argument(parser, f"--{arg}")
    parser.add_argument(f"-{arg}", **kwargs)

def args_to_cmdline(args: dict[str, Any]) -> str:
    cmdline = ""
    for key, value in args.items():
        if isinstance(value, bool):
            if value:
                cmdline += f" --{key}"
        else:
            if len(key) == 1:
                cmdline += f" -{key} {value}"
            else:
                cmdline += f" --{key} {value}"

import os

def get_username():
    import pwd

    return pwd.getpwuid(os.getuid())[0]
