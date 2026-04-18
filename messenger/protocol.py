import json
import secrets
import socket
from datetime import datetime, timezone


PROTOCOL_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_message(sender_id: str, msg_type: str, payload: dict, msg_id: str | None = None) -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "message_id": msg_id or secrets.token_hex(8),
        "sender_id": sender_id,
        "timestamp": utc_now_iso(),
        "type": msg_type,
        "payload": payload,
    }


def send_json_line(conn: socket.socket, data: dict) -> None:
    encoded = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    conn.sendall(encoded)


def read_json_line(conn: socket.socket, timeout_sec: float = 15.0) -> dict:
    conn.settimeout(timeout_sec)
    buffer = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed before full message was received")
        buffer += chunk
        line_end = buffer.find(b"\n")
        if line_end != -1:
            line = buffer[:line_end]
            return json.loads(line.decode("utf-8"))
