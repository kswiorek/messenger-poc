import json
import secrets
import socket
import threading
import time
from pathlib import Path
from datetime import datetime, timezone


PROTOCOL_VERSION = 1
CONFIG_PATH = Path("messenger_config.json")


def utc_now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def load_config(config_path: Path) -> dict:
	if not config_path.exists():
		raise FileNotFoundError(
			f"Missing config file: {config_path}. Create it from messenger_config.example.json or messenger_config.json template."
		)

	with config_path.open("r", encoding="utf-8") as handle:
		cfg = json.load(handle)

	required_top_level = ["sender_id", "listen", "tor_socks", "peers"]
	for key in required_top_level:
		if key not in cfg:
			raise ValueError(f"Config is missing required key: {key}")

	return cfg


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


class MessengerApp:
	def __init__(self, cfg: dict):
		self.cfg = cfg
		self.sender_id = cfg["sender_id"]
		self.listen_host = cfg["listen"]["host"]
		self.listen_port = int(cfg["listen"]["port"])
		self.socks_host = cfg["tor_socks"]["host"]
		self.socks_port = int(cfg["tor_socks"]["port"])
		self.peers = cfg["peers"]

		self.stop_event = threading.Event()
		self.server: socket.socket | None = None
		self.listener_thread: threading.Thread | None = None

	def start(self) -> None:
		server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		server.bind((self.listen_host, self.listen_port))
		server.listen(16)
		server.settimeout(1.0)
		self.server = server

		self.listener_thread = threading.Thread(target=self._listener_loop, daemon=True)
		self.listener_thread.start()
		print(f"[listener] ready on {self.listen_host}:{self.listen_port} (sender_id={self.sender_id})")
		print("[info] use /help for commands")

	def stop(self) -> None:
		self.stop_event.set()
		if self.server is not None:
			self.server.close()
		if self.listener_thread is not None:
			self.listener_thread.join(timeout=2.0)

	def _listener_loop(self) -> None:
		assert self.server is not None
		while not self.stop_event.is_set():
			try:
				conn, addr = self.server.accept()
			except socket.timeout:
				continue
			except OSError:
				break

			thread = threading.Thread(
				target=self._handle_incoming_connection,
				args=(conn, addr),
				daemon=True,
			)
			thread.start()

	def _handle_incoming_connection(self, conn: socket.socket, addr: tuple) -> None:
		try:
			incoming = read_json_line(conn)
			msg_type = incoming.get("type")
			msg_id = incoming.get("message_id")

			if msg_type == "ping":
				pong = build_message(
					sender_id=self.sender_id,
					msg_type="pong",
					payload={"echo": incoming.get("payload", {}).get("nonce")},
					msg_id=msg_id,
				)
				send_json_line(conn, pong)
				print(f"\n[recv] ping from={addr} id={msg_id}")
				print(f"[send] pong id={msg_id}")
				print("> ", end="", flush=True)
			elif msg_type == "text":
				text = incoming.get("payload", {}).get("text", "")
				sender = incoming.get("sender_id", "unknown")
				print(f"\n[msg] {sender}: {text}")

				ack = build_message(
					sender_id=self.sender_id,
					msg_type="ack",
					payload={"for_message_id": msg_id},
				)
				send_json_line(conn, ack)
				print("> ", end="", flush=True)
			else:
				error = build_message(
					sender_id=self.sender_id,
					msg_type="error",
					payload={"reason": f"unsupported message type: {msg_type}"},
				)
				send_json_line(conn, error)
				print(f"\n[warn] unsupported message type from {addr}: {msg_type}")
				print("> ", end="", flush=True)
		except Exception as exc:
			print(f"\n[error] handling connection from {addr}: {exc}")
			print("> ", end="", flush=True)
		finally:
			conn.close()

	def _resolve_peer(self, peer_name: str) -> tuple[str, int]:
		peer = self.peers.get(peer_name)
		if not peer:
			raise ValueError(f"Unknown peer '{peer_name}'. Use /peers to list configured peers.")

		target_onion = peer["onion"]
		target_port = int(peer.get("port", 7000))
		if not target_onion.endswith(".onion"):
			raise ValueError(f"Peer '{peer_name}' onion address is invalid: {target_onion}")

		return target_onion, target_port

	def ping_peer(self, peer_name: str) -> None:
		target_onion, target_port = self._resolve_peer(peer_name)
		conn = socks5_connect_via_tor(
			socks_host=self.socks_host,
			socks_port=self.socks_port,
			dest_host=target_onion,
			dest_port=target_port,
		)
		try:
			nonce = secrets.token_hex(8)
			ping = build_message(sender_id=self.sender_id, msg_type="ping", payload={"nonce": nonce})
			t0 = time.time()
			send_json_line(conn, ping)

			response = read_json_line(conn)
			elapsed_ms = (time.time() - t0) * 1000.0
			if response.get("type") != "pong":
				raise RuntimeError(f"expected pong, got {response.get('type')}")

			echoed = response.get("payload", {}).get("echo")
			if echoed != nonce:
				raise RuntimeError("pong echo nonce did not match ping nonce")

			print(f"[ok] ping {peer_name} rtt_ms={elapsed_ms:.1f}")
		finally:
			conn.close()

	def send_text(self, peer_name: str, text: str) -> None:
		target_onion, target_port = self._resolve_peer(peer_name)
		conn = socks5_connect_via_tor(
			socks_host=self.socks_host,
			socks_port=self.socks_port,
			dest_host=target_onion,
			dest_port=target_port,
		)
		try:
			msg = build_message(
				sender_id=self.sender_id,
				msg_type="text",
				payload={"text": text},
			)
			send_json_line(conn, msg)

			response = read_json_line(conn)
			if response.get("type") != "ack":
				raise RuntimeError(f"expected ack, got {response.get('type')}")

			ack_for = response.get("payload", {}).get("for_message_id")
			if not ack_for:
				raise RuntimeError("ack is missing payload.for_message_id")

			print(f"[sent] to={peer_name} id={msg['message_id']}")
		finally:
			conn.close()

	def run_cli(self) -> None:
		while not self.stop_event.is_set():
			try:
				line = input("> ").strip()
			except (KeyboardInterrupt, EOFError):
				print("\n[info] shutting down")
				break

			if not line:
				continue

			if line in {"/quit", "/exit"}:
				print("[info] shutting down")
				break

			if line == "/help":
				print("/help")
				print("/peers")
				print("/ping <peer_name>")
				print("/msg <peer_name> <text>")
				print("/quit")
				continue

			if line == "/peers":
				for name, peer in self.peers.items():
					print(f"- {name}: {peer.get('onion')}:{peer.get('port', 7000)}")
				continue

			if line.startswith("/ping "):
				peer_name = line.split(maxsplit=1)[1].strip()
				try:
					self.ping_peer(peer_name)
				except Exception as exc:
					print(f"[error] {exc}")
				continue

			if line.startswith("/msg "):
				parts = line.split(maxsplit=2)
				if len(parts) < 3:
					print("[error] usage: /msg <peer_name> <text>")
					continue

				peer_name = parts[1]
				text = parts[2]
				try:
					self.send_text(peer_name, text)
				except Exception as exc:
					print(f"[error] {exc}")
				continue

			print("[error] unknown command. Use /help")


def main() -> None:
	cfg = load_config(CONFIG_PATH)
	app = MessengerApp(cfg)
	app.start()
	try:
		app.run_cli()
	finally:
		app.stop()


if __name__ == "__main__":
	main()
