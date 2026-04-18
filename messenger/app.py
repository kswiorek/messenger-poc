import asyncio
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import build_message, read_json_line, send_json_line, utc_now_iso
from .tor_transport import send_to_peer_expect_ack, socks5_connect_via_tor

try:
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    AIORTC_AVAILABLE = True
except ImportError:
    RTCConfiguration = Any  # type: ignore[assignment]
    RTCIceServer = Any  # type: ignore[assignment]
    RTCPeerConnection = Any  # type: ignore[assignment]
    RTCSessionDescription = Any  # type: ignore[assignment]
    AIORTC_AVAILABLE = False


def print_prompt() -> None:
    print("> ", end="", flush=True)


class MessengerApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sender_id = cfg["sender_id"]
        self.listen_host = cfg["listen"]["host"]
        self.listen_port = int(cfg["listen"]["port"])
        self.socks_host = cfg["tor_socks"]["host"]
        self.socks_port = int(cfg["tor_socks"]["port"])
        self.peers = cfg["peers"]
        self.webrtc_cfg = cfg["webrtc"]

        self.stop_event = threading.Event()
        self.server: socket.socket | None = None
        self.listener_thread: threading.Thread | None = None

        self.state_lock = threading.Lock()
        self.pending_offers: dict[str, dict] = {}
        self.rtc_sessions: dict[str, dict] = {}
        self.incoming_files: dict[str, dict] = {}

        self.rtc_loop = asyncio.new_event_loop()
        self.rtc_thread: threading.Thread | None = None
        self.rtc_configuration = self._build_rtc_configuration() if AIORTC_AVAILABLE else None

    def _build_rtc_configuration(self) -> RTCConfiguration:
        ice_servers: list[RTCIceServer] = []
        for entry in self.webrtc_cfg.get("ice_servers", []):
            urls = entry.get("urls")
            if not urls:
                continue
            username = entry.get("username")
            credential = entry.get("credential")
            ice_servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
        return RTCConfiguration(iceServers=ice_servers)

    def _run_rtc_loop(self) -> None:
        asyncio.set_event_loop(self.rtc_loop)
        self.rtc_loop.run_forever()

    def _run_coro_threadsafe(self, coro: Any, timeout_sec: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.rtc_loop)
        return future.result(timeout=timeout_sec)

    def start(self) -> None:
        if AIORTC_AVAILABLE:
            self.rtc_thread = threading.Thread(target=self._run_rtc_loop, daemon=True)
            self.rtc_thread.start()

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
        if AIORTC_AVAILABLE:
            self._shutdown_webrtc()

        if self.server is not None:
            self.server.close()
        if self.listener_thread is not None:
            self.listener_thread.join(timeout=2.0)

        self._close_incoming_file_handles()

    def _close_incoming_file_handles(self) -> None:
        with self.state_lock:
            files = list(self.incoming_files.values())
            self.incoming_files.clear()
        for state in files:
            handle = state.get("handle")
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

    def _shutdown_webrtc(self) -> None:
        try:
            self._run_coro_threadsafe(self._close_all_peer_connections(), timeout_sec=5.0)
        except Exception:
            pass
        self.rtc_loop.call_soon_threadsafe(self.rtc_loop.stop)
        if self.rtc_thread is not None:
            self.rtc_thread.join(timeout=2.0)

    async def _close_all_peer_connections(self) -> None:
        pcs: list[RTCPeerConnection] = []
        with self.state_lock:
            for session in self.rtc_sessions.values():
                pc = session.get("pc")
                if pc is not None:
                    pcs.append(pc)
        for pc in pcs:
            await pc.close()

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
            sender = incoming.get("sender_id", "unknown")

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
                print_prompt()
            elif msg_type == "text":
                text = incoming.get("payload", {}).get("text", "")
                print(f"\n[msg] {sender}: {text}")

                ack = build_message(
                    sender_id=self.sender_id,
                    msg_type="ack",
                    payload={"for_message_id": msg_id},
                )
                send_json_line(conn, ack)
                print_prompt()
            elif msg_type == "signal_offer":
                payload = incoming.get("payload", {})
                self._handle_signal_offer(sender, payload)
                ack = build_message(
                    sender_id=self.sender_id,
                    msg_type="ack",
                    payload={"for_message_id": msg_id},
                )
                send_json_line(conn, ack)
                print_prompt()
            elif msg_type == "signal_answer":
                payload = incoming.get("payload", {})
                self._handle_signal_answer(sender, payload)
                ack = build_message(
                    sender_id=self.sender_id,
                    msg_type="ack",
                    payload={"for_message_id": msg_id},
                )
                send_json_line(conn, ack)
                print_prompt()
            else:
                error = build_message(
                    sender_id=self.sender_id,
                    msg_type="error",
                    payload={"reason": f"unsupported message type: {msg_type}"},
                )
                send_json_line(conn, error)
                print(f"\n[warn] unsupported message type from {addr}: {msg_type}")
                print_prompt()
        except Exception as exc:
            print(f"\n[error] handling connection from {addr}: {exc}")
            print_prompt()
        finally:
            conn.close()

    def _peer_name_from_sender_id(self, sender_id: str) -> str | None:
        if sender_id in self.peers:
            return sender_id
        for peer_name, peer in self.peers.items():
            if peer.get("sender_id") == sender_id:
                return peer_name
        return None

    def _handle_signal_offer(self, sender_id: str, payload: dict) -> None:
        peer_name = self._peer_name_from_sender_id(sender_id)
        if not peer_name:
            print(f"\n[warn] signal_offer from unknown sender_id={sender_id}. Add peer mapping in config.")
            return

        sdp = payload.get("sdp")
        sdp_type = payload.get("type")
        if not sdp or not sdp_type:
            print(f"\n[warn] invalid signal_offer from {peer_name}")
            return

        with self.state_lock:
            self.pending_offers[peer_name] = {
                "sdp": sdp,
                "type": sdp_type,
                "received_at": utc_now_iso(),
            }

        print(f"\n[rtc] received offer from {peer_name}. Run /rtc accept {peer_name}")

    def _handle_signal_answer(self, sender_id: str, payload: dict) -> None:
        if not AIORTC_AVAILABLE:
            return

        peer_name = self._peer_name_from_sender_id(sender_id)
        if not peer_name:
            print(f"\n[warn] signal_answer from unknown sender_id={sender_id}")
            return

        sdp = payload.get("sdp")
        sdp_type = payload.get("type")
        if not sdp or not sdp_type:
            print(f"\n[warn] invalid signal_answer from {peer_name}")
            return

        try:
            self._run_coro_threadsafe(self._apply_answer(peer_name, sdp, sdp_type), timeout_sec=30.0)
            print(f"\n[rtc] answer from {peer_name} applied")
        except Exception as exc:
            print(f"\n[error] failed applying answer from {peer_name}: {exc}")

    async def _wait_ice_gathering_complete(self, pc: RTCPeerConnection, timeout_sec: float = 10.0) -> None:
        start = time.time()
        while pc.iceGatheringState != "complete":
            if time.time() - start > timeout_sec:
                break
            await asyncio.sleep(0.1)

    def _bind_data_channel(self, peer_name: str, channel: Any) -> None:
        @channel.on("open")
        def on_open() -> None:
            print(f"\n[rtc] data channel open with {peer_name}")
            print_prompt()

        @channel.on("close")
        def on_close() -> None:
            print(f"\n[rtc] data channel closed with {peer_name}")
            print_prompt()

        @channel.on("message")
        def on_message(message: Any) -> None:
            if isinstance(message, bytes):
                print(f"\n[rtc] binary data from {peer_name}: {len(message)} bytes")
                print_prompt()
                return

            try:
                data = json.loads(message)
            except Exception:
                print(f"\n[rtc] {peer_name}: {message}")
                print_prompt()
                return

            msg_type = data.get("type")
            if msg_type == "rtc_test":
                print(f"\n[rtc-test] {peer_name}: {data.get('text', '')}")
            elif msg_type == "file_meta":
                self._on_file_meta(peer_name, data)
            elif msg_type == "file_chunk":
                self._on_file_chunk(peer_name, data)
            elif msg_type == "file_done":
                self._on_file_done(peer_name, data, channel)
            elif msg_type == "file_ack":
                transfer_id = data.get("transfer_id")
                print(f"\n[file] transfer complete ack from {peer_name} transfer_id={transfer_id}")
            elif msg_type == "file_error":
                transfer_id = data.get("transfer_id")
                reason = data.get("reason", "unknown")
                print(f"\n[file] transfer error from {peer_name} transfer_id={transfer_id}: {reason}")
            else:
                print(f"\n[rtc] message from {peer_name}: {data}")
            print_prompt()

    def _attach_peer_connection_handlers(self, peer_name: str, pc: RTCPeerConnection) -> None:
        @pc.on("connectionstatechange")
        def on_connectionstatechange() -> None:
            state = pc.connectionState
            with self.state_lock:
                session = self.rtc_sessions.get(peer_name)
                if session:
                    session["connection_state"] = state
            print(f"\n[rtc] {peer_name} connection state: {state}")
            print_prompt()

        @pc.on("iceconnectionstatechange")
        def on_iceconnectionstatechange() -> None:
            state = pc.iceConnectionState
            with self.state_lock:
                session = self.rtc_sessions.get(peer_name)
                if session:
                    session["ice_state"] = state
            print(f"\n[rtc] {peer_name} ICE state: {state}")
            print_prompt()

        @pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            with self.state_lock:
                session = self.rtc_sessions.get(peer_name)
                if session:
                    session["channel"] = channel
            print(f"\n[rtc] incoming data channel from {peer_name}: {channel.label}")
            self._bind_data_channel(peer_name, channel)
            print_prompt()

    async def _create_offer(self, peer_name: str) -> dict:
        pc = RTCPeerConnection(configuration=self.rtc_configuration)
        channel = pc.createDataChannel("chat")
        self._attach_peer_connection_handlers(peer_name, pc)
        self._bind_data_channel(peer_name, channel)

        with self.state_lock:
            self.rtc_sessions[peer_name] = {
                "pc": pc,
                "channel": channel,
                "role": "caller",
                "connection_state": pc.connectionState,
                "ice_state": pc.iceConnectionState,
            }

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._wait_ice_gathering_complete(pc)

        local = pc.localDescription
        if local is None:
            raise RuntimeError("localDescription is empty after offer creation")

        return {"sdp": local.sdp, "type": local.type}

    async def _create_answer(self, peer_name: str, offer_sdp: str, offer_type: str) -> dict:
        pc = RTCPeerConnection(configuration=self.rtc_configuration)
        self._attach_peer_connection_handlers(peer_name, pc)

        with self.state_lock:
            self.rtc_sessions[peer_name] = {
                "pc": pc,
                "channel": None,
                "role": "callee",
                "connection_state": pc.connectionState,
                "ice_state": pc.iceConnectionState,
            }

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering_complete(pc)

        local = pc.localDescription
        if local is None:
            raise RuntimeError("localDescription is empty after answer creation")

        return {"sdp": local.sdp, "type": local.type}

    async def _apply_answer(self, peer_name: str, answer_sdp: str, answer_type: str) -> None:
        with self.state_lock:
            session = self.rtc_sessions.get(peer_name)
        if not session:
            raise RuntimeError(f"No RTC session found for {peer_name}")

        pc = session.get("pc")
        if pc is None:
            raise RuntimeError(f"RTC session for {peer_name} has no peer connection")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type=answer_type))

    async def _send_rtc_test_message(self, peer_name: str, text: str) -> None:
        channel = self._require_open_channel(peer_name)
        payload = json.dumps({"type": "rtc_test", "text": text})
        channel.send(payload)

    def _require_open_channel(self, peer_name: str) -> Any:
        with self.state_lock:
            session = self.rtc_sessions.get(peer_name)
        if not session:
            raise RuntimeError(f"No RTC session found for {peer_name}")

        channel = session.get("channel")
        if channel is None:
            raise RuntimeError(f"No RTC data channel with {peer_name}")

        if channel.readyState != "open":
            raise RuntimeError(f"RTC data channel with {peer_name} is not open (state={channel.readyState})")

        return channel

    async def _send_file(self, peer_name: str, file_path: Path) -> None:
        channel = self._require_open_channel(peer_name)
        if not file_path.exists() or not file_path.is_file():
            raise RuntimeError(f"File not found: {file_path}")

        chunk_size = int(self.webrtc_cfg.get("file_chunk_bytes", 65536))
        transfer_id = secrets.token_hex(8)
        file_size = file_path.stat().st_size

        hasher = hashlib.sha256()
        with file_path.open("rb") as in_file:
            while True:
                block = in_file.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
        sha256_hex = hasher.hexdigest()

        meta = {
            "type": "file_meta",
            "transfer_id": transfer_id,
            "name": file_path.name,
            "size": file_size,
            "sha256": sha256_hex,
            "chunk_size": chunk_size,
        }
        channel.send(json.dumps(meta))

        sent_bytes = 0
        chunk_index = 0
        with file_path.open("rb") as in_file:
            while True:
                chunk = in_file.read(chunk_size)
                if not chunk:
                    break

                while channel.bufferedAmount > (chunk_size * 8):
                    await asyncio.sleep(0.01)

                payload = {
                    "type": "file_chunk",
                    "transfer_id": transfer_id,
                    "index": chunk_index,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
                channel.send(json.dumps(payload))
                chunk_index += 1
                sent_bytes += len(chunk)

                if file_size > 0:
                    pct = (sent_bytes / file_size) * 100.0
                    if chunk_index == 1 or sent_bytes == file_size or chunk_index % 50 == 0:
                        print(f"\n[file] sending to {peer_name}: {pct:.1f}% ({sent_bytes}/{file_size} bytes)")
                        print_prompt()

                await asyncio.sleep(0)

        done = {
            "type": "file_done",
            "transfer_id": transfer_id,
        }
        channel.send(json.dumps(done))
        print(f"\n[file] sent transfer_id={transfer_id} to {peer_name}")
        print_prompt()

    def _on_file_meta(self, peer_name: str, data: dict) -> None:
        transfer_id = data.get("transfer_id")
        file_name = data.get("name")
        file_size = int(data.get("size", 0))
        sha256_hex = data.get("sha256")

        if not transfer_id or not file_name or not sha256_hex:
            print(f"\n[file] invalid file_meta from {peer_name}")
            return

        download_dir = Path(self.webrtc_cfg.get("download_dir", "downloads"))
        download_dir.mkdir(parents=True, exist_ok=True)

        safe_name = Path(file_name).name
        target_path = download_dir / safe_name
        if target_path.exists():
            stem = target_path.stem
            suffix = target_path.suffix
            idx = 1
            while True:
                candidate = download_dir / f"{stem}_{idx}{suffix}"
                if not candidate.exists():
                    target_path = candidate
                    break
                idx += 1

        handle = target_path.open("wb")
        state = {
            "peer_name": peer_name,
            "path": target_path,
            "handle": handle,
            "size": file_size,
            "sha256": sha256_hex,
            "received": 0,
            "hasher": hashlib.sha256(),
        }
        with self.state_lock:
            self.incoming_files[transfer_id] = state

        print(f"\n[file] receiving from {peer_name}: {safe_name} ({file_size} bytes) transfer_id={transfer_id}")

    def _on_file_chunk(self, peer_name: str, data: dict) -> None:
        transfer_id = data.get("transfer_id")
        b64_data = data.get("data")
        if not transfer_id or b64_data is None:
            print(f"\n[file] invalid file_chunk from {peer_name}")
            return

        with self.state_lock:
            state = self.incoming_files.get(transfer_id)
        if not state:
            print(f"\n[file] unknown transfer_id from {peer_name}: {transfer_id}")
            return

        try:
            chunk = base64.b64decode(b64_data)
        except Exception:
            print(f"\n[file] invalid base64 chunk from {peer_name}")
            return

        handle = state["handle"]
        handle.write(chunk)
        state["hasher"].update(chunk)
        state["received"] += len(chunk)

        total = state["size"]
        recv = state["received"]
        if total > 0 and (recv == total or recv % (1024 * 1024) < len(chunk)):
            pct = (recv / total) * 100.0
            print(f"\n[file] receiving from {peer_name}: {pct:.1f}% ({recv}/{total} bytes)")

    def _send_file_result(self, channel: Any, transfer_id: str, success: bool, reason: str = "") -> None:
        payload = {"transfer_id": transfer_id}
        if success:
            payload["type"] = "file_ack"
        else:
            payload["type"] = "file_error"
            payload["reason"] = reason
        channel.send(json.dumps(payload))

    def _on_file_done(self, peer_name: str, data: dict, channel: Any) -> None:
        transfer_id = data.get("transfer_id")
        if not transfer_id:
            print(f"\n[file] invalid file_done from {peer_name}")
            return

        with self.state_lock:
            state = self.incoming_files.pop(transfer_id, None)
        if not state:
            print(f"\n[file] unknown transfer_id on done from {peer_name}: {transfer_id}")
            return

        handle = state["handle"]
        handle.close()

        expected_size = state["size"]
        expected_hash = state["sha256"]
        received_size = state["received"]
        actual_hash = state["hasher"].hexdigest()

        if received_size != expected_size:
            reason = f"size mismatch expected={expected_size} received={received_size}"
            print(f"\n[file] transfer failed from {peer_name}: {reason}")
            self._send_file_result(channel, transfer_id, success=False, reason=reason)
            return

        if actual_hash != expected_hash:
            reason = "sha256 mismatch"
            print(f"\n[file] transfer failed from {peer_name}: {reason}")
            self._send_file_result(channel, transfer_id, success=False, reason=reason)
            return

        file_path = state["path"]
        print(f"\n[file] saved from {peer_name}: {file_path}")
        self._send_file_result(channel, transfer_id, success=True)

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
        send_to_peer_expect_ack(
            sender_id=self.sender_id,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            target_onion=target_onion,
            target_port=target_port,
            msg_type="text",
            payload={"text": text},
        )
        print(f"[sent] to={peer_name}")

    def rtc_connect(self, peer_name: str) -> None:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("WebRTC support requires aiortc. Install with: pip install aiortc")

        target_onion, target_port = self._resolve_peer(peer_name)
        offer_payload = self._run_coro_threadsafe(
            self._create_offer(peer_name),
            timeout_sec=float(self.webrtc_cfg.get("signaling_timeout_sec", 30)),
        )
        send_to_peer_expect_ack(
            sender_id=self.sender_id,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            target_onion=target_onion,
            target_port=target_port,
            msg_type="signal_offer",
            payload=offer_payload,
        )
        print(f"[rtc] offer sent to {peer_name}. Waiting for answer...")

    def rtc_accept(self, peer_name: str) -> None:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("WebRTC support requires aiortc. Install with: pip install aiortc")

        target_onion, target_port = self._resolve_peer(peer_name)
        with self.state_lock:
            offer = self.pending_offers.pop(peer_name, None)
        if not offer:
            raise RuntimeError(f"No pending offer from {peer_name}")

        answer_payload = self._run_coro_threadsafe(
            self._create_answer(peer_name, offer["sdp"], offer["type"]),
            timeout_sec=float(self.webrtc_cfg.get("signaling_timeout_sec", 30)),
        )
        send_to_peer_expect_ack(
            sender_id=self.sender_id,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            target_onion=target_onion,
            target_port=target_port,
            msg_type="signal_answer",
            payload=answer_payload,
        )
        print(f"[rtc] answer sent to {peer_name}")

    def rtc_status(self, peer_name: str | None = None) -> None:
        with self.state_lock:
            pending = dict(self.pending_offers)
            sessions = dict(self.rtc_sessions)

        if pending:
            print("[rtc] pending offers:")
            for pending_peer in pending.keys():
                if peer_name and pending_peer != peer_name:
                    continue
                print(f"- {pending_peer}")
        else:
            print("[rtc] no pending offers")

        if not sessions:
            print("[rtc] no active sessions")
            return

        for session_peer, session in sessions.items():
            if peer_name and session_peer != peer_name:
                continue
            channel = session.get("channel")
            channel_state = channel.readyState if channel else "none"
            print(
                f"- {session_peer}: role={session.get('role')} conn={session.get('connection_state')} "
                f"ice={session.get('ice_state')} data={channel_state}"
            )

    def rtc_test(self, peer_name: str, text: str) -> None:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("WebRTC support requires aiortc. Install with: pip install aiortc")

        self._run_coro_threadsafe(self._send_rtc_test_message(peer_name, text), timeout_sec=10.0)
        print(f"[rtc-test] sent to {peer_name}")

    def send_file(self, peer_name: str, path_text: str) -> None:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("WebRTC support requires aiortc. Install with: pip install aiortc")

        file_path = Path(path_text)
        self._run_coro_threadsafe(self._send_file(peer_name, file_path), timeout_sec=300.0)

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
                print("/rtc connect <peer_name>")
                print("/rtc accept <peer_name>")
                print("/rtc status [peer_name]")
                print("/rtc test <peer_name> <text>")
                print("/file <peer_name> <path>")
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

            if line.startswith("/rtc "):
                parts = line.split(maxsplit=3)
                if len(parts) < 2:
                    print("[error] usage: /rtc <connect|accept|status|test> ...")
                    continue

                subcommand = parts[1]
                try:
                    if subcommand == "connect":
                        if len(parts) < 3:
                            print("[error] usage: /rtc connect <peer_name>")
                            continue
                        self.rtc_connect(parts[2])
                    elif subcommand == "accept":
                        if len(parts) < 3:
                            print("[error] usage: /rtc accept <peer_name>")
                            continue
                        self.rtc_accept(parts[2])
                    elif subcommand == "status":
                        peer_name = parts[2] if len(parts) >= 3 else None
                        self.rtc_status(peer_name)
                    elif subcommand == "test":
                        if len(parts) < 4:
                            print("[error] usage: /rtc test <peer_name> <text>")
                            continue
                        self.rtc_test(parts[2], parts[3])
                    else:
                        print("[error] usage: /rtc <connect|accept|status|test> ...")
                except Exception as exc:
                    print(f"[error] {exc}")
                continue

            if line.startswith("/file "):
                parts = line.split(maxsplit=2)
                if len(parts) < 3:
                    print("[error] usage: /file <peer_name> <path>")
                    continue

                peer_name = parts[1]
                file_path = parts[2]
                try:
                    self.send_file(peer_name, file_path)
                except Exception as exc:
                    print(f"[error] {exc}")
                continue

            print("[error] unknown command. Use /help")
