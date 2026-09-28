"""Small dependency-free, read-only RFC 6455 client for Toss validation."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Any

from .client import ValidationError


WS_HOST = "openapi-ws.tossinvest.com"
WS_PATH = "/ws/v1"
ALLOWED_TOPICS = frozenset({"trade:us", "orderbook:us", "personal:order"})


@dataclass(frozen=True)
class WebSocketResult:
    connected: bool
    latency_ms: int
    messages: int
    disconnects: int
    ack_received: bool
    rejected: int


class TossWebSocketClient:
    def __init__(self, token: str, *, timeout: float = 12.0) -> None:
        if not token:
            raise ValidationError("AUTHENTICATION_REQUIRED")
        self._token = token
        self.timeout = timeout

    @staticmethod
    def _validate_subscriptions(subscriptions: list[dict[str, Any]]) -> None:
        for item in subscriptions:
            topic = item.get("type")
            if topic is not None and topic not in ALLOWED_TOPICS:
                raise ValidationError("BLOCKED_MUTATING_REQUEST")

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        result = b""
        while len(result) < size:
            chunk = sock.recv(size - len(result))
            if not chunk:
                raise EOFError("websocket closed")
            result += chunk
        return result

    @staticmethod
    def _send_text(sock: socket.socket, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x81])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 0xFFFF:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + masked)

    @classmethod
    def _recv_frame(cls, sock: socket.socket) -> tuple[int, bytes]:
        first, second = cls._recv_exact(sock, 2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", cls._recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", cls._recv_exact(sock, 8))[0]
        masked = bool(second & 0x80)
        mask = cls._recv_exact(sock, 4) if masked else b""
        payload = cls._recv_exact(sock, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def subscribe(self, subscriptions: list[dict[str, Any]], *, listen_seconds: float = 4.0) -> WebSocketResult:
        self._validate_subscriptions(subscriptions)
        started = time.monotonic()
        raw = socket.create_connection((WS_HOST, 443), timeout=self.timeout)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=WS_HOST)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        # Token is only ever sent on the wire; it is never stored in a result/error/log.
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer %s\r\n\r\n"
        ) % (WS_PATH, WS_HOST, key, self._token)
        try:
            sock.sendall(handshake.encode("ascii"))
            response = sock.recv(4096).split(b"\r\n", 1)[0]
            if b" 101 " not in response:
                raise ValidationError("WEBSOCKET_HANDSHAKE_FAILED")
            self._send_text(sock, json.dumps(subscriptions, separators=(",", ":")))
            messages = rejected = 0
            ack_received = False
            deadline = time.monotonic() + listen_seconds
            while time.monotonic() < deadline:
                sock.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    opcode, payload = self._recv_frame(sock)
                except socket.timeout:
                    break
                if opcode == 8:
                    return WebSocketResult(True, int((time.monotonic() - started) * 1000), messages, 1, ack_received, rejected)
                if opcode != 1:
                    continue
                messages += 1
                try:
                    frame = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if frame.get("type") == "subscriptions":
                    ack_received = True
                    rejected += len(frame.get("rejected") or [])
            return WebSocketResult(True, int((time.monotonic() - started) * 1000), messages, 0, ack_received, rejected)
        except (OSError, EOFError) as exc:
            raise ValidationError("WEBSOCKET_CONNECTION_ERROR") from None
        finally:
            try:
                sock.close()
            except OSError:
                pass
