class Colors:
    """ ANSI color codes """
    BLACK = "\033[0;30m"
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    BROWN = "\033[0;33m"
    BLUE = "\033[0;34m"
    PURPLE = "\033[0;35m"
    CYAN = "\033[0;36m"
    LIGHT_GRAY = "\033[0;37m"
    DARK_GRAY = "\033[1;30m"
    LIGHT_RED = "\033[1;31m"
    LIGHT_GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    LIGHT_BLUE = "\033[1;34m"
    LIGHT_PURPLE = "\033[1;35m"
    LIGHT_CYAN = "\033[1;36m"
    LIGHT_WHITE = "\033[1;37m"
    BOLD = "\033[1m"
    FAINT = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    BLINK = "\033[5m"
    NEGATIVE = "\033[7m"
    CROSSED = "\033[9m"
    END = "\033[0m"

import logging
import os
import sys

import inspect
from pathlib import Path


# Create an enum of log levels
from enum import Enum
from typing import Optional, TYPE_CHECKING, cast
from datetime import datetime as datetime_class
class LogLevel(Enum):
    VERBOSE = 5
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    NOTICE = 25
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL
    NONE = 0

logging.addLevelName(LogLevel.VERBOSE.value, "VERBOSE")
logging.addLevelName(LogLevel.NOTICE.value, "NOTICE")
class MyLogger(logging.Logger):
    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, level)
    if TYPE_CHECKING:
        verbose = logging.Logger.info
        notice = logging.Logger.info
    else:
        def verbose(self, message, *args, **kws):
            if self.isEnabledFor(LogLevel.VERBOSE.value):
                # Yes, logger takes its '*args' as 'args'.
                self._log(LogLevel.VERBOSE.value, message, args, **kws, stacklevel=2)
        
        def notice(self, message, *args, **kws):
            if self.isEnabledFor(LogLevel.NOTICE.value):
                # Yes, logger takes its '*args' as 'args'.
                self._log(LogLevel.NOTICE.value, message, args, **kws, stacklevel=2) 
    

# logging.Logger.notice = notice
class CustomFormatter(logging.Formatter):

    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    fmt = "%(asctime)s | %(name)s | %(levelname)s | %(message)s - %(filename)s:%(lineno)d"
    datefmt='%Y-%m-%d %H:%M:%S'

    FORMATS = {
        LogLevel.VERBOSE.value: Colors.DARK_GRAY + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.DARK_GRAY + " %(message)s " + Colors.DARK_GRAY + "- %(filename)s:%(lineno)d" + Colors.END,
        logging.DEBUG: Colors.DARK_GRAY + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.LIGHT_GRAY + " %(message)s " + Colors.DARK_GRAY + "- %(filename)s:%(lineno)d" + Colors.END,
        logging.INFO: Colors.LIGHT_GRAY + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.LIGHT_WHITE + " %(message)s " + Colors.LIGHT_GRAY + "- %(filename)s:%(lineno)d" + Colors.END,
        LogLevel.NOTICE.value: Colors.GREEN + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.LIGHT_GREEN + " %(message)s " + Colors.GREEN + "- %(filename)s:%(lineno)d" + Colors.END,
        logging.WARNING: Colors.BROWN + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.YELLOW + " %(message)s " + Colors.BROWN + "- %(filename)s:%(lineno)d" + Colors.END,
        logging.ERROR: Colors.RED + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.LIGHT_RED + " %(message)s " + Colors.RED + "- %(filename)s:%(lineno)d" + Colors.END,
        logging.CRITICAL: Colors.RED + "%(asctime)s | %(name)s | %(levelname)s |" + Colors.LIGHT_RED + " %(message)s " + Colors.RED + "- %(filename)s:%(lineno)d" + Colors.END,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, self.datefmt)
        return formatter.format(record)

def setup_logging(log_name: Optional[str] = None, file_level: LogLevel = LogLevel.NONE, console_level: LogLevel = LogLevel.DEBUG, log_path: str = '', simple_fmt: bool = False) -> MyLogger:

    if log_name is None:
        # Use sys._getframe() instead of inspect.stack() — the latter walks
        # ALL frames and calls getmodule() which scans sys.modules doing OS
        # stat on every entry. With hundreds of imported modules this takes
        # 100ms-1s per call and burns 100% CPU at import time.
        caller_frame = sys._getframe(1) if hasattr(sys, '_getframe') else None
        if caller_frame is None:
            log_name = __name__
        else:
            caller_path = caller_frame.f_code.co_filename
            file_path = Path(caller_path)
            file_stem = file_path.stem
            log_name = file_stem

    # Create a logger with the name of your project
    logging.setLoggerClass(MyLogger)
    logger: MyLogger = cast(MyLogger, logging.getLogger(f"{log_name}"))
    
    list(map(logger.removeHandler, logger.handlers))
    list(map(logger.removeFilter, logger.filters))
    if isinstance(console_level, str):
        if console_level.upper() in LogLevel.__members__:
            console_level = LogLevel[console_level.upper()]
    if isinstance(file_level, str):
        if file_level.upper() in LogLevel.__members__:
            file_level = LogLevel[file_level.upper()]

    # Set the logging level
    logger.setLevel(min(file_level.value, console_level.value))

    # Create a formatter
    if simple_fmt:
        format = "%(asctime)s %(levelname)s - %(message)s"
        datefmt = '%Y-%m-%d %H:%M:%S'
        simple_formatter = logging.Formatter(format, datefmt)
    else:
        format = "%(asctime)s - %(name)s - %(levelname)s - %(process)s - %(message)s"
        formatter = CustomFormatter()

    # Create a console handler to log to the console
    if console_level != LogLevel.NONE:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level.value)

        if simple_fmt:
            # Use a simpler format without colors
            console_handler.setFormatter(simple_formatter) # type: ignore
        else:
            # Use the custom color formatter
            console_handler.setFormatter(formatter) # type: ignore

        # Add the handlers to the logger
        logger.addHandler(console_handler)

    if (file_level != LogLevel.NONE):
        if log_path == '':
            # Get current date and time
            now = datetime_class.now()
            current_time = now.strftime("%Y-%m-%d_%H-%M-%S")

            # Create log file name with current date and time
            log_name = log_name + '_' + current_time + '.log'

            # Create a file handler to log to a file
            log_path = os.path.join(os.getcwd(), 'logs', log_name)
            # Create the logs directory if it doesn't exist
            if not os.path.exists(os.path.dirname(log_path)):
                os.makedirs(os.path.dirname(log_path))
        log_path = os.path.realpath(os.path.expanduser(log_path))
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(file_level.value)

        if simple_fmt:
            # Apply the simple format for the file handler as well
            file_handler.setFormatter(simple_formatter) # type: ignore
        else:
            file_handler.setFormatter(formatter) # type: ignore

        logger.addHandler(file_handler)

    logger.propagate = False

    return logger
