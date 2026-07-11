
import os
import sys
import tty
import termios
import select
import subprocess
import subprocess, sys
import signal
import types
import importlib
# import pty
from functools import partial

from typing import Any, Callable, Optional, Generic, TypeVar, Union, cast
from pathlib import Path
from queue import Queue
from subprocess import PIPE
import sys, importlib
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


if __name__ == '__main__' and __package__ is None:
    import_parents()


from . check_interpreter import check_conda_interpreter
from . logging_lib import setup_logging
from . my_pty import spawn as my_pty_spawn
from . my_pty import Poller

# if __package__ is None or __package__ == '':
# else:
#     print("__package__: ", __package__)
#     from py_modules.check_interpreter import check_interpreter
#     from py_modules.logging_lib import setup_logging
#     from py_modules.my_pty import spawn as my_pty_spawn
#     from py_modules.my_pty import Poller

import importlib.util, importlib.machinery
import sys

def modify_and_import(module_name: str, package: Optional[str], modification_func: Callable[[str], str]) -> types.ModuleType:
    spec = importlib.util.find_spec(module_name, package)
    assert(spec)
    assert(isinstance(spec.loader, importlib.machinery.SourceFileLoader))
    source = spec.loader.get_source(module_name)
    assert(source)
    new_source = modification_func(source)
    module = importlib.util.module_from_spec(spec)
    assert(isinstance(module, types.ModuleType))
    assert(isinstance(module.__spec__, importlib.machinery.ModuleSpec))
    assert(isinstance(module.__spec__.origin, str))
    codeobj = compile(new_source, module.__spec__.origin, 'exec')
    exec(codeobj, module.__dict__)
    sys.modules[module_name] = module
    return module

# def my_pty_modify(src: str) -> str:
#     src_old_1 = """
#     return waitpid(pid, 0)[1]
# """
#     src_new_1 = """
#     return waitpid(pid, 0)[1]
# """
#     # src_old should appear in src exactly once
#     assert(src.count(src_old) == 1)
#     return src.replace(src_old, src_new)

# my_pty = modify_and_import("pty", None, my_pty_modify)


    

pid_t = int
MonitorType = Callable[[Optional[bytes]], None]
MonOutType = Queue[bytes]
import threading
MonProcessType = tuple[Optional[MonOutType], pid_t, Poller]


