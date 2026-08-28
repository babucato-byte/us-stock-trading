"""A minimal WebSocket client, because the probe needs one and nothing
else here does.

KIS publishes overseas real-time trades (HDFSCNT0) over a plain
WebSocket on ops.koreainvestment.com:21000. The alternative to these
~150 lines was adding `websocket-client` to the runtime image for a
single diagnostic, which would put a new dependency inside every
release that places real orders. This speaks exactly the part of RFC
6455 the probe needs -- a client handshake, masked text frames out,
unmasked text frames in, ping/pong -- and nothing else.

Deliberately NOT a general-purpose client. No extensions, no
continuation frames beyond simple reassembly, no reconnection policy.
If real-time data ever becomes a trading input rather than a
measurement, this should be replaced by a maintained library, and the
freshness and disconnect handling that a trading input needs belongs
with it.
"""

import base64
import os
import socket
import struct

_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, host, port, path="/", *, timeout=30.0):
        self.host = host
        self.port = int(port)
        self.path = path or "/"
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        self._handshake()

    # -- handshake -------------------------------------------------------

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self._sock.sendall(request)

        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketError(f"server refused the upgrade: {status}")

    # -- frames ----------------------------------------------------------

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x80 | _OPCODE_TEXT])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        # A client MUST mask. Servers close the connection if it does not.
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed by the server")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """One application message as text, or None when it was a control
        frame this client answers itself."""
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == _OPCODE_PING:
            self._send_control(_OPCODE_PONG, payload)
            return None
        if opcode == _OPCODE_PONG:
            return None
        if opcode == _OPCODE_CLOSE:
            raise WebSocketError("server closed the connection")
        if opcode in (_OPCODE_TEXT, _OPCODE_CONTINUATION, _OPCODE_BINARY):
            while not fin:
                more = self.recv()
                if more:
                    payload += more.encode("utf-8")
                break
            return payload.decode("utf-8", errors="replace")
        return None

    def _send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(
            bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def close(self):
        try:
            self._send_control(_OPCODE_CLOSE)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
