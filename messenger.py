import argparse
import json
import secrets
import socket
import threading
import time
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


def handle_incoming_connection(conn: socket.socket, addr: tuple, sender_id: str) -> None:
	try:
		incoming = read_json_line(conn)
		print(f"[recv] from={addr} type={incoming.get('type')} id={incoming.get('message_id')}")

		if incoming.get("type") == "ping":
			pong = build_message(
				sender_id=sender_id,
				msg_type="pong",
				payload={"echo": incoming.get("payload", {}).get("nonce")},
				msg_id=incoming.get("message_id"),
			)
			send_json_line(conn, pong)
			print(f"[send] pong id={pong['message_id']}")
		else:
			error = build_message(
				sender_id=sender_id,
				msg_type="error",
				payload={"reason": f"unsupported message type: {incoming.get('type')}"},
			)
			send_json_line(conn, error)
			print(f"[send] error id={error['message_id']}")
	except Exception as exc:
		print(f"[error] handling connection from {addr}: {exc}")
	finally:
		conn.close()


def run_listener(bind_host: str, bind_port: int, sender_id: str) -> None:
	server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	server.bind((bind_host, bind_port))
	server.listen(16)

	print(f"[listener] ready on {bind_host}:{bind_port} (sender_id={sender_id})")
	while True:
		conn, addr = server.accept()
		thread = threading.Thread(
			target=handle_incoming_connection,
			args=(conn, addr, sender_id),
			daemon=True,
		)
		thread.start()


def socks5_connect_via_tor(
	socks_host: str,
	socks_port: int,
	dest_host: str,
	dest_port: int,
	timeout_sec: float = 15.0,
) -> socket.socket:
	s = socket.create_connection((socks_host, socks_port), timeout=timeout_sec)

	# SOCKS5 greeting: version 5, one auth method, no-authentication.
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


def send_ping(
	target_onion: str,
	target_port: int,
	socks_host: str,
	socks_port: int,
	sender_id: str,
) -> None:
	if not target_onion.endswith(".onion"):
		raise ValueError("target host must be a .onion address")

	conn = socks5_connect_via_tor(
		socks_host=socks_host,
		socks_port=socks_port,
		dest_host=target_onion,
		dest_port=target_port,
	)
	try:
		nonce = secrets.token_hex(8)
		ping = build_message(sender_id=sender_id, msg_type="ping", payload={"nonce": nonce})
		t0 = time.time()
		send_json_line(conn, ping)
		print(f"[send] ping id={ping['message_id']} to={target_onion}:{target_port}")

		response = read_json_line(conn)
		elapsed_ms = (time.time() - t0) * 1000.0
		print(f"[recv] type={response.get('type')} id={response.get('message_id')} rtt_ms={elapsed_ms:.1f}")

		if response.get("type") != "pong":
			raise RuntimeError(f"expected pong, got {response.get('type')}")

		echoed = response.get("payload", {}).get("echo")
		if echoed != nonce:
			raise RuntimeError("pong echo nonce did not match ping nonce")

		print("[ok] ping/pong validation passed")
	finally:
		conn.close()


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Tor onion peer-to-peer messaging PoC (ping/pong only)."
	)
	parser.add_argument("--sender-id", default="peer", help="Logical sender id added to message envelopes")

	sub = parser.add_subparsers(dest="command", required=True)

	serve = sub.add_parser("listen", help="Run local listener that handles ping and replies with pong")
	serve.add_argument("--host", default="127.0.0.1", help="Bind host for local listener")
	serve.add_argument("--port", type=int, default=7000, help="Bind port for local listener")

	ping = sub.add_parser("ping", help="Send ping to target onion service through Tor SOCKS")
	ping.add_argument("--target", required=True, help="Target onion hostname, e.g. abcdef...xyz.onion")
	ping.add_argument("--port", type=int, default=7000, help="Target onion service port")
	ping.add_argument("--socks-host", default="127.0.0.1", help="Local Tor SOCKS host")
	ping.add_argument("--socks-port", type=int, default=9050, help="Local Tor SOCKS port")

	return parser.parse_args()


def main() -> None:
	args = parse_args()

	if args.command == "listen":
		run_listener(bind_host=args.host, bind_port=args.port, sender_id=args.sender_id)
	elif args.command == "ping":
		send_ping(
			target_onion=args.target,
			target_port=args.port,
			socks_host=args.socks_host,
			socks_port=args.socks_port,
			sender_id=args.sender_id,
		)
	else:
		raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
	main()
