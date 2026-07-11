# if __package__ is None or len(__package__) == 0:
#     from logging_lib import setup_logging
# else:
#     print("package: ", __package__)
#     from py_modules.logging_lib import setup_logging

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

from .logging_lib import setup_logging

import sys
import subprocess
import os, logging
from typing import Callable, Optional

CONDA_ENV_NAME = "android_automatic_314"

def check_interpreter(py_home: str, logger: Optional[logging.Logger] = None) -> None:
    if not logger:
        logger = setup_logging()
    expanded_path = os.path.expanduser(py_home)

    # Get the current path
    script_path = os.path.abspath(sys.argv[0])
    script_dir = os.path.dirname(script_path)
    join_f: Callable[[str], str]
    if os.name == 'nt':
        join_f = lambda p: os.path.join(p, 'python.exe')
    else:
        join_f = lambda p: os.path.join(p, 'bin', 'python')
    if os.path.isabs(expanded_path):
        desired_interpreter = os.path.realpath(join_f(expanded_path))
    else:
        desired_interpreter = os.path.realpath(
            join_f(os.path.join(script_dir, py_home)))
    current_interpreter = os.path.realpath(sys.executable)
    # logger.debug(
    #     f'Desired interpreter: {desired_interpreter}, current interpreter: {current_interpreter}')
    if current_interpreter != desired_interpreter:
        # Check the existence of the desired interpreter
        if not os.path.exists(desired_interpreter):
            # Warn the user and exit
            print(
                f'ERROR: The desired interpreter {desired_interpreter} does not exist.')
            print('Please create the virtual environment and try again.')
            sys.exit(1)
        subprocess.run([desired_interpreter, sys.argv[0]] + sys.argv[1:])
        sys.exit()

from pathlib import Path
def check_conda_interpreter(env_name: str, logger: Optional[logging.Logger] = None) -> None:
    if not logger:
        logger = setup_logging()
    # Get the path of conda
    conda_exe: str | None = os.environ.get("CONDA_EXE")
    if conda_exe is None or len (conda_exe) == 0:
        print("Conda is not installed, please install conda")
        exit(1)
    env_path = Path(conda_exe).parent.parent
    check_interpreter(str(env_path / "envs" / env_name), logger)