import html
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from messenger.app import MessengerApp
from messenger.config import load_config


BASE_CONFIG_PATH = Path("messenger_config.json")
LOCAL_CONFIG_PATH = Path("messenger_config.local.json")


class MessengerGui(QMainWindow):
    backend_event_signal = pyqtSignal(str, dict)
    backend_log_signal = pyqtSignal(str)
    task_done_signal = pyqtSignal(str, bool, object, object, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Messenger")
        self.resize(1120, 760)

        self.backend: MessengerApp | None = None
        self.cfg: dict[str, Any] = self._load_config()
        self.peer_map = self._peers_from_cfg(self.cfg)

        self.current_peer = ""
        self.chat_history: dict[str, list[tuple[str, str]]] = {name: [] for name in self.peer_map.keys()}
        self.peer_status: dict[str, str] = {name: "unknown" for name in self.peer_map.keys()}
        self.peer_rtt_ms: dict[str, str] = {}

        self.ping_queue: list[str] = []
        self.active_ping_peer = ""
        self.active_ping_started_ms = 0
        self.active_ping_request_id = 0
        self.next_ping_request_id = 1

        self.probe_interval_timer = QTimer(self)
        self.probe_interval_timer.setInterval(30000)
        self.probe_interval_timer.timeout.connect(self._enqueue_full_probe)

        self.ping_worker_timer = QTimer(self)
        self.ping_worker_timer.setInterval(200)
        self.ping_worker_timer.timeout.connect(self._process_ping_queue)

        self.backend_event_signal.connect(self._on_backend_event)
        self.backend_log_signal.connect(self._append_debug)
        self.task_done_signal.connect(self._on_task_done)

        self._build_ui()
        self._wire_events()
        self._populate_peer_list()

    def _load_config(self) -> dict[str, Any]:
        try:
            cfg = load_config(BASE_CONFIG_PATH, LOCAL_CONFIG_PATH)
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            pass
        return {"peers": {}}

    def _peers_from_cfg(self, cfg: dict[str, Any]) -> dict[str, dict]:
        peers = cfg.get("peers", {})
        return peers if isinstance(peers, dict) else {}

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        left_title = QLabel("Peers")
        left_title.setStyleSheet("font-weight: 600; font-size: 16px;")

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search peers")

        self.refresh_peers_button = QPushButton("Refresh Peers")
        self.probe_now_button = QPushButton("Probe Availability")
        self.peer_list = QListWidget()

        left_layout.addWidget(left_title)
        left_layout.addWidget(self.search_input)
        left_layout.addWidget(self.refresh_peers_button)
        left_layout.addWidget(self.probe_now_button)
        left_layout.addWidget(self.peer_list, stretch=1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        self.peer_title = QLabel("No peer selected")
        self.peer_title.setStyleSheet("font-weight: 600; font-size: 16px;")

        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.ping_button = QPushButton("Ping")
        self.rtc_connect_button = QPushButton("RTC Connect")
        self.rtc_accept_button = QPushButton("RTC Accept")
        self.rtc_status_button = QPushButton("RTC Status")

        header_layout.addWidget(self.peer_title, stretch=1)
        header_layout.addWidget(self.start_button)
        header_layout.addWidget(self.stop_button)
        header_layout.addWidget(self.ping_button)
        header_layout.addWidget(self.rtc_connect_button)
        header_layout.addWidget(self.rtc_accept_button)
        header_layout.addWidget(self.rtc_status_button)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)

        composer = QWidget()
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(0, 0, 0, 0)
        composer_layout.setSpacing(6)

        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Type a message")
        self.send_message_button = QPushButton("Send")
        self.send_file_button = QPushButton("Send File")

        composer_layout.addWidget(self.message_input, stretch=1)
        composer_layout.addWidget(self.send_file_button)
        composer_layout.addWidget(self.send_message_button)

        rtc_test = QWidget()
        rtc_test_layout = QHBoxLayout(rtc_test)
        rtc_test_layout.setContentsMargins(0, 0, 0, 0)
        rtc_test_layout.setSpacing(6)

        self.rtc_test_input = QLineEdit()
        self.rtc_test_input.setPlaceholderText("WebRTC test payload")
        self.rtc_test_button = QPushButton("RTC Test")
        rtc_test_layout.addWidget(self.rtc_test_input, stretch=1)
        rtc_test_layout.addWidget(self.rtc_test_button)

        debug_label = QLabel("Debug Log")
        debug_label.setStyleSheet("font-weight: 600;")
        self.debug_output = QTextEdit()
        self.debug_output.setReadOnly(True)

        right_layout.addWidget(header)
        right_layout.addWidget(self.chat_view, stretch=1)
        right_layout.addWidget(composer)
        right_layout.addWidget(rtc_test)
        right_layout.addWidget(debug_label)
        right_layout.addWidget(self.debug_output, stretch=1)

        main_layout.addWidget(left, stretch=3)
        main_layout.addWidget(right, stretch=8)

        self.setStyleSheet(
            """
            QWidget { font-size: 13px; }
            QTextBrowser, QTextEdit, QListWidget {
                border: 1px solid #d0d0d0;
                border-radius: 8px;
                background: #ffffff;
            }
            QPushButton {
                border: 1px solid #bfc7d1;
                border-radius: 8px;
                padding: 6px 10px;
                background: #f4f7fb;
            }
            QPushButton:disabled {
                color: #919191;
                background: #efefef;
            }
            QLineEdit {
                border: 1px solid #d0d0d0;
                border-radius: 8px;
                padding: 6px;
                background: #ffffff;
            }
            """
        )

    def _wire_events(self) -> None:
        self.start_button.clicked.connect(self.start_backend)
        self.stop_button.clicked.connect(self.stop_backend)

        self.send_message_button.clicked.connect(self.send_message)
        self.send_file_button.clicked.connect(self.send_file)
        self.message_input.returnPressed.connect(self.send_message)

        self.ping_button.clicked.connect(lambda: self._request_ping("manual"))
        self.rtc_connect_button.clicked.connect(self.send_rtc_connect)
        self.rtc_accept_button.clicked.connect(self.send_rtc_accept)
        self.rtc_status_button.clicked.connect(self.send_rtc_status)
        self.rtc_test_button.clicked.connect(self.send_rtc_test)

        self.peer_list.currentItemChanged.connect(self._on_peer_selected)
        self.search_input.textChanged.connect(self._filter_peers)
        self.refresh_peers_button.clicked.connect(self._refresh_peers)
        self.probe_now_button.clicked.connect(self._enqueue_full_probe)

    def _populate_peer_list(self) -> None:
        previous_peer = self.current_peer
        self.peer_list.clear()

        status_label = {
            "online": "[online]",
            "checking": "[checking]",
            "offline": "[offline]",
            "unknown": "[unknown]",
        }

        for peer_name, peer_cfg in self.peer_map.items():
            onion = str(peer_cfg.get("onion", ""))
            subtitle = onion if len(onion) < 40 else onion[:40] + "..."
            status = self.peer_status.get(peer_name, "unknown")
            rtt = self.peer_rtt_ms.get(peer_name, "")
            second = (status_label.get(status, "[unknown]") + (f" {rtt}" if rtt else "")).strip()
            item = QListWidgetItem(f"{peer_name} {second}\n{subtitle}")
            item.setData(256, peer_name)
            self.peer_list.addItem(item)

        if self.peer_list.count() > 0:
            row_to_select = 0
            if previous_peer:
                for row in range(self.peer_list.count()):
                    item = self.peer_list.item(row)
                    if item is not None and str(item.data(256) or "") == previous_peer:
                        row_to_select = row
                        break
            self.peer_list.setCurrentRow(row_to_select)

    def _refresh_peers(self) -> None:
        self.cfg = self._load_config()
        self.peer_map = self._peers_from_cfg(self.cfg)
        for name in self.peer_map.keys():
            self.chat_history.setdefault(name, [])
            self.peer_status.setdefault(name, "unknown")
        self._populate_peer_list()
        self._append_debug("[gui] peer list reloaded from config")
        self._enqueue_full_probe()

    def _set_peer_status(self, peer_name: str, status: str, rtt_ms: str = "") -> None:
        if peer_name not in self.peer_map:
            return
        self.peer_status[peer_name] = status
        if rtt_ms:
            self.peer_rtt_ms[peer_name] = rtt_ms
        elif status != "online":
            self.peer_rtt_ms.pop(peer_name, None)
        self._populate_peer_list()

    def _enqueue_full_probe(self) -> None:
        if not self.is_running():
            return
        for peer_name in self.peer_map.keys():
            if self.peer_status.get(peer_name) != "online":
                self._set_peer_status(peer_name, "checking")
            if peer_name != self.active_ping_peer and peer_name not in self.ping_queue:
                self.ping_queue.append(peer_name)
        if self.ping_queue and not self.ping_worker_timer.isActive():
            self.ping_worker_timer.start()

    def _process_ping_queue(self) -> None:
        if not self.is_running():
            self.ping_worker_timer.stop()
            self.ping_queue.clear()
            self.active_ping_peer = ""
            self.active_ping_request_id = 0
            return

        now_ms = int(time.time() * 1000)
        if self.active_ping_peer:
            if now_ms - self.active_ping_started_ms > 12000:
                timed_out_peer = self.active_ping_peer
                self.active_ping_peer = ""
                self.active_ping_started_ms = 0
                self.active_ping_request_id = 0
                self._set_peer_status(timed_out_peer, "offline")
                self._append_debug(f"[presence] ping timeout for {timed_out_peer}")
            return

        if not self.ping_queue:
            self.ping_worker_timer.stop()
            return

        peer_name = self.ping_queue.pop(0)
        request_id = self.next_ping_request_id
        self.next_ping_request_id += 1
        self.active_ping_peer = peer_name
        self.active_ping_request_id = request_id
        self.active_ping_started_ms = now_ms
        self._run_backend_task(
            "ping",
            self._must_backend().ping_peer,
            peer_name,
            context={"peer": peer_name, "purpose": "presence", "request_id": request_id},
        )

    def _filter_peers(self, text: str) -> None:
        text_lower = text.strip().lower()
        for row in range(self.peer_list.count()):
            item = self.peer_list.item(row)
            if item is None:
                continue
            peer_name = str(item.data(256) or "")
            visible = text_lower in peer_name.lower() if text_lower else True
            item.setHidden(not visible)

    def _on_peer_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.current_peer = ""
            self.peer_title.setText("No peer selected")
            self.chat_view.clear()
            return

        self.current_peer = str(current.data(256) or "")
        self.peer_title.setText(self.current_peer)
        self._render_chat()

    def _render_chat(self) -> None:
        if not self.current_peer:
            self.chat_view.clear()
            return

        items = self.chat_history.get(self.current_peer, [])
        html_parts = ["<div style='font-family: Segoe UI; font-size: 13px;'>"]
        for kind, text in items:
            safe = html.escape(text)
            if kind == "out":
                html_parts.append(
                    "<div style='text-align:right; margin:8px 0;'>"
                    "<span style='display:inline-block; background:#d9ecff; border-radius:10px; padding:8px 10px;'>"
                    f"{safe}</span></div>"
                )
            elif kind == "in":
                html_parts.append(
                    "<div style='text-align:left; margin:8px 0;'>"
                    "<span style='display:inline-block; background:#f1f3f5; border-radius:10px; padding:8px 10px;'>"
                    f"{safe}</span></div>"
                )
            else:
                html_parts.append(
                    "<div style='text-align:center; margin:6px 0; color:#666;'>"
                    f"{safe}</div>"
                )
        html_parts.append("</div>")
        self.chat_view.setHtml("".join(html_parts))
        self.chat_view.verticalScrollBar().setValue(self.chat_view.verticalScrollBar().maximum())

    def _append_chat(self, peer_name: str, kind: str, text: str) -> None:
        self.chat_history.setdefault(peer_name, []).append((kind, text))
        if self.current_peer == peer_name:
            self._render_chat()

    def _append_debug(self, text: str) -> None:
        self.debug_output.moveCursor(self.debug_output.textCursor().MoveOperation.End)
        self.debug_output.insertPlainText(text + "\n")
        self.debug_output.moveCursor(self.debug_output.textCursor().MoveOperation.End)

    def is_running(self) -> bool:
        return self.backend is not None

    def _must_backend(self) -> MessengerApp:
        if self.backend is None:
            raise RuntimeError("Backend is not running")
        return self.backend

    def _run_backend_task(
        self,
        task_name: str,
        func,
        *args,
        context: dict[str, Any] | None = None,
    ) -> None:
        def target() -> None:
            try:
                result = func(*args)
                self.task_done_signal.emit(task_name, True, context or {}, result, "")
            except Exception as exc:
                self.task_done_signal.emit(task_name, False, context or {}, None, str(exc))

        thread = threading.Thread(target=target, daemon=True)
        thread.start()

    def start_backend(self) -> None:
        if self.is_running():
            return

        self.cfg = self._load_config()
        self.peer_map = self._peers_from_cfg(self.cfg)
        self._populate_peer_list()

        try:
            self.backend = MessengerApp(
                self.cfg,
                on_event=lambda event_type, payload: self.backend_event_signal.emit(event_type, payload),
                on_log=lambda message: self.backend_log_signal.emit(message),
            )
            self.backend.start()
        except Exception as exc:
            self.backend = None
            QMessageBox.critical(self, "Error", f"Failed to start backend: {exc}")
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._append_debug("[gui] backend started")

        self._enqueue_full_probe()
        self.probe_interval_timer.start()

    def stop_backend(self) -> None:
        if self.backend is None:
            return

        try:
            self.backend.stop()
        except Exception as exc:
            self._append_debug(f"[error] backend stop failed: {exc}")

        self.backend = None
        self.probe_interval_timer.stop()
        self.ping_worker_timer.stop()
        self.ping_queue.clear()
        self.active_ping_peer = ""
        self.active_ping_started_ms = 0
        self.active_ping_request_id = 0

        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._append_debug("[gui] backend stopped")

    def _on_backend_event(self, event_type: str, payload: dict) -> None:
        if event_type == "text_received":
            sender_id = str(payload.get("sender_id", ""))
            text = str(payload.get("text", ""))
            peer_name = self._map_sender_to_peer(sender_id)
            self._append_chat(peer_name, "in", text)
            self._set_peer_status(peer_name, "online")
            return

        if event_type == "ping_result":
            peer_name = str(payload.get("peer_name", ""))
            ok = bool(payload.get("ok", False))
            if peer_name:
                if ok:
                    rtt = float(payload.get("rtt_ms", 0.0))
                    self._set_peer_status(peer_name, "online", rtt_ms=f"{rtt:.1f} ms")
                else:
                    self._set_peer_status(peer_name, "offline")
            return

        if event_type == "rtc_offer_received":
            peer_name = str(payload.get("peer_name", ""))
            if peer_name:
                self._append_chat(peer_name, "sys", "Incoming RTC offer. Click RTC Accept to continue.")
            return

        if event_type in {
            "rtc_data_open",
            "rtc_data_closed",
            "rtc_answer_applied",
            "rtc_offer_sent",
            "rtc_answer_sent",
            "rtc_test_received",
            "file_sent",
            "file_received",
            "file_error",
            "file_ack",
        }:
            peer_name = str(payload.get("peer_name", ""))
            if peer_name:
                self._append_chat(peer_name, "sys", f"{event_type}: {payload}")

    def _on_task_done(self, task_name: str, ok: bool, context_obj: object, result: object, error: str) -> None:
        context = context_obj if isinstance(context_obj, dict) else {}
        peer_name = str(context.get("peer", "")) if context else ""
        purpose = str(context.get("purpose", "")) if context else ""
        request_id = int(context.get("request_id", 0)) if context else 0

        if task_name == "ping":
            # Presence pipeline completion is authoritative for timeout bookkeeping.
            if purpose == "presence" and request_id and request_id == self.active_ping_request_id:
                self.active_ping_peer = ""
                self.active_ping_started_ms = 0
                self.active_ping_request_id = 0

            if ok and peer_name:
                try:
                    rtt = float(result) if result is not None else 0.0
                except (TypeError, ValueError):
                    rtt = 0.0
                self._set_peer_status(peer_name, "online", rtt_ms=f"{rtt:.1f} ms")
            elif peer_name:
                self._set_peer_status(peer_name, "offline")

            if not ok and purpose == "manual":
                QMessageBox.warning(self, "Ping Failed", error)
            return

        if task_name == "rtc_status":
            if not ok:
                self._append_debug(f"[error] rtc status failed: {error}")
                return
            status = result if isinstance(result, dict) else {}
            sessions = status.get("sessions", {}) if isinstance(status, dict) else {}
            if peer_name and isinstance(sessions, dict) and peer_name in sessions:
                self._append_chat(peer_name, "sys", f"RTC status: {sessions[peer_name]}")
            elif peer_name:
                self._append_chat(peer_name, "sys", "RTC status: no active session")
            return

        if not ok:
            self._append_debug(f"[error] {task_name} failed: {error}")
            if task_name == "send_text" and peer_name:
                self._append_chat(peer_name, "sys", f"Send failed: {error}")

    def _map_sender_to_peer(self, sender_id: str) -> str:
        if sender_id in self.peer_map:
            return sender_id
        for name, peer in self.peer_map.items():
            if str(peer.get("sender_id", "")) == sender_id:
                return name
        return sender_id

    def require_peer(self) -> str:
        if not self.current_peer:
            QMessageBox.warning(self, "No Peer", "Select a peer from the list.")
            return ""
        return self.current_peer

    def _request_ping(self, purpose: str) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        self._set_peer_status(peer, "checking")
        self._run_backend_task("ping", self._must_backend().ping_peer, peer, context={"peer": peer, "purpose": purpose})

    def send_message(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        text = self.message_input.text().strip()
        if not text:
            return

        self._append_chat(peer, "out", text)
        self._run_backend_task("send_text", self._must_backend().send_text, peer, text, context={"peer": peer})
        self.message_input.clear()

    def send_file(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        path, _ = QFileDialog.getOpenFileName(self, "Choose file to send")
        if not path:
            return

        self._run_backend_task("send_file", self._must_backend().send_file, peer, path, context={"peer": peer})
        self._append_chat(peer, "out", f"Sent file: {Path(path).name}")

    def send_rtc_connect(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        self._run_backend_task("rtc_connect", self._must_backend().rtc_connect, peer, context={"peer": peer})

    def send_rtc_accept(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        self._run_backend_task("rtc_accept", self._must_backend().rtc_accept, peer, context={"peer": peer})

    def send_rtc_status(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        self._run_backend_task("rtc_status", self._must_backend().rtc_status, peer, context={"peer": peer})

    def send_rtc_test(self) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        peer = self.require_peer()
        if not peer:
            return

        text = self.rtc_test_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Missing Data", "RTC test payload is required.")
            return

        self._run_backend_task("rtc_test", self._must_backend().rtc_test, peer, text, context={"peer": peer})
        self.rtc_test_input.clear()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.stop_backend()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    win = MessengerGui()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
