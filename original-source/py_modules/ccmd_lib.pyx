import os, sys
import subprocess
import select
cimport libc.stdio as stdio
cimport posix.select as select
cimport posix.unistd as unistd
from posix.select cimport fd_set
from posix.types cimport pid_t
from posix.wait cimport waitpid, WNOHANG, WIFSTOPPED, WSTOPSIG, WEXITSTATUS

cdef int run_subprocess(cmd: str):
    cdef int master_fds[3]
    cdef int slave_fds[3]
    for i in range(3):
        master_fds[i], slave_fds[i] = os.openpty()

    cdef int BATCH_READ_BYTES = 1024

    cdef int master_stdin = master_fds[0]
    cdef int slave_stdin = slave_fds[0]
    cdef int master_stdout = master_fds[1]
    cdef int slave_stdout = slave_fds[1]
    cdef int master_stderr = master_fds[2]
    cdef int slave_stderr = slave_fds[2]

    cdef int stdin = sys.stdin.fileno()
    cdef int stdout = sys.stdout.fileno()
    cdef int stderr = sys.stderr.fileno()

    cdef int retcode
    cdef char buffer[1024]

    process = subprocess.Popen(
        args=cmd,
        stdin=slave_stdin,
        stdout=slave_stdout,
        stderr=slave_stdin,
        close_fds=True,
        start_new_session=True,
    )
    
    cdef fd_set read_set
    select.FD_ZERO(&read_set)
    select.FD_SET(stdin, &read_set)
    select.FD_SET(master_stdout, &read_set)
    select.FD_SET(master_stderr, &read_set)
    cdef pid_t pid = process.pid
    cdef int status
    cdef int waited_pid 
    while True:
        waited_pid = waitpid(pid, &status, WNOHANG)
        if waited_pid == 0:
            pass
        elif waited_pid == pid:
            if WIFSTOPPED(status):
                retcode = -WSTOPSIG(status)
            else:
                retcode = WEXITSTATUS(status)
            break
        else:
            raise Exception
#         retcode = process.poll()
#         if retcode is not None:
#             break
        
        select.select(master_stderr+1, &read_set, NULL, NULL, NULL)

        if select.FD_ISSET(stdin, &read_set):
            unistd.read(stdin, buffer, BATCH_READ_BYTES)
            unistd.write(master_stdin, buffer, BATCH_READ_BYTES)
        if select.FD_ISSET(master_stdout, &read_set):
            unistd.read(master_stdout, buffer, BATCH_READ_BYTES)
            unistd.write(stdout, buffer, BATCH_READ_BYTES)
        if select.FD_ISSET(master_stderr, &read_set):
            unistd.read(master_stderr, buffer, BATCH_READ_BYTES)
            unistd.write(stderr, buffer, BATCH_READ_BYTES)
            
    for i in range(3):
        os.close(master_fds[i])
        os.close(slave_fds[i])
    return retcode