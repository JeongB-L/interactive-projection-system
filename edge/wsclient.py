"""Minimal WebSocket client using only the standard library (so the Pi agent needs no pip install
for the simulator). Supports text/binary send, and receiving text frames; no fragmentation, no TLS."""
import base64
import os
import socket
import struct
import threading


class WSClosed(Exception):
    pass


class WS:
    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise WSClosed("handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise WSClosed("upgrade refused: " + head.split(b"\r\n")[0].decode(errors="replace"))
        self.sock.settimeout(None)
        self._lock = threading.Lock()
        self._rest = head.split(b"\r\n\r\n", 1)[1]

    def _send(self, opcode, payload):
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_big(payload, mask)
        with self._lock:
            self.sock.sendall(hdr + mask + masked)

    def send_text(self, s):
        self._send(0x1, s.encode())

    def send_binary(self, b):
        self._send(0x2, b)

    def _read(self, n):
        buf = b""
        if self._rest:
            buf, self._rest = self._rest[:n], self._rest[n:]
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise WSClosed("closed")
            buf += chunk
        return buf

    def recv_text(self):
        """Blocks until a text frame arrives; answers pings; raises WSClosed on close."""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)  # server frames are unmasked
            if op == 0x1:
                return data.decode()
            if op == 0x9:
                self._send(0xA, data)
            elif op == 0x8:
                raise WSClosed("server closed")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _mask_big(payload, mask):
    # fast path for large JPEG payloads
    m = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(len(payload), "big")
