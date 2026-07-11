from check_interpreter import check_conda_interpreter
if __name__ == '__main__':
    check_conda_interpreter("android_automatic")
import queue

from flask import Flask, Response, jsonify, request
from typing import Any
import jsonschema
from simple_http_notification_conf import schema, cloud_server_port
from datetime import datetime, timedelta

app = Flask(__name__)
notifications: queue.Queue[tuple[datetime, str]] = queue.Queue()


def add_notification(message: str) -> None:
    now = datetime_class.now()
    notifications.put((now, message))


def get_notification() -> str | None:
    while notifications.qsize() > 0:
        latest_date, msg = notifications.get()
        if latest_date - datetime_class.now() > timedelta(seconds=10):
            continue
        return msg
    return None


@app.route('/clear', methods=['GET'])
def clear_notifications() -> tuple[Response, int]:
    while notifications.qsize() > 0:
        notifications.get()
    return jsonify({"status": "ok"}), 200


@app.route('/notification', methods=['POST'])
def receive_notification() -> tuple[Response, int]:
    data: Any | None = request.get_json()
    if data:
        try:
            jsonschema.validate(data, schema)
            add_notification(data["message"])
        except jsonschema.exceptions.ValidationError as e:
            return jsonify({"error": e.message}), 400
    return jsonify({"status": "ok"}), 200


@app.route('/notification', methods=['GET'])
def get_notifications() -> tuple[Response, int]:
    # Get the first message in the queue
    notification = get_notification()
    if notification:

        ret = {"message": notification}
        jsonschema.validate(ret, schema)
        return jsonify(ret), 200
    else:
        return jsonify({"error": "No notification"}), 204


import sys, subprocess
if __name__ == '__main__':
    check_conda_interpreter("android_automatic")
    cmd = sys.argv[1:]
    process = subprocess.Popen(cmd, shell=True)
    app.run(host="unix:///tmp/app.sock")