from typing import ClassVar
import threading
class MonProcess():
    _EOF: ClassVar[Optional[bytes]] = None
    _INTR: ClassVar[Optional[bytes]] = None
    @staticmethod
    def _make_eof_intr() -> None:
        """Set constants _EOF and _INTR.
        
        This avoids doing potentially costly operations on module load.
        """
        if (MonProcess._EOF is not None) and (MonProcess._INTR is not None):
            return

        # inherit EOF and INTR definitions from controlling process.
        try:
            from termios import VEOF, VINTR
            fd = None
            for name in 'stdin', 'stdout':
                stream = getattr(sys, '__%s__' % name, None)
                if stream is None or not hasattr(stream, 'fileno'):
                    continue
                try:
                    fd = stream.fileno()
                except ValueError:
                    continue
            if fd is None:
                # no fd, raise ValueError to fallback on CEOF, CINTR
                raise ValueError("No stream has a fileno")
            intr = ord(termios.tcgetattr(fd)[6][VINTR])
            eof = ord(termios.tcgetattr(fd)[6][VEOF])
        except (ImportError, OSError, IOError, ValueError, termios.error):
            # unless the controlling process is also not a terminal,
            # such as cron(1), or when stdin and stdout are both closed.
            # Fall-back to using CEOF and CINTR. There
            try:
                from termios import CEOF, CINTR
                (intr, eof) = (CINTR, CEOF)
            except ImportError:
                #                         ^C, ^D
                (intr, eof) = (3, 4)
        
        print(intr, eof)
        MonProcess._INTR = bytes([intr])
        MonProcess._EOF = bytes([eof])


    @staticmethod
    def run_subprocess_proc_lxr_homebrew(cmd: list[str], proc: MonitorType) -> int:
        master_pty, slave_pty = os.openpty()
        BATCH_READ_BYTES = 1024

        stdin: int = sys.stdin.fileno()
        stdout: int = sys.stdout.fileno()
        stderr: int = sys.stderr.fileno()
        # screen = pyte.Screen(203, 24)
        # stream = pyte.ByteStream(screen)
        old_settings = {}
        for fd in [sys.stdin]:
            old_settings[fd] = termios.tcgetattr(sys.stdin)
            tty.setraw(fd)
        try:
            my_env = os.environ.copy()
            my_env["LINES"] = "24"
            my_env["COLUMNS"] = "203"
            # my_env["TERM"] = "linux"

            process = subprocess.Popen(
                args=cmd,
                stdin=slave_pty,
                stdout=slave_pty,
                stderr=slave_pty,
                close_fds=True,
                start_new_session=True,
                env=my_env
            )

            retcode: Optional[int] = None

            if MonProcess._EOF is None:
                MonProcess._make_eof_intr()
                assert MonProcess._EOF is not None
            while True:
                # := operator must be surrounded by parentheses otherwise the value of retcode will be True/False
                if (retcode := process.poll()) is not None:
                    break
                r, _, _ = select.select([stdin, master_pty], [], [], 0.2)
                master_stdout_buf = None
                if stdin in r:
                    stdin_buf = os.read(stdin, BATCH_READ_BYTES)
                    if len(stdin_buf) == 0:
                        os.write(master_pty, MonProcess._EOF)
                    os.write(master_pty, stdin_buf)
                if master_pty in r:
                    master_stdout_buf = os.read(master_pty, BATCH_READ_BYTES)
                    os.write(stdout, master_stdout_buf)
                proc(master_stdout_buf)
        except KeyboardInterrupt:
                process.send_signal(signal.SIGINT)
        finally:
            if retcode is None:
                retcode = -1
            for fd in [sys.stdin]:
                termios.tcsetattr(fd, termios.TCSANOW, old_settings[fd])
            os.close(master_pty)
            os.close(slave_pty)
        return retcode

    @staticmethod
    def run_subprocess_proc(cmd: list[str], proc: MonitorType) -> tuple[pid_t, Poller]:
        def pty_out_read(fd: int) -> bytes:
            buffer = os.read(fd, 1024)
            proc(buffer)
            return buffer

        env = os.environ.copy()
        env["COLUMNS"] = "160"
        pid, poller = my_pty_spawn(cmd, pty_out_read, env=env)
        assert isinstance(pid, pid_t)
        return pid, poller


    @staticmethod
    def run_subprocess(cmd: list[str], ptyout_queue: Queue[bytes]) -> tuple[pid_t, Poller]:
        def proc(ptyout: Optional[bytes]) -> None:
            if ptyout is not None:
                # cut ptyout by b'\r\n'
                ptyout_queue.put(ptyout)
        return MonProcess.run_subprocess_proc(cmd, proc)
    
    @staticmethod
    def run(cmd: list[str], proc: MonitorType | None) -> MonProcessType:
        if proc:
            pid, poller = MonProcess.run_subprocess_proc(cmd, proc)
            return None, pid, poller
        else:
            ptyout_queue: Queue[bytes] = Queue()
            pid, poller = MonProcess.run_subprocess(cmd, ptyout_queue)
            return ptyout_queue, pid, poller

    @staticmethod
    def zsh(cmd: str, proc: MonitorType | None) -> MonProcessType:
        zsh_wrap = ["zsh", "-c"]
        zsh_wrap.append(cmd)
        return MonProcess.run(zsh_wrap, proc)
    
    @staticmethod
    def pwsh(cmd: str, proc: MonitorType | None) -> MonProcessType:
        pwsh_wrap = ["pwsh", "-NoProfile", "-Nologo", "-Command"]
        pwsh_wrap.append(cmd)
        return MonProcess.run(pwsh_wrap, proc)

import subprocess
import threading
from time import monotonic as _time
import time

