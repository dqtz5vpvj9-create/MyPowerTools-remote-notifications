import importlib, sys, os
from os.path import dirname, pardir
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
sys.path.append(dirname(__file__) + os.sep + pardir)
from py_modules.logging_lib import setup_logging
logger = setup_logging()
from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
if __name__ == '__main__':
    check_conda_interpreter(CONDA_ENV_NAME)
from py_modules.lib_aosp_base import *
from py_modules.lib_aosp_testing import *
from py_modules.simple_http_notification_conf import redis_port_offset
assert LIB_AOSP_BASE_INITED
import git

def get_git_root(path: str) -> str:
        git_repo = git.Repo(path, search_parent_directories=True)
        git_root = git_repo.git.rev_parse("--show-toplevel")
        return str(git_root)

import os
import subprocess
import pwd
def get_uid(username) -> Optional[int]:
    """Return the UID of the specified username."""
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        return None

def get_gid(username) -> Optional[int]:
    """Return the GID of the specified username."""
    try:
        return pwd.getpwnam(username).pw_gid
    except KeyError:
        return None


def write_redis_conf(port: int) -> None:
    """Write a Redis configuration file for the specified port number."""
    username = "redis"
    redis_uid = get_uid(username)
    redi_gid = get_gid(username)
    assert(redis_uid is not None)
    assert(redi_gid is not None)
    conf_file = "/etc/redis/redis_instance_{}.conf".format(port)
    with open(conf_file, "w") as f:
        f.write("port {}\n".format(port))
    os.chown(conf_file, redis_uid, redi_gid)

def write_systemd_unit(port: int) -> None:
    """Write a systemd unit file for the specified port number."""
    unit_file = "/etc/systemd/system/redis_instance@{}.service".format(port)
    with open(unit_file, "w") as f:
        f.write("""[Unit]
Description=Redis Instance {}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/redis-server /etc/redis/redis_instance_{}.conf --supervised systemd --daemonize no
User=redis
Group=redis
PIDFile=/run/redis_instance_{}.pid
Restart=always
TimeoutStopSec=0
User=redis
Group=redis

[Install]
WantedBy=multi-user.target
""".format(port, port, port))

def journalctl_last_start(unit: str) -> None:
    # Get InvocationID from journalctl
    invID_b: bytes = subprocess.check_output(args=["systemctl", "show", "--value", "-p", "InvocationID", unit])
    invID: str = invID_b.decode("utf-8").strip()
    # Check invID is 128bit
    assert(len(invID) == 32)
    subprocess.run(f"journalctl _SYSTEMD_INVOCATION_ID={invID}", shell=True)


def start_redis_instance(port: int) -> None:
    """Start the Redis instance for the specified port number."""
    subprocess.run(f"systemctl daemon-reload", shell=True, check=True)
    subprocess.run(["systemctl", "enable", "redis_instance@{}.service".format(port)], check=True)
    subprocess.run(["systemctl", "start", "redis_instance@{}.service".format(port)], check=True)
    journalctl_last_start("redis_instance@{}.service".format(port))


if __name__ == "__main__":
    git_root = get_git_root(__file__)
    users = {"lxr": 8888, "yzy": 8891}
    for user, port in users.items():
        redis_port = port + redis_port_offset
        write_redis_conf(redis_port)
        write_systemd_unit(redis_port)
        start_redis_instance(redis_port)
        repo_dir = os.path.join(os.path.dirname(git_root), f"androidtools_{user}")
        print(repo_dir)
        if not os.path.exists(repo_dir):
            subprocess.run(f"git clone https://ipads.se.sjtu.edu.cn:1312/mobile_runtime/androidtools.git {repo_dir}", shell=True)
        else:
            subprocess.run(f"git -C {repo_dir} pull", shell=True)
        with open(os.path.join(repo_dir, "py_modules", "simple_http_notification_conf_user.yaml"), "w") as f:
            f.write(f"cloud_server_port: {port}")
        # modify .\py_modules\simple_http_notification_server.service
        with open(os.path.join(repo_dir, "py_modules", "simple_http_notification_server.service"), "r") as f:
            # replace {user} with {user}
            content = f.read().replace("{user}", user)
            # save to /etc/systemd/system/simple_http_notification_server_{user}.service
            with open(f"/etc/systemd/system/simple_http_notification_server_{user}.service", "w") as f:
                f.write(content)
        subprocess.run(f"systemctl daemon-reload", shell=True)
        subprocess.run(f"systemctl restart simple_http_notification_server_{user}.service", shell=True)
        journalctl_last_start(f"simple_http_notification_server_{user}.service")

        
            
