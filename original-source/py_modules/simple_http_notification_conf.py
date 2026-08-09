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

import jsonschema

cloud_server_ip: str = "message.lixinrui000.cn"
cloud_server_port: int = 8888
cloud_server_protocol: str = "https"
redis_port_offset: int = 20000

import yaml
import os
user_conf_file = os.path.join(os.path.dirname(__file__), "simple_http_notification_conf_user.yaml")
if os.path.exists(user_conf_file):
    with open(user_conf_file, "r") as f:
        config = yaml.safe_load(f)
        for k, value in config.items():
            if globals()[k]:
                assert(isinstance(value, type(globals()[k])))
            globals()[k] = value
            print(f"Loaded {k} <- {value} from {user_conf_file}")

add_schema = {
    "type": "object",
    "required": ["message", "channel"],
    "properties": {
        "message": {"type": "string"},
        "channel": {"type": "string"},
        "icon": {"type": "string"},
        "id": {"type": "string"},
        "client_msg_id": {"type": "string"},
        "timestamp": {"type": "string"},
        "schema_version": {"type": "integer", "minimum": 1},
        "session_id": {"type": "string", "maxLength": 128},
        "session_name": {"type": "string", "maxLength": 512},
        "source_client": {"type": "string", "maxLength": 64}
    }
}

result_schema = {
    "type": "object",
    "required": ["message"],
    "properties": {
        "message": {"type": "string"},
        "icon": {"type": "string"},
        "id": {"type": "string"},
        "channel": {"type": "string"},
        "timestamp": {"type": "string"},
        "server_timestamp": {"type": "string"},
        "schema_version": {"type": "integer"},
        "session_id": {"type": "string"},
        "session_name": {"type": "string"},
        "source_client": {"type": "string"}
    }
}

VALID_ICONS = ["info", "warning", "error", "success"]