# _PidSubprocess is the code copied from Subprocess
class _PidSubprocess():
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None
        self._waitpid_lock = threading.Lock()
        

    def _handle_exitstatus(self, sts: int) -> None:
        """All callers to this function MUST hold self._waitpid_lock."""
        # This method is called (indirectly) by __del__, so it cannot
        # refer to anything outside of its local scope.
        if os.WIFSTOPPED(sts):
            self.returncode = -os.WSTOPSIG(sts)
        else:
            self.returncode = os.waitstatus_to_exitcode(sts)

    def _try_wait(self, wait_flags: int) -> tuple[int, int]:
        """All callers to this function MUST hold self._waitpid_lock."""
        try:
            (pid, sts) = os.waitpid(self.pid, wait_flags)
        except ChildProcessError:
            # This happens if SIGCLD is set to be ignored or waiting
            # for child processes has otherwise been disabled for our
            # process.  This child is dead, we can't get the status.
            pid = self.pid
            sts = 0
        return (pid, sts)



    def _remaining_time(self, endtime: Optional[float]) -> Optional[float]:
        """Convenience for _communicate when computing timeouts."""
        if endtime is None:
            return None
        else:
            return endtime - _time()


    def _wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Internal implementation of wait() on POSIX."""
        if self.returncode is not None:
            return self.returncode

        if timeout is not None:
            endtime = _time() + timeout
            # Enter a busy loop if we have a timeout.  This busy loop was
            # cribbed from Lib/threading.py in Thread.wait() at r71065.
            delay = 0.0005 # 500 us -> initial delay of 1 ms
            while True:
                if self._waitpid_lock.acquire(False):
                    if self.returncode is not None:
                        break  # Another thread waited.
                    try_wait_success = False
                    try:
                        (pid, sts) = self._try_wait(os.WNOHANG)
                        try_wait_success = True
                    finally:
                        self._waitpid_lock.release()
                    if try_wait_success:
                        assert pid == self.pid or pid == 0
                        if pid == self.pid:
                            self._handle_exitstatus(sts)
                            break
                remaining = self._remaining_time(endtime)
                assert remaining is not None
                if remaining <= 0:
                    return None
                delay = min(delay * 2, remaining, .05)
                time.sleep(delay)
        else:
            while self.returncode is None:
                if self.returncode is not None:
                    break  # Another thread waited.
                with self._waitpid_lock:
                    (pid, sts) = self._try_wait(0)
                    # Check the pid and loop as waitpid has been known to
                    # return 0 even without WNOHANG in odd situations.
                    # http://bugs.python.org/issue14396.
                    if pid == self.pid:
                        self._handle_exitstatus(sts)
        return self.returncode
        

class PidSubprocess(_PidSubprocess):
    def __init__(self, pid: int, poller: Poller) -> None:
        super().__init__(pid)
        self.poller = poller

    def try_wait(self) -> Optional[int]:
        if self.returncode:
            return self.returncode
        (pid, sts) = self._try_wait(os.WNOHANG)
        assert pid == self.pid or pid == 0
        if pid == self.pid:
            self._handle_exitstatus(sts)
            # print(f"change self.return code to {self.returncode}")
            assert self.returncode is not None
            self.poller.stop()
            return self.returncode
        else:
            return None

    def wait(self, timeout: Optional[float]) -> Optional[int]:
        ret = self._wait(timeout)
        self.poller.stop()
        return ret
    
    def kill(self, sig: int = signal.SIGKILL) -> None:
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError as e:
            print(e)
        self.poller.stop()
    
class BufferedQueue():
    def __init__(self, queue: Queue[bytes]) -> None:
        self.queue = queue
        self.buffer = b''
    def readlines(self) -> list[str]:
        ret = []
        while self.queue.qsize():
            out = self.queue.get_nowait()
            self.buffer += out
            lines = self.buffer.split(b"\r\n")
            self.buffer = lines.pop(-1)
            ret.extend([line.decode("UTF-8") + "\n" for line in lines])
        return ret
    def flush(self) -> list[str]:
        ret = self.readlines()
        ret.append(self.buffer.decode("UTF-8").replace("\r\n", "\n").replace("\r", "\n"))
        return ret
            



if __name__ == "__main__":
    logger = setup_logging()
    cmd = " ".join(sys.argv[1:])
    logger.debug(cmd)
    # spawn("/bin/bash")    
    # out, pid, poller = MonProcess.run(["/usr/bin/time", "cat", "/home/lixr/aosp_host_working_dir/adb-20230112.04.08.29.log"], None)
    out, pid, poller = MonProcess.run(["/bin/bash"], None)
    assert out is not None
    process = PidSubprocess(pid, poller)
    stream = BufferedQueue(out)
    with open("/tmp/out.txt", "w") as f:
        while True:
            ret_code = process.try_wait()
            time.sleep(1)
            if ret_code is not None:
                logger.debug(f"retcode: {ret_code}")
                f.writelines(stream.flush())
                break
            f.writelines(stream.readlines())
    print("out: ")
    with open("/tmp/out.txt", "r") as f:
        print(f.read())