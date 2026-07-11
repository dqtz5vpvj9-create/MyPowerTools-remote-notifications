import os
import time
import subprocess
import re
from typing import Optional, Tuple
import importlib, sys
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

from . lib_aosp_base import *
from . logging_lib import setup_logging, LogLevel, MyLogger
import logging
from datetime import datetime as datetime_class
from typing import Any
import os
import re
import socket
import logging
from typing import Optional
from abc import ABCMeta, abstractmethod
from typing import final
import concurrent.futures
from functional import seq
from typing import Callable
import string
import base64
import hashlib
import re
import logging
import threading
from typing import List, Optional, Union

import psutil

class FnStr():
    @staticmethod
    def time() -> str:
        timestamp = datetime_class.now().strftime("%Y%m%d.%H.%M.%S")
        return timestamp

def zsh(cmd: str) -> str:
    try:
        ret = subprocess.check_output(["/usr/bin/zsh", "-i", "-c", cmd], stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        ret = ""
    return ret

def pwsh(cmd: str) -> str:
    try:
        ret = subprocess.check_output(["pwsh", "-C", cmd], stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        ret = ""
    return ret

def get_zygote_pid(serial):
    """
    Get the PID of zygote from the adb device with the given serial.
    
    Parameters:
    - serial (str): The serial number of the adb device.
    
    Returns:
    - int: The PID of zygote. Returns None if not found.
    """
    try:
        # Run the adb command to get the list of processes
        cmd = ["adb", "-s", serial, "shell", "ps", "-A"]
        result = subprocess.check_output(cmd, text=True)

        # Search for the zygote line in the output
        for line in result.splitlines():
            if "zygote" in line:
                # Split the line into columns and get the PID, which is typically the second column
                columns = line.split()
                return int(columns[1])
                
    except subprocess.CalledProcessError:
        print("Error executing the adb command.")
        
    return None

def convert_to_valid_filename(path: str) ->str:
    valid_chars = "-_.%s%s" % (string.ascii_letters, string.digits)
    return ''.join(c if c in valid_chars else '_' for c in path)

def slugify_path(text):

    # Replace non-alphanumeric characters (excluding space, dash, and dot) with underscore
    result = re.sub(r'[^a-zA-Z0-9\s\-\.]', '_', text)
    
    # Replace multiple spaces with a single space and trim leading and trailing spaces
    result = re.sub(r'\s+', ' ', result).strip()

    # Replace spaces with dashes
    result = re.sub(r'\s', '-', result)
    
    return result

def slugify_path_wrong(text):

    # Replace non-alphanumeric characters (excluding space, dash, and dot) with underscore
    result = re.sub(r'[^a-z0-9\s\-\.]', '_', text)
    
    # Replace multiple spaces with a single space and trim leading and trailing spaces
    result = re.sub(r'\s+', ' ', result).strip()

    # Replace spaces with dashes
    result = re.sub(r'\s', '-', result)
    
    return result

def get_package_uid_old(package: str) -> int:
    uid = As(f"dumpsys package {package} | grep userId", [AsOption.STDOUT_NO_PRINT])
    uid = re.findall(r'userId=(\d+)', uid)[0]
    return int(uid)

def get_apk_package_name(apk_path: str) -> str:
    # Use aapt to get the package name from the APK file
    result = subprocess.check_output([f'{os.environ.get("ANDROID_HOME")}/build-tools/31.0.0/aapt', 'dump', 'badging', os.path.realpath(apk_path)])
    match = re.search(r"package: name='([^']+)'", result.decode('utf-8'))
    if match:
        return match.group(1)
    raise ValueError("Package name not found in APK")

def fetch_target_uid_list():
    # 使用adb shell cat命令读取文件内容
    cmd = f"adb -s {serial} shell cat /data/local/tmp/test_uid"
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()

    if err:
        print("Error:", err.decode())
        return

    target_uid_list = set()
    for line in out.decode().split('\n'):
        line = line.strip()
        if line:
            target_uid_list.add(int(line))
    return target_uid_list

import tempfile
def send_target_uid_list(lst):
    # 使用adb shell cat命令读取文件内容
    with tempfile.NamedTemporaryFile() as f:
        for line in lst:
            f.write(f"{line}\n".encode())
        f.flush()
        cmd = f"adb -s {serial} push {f.name} /data/local/tmp/test_uid"
        subprocess.run(cmd, shell=True, check=True)

def send_file_content(file_path, content):
    # 使用adb shell cat命令读取文件内容
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write(content)
        f.flush()
        cmd = f"adb -s {serial} push {f.name} {file_path}"
        subprocess.run(cmd, shell=True, check=True)

def fetch_file_content(file_path):
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file_path = temp_file.name
    try:
        cmd = f"adb -s {serial} pull {file_path} {temp_file_path}"
        subprocess.run(cmd, shell=True, check=True)
        with open(temp_file_path, "r") as file:
            content = file.read()
    finally:
        os.unlink(temp_file_path)
    return content

import os
from pathlib import Path

def get_changed_files_in_working_tree(git_root):
    try:
        result = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=git_root).decode('utf-8')
        return result.splitlines()
    except subprocess.CalledProcessError:
        return []

def get_latest_modified_date(root, files=None):
    latest_date = 0
    if files is None:
        # Get all files in the root directory recursively
        files = []
        for subdir, dirs, files_in_dir in os.walk(root):
            for file in files_in_dir:
                files.append(os.path.join(subdir, file))
        try:
            files.remove('')
            files.remove('.')
        except ValueError:
            pass
    
    for file in files:
        try:
            file_modified_date = os.path.getmtime(Path(root) / file)
            if file_modified_date > latest_date:
                latest_date = file_modified_date
        except Exception:
            pass
    
    if latest_date == 0:
        return None
    return datetime_class.fromtimestamp(latest_date).strftime('%Y-%m-%d %H:%M:%S')


class AndroidAppFinder():
    def __init__(self, logger: MyLogger) -> None:
        self.logger = logger
    def get_app_pid_and_start_time_raw(self) -> Tuple[Optional[str], Optional[str]]:
        """ Read from android device to find the pid and start time of the app
            return un-parsed(str) format

        Raises:
            Exception: _description_

        Returns:
            Tuple[Optional[str], Optional[str]]: pid, start_time in str format
        """        

        pattern = r"(\w+)_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2}) (\d+)"
        cmd = "cat /data/data/com.example.largeobjectstest/files/pid.txt"
        try:
            output = As(cmd)
            output_str = output.strip()
            match = re.match(pattern, output_str)
            if not match:
                self.logger.debug(output)
                raise Exception("Invalid output format")
            return match.group(7), match.group(1) + "_" + match.group(2) + "_" + match.group(3) + "_" + match.group(4) + "_" + match.group(5) + "_" + match.group(6)
        except subprocess.CalledProcessError as e:
            self.logger.debug("Error: Unable to read pid.txt")
            return None, None
        except Exception as e:
            self.logger.debug("Error: " + str(e))
            return None, None

    import time

    def get_app_pid_and_start_time(self) -> Tuple[Optional[int], Optional[time.struct_time]]:
        """Read from android device to find the pid and start time of the app
           return parsed(str, time.struct_time) format

        Returns:
            Tuple[Optional[int], Optional[time.struct_time]]: pid, start_time
        """        

        pid, file_time = self.get_app_pid_and_start_time_raw()
        if pid is not None and file_time is not None:
            try:
                file_time_float = time.mktime(time.strptime(file_time, "%Y_%m_%d_%H_%M_%S"))
                return int(pid), time.localtime(file_time_float)
            # Exceptions might be raised if the time string is invalid or the pid is not a number
            except ValueError as e:
                self.logger.debug("Error: Invalid pid format")
            except Exception as e:
                self.logger.debug("Error: Invalid time format" + str(e))
        else:
            self.logger.debug("can not get pid and start time")
        return None, None

    def get_last_running_application(self, start_time: Optional[float] = None) -> int:
        """Wait for a new application to start and return its pid

        Args:
            start_time (Optional[float], optional): the time started waiting an app, app started before this time is ignored. Defaults to None.

        Returns:
            int: pid
        """        
        if not start_time:
            start_time = time.time()
        while True:
            pid, time_st = self.get_app_pid_and_start_time()
            if pid is not None and time_st is not None:
                try:
                    file_time_float = time.mktime(time_st)
                    if file_time_float > start_time:
                        self.logger.debug("A new application with PID {} has been started.".format(pid))
                        return int(pid)
                    else:
                        self.logger.debug("The application with PID {} has been started at {} before {}.".format(pid, time_st, time.strftime("%Y_%m_%d_%H_%M_%S", time.localtime(start_time))))
                except Exception as e:
                    self.logger.debug("Error: Invalid time format")
            else:
                self.logger.debug("pid.txt file not found")
            time.sleep(1)


class CommandExecutor:
    def execute(self, command: str) -> str:
        return As(command, options=AsOption.STDOUT_NO_PRINT)

class AndroidRuntimeFinder:
    def __init__(
        self, 
        logger: MyLogger, 
        command_executor: 'CommandExecutor' = CommandExecutor(),
        pid_file_dir: str = "/data/local/tmp/aproc/*", 
        pid_file: str = "art.pid") -> None:
        
        self.logger = logger
        self.pid_file_dir = pid_file_dir
        self.pid_file = pid_file
        self.command_executor = command_executor
        self.debug_output_interval = 500

    def find_runtime(self, _start_date: Optional[datetime_class] = None) -> Optional[int]:
        found_pid = False
        iter = 0
        
        if not _start_date:
            start_date = datetime_class.now()
        else:
            start_date = _start_date

        while not found_pid:
            try:
                ls_out = self.command_executor.execute(f"ls -lltr {self.pid_file_dir}/{self.pid_file}")
            except Exception as e:
                self._log_debug_output(f"Error executing ls: {e}")
                time.sleep(0.01)
                continue
                
            found_pid, pid = self._parse_ls_output(ls_out, start_date)
            if found_pid:
                return pid
            time.sleep(0.01)
        return None

    def _parse_ls_output(self, ls_out: str, start_date: datetime_class) -> Tuple[bool, Optional[int]]:
        ls_out = ls_out.strip()
        if not ls_out or len(ls_out) == 0:
            self.logger.debug("No output from ls" + ls_out)
            return False, None

        if re.match(r"device.*not found", ls_out):
            self.logger.debug("Device not found" + ls_out)
            return False, None

        ls_out = ls_out.split("\n")[-1]
        self._log_debug_output(ls_out)

        pattern = r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*" + re.escape(self.pid_file)
        fn_pattern = r"([\S]+$)"
        match = re.search(pattern, ls_out)

        if match:
            date = datetime_class.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            if date > start_date:
                match = re.search(fn_pattern, ls_out)
                if match:
                    fn = match.group(1)
                    self.logger.debug("Found pid file %s", fn)
                    return self._get_pid_from_file(fn)
            else:
                self._log_date_debug(date, start_date)
                return False, None
        else:
            self.logger.debug("No match for pattern %s", pattern)
            return False, None
        return False, None

    def _get_pid_from_file(self, fn: str) -> Tuple[bool, Optional[int]]:
        pid_out = self.command_executor.execute(f"cat {fn}").strip()
        match = re.search(r"\d+", pid_out)
        
        if match:
            pid_out = match.group(0)
            self.logger.info("Found pid: %s", pid_out)
            return True, int(pid_out)
        else:
            self.logger.info("Invalid pid")
            return False, None

    def _log_debug_output(self, ls_out: str) -> None:
        is_debug = False
        
        if self.debug_output_interval == 0:
            is_debug = True
            self.debug_output_interval = 100
        else:
            self.debug_output_interval -= 1

        if is_debug:
            self.logger.debug("last line: %s", ls_out)

    def _log_date_debug(self, date: datetime_class, start_date: datetime_class) -> None:
        if self.debug_output_interval == 0:
            self.logger.debug("Found pid file at %s but it is older than %s", date, start_date)


class AndroidRuntimeFinder_old:
    def __init__(self, logger: MyLogger, pid_file_dir: str = "/data/local/tmp/aproc/*", pid_file: str = "art.pid") -> None:
        self.logger = logger
        self.pid_file_dir = pid_file_dir
        self.pid_file = pid_file
        self.debug_output_interval = 100

    def find_runtime(self, _start_date: Optional[datetime_class] = None) -> Optional[int]:
        found_pid = False
        iter = 0
        if not _start_date:
            start_date = datetime_class.now()
        else:
            start_date = _start_date

        while not found_pid:
            try:
                ls_out = As(f"ls -lltr {self.pid_file_dir}/{self.pid_file}", options=AsOption.STDOUT_NO_PRINT)
            except Exception as e:
                self.logger.debug(f"Error executing ls: {e}")
                continue
            ls_out = ls_out.strip()
            iter += 1
            if not ls_out or len(ls_out) == 0:
                self.logger.debug("No output from ls" + ls_out)
                continue
            if re.match(r"device.*not found", ls_out):
                self.logger.debug("Device not found" + ls_out)
                return None
            ls_out = ls_out.split("\n")[-1]
            is_debug = False
            if self.debug_output_interval == 0:
                is_debug = True
                self.debug_output_interval = 100
            else:
                self.debug_output_interval -= 1

            if is_debug:
                self.logger.debug("last line: %s", ls_out)
            pattern = r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*" + re.escape(self.pid_file)
            fn_pattern = r"([\S]+$)"
            match = re.search(pattern, ls_out)
            if match:
                date = datetime_class.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                if date > start_date:
                    found_pid = True
                    match = re.search(fn_pattern, ls_out)
                    if match:
                        fn = match.group(1)
                        self.logger.debug("Found pid file %s", fn)
                        pid_out = As(f"cat {fn}").strip()
                        match = re.search(r"\d+", pid_out)
                        if match:
                            pid_out = match.group(0)
                            self.logger.info("Found pid: %s", pid_out)
                            # As(f"echo {pid_out} > /dev/memcg/test_art/tasks")
                            # tasks_out = As("cat /dev/memcg/test_art/tasks")
                            # self.logger.debug("Tasks: %s", tasks_out)
                            return int(pid_out)
                        else:
                            self.logger.info("Invalid pid")
                else:
                    if is_debug:
                        self.logger.debug("Found pid file at %s but it is older than %s", date, start_date)
            else:
                self.logger.debug("No match for pattern %s", pattern)
        return None

class RepoManagerBase(metaclass=ABCMeta):
    def __init__(self, path: str, logger: MyLogger) -> None:
        self.path = path
        self.logger = logger
        self.last_status_fn = os.path.join("/tmp", convert_to_valid_filename(self.path) + "_last_status.txt")
    @abstractmethod
    def get_current_status(self) -> str:
        pass
    @abstractmethod
    def get_current_status_digest(self) -> str:
        pass
    @final
    def has_changed_since_last(self) -> bool:
        status = self.get_current_status_digest().splitlines()
        if isinstance(status, str):
            status = [status]
        # count lines of status
        status_line_cnt = len(status)
        changed: bool = False
        if not os.path.exists(self.last_status_fn):
            self.logger.debug("last_status_fn not found")
            changed = True
        else:
            with open(self.last_status_fn, "r") as f:
                last_status = f.readlines()
                last_status_line_cnt = len(last_status)
                if status_line_cnt != last_status_line_cnt:
                    self.logger.debug("Count of repo changed {}->{}".format(last_status_line_cnt, status_line_cnt))
                    changed = True
                else:
                    for i in range(status_line_cnt):
                        if status[i].strip() != last_status[i].strip():
                            self.logger.debug("Repo changed {}->{}".format(last_status[i], status[i]))
                            changed = True
        return changed

    @final
    def record_current_status(self) -> None:
        status = self.get_current_status_digest()
        with open(self.last_status_fn, "w") as f:
            f.write(status)


class MyRepoManager(RepoManagerBase):
    def __init__(self, path: str, repos: List[str], logger: MyLogger) -> None:
        super().__init__(path, logger)
        self.repos = repos
        pass

    @staticmethod
    def GetRepoManager(aosp_repos: List[str], extend_repos: List[str], blacklist: List[str], update_only: List[str], record_status_repos: List[str], kernel_dir: str, kernel_repos: List[str], logger):
        aosp_repos += extend_repos
        filtered_not_repos: Callable[[list], list] = lambda lst: seq(aosp_repos).filter_not(lambda x: any([re.match(pt, x) for pt in lst])).map(lambda x: os.path.join(ASRCDIR, x)).to_list()
        filtered_repos: Callable[[list], list] = lambda lst: seq(aosp_repos).filter(lambda x: any([re.match(pt, x) for pt in lst])).map(lambda x: os.path.join(ASRCDIR, x)).to_list()
        aosp_compile_repos = filtered_not_repos(blacklist + update_only)
        aosp_update_only_repos = filtered_repos(update_only)
        aosp_record_status_repos = filtered_repos(record_status_repos)
        check_repo_exist = lambda repos: seq(repos).map(lambda x: os.path.exists(x)).all()
        kernel_repos = seq(kernel_repos).map(lambda x: os.path.join(kernel_dir, x)).to_list()
        if not check_repo_exist(aosp_compile_repos):
            for repo in aosp_compile_repos:
                if not os.path.exists(repo):
                    logger.error("Repo not found: " + repo)
        assert(check_repo_exist(aosp_update_only_repos))
        assert(check_repo_exist(aosp_record_status_repos))
        logger.debug(len(aosp_compile_repos))
        logger.debug(len(aosp_update_only_repos))
        logger.debug(len(aosp_record_status_repos))
        aosp_sync_repo_manager = MyRepoManager(ASRCDIR + "sync", aosp_update_only_repos, logger)
        aosp_compile_repo_manager = MyRepoManager(ASRCDIR + "compile", aosp_compile_repos, logger)
        record_status_repos_manager = MyRepoManager(ASRCDIR + "record_status" ,aosp_record_status_repos + kernel_repos, logger)
        kernel_repo_manager = MyRepoManager(kernel_dir, kernel_repos, logger)
        # return a dict of repo managers, with full names 
        return {"aosp_sync": aosp_sync_repo_manager, "aosp_compile": aosp_compile_repo_manager, "record_status": record_status_repos_manager, "kernel": kernel_repo_manager}
        
        

    def get_current_status_digest(self) -> str:
        ret = ""
        # Create a ThreadPoolExecutor with 16 workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            # Submit a function for each repo and store the futures in a list
            futures = [executor.submit(self.get_current_status_digest_per_repo, repo) for repo in self.repos]
            # Iterate over the futures and get their values
            for future in concurrent.futures.as_completed(futures):
                ret += future.result()
        # Sort ret by repo name so it returns same result for unchanged repos in different runs
        return ''.join(sorted(ret.splitlines(keepends=True)))

    def get_current_status(self) -> str:
        ret = ""
        for repo in self.repos:
            ret += GitRepoManager(repo, self.logger).get_current_status()
            # 分割线
            ret += "\n--------------------------------\n"
        return ret

    def get_current_status_digest_per_repo(self, repo: str) -> str:
        # This function returns the status of a single repo
        if not os.path.exists(os.path.join(self.path, repo)):
            raise Exception("Repo not found: " + repo)
        gitrepo = GitRepoManager(repo, self.logger)
        return f"{repo}: {gitrepo.get_current_status_digest()}\n"
    def fast_forward(self) -> bool:
        ret = False
        for repo in self.repos:
            gitrepo = GitRepoManager(repo, self.logger)
            ret = gitrepo.fast_forward() or ret
        return ret


class GitRepoManager(RepoManagerBase):
    def __init__(self, path: str, logger: MyLogger) -> None:
        super().__init__(path, logger)

    def fast_forward(self) -> bool:
        git_status = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "status"]).decode("UTF-8")
        self.logger.debug(f"Current status: {git_status}")
        NO_TRACKING_PROMPT = "There is no tracking information for the current branch"
        if "nothing to commit, working tree clean" in git_status and ("HEAD detached" not in git_status):
            subprocess.run(["/usr/bin/git", "-C", f"{self.path}", "log", "-n", "1"])
            try:
                git_update = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "pull", "--ff-only"], stderr=subprocess.STDOUT).decode("UTF-8")
            except subprocess.CalledProcessError as e:
                if isinstance(e.stdout, bytes):
                    stdout = e.stdout.decode("UTF-8")
                    if "There is no tracking information for the current branch" in stdout:
                        git_update = stdout
                    else:
                        raise e
                else:
                    raise e
            self.logger.debug(git_update)
            git_status = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "status"]).decode("UTF-8")
            subprocess.run(["/usr/bin/git", "-C", f"{self.path}", "log", "-n", "1"])
            if not "Fast-forward" in git_update and not "Already up to date" in git_update and not NO_TRACKING_PROMPT in git_update:
                raise Exception("Please review git update")
            else:
                return True
        return False
    def get_current_status(self) -> str:
        commit_id = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "rev-parse", "HEAD"]).decode("UTF-8").strip()
        commit_msg = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "log", "-n", "1", commit_id]).decode("UTF-8").strip()
        git_status = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "status"]).decode("UTF-8").strip()
        git_diff = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "diff"]).decode("UTF-8").strip()
        try:
            branch_tag = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "describe", "--tags", "--exact-match"], stderr=subprocess.PIPE).decode("UTF-8").strip()
        except subprocess.CalledProcessError:
            branch_tag = ''
        if branch_tag == '':
            branch = subprocess.check_output(["/usr/bin/git", "-C", f"{self.path}", "rev-parse", "--abbrev-ref", "HEAD"]).decode("UTF-8").strip()
        else:
            branch = branch_tag
        template = """repo: %s
branch: %s
tag: %s
commit_id: %s
commit_msg:
>>>>>>>>
%s
<<<<<<<<
git_status:
>>>>>>>>
%s
<<<<<<<<
git_diff: 
>>>>>>>>>>>>>>>>>
%s
<<<<<<<<<<<<<<<<<
"""
        status = template % (self.path, branch, branch_tag, commit_id, commit_msg,git_status, git_diff)
        status = status.strip()
        return status
    def get_current_status_digest(self) -> str:
        # with open(os.path.join("/tmp", convert_to_valid_filename(self.path) + "_last_status" + convert_to_valid_filename(str(datetime_class.now())) + ".txt"), "w") as f:
        #     f.write(self.get_current_status())
        return hashlib.sha256(self.get_current_status().encode("utf-8")).hexdigest()


