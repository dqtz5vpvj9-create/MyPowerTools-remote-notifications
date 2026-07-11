#! /usr/bin/env python3
import asyncio
import argparse
import sys, importlib
from pathlib import Path


def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]

    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError:  # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__)  # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from . check_interpreter import check_conda_interpreter
from pathlib import Path
import os,sys
import socket


if __name__ == "__main__":
    env_name = "android_automatic"
    check_conda_interpreter(env_name)
    parser = argparse.ArgumentParser(description='Listen to a unix domain socket and ')
    parser.add_argument('-s', '--socket_file', dest="socket_file", type=str, help='The socket file to be written to', required=True)
    parser.add_argument('-d', '--data', dest="data", type=str, help='The data to send', required=True)
    args = parser.parse_args()
    socket_file = args.socket_file
    # Check path is a valid file name

    data: str = args.data
    # Create a socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Bind the socket to a file system path
    server_address = os.path.join("/tmp", socket_file)
    # Remove the file if it already exists
    try:
        os.remove(server_address)
    except OSError:
        pass
    sock.bind(server_address)

    # Listen for incoming connections
    sock.listen(1)

    # Wait for a client to connect
    connection, client_address = sock.accept()

    try:
        # Send data to the client
        connection.sendall(data.encode("UTF-8"))
    finally:
        # Clean up the connection
        try:
            os.remove(server_address)
        except OSError:
            pass
        connection.close()
        sock.close()