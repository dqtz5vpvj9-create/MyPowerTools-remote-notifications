import socket
import time
from typing import Optional

class InputClientError(Exception):
    """自定义异常类用于处理 InputClient 错误"""
    pass

class AndroidInputClient:
    def __init__(self, host='127.0.0.1', port=27797, timeout=5.0, auto_reconnect=True):
        """
        初始化 AndroidInputClient 实例。

        :param host: Daemon 所在主机地址，通常为 127.0.0.1（通过 adb forward 映射）
        :param port: Daemon 监听的端口
        :param timeout: 命令响应超时时间（秒）
        :param auto_reconnect: 是否在连接断开时自动重连
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        self.sock = None
        self.connect()

    def connect(self):
        """建立与 daemon 的 TCP 连接"""
        self.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect((self.host, self.port))
        except socket.error as e:
            self.sock = None
            raise InputClientError(f"无法连接到 {self.host}:{self.port} - {e}")

    def close(self):
        """关闭与 daemon 的连接"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def _ensure_connection(self):
        """确保与 daemon 的连接是活跃的"""
        if self.sock is None:
            if self.auto_reconnect:
                self.connect()
            else:
                raise InputClientError("没有活动连接，并且 auto_reconnect=False")

    def _send_command(self, cmd: str) -> str:
        """
        发送命令并等待响应。

        :param cmd: 要发送的命令字符串
        :return: Daemon 的响应内容
        """
        for attempt in range(2):
            try:
                self._ensure_connection()
                self.sock.settimeout(self.timeout)
                self.sock.sendall((cmd + '\n').encode('utf-8'))
                start_time = time.time()
                data = b''
                # 简单行读取，直到遇到换行符
                while True:
                    if time.time() - start_time > self.timeout:
                        raise InputClientError("等待响应超时")
                    try:
                        chunk = self.sock.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                        if b'\n' in data:
                            break
                    except socket.timeout:
                        raise InputClientError("等待响应超时")
                resp = data.decode('utf-8', 'replace').strip()
                return resp
            except (socket.error, InputClientError) as e:
                if self.auto_reconnect and attempt == 0:
                    # 尝试重连一次
                    self.connect()
                    continue
                else:
                    raise InputClientError(f"发送命令 '{cmd}' 失败: {e}")
        # 理论上不会执行到这里
        raise InputClientError("发送命令时发生未知错误")

    def send_command(self, cmd: str) -> str:
        """
        发送原始命令字符串到 daemon。

        :param cmd: 要发送的命令字符串
        :return: Daemon 的响应内容
        """
        resp = self._send_command(cmd)
        if resp.startswith("ERROR:"):
            raise InputClientError(resp)
        print(f"发送命令 '{cmd}'，响应: {resp}")
        return resp

    def send_commands(self, cmds: list) -> list:
        """
        发送多条命令，并返回所有响应。

        :param cmds: 要发送的命令字符串列表
        :return: 响应内容列表
        """
        responses = []
        for cmd in cmds:
            resp = self.send_command(cmd)
            responses.append(resp)
        return responses