class StreamFileFilter:
    condition_type = Union[str, re.Pattern[str]]
    def __init__(self, logger: MyLogger, logcat_fn: str, app_pid: int,
                 conditions: Optional[List[condition_type]] = None,
                 failed_event: Optional[threading.Event] = None,
                 stop: Optional[threading.Event] = None, 
                 failed_reasons: Optional[List[str]] = None):
        self.logger = logger
        self.logcat_fn = logcat_fn
        self.app_pid = app_pid
        self.app_logcat_fn = f"{self.logcat_fn}_app_{self.app_pid}.log"
        self.full_fn = f"{self.logcat_fn}_full.log"

        self.conditions: List[StreamFileFilter.condition_type] = [
            re.compile(r"0\s+?0"),
            re.compile("--------- beginning of .*"),
        ]
        if conditions:
            self.conditions += conditions
        self.conditions.insert(0, re.compile(f"\\b{app_pid}\\b"))

        self.failed_event = failed_event
        self.stop = stop
        self.failed_reasons = failed_reasons

    def should_print_line(self, line: str) -> bool:
        match = re.search(r"(\d+)\s+I\s+crash_dump64:\s+performing dump of process (\d+)", line)
        ret = False
        if match:
            crash_dump64_pid = match.group(1)
            dumped_pid = match.group(2)
            self.logger.debug(f"Found crash_dump64 ({crash_dump64_pid}) dumping pid {dumped_pid}")
            if int(dumped_pid) == self.app_pid:
                self.conditions.append(crash_dump64_pid)
                self.logger.info(f"Added pid of crash_dump64 ({crash_dump64_pid}) to filter conditions")
                if self.failed_event:
                    self.failed_event.set()
                    if self.failed_reasons:
                        self.failed_reasons.append("crash_dump started")
                ret = True
        for condition in self.conditions:
            if isinstance(condition, str) and condition in line:
                ret = True
            elif isinstance(condition, re.Pattern) and condition.search(line) is not None:
                ret = True
        return ret

    def run(self) -> None:
        position = 0
        full_fl = open(self.full_fn, "w")
        last_change_time = time.time()
        with open(self.app_logcat_fn, "w") as output_file:
            while True:
                if self.stop and self.stop.is_set():
                    break
                if self.failed_event and self.failed_event.is_set():
                    break
                with open(self.logcat_fn, "r") as f:
                    f.seek(position)
                    lines = f.readlines()
                    new_position = f.tell()
                    if new_position != position:
                        position = new_position
                        last_change_time = time.time()
                    else:
                        if time.time() - last_change_time > 20:
                            self.logger.error("No new lines for 20 seconds")
                            if self.failed_event:
                                self.failed_event.set()
                            if self.failed_reasons:
                                self.failed_reasons.append("No new lines for 20 seconds")

                for line in lines:
                    full_fl.write(line)
                    full_fl.flush()
                    if self.should_print_line(line):
                        output_file.write(line)
                        output_file.flush()
                    

def TestStreamFileFilter() -> None:
    logger = setup_logging("StreamFileFilter")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    app_pid = 4113
    logcat_fn = f"{aosp_host_working_dir}/adb-20230221.13.12.43.log"

    failed_event = threading.Event()
    stop = threading.Event()

    stream_file_filter = StreamFileFilter(logger, logcat_fn, app_pid, failed_event=failed_event, stop=stop)
    stream_file_filter_thread = threading.Thread(target=stream_file_filter.run)
    stream_file_filter_thread.start()

    # Wait for crash dump to start
    logger.debug("Waiting for crash dump to start")
    failed_event.wait()

    # Do something

    # Set the stop flag to stop the stream_file_filter
    stop.set()

    # Wait for stream_file_filter to finish
    stream_file_filter_thread.join()


class EmulatorManager():
    def __init__(self, logger: MyLogger) -> None:
        pass
    
    @staticmethod
    def detect_emulator(port: int, adb_output: Optional[str] = None) -> bool:
        if not adb_output:
            result = subprocess.run(["adb", "devices"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            adb_output = result.stdout.decode()
            print(adb_output)
        if f"emulator-{port}" in adb_output and not "offline" in adb_output:
            return True
        return False
    
    @staticmethod
    def get_emulator_pid(port: int) -> Optional[int]:
        for proc in psutil.process_iter(['pid', 'connections']):
            conns = proc.info['connections'] # type: ignore
            if conns:
                for connection in conns:
                    if connection.laddr.port == port and connection.status == 'LISTEN':
                        return proc.info['pid'] # type: ignore
        return None
    
# def kill_all_children_process(logger: Optional[MyLogger]) -> None:
#     current_process = psutil.Process()
#     children = current_process.children(recursive=True)
#     for child in children:
#         if logger:
#             logger.info(f"Killing child process: {child.pid} {child.cmdline()}")
#         try:
#             child.kill()
#         except psutil.NoSuchProcess as e:
#             if logger:
#                 logger.error(f"Failed to kill child process: {e}")

def kill_process_dfs(process: psutil.Process, logger: Optional[MyLogger] = None, kill_self = True, exclude_list = None) -> None:
    try:
        if exclude_list and process.pid in exclude_list:
            logger.debug(f"Process {process.pid} is in exclude list, skipping")
            return
        else:
            logger.debug(f"Process {process.pid} is not in exclude list {exclude_list}")
        children = process.children()
        if logger:
            logger.debug(f"Found {len(children)} children for process {process.pid}: {[child.pid for child in children]}")
        for child in children:
            kill_process_dfs(child, logger, exclude_list=exclude_list)  # Recursively kill all descendants
        if logger:
            try:
                process_desc = str(process)
            except Exception:
                process_desc = ""
            logger.debug(f"Killed process {process.pid}, {process_desc}")
        if kill_self:
            process.kill()
    except psutil.NoSuchProcess as e:
        if logger:
            logger.debug(f"Process {process.pid} already dead: {e}")
    except psutil.AccessDenied as e:
        if logger:
            logger.error(f"Access denied when trying to kill process {process.pid}: {e}")

def kill_all_children_process(logger: Optional[MyLogger] = None, kill_self = False, exclude_list = None) -> None:
    current_process = psutil.Process()
    if logger:
        logger.info(f"Starting to kill all child processes of {current_process.pid}")
    kill_process_dfs(current_process, logger, kill_self, exclude_list)

def list_process_dfs(process: psutil.Process, logger: Optional[MyLogger] = None, result_list = None) -> None:
    try:
        children = process.children()
        if logger:
            logger.debug(f"Found {len(children)} children for process {process.pid}: {[child.pid for child in children]}")
        for child in children:
            list_process_dfs(child, logger, result_list)  # Recursively kill all descendants
        if logger:
            try:
                process_desc = str(process)
            except Exception:
                process_desc = ""
            logger.debug(f"List process {process.pid}, {process_desc}")
        result_list.append(process.pid)
    except psutil.NoSuchProcess as e:
        if logger:
            logger.debug(f"Process {process.pid} already dead: {e}")
    except psutil.AccessDenied as e:
        if logger:
            logger.error(f"Access denied when trying to kill process {process.pid}: {e}")

def list_all_children_process(pid, logger: Optional[MyLogger] = None) -> list:
    current_process = psutil.Process(pid)
    result_list = []
    if logger:
        logger.info(f"Starting to List all child processes of {current_process.pid}")
    list_process_dfs(current_process, logger, result_list)
    return result_list
    
if __name__ == '__main__':
    __self_test_logger = setup_logging()
    # repom = GitRepoManager("/android/aosp/art", __self_test_logger)
    # print(repom.get_current_status())
    # TestStreamFileFilter()
    # artfinder = AndroidRuntimeFinder(logger=logger)
    # print(artfinder.find_runtime())
    print(list_all_children_process(psutil.Process().pid))
