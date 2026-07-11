# %%

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

if __name__ == '__main__':
    check_conda_interpreter("android_automatic")
import queue
import requests_unixsocket


from flask import Flask, Response, jsonify, request
from typing import Any
import jsonschema
from simple_http_notification_conf import schema, cloud_server_port
from datetime import datetime, timedelta
def quote_for_uds(host: str) -> str:
    return "http+unix://" + host.replace('/', '%2F')

if __name__ == '__main__':
    check_conda_interpreter("android_automatic")
    session = requests_unixsocket.Session()

    host = quote_for_uds("/tmp/app.sock")
    # Access /path/to/page from /tmp/profilesvc.sock
    r = session.get(f"{host}/notification")
    print(r)
# %%
