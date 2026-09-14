"""Minimal Minecraft RCON client shared by benchmark tools.

The module has no command-line side effects; construct :class:`Rcon` and call
:meth:`Rcon.cmd` from an application.
"""
import re
import socket
import struct
import time


class Rcon:
    """Small Source RCON client with reconnect/retry behavior."""

    def __init__(self, host, port, password, timeout=15):
        self.addr = (host, port)
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.req = 0

    def connect(self):
        self.sock = socket.create_connection(self.addr, self.timeout)
        self.sock.settimeout(self.timeout)
        rid, _ = self._send(3, self.password)
        if rid == -1:
            raise RuntimeError("RCON 认证失败(密码错误?)")

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON 连接被关闭")
            buf += chunk
        return buf

    def _send(self, ptype, payload):
        self.req += 1
        rid = self.req
        body = struct.pack("<ii", rid, ptype) + payload.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        rlen = struct.unpack("<i", self._recvn(4))[0]
        resp = self._recvn(rlen)
        prid, _ = struct.unpack("<ii", resp[:8])
        return prid, resp[8:-2].decode("utf-8", "replace")

    def cmd(self, c, retries=3):
        last = None
        for _ in range(retries):
            try:
                if self.sock is None:
                    self.connect()
                return self._send(2, c)[1]
            except (OSError, ConnectionError, RuntimeError) as e:
                last = e
                self.sock = None
                time.sleep(2)
        raise RuntimeError(f"RCON 命令失败: {c!r}: {last}")


def strip_colors(s):
    """Remove Minecraft section-sign formatting codes from *s*."""
    return re.sub("§.", "", s)


__all__ = ["Rcon", "strip_colors"]
