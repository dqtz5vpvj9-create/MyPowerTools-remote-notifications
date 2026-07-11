
from libtmux.server import Server
from libtmux.session import Session
from libtmux.session import Window
from libtmux.session import Pane
import logging
import time
import hashlib
from typing import Callable
import pytest
import socket,os
from py_modules.lib_aosp_testing import *
from datetime import datetime as datetime_class

def simple_unix_domain_socket_client(socket_file: str) -> str | None:
    # Create a socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    # Connect to the server
    server_address = os.path.join("/tmp", socket_file)
    connected = False
    while not connected:
        try:
            sock.connect(server_address)
            time.sleep(1)
            connected = True
        except Exception as e:
            pass
    ret = None
    try:
        # Receive data from the server
        data = sock.recv(1024)
        ret = data.decode("UTF-8")
    finally:
        # Close the socket
        sock.close()
        return ret

import sys
from functional import seq
class PaneOperation:
    fast_exec = False
    def __init__(self, pane: Pane, logger: MyLogger):
        self.pane = pane
        self.start_prompt = "Ŝĥėļļ"
        self.logger = logger
    
    def wait_output(self, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, predict: Callable[[list[str]], bool] | None= None, exact: bool = False) -> str | None:
        while True:
            ret = self.search_output(wait_output, output_limit, bound, predict, exact)
            if ret["found"]:
                found_what = ret["what"]
                assert(isinstance(found_what, str))
                return found_what
            if ret["break"]:
                return None
            time.sleep(0.05)
    

    def search_output(self, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, predict: Callable[[list[str]], bool] | None= None, exact: bool = False) -> dict[str, bool | str]:
        # Get console output and convert to list 
        output: str | list[str] = self.pane.capture_pane(start=-1000)
        output_list: list[str]
        if isinstance(output, str) and len(output) > 0:
            output_list = [output]
        elif isinstance(output, list) and len(output) > 0 and isinstance(output[0], str):
            output_list = output
        else:
            return {"found": False, "break": False}
        
        if predict and predict(output_list):
            # If user defined predict function, use it
            return {"found": True, "break": False}
        else:
            # otherwise, search for <wait_output> in the output
            if wait_output is None:
                raise AssertionError("You must provide wait_output or predict")
            if isinstance(wait_output, str):
                wait_output = [wait_output]
            # only search in the last <output_limit> lines
            if output_limit:
                if output_limit <= 0:
                    raise AssertionError("output_limit must be none negative")
                output_list = output_list[-output_limit:]
            # only search until the last "start prompt"
            if not bound:
                bound = self.start_prompt
            shell_prompt_exists = seq(output_list).zip_with_index().filter(lambda t: bound in t[0])
            if shell_prompt_exists.len() > 0:
                last_shell_prompt_index = shell_prompt_exists[-1][1]
                output_list = output_list[last_shell_prompt_index:]
            # do the search
            found = False
            found_what = None
            for _wait_output in wait_output:
                if exact:
                    if any([_wait_output == output_line for output_line in output_list]):
                        found = True
                        found_what = _wait_output
                        break
                else:
                    if any([_wait_output in output_line for output_line in output_list]):
                        found = True
                        found_what = _wait_output
                        break
            if found:
                assert found_what is not None
                self.logger.debug("Finished waiting for {cmd}: because we find " + found_what)
                return {"found": True, "break": False, "what": found_what}
            return {"found": False, "break": False}
    
    def texec(self, cmd: str, detach: bool = True, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, exact: bool = False) -> str | None:
        # Generate a task name by hashing the command
        task = hashlib.sha1(cmd.encode()).hexdigest()
        # append hash with datetime
        task = task + "_" + datetime_class.now().strftime("%m%d_%H%M_%S")
        if detach:
            wcmd = cmd
            ret = None
        else:
            wcmd = f"echo {self.start_prompt}; {cmd}; {sys.executable} {androidtools}/py_modules/simple_unix_domain_socket_server.py -s {task} -d $?"
            ret = task
        if not PaneOperation.fast_exec:
            self.pane.send_keys("C-c", enter=False)
            time.sleep(1)
        self.pane.send_keys(wcmd)
        if wait_output:
            self.wait_output(wait_output, output_limit, bound=bound, exact=exact)
        return ret

    def tjoin(self, task: str) -> str | None:
        return simple_unix_domain_socket_client(task)

    def texec_and_join(self, cmd: str) -> str | None:
        task = self.texec(cmd, detach=False)
        if task is None:
            raise Exception("Can not join a detached task")
        self.logger.debug(f"Waiting for task: \x1b[92m {cmd} \x1b[0m")
        ret = self.tjoin(task)
        if ret and ((isinstance(ret, str) and ret == "0") or (isinstance(ret, int) and ret == 0)):
            self.logger.debug(f"Task finished: \x1b[92m {cmd} with ret {ret}\x1b[0m")
        else:
            self.logger.debug(f"Task failed: \x1b[91m {cmd} with ret {ret}\x1b[0m")
        return ret
    
    # Execute using powershell
    def pwexec(self, cmd: str, detach: bool = True, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, exact: bool = False) -> str | None:
        try:
            self.pane.refresh()
        except:
            pass
        if self.pane.pane_current_command != "pwsh":
            self.texec("pwsh", wait_output="❯", output_limit=1, bound="PowerShell", exact=True)
        if not PaneOperation.fast_exec:
            time.sleep(3)
        return self.texec(cmd, detach, wait_output, output_limit, bound, exact)

class SessionOperation:
    def __init__(self, session: Session, logger: MyLogger) -> None:
        self.session = session
        self.logger = logger
    def texec_and_join(self, window: str, pane_id: int, cmd: str) -> str | None:
        wd = self.session.select_window(window)
        pane_ops = PaneOperation(wd.panes[pane_id], self.logger)
        return pane_ops.texec_and_join(cmd)
    def texec(self, window: str, pane_id: int, cmd: str, detach: bool = True, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, exact: bool = False) -> str | None:
        wd = self.session.select_window(window)
        pane_ops = PaneOperation(wd.panes[pane_id], self.logger)
        return pane_ops.texec(cmd, detach, wait_output, output_limit, bound, exact)
    def tjoin(self, window: str, pane_id: int, task: str) -> str | None:
        wd = self.session.select_window(window)
        pane_ops = PaneOperation(wd.panes[pane_id], self.logger)
        return pane_ops.tjoin(task)
    def pwexec(self, window: str, pane_id: int, cmd: str, detach: bool = True, wait_output: str | list[str] | None = None, output_limit: int | None = None, bound: str | None = None, exact: bool = False) -> str | None:
        wd = self.session.select_window(window)
        pane_ops = PaneOperation(wd.panes[pane_id], self.logger)
        return pane_ops.pwexec(cmd, detach, wait_output, output_limit, bound, exact)

import unittest
# class WaitOutputTest(unittest.TestCase):
#     def test_match1(self):
#         pass




if __name__ == "__main__":
    unittest.main(argv=['ignored', '-v'], exit=False)

