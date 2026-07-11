import os
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

from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
from py_modules.logging_lib import setup_logging
from py_modules.simple_http_notification_conf import add_schema, result_schema, cloud_server_port, cloud_server_ip
if __name__ == '__main__':
    check_conda_interpreter(CONDA_ENV_NAME)


bind = "0.0.0.0:{}".format(cloud_server_port)
certfile = os.environ['SSL_CERT_FILE']
keyfile = os.environ['SSL_KEY_FILE']
loglevel = 'info'
workers = 1
threads = 1
worker_class = 'gevent'