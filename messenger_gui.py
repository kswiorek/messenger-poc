import html
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess
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

from messenger.config import load_config


BASE_CONFIG_PATH = Path("messenger_config.json")
LOCAL_CONFIG_PATH = Path("messenger_config.local.json")


MSG_IN_PATTERN = re.compile(r"\[msg\]\s+([^:]+):\s*(.*)")


class MessengerGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Messenger")
        self.resize(1120, 760)

        self.process: QProcess | None = None
        self.python_path = Path(sys.executable)
        self.script_path = Path(__file__).parent / "messenger.py"
        self.stdout_buffer = ""

        self.peer_map = self._load_peers_from_config()
        self.current_peer = ""
        self.chat_history: dict[str, list[tuple[str, str]]] = {name: [] for name in self.peer_map.keys()}

        self._build_ui()
        self._wire_events()
        self._populate_peer_list()

    def _load_peers_from_config(self) -> dict[str, dict]:
        try:
            cfg = load_config(BASE_CONFIG_PATH, LOCAL_CONFIG_PATH)
            peers = cfg.get("peers", {})
            if isinstance(peers, dict):
                return peers
        except Exception:
            pass
        return {}

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Left sidebar (peer list)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        left_title = QLabel("Peers")
        left_title.setStyleSheet("font-weight: 600; font-size: 16px;")

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search peers")

        self.refresh_peers_button = QPushButton("Refresh Peers")
        self.peer_list = QListWidget()

        left_layout.addWidget(left_title)
        left_layout.addWidget(self.search_input)
        left_layout.addWidget(self.refresh_peers_button)
        left_layout.addWidget(self.peer_list, stretch=1)

        # Right panel (conversation)
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
        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText("Advanced raw command, e.g. /rtc status peer2")
        self.send_raw_button = QPushButton("Send Raw")
        raw_row = QWidget()
        raw_row_layout = QHBoxLayout(raw_row)
        raw_row_layout.setContentsMargins(0, 0, 0, 0)
        raw_row_layout.setSpacing(6)
        raw_row_layout.addWidget(self.raw_input, stretch=1)
        raw_row_layout.addWidget(self.send_raw_button)

        right_layout.addWidget(header)
        right_layout.addWidget(self.chat_view, stretch=1)
        right_layout.addWidget(composer)
        right_layout.addWidget(rtc_test)
        right_layout.addWidget(debug_label)
        right_layout.addWidget(self.debug_output, stretch=1)
        right_layout.addWidget(raw_row)

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

        self.ping_button.clicked.connect(lambda: self.send_peer_command("/ping"))
        self.rtc_connect_button.clicked.connect(lambda: self.send_peer_command("/rtc connect"))
        self.rtc_accept_button.clicked.connect(lambda: self.send_peer_command("/rtc accept"))
        self.rtc_status_button.clicked.connect(self.send_rtc_status)
        self.rtc_test_button.clicked.connect(self.send_rtc_test)

        self.send_raw_button.clicked.connect(self.send_raw)
        self.raw_input.returnPressed.connect(self.send_raw)

        self.peer_list.currentItemChanged.connect(self._on_peer_selected)
        self.search_input.textChanged.connect(self._filter_peers)
        self.refresh_peers_button.clicked.connect(self._refresh_peers)

    def _populate_peer_list(self) -> None:
        self.peer_list.clear()
        for peer_name, peer_cfg in self.peer_map.items():
            onion = str(peer_cfg.get("onion", ""))
            subtitle = onion if len(onion) < 40 else onion[:40] + "..."
            item = QListWidgetItem(f"{peer_name}\n{subtitle}")
            item.setData(256, peer_name)
            self.peer_list.addItem(item)

        if self.peer_list.count() > 0:
            self.peer_list.setCurrentRow(0)

    def _refresh_peers(self) -> None:
        self.peer_map = self._load_peers_from_config()
        for name in self.peer_map.keys():
            self.chat_history.setdefault(name, [])
        self._populate_peer_list()
        self._append_debug("[gui] peer list reloaded from config")

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
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start_backend(self) -> None:
        if self.is_running():
            return

        if not self.script_path.exists():
            QMessageBox.critical(self, "Error", f"Backend script not found: {self.script_path}")
            return

        self.process = QProcess(self)
        self.process.setProgram(str(self.python_path))
        self.process.setArguments([str(self.script_path)])
        self.process.setWorkingDirectory(str(self.script_path.parent))
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._on_process_finished)
        self.process.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._append_debug("[gui] backend started")

    def stop_backend(self) -> None:
        if not self.is_running():
            return

        self.send_command("/quit")
        assert self.process is not None
        if not self.process.waitForFinished(2000):
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()

    def _on_process_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._append_debug("[gui] backend stopped")

    def _handle_backend_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        msg_match = MSG_IN_PATTERN.match(stripped)
        if msg_match:
            sender_id = msg_match.group(1).strip()
            text = msg_match.group(2).strip()
            peer_name = self._map_sender_to_peer(sender_id)
            self._append_chat(peer_name, "in", text)
            return

        if stripped.startswith("[file]") or stripped.startswith("[rtc]") or stripped.startswith("[rtc-test]"):
            if self.current_peer:
                self._append_chat(self.current_peer, "sys", stripped)
            self._append_debug(stripped)
            return

        self._append_debug(stripped)

    def _read_stdout(self) -> None:
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.stdout_buffer += chunk.replace("\r", "")

        while "\n" in self.stdout_buffer:
            line, rest = self.stdout_buffer.split("\n", 1)
            self.stdout_buffer = rest
            self._handle_backend_line(line)

    def _read_stderr(self) -> None:
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            self._append_debug(f"[stderr] {line}")

    def _map_sender_to_peer(self, sender_id: str) -> str:
        if sender_id in self.peer_map:
            return sender_id
        for name, peer in self.peer_map.items():
            if str(peer.get("sender_id", "")) == sender_id:
                return name
        return sender_id

    def send_command(self, command: str) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        assert self.process is not None
        self.process.write((command + "\n").encode("utf-8"))
        self._append_debug(f"[gui] -> {command}")

    def require_peer(self) -> str:
        if not self.current_peer:
            QMessageBox.warning(self, "No Peer", "Select a peer from the list.")
            return ""
        return self.current_peer

    def send_peer_command(self, prefix: str) -> None:
        peer = self.require_peer()
        if not peer:
            return
        self.send_command(f"{prefix} {peer}")

    def send_message(self) -> None:
        peer = self.require_peer()
        if not peer:
            return

        text = self.message_input.text().strip()
        if not text:
            return

        self.send_command(f"/msg {peer} {text}")
        self._append_chat(peer, "out", text)
        self.message_input.clear()

    def send_file(self) -> None:
        peer = self.require_peer()
        if not peer:
            return

        path, _ = QFileDialog.getOpenFileName(self, "Choose file to send")
        if not path:
            return

        self.send_command(f"/file {peer} {path}")
        self._append_chat(peer, "out", f"Sent file: {Path(path).name}")

    def send_rtc_status(self) -> None:
        peer = self.require_peer()
        if not peer:
            return
        self.send_command(f"/rtc status {peer}")

    def send_rtc_test(self) -> None:
        peer = self.require_peer()
        if not peer:
            return
        text = self.rtc_test_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Missing Data", "RTC test payload is required.")
            return
        self.send_command(f"/rtc test {peer} {text}")
        self.rtc_test_input.clear()

    def send_raw(self) -> None:
        raw = self.raw_input.text().strip()
        if not raw:
            return
        self.send_command(raw)
        self.raw_input.clear()

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
