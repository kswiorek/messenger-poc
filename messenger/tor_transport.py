import socket

from .protocol import build_message, read_json_line, send_json_line


def socks5_connect_via_tor(
    socks_host: str,
    socks_port: int,
    dest_host: str,
    dest_port: int,
    timeout_sec: float = 15.0,
) -> socket.socket:
    s = socket.create_connection((socks_host, socks_port), timeout=timeout_sec)

    s.sendall(b"\x05\x01\x00")
    response = s.recv(2)
    if response != b"\x05\x00":
        s.close()
        raise ConnectionError(f"SOCKS5 auth negotiation failed: {response!r}")

    host_bytes = dest_host.encode("ascii")
    if len(host_bytes) > 255:
        s.close()
        raise ValueError("destination host name is too long for SOCKS5 domain format")

    req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + dest_port.to_bytes(2, "big")
    s.sendall(req)

    head = s.recv(4)
    if len(head) < 4 or head[0] != 0x05:
        s.close()
        raise ConnectionError(f"invalid SOCKS5 connect response head: {head!r}")
    if head[1] != 0x00:
        s.close()
        raise ConnectionError(f"SOCKS5 connect failed with code 0x{head[1]:02x}")

    atyp = head[3]
    if atyp == 0x01:
        to_read = 4 + 2
    elif atyp == 0x03:
        ln = s.recv(1)
        if len(ln) != 1:
            s.close()
            raise ConnectionError("short SOCKS5 domain-length response")
        to_read = ln[0] + 2
    elif atyp == 0x04:
        to_read = 16 + 2
    else:
        s.close()
        raise ConnectionError(f"unknown SOCKS5 ATYP in response: {atyp}")

    remaining = to_read
    while remaining > 0:
        chunk = s.recv(remaining)
        if not chunk:
            s.close()
            raise ConnectionError("SOCKS5 response truncated")
        remaining -= len(chunk)

    s.settimeout(timeout_sec)
    return s


def send_to_peer_expect_ack(
    sender_id: str,
    socks_host: str,
    socks_port: int,
    target_onion: str,
    target_port: int,
    msg_type: str,
    payload: dict,
) -> None:
    conn = socks5_connect_via_tor(
        socks_host=socks_host,
        socks_port=socks_port,
        dest_host=target_onion,
        dest_port=target_port,
    )
    try:
        msg = build_message(sender_id=sender_id, msg_type=msg_type, payload=payload)
        send_json_line(conn, msg)

        response = read_json_line(conn)
        if response.get("type") != "ack":
            raise RuntimeError(f"expected ack, got {response.get('type')}")

        ack_for = response.get("payload", {}).get("for_message_id")
        if ack_for != msg["message_id"]:
            raise RuntimeError("received ack for unexpected message id")
    finally:
        conn.close()
