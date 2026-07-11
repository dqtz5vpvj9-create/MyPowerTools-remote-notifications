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
from typing import Callable, Optional, Tuple
import psutil
from queue import Empty
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
from . lib_sh import *
from . logging_lib import setup_logging
import logging
import psutil

class TestSIGINT:
    def __init__(self) -> None:
        pid = int(input())
        kill_signal = signal.SIGHUP
        call_graph_option = " --call-graph fp "
        perf_cmd = f"adb -s {serial} shell " + "'" + f"cd /data/local/tmp/ && simpleperf record -c 1 -e major-faults {call_graph_option} -p {pid} -o perf-{pid}.data" + "'"
        self.stop_event = multiprocessing.Event()
        self.perf_thread = threading.Thread(target=shell_run, kwargs={"cmd": perf_cmd, "cwd": ASRCDIR, "stop_event": self.stop_event, "check_error": False})
        self.perf_thread.start()

t = TestSIGINT()
time.sleep(2)
As("killall -2 simpleperf", AsOption.STDERR_TO_STDOUT)

