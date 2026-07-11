"""Pseudo terminal utilities."""

# Bugs: No signal handling.  Doesn't set slave termios and window size.
#       Only tested on Linux, FreeBSD, and macOS.
# See:  W. Richard Stevens. 1992.  Advanced Programming in the
#       UNIX Environment.  Chapter 19.
# Author: Steen Lumholt -- with additions by Guido.

from select import select
import os
import sys
import tty

# names imported directly for test mocking purposes
from os import close, waitpid
from tty import setraw
import termios
from termios import tcgetattr, tcsetattr

__all__ = ["openpty", "fork", "spawn"]

STDIN_FILENO = 0
STDOUT_FILENO = 1
STDERR_FILENO = 2

CHILD = 0

def openpty() -> tuple[int, int]:
    """openpty() -> (master_fd, slave_fd)
    Open a pty master/slave pair, using os.openpty() if possible."""

    try:
        return os.openpty()
    except (AttributeError, OSError):
        pass
    master_fd, slave_name = _open_terminal()
    slave_fd = slave_open(slave_name)
    return master_fd, slave_fd

# def master_open():
#     """master_open() -> (master_fd, slave_name)
#     Open a pty master and return the fd, and the filename of the slave end.
#     Deprecated, use openpty() instead."""

#     try:
#         master_fd, slave_fd = os.openpty()
#     except (AttributeError, OSError):
#         pass
#     else:
#         slave_name = os.ttyname(slave_fd)
#         os.close(slave_fd)
#         return master_fd, slave_name

#     return _open_terminal()

def _open_terminal() -> tuple[int, str]:
    """Open pty master and return (master_fd, tty_name)."""
    for x in 'pqrstuvwxyzPQRST':
        for y in '0123456789abcdef':
            pty_name = '/dev/pty' + x + y
            try:
                fd = os.open(pty_name, os.O_RDWR)
            except OSError:
                continue
            return (fd, '/dev/tty' + x + y)
    raise OSError('out of pty devices')

def slave_open(tty_name: str) -> int:
    """slave_open(tty_name) -> slave_fd
    Open the pty slave and acquire the controlling terminal, returning
    opened filedescriptor.
    Deprecated, use openpty() instead."""

    result = os.open(tty_name, os.O_RDWR)
    try:
        from fcntl import ioctl, I_PUSH
    except ImportError:
        return result
    try:
        ioctl(result, I_PUSH, "ptem".encode("ascii"))
        ioctl(result, I_PUSH, "ldterm".encode("ascii"))
    except OSError:
        pass
    return result

def fork() -> tuple[int, int]:
    """fork() -> (pid, master_fd)
    Fork and make the child a session leader with a controlling terminal."""

    try:
        pid, fd = os.forkpty()
    except (AttributeError, OSError):
        pass
    else:
        if pid == CHILD:
            try:
                os.setsid()
            except OSError:
                # os.forkpty() already set us session leader
                pass
        return pid, fd

    master_fd, slave_fd = openpty()
    pid = os.fork()
    if pid == CHILD:
        # Establish a new session.
        os.setsid()
        os.close(master_fd)

        # Slave becomes stdin/stdout/stderr of child.
        os.dup2(slave_fd, STDIN_FILENO)
        os.dup2(slave_fd, STDOUT_FILENO)
        os.dup2(slave_fd, STDERR_FILENO)
        if slave_fd > STDERR_FILENO:
            os.close(slave_fd)

        # Explicitly open the tty to make it become a controlling tty.
        tmp_fd = os.open(os.ttyname(STDOUT_FILENO), os.O_RDWR)
        os.close(tmp_fd)
    else:
        os.close(slave_fd)

    # Parent and child process.
    return pid, master_fd

def _writen(fd: int, data: bytes) -> None:
    """Write all the data to a descriptor."""
    while data:
        n = os.write(fd, data)
        data = data[n:]

def _read(fd: int) -> bytes:
    """Default read function."""
    return os.read(fd, 1024)

from typing import Callable
ReadFuncType = Callable[[int], bytes]

from typing import Callable, Any
def _copy(master_fd: int, master_read: ReadFuncType =_read, stdin_read: ReadFuncType =_read, flag: bool = True) -> None:
    """Parent copy loop.
    Copies
            pty master -> standard output   (master_read)
            standard input -> pty master    (stdin_read)"""
    fds = [master_fd, STDIN_FILENO]
    while fds and flag:
        rfds, _wfds, _xfds = select(fds, [], [])

        if master_fd in rfds:
            # Some OSes signal EOF by returning an empty byte string,
            # some throw OSErrors.
            try:
                data = master_read(master_fd)
            except OSError:
                data = b""
            if not data:  # Reached EOF.
                return    # Assume the child process has exited and is
                          # unreachable, so we clean up.
            else:
                os.write(STDOUT_FILENO, data)

        if STDIN_FILENO in rfds:
            data = stdin_read(STDIN_FILENO)
            if not data:
                fds.remove(STDIN_FILENO)
            else:
                _writen(master_fd, data)

import threading
import tty
import termios
class Poller:
    def __init__(self, master_fd: int, master_read: ReadFuncType, stdin_read: ReadFuncType, restore: bool, mode: list[Any]) -> None:
        self.master_fd = master_fd
        self.master_read = master_read
        self.stdin_read = stdin_read
        self.restore = restore
        self.mode = mode
        self.flag = True
        self.poll_thread = threading.Thread(target=_copy, args=(self.master_fd, self.master_read, self.stdin_read, self.flag))
        self.poll_thread.start()

    def stop(self) -> None:
        self.flag = False
        self.poll_thread.join(timeout=1)
        if self.restore:
            tcsetattr(STDIN_FILENO, termios.TCSAFLUSH, self.mode)
        try:
            close(self.master_fd)
        except OSError as e:
            print(e)

        
# Modify 1
def spawn(_argv: str | list[str], master_read:ReadFuncType =_read, stdin_read:ReadFuncType =_read, env:dict[str, str] | None =None) -> tuple[int, Poller]:
    """Create a spawned process."""
    if isinstance(_argv, str):
        argv = [_argv,]
    else:
        argv = _argv
    sys.audit('pty.spawn', argv)

    if env:
        bytes_env = {}
        for k, v in env.items():
            bytes_env[k.encode('ascii')] = v
    else:
        bytes_env = None

    pid, master_fd = fork()
    if pid == CHILD:
        if bytes_env:
            os.execvpe(argv[0], argv, bytes_env)
        else:
            os.execvp(argv[0], argv)

    try:
        mode = tcgetattr(STDIN_FILENO)
        setraw(STDIN_FILENO)
        restore = True
    except termios.error:    # This is the same as termios.error
        restore = False

    # try:
    #     _copy(master_fd, master_read, stdin_read)
    # finally:
    #     if restore:
    #         tcsetattr(STDIN_FILENO, tty.TCSAFLUSH, mode)
    poller = Poller(master_fd, master_read, stdin_read, restore, mode)

    # close(master_fd)
    # Modify 2
    return pid, poller
