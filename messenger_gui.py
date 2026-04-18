import sys
from pathlib import Path

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class MessengerGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Messenger PoC GUI")
        self.resize(980, 720)

        self.process: QProcess | None = None
        self.python_path = Path(sys.executable)
        self.script_path = Path(__file__).parent / "messenger.py"

        self.output = QTextEdit()
        self.output.setReadOnly(True)

        self.peer_input = QLineEdit("peer2")
        self.msg_input = QLineEdit()
        self.msg_input.setPlaceholderText("Type message text")

        self.rtc_test_input = QLineEdit()
        self.rtc_test_input.setPlaceholderText("RTC test text")

        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("Path to file")

        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText("Any raw command, e.g. /rtc status")

        self.start_button = QPushButton("Start Backend")
        self.stop_button = QPushButton("Stop Backend")
        self.stop_button.setEnabled(False)

        self._build_ui()
        self._wire_events()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)

        process_box = QGroupBox("Backend Process")
        process_layout = QHBoxLayout(process_box)
        process_layout.addWidget(self.start_button)
        process_layout.addWidget(self.stop_button)

        peer_box = QGroupBox("Peer")
        peer_layout = QHBoxLayout(peer_box)
        peer_layout.addWidget(QLabel("Peer Name:"))
        peer_layout.addWidget(self.peer_input)

        text_box = QGroupBox("Text Messaging")
        text_layout = QHBoxLayout(text_box)
        text_layout.addWidget(self.msg_input)
        send_msg_button = QPushButton("Send /msg")
        text_layout.addWidget(send_msg_button)

        rtc_box = QGroupBox("WebRTC")
        rtc_layout = QGridLayout(rtc_box)
        rtc_connect_button = QPushButton("/rtc connect")
        rtc_accept_button = QPushButton("/rtc accept")
        rtc_status_button = QPushButton("/rtc status")
        rtc_test_button = QPushButton("/rtc test")
        rtc_layout.addWidget(rtc_connect_button, 0, 0)
        rtc_layout.addWidget(rtc_accept_button, 0, 1)
        rtc_layout.addWidget(rtc_status_button, 0, 2)
        rtc_layout.addWidget(self.rtc_test_input, 1, 0, 1, 2)
        rtc_layout.addWidget(rtc_test_button, 1, 2)

        file_box = QGroupBox("File Transfer")
        file_layout = QHBoxLayout(file_box)
        browse_button = QPushButton("Browse")
        send_file_button = QPushButton("Send /file")
        file_layout.addWidget(self.file_input)
        file_layout.addWidget(browse_button)
        file_layout.addWidget(send_file_button)

        util_box = QGroupBox("Utilities")
        util_layout = QHBoxLayout(util_box)
        peers_button = QPushButton("/peers")
        ping_button = QPushButton("/ping")
        help_button = QPushButton("/help")
        util_layout.addWidget(peers_button)
        util_layout.addWidget(ping_button)
        util_layout.addWidget(help_button)

        raw_box = QGroupBox("Raw Command")
        raw_layout = QHBoxLayout(raw_box)
        send_raw_button = QPushButton("Send")
        raw_layout.addWidget(self.raw_input)
        raw_layout.addWidget(send_raw_button)

        layout.addWidget(process_box)
        layout.addWidget(peer_box)
        layout.addWidget(text_box)
        layout.addWidget(rtc_box)
        layout.addWidget(file_box)
        layout.addWidget(util_box)
        layout.addWidget(raw_box)
        layout.addWidget(QLabel("Output"))
        layout.addWidget(self.output, stretch=1)

        self.send_msg_button = send_msg_button
        self.rtc_connect_button = rtc_connect_button
        self.rtc_accept_button = rtc_accept_button
        self.rtc_status_button = rtc_status_button
        self.rtc_test_button = rtc_test_button
        self.browse_button = browse_button
        self.send_file_button = send_file_button
        self.peers_button = peers_button
        self.ping_button = ping_button
        self.help_button = help_button
        self.send_raw_button = send_raw_button

    def _wire_events(self) -> None:
        self.start_button.clicked.connect(self.start_backend)
        self.stop_button.clicked.connect(self.stop_backend)

        self.send_msg_button.clicked.connect(self.send_msg)
        self.rtc_connect_button.clicked.connect(lambda: self.send_peer_command("/rtc connect"))
        self.rtc_accept_button.clicked.connect(lambda: self.send_peer_command("/rtc accept"))
        self.rtc_status_button.clicked.connect(self.send_rtc_status)
        self.rtc_test_button.clicked.connect(self.send_rtc_test)

        self.browse_button.clicked.connect(self.pick_file)
        self.send_file_button.clicked.connect(self.send_file)

        self.peers_button.clicked.connect(lambda: self.send_command("/peers"))
        self.ping_button.clicked.connect(lambda: self.send_peer_command("/ping"))
        self.help_button.clicked.connect(lambda: self.send_command("/help"))

        self.send_raw_button.clicked.connect(self.send_raw)
        self.raw_input.returnPressed.connect(self.send_raw)

    def append_output(self, text: str) -> None:
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)

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
        self.append_output("[gui] backend started\n")

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
        self.append_output("[gui] backend stopped\n")

    def _read_stdout(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.append_output(data)

    def _read_stderr(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self.append_output(data)

    def send_command(self, command: str) -> None:
        if not self.is_running():
            QMessageBox.information(self, "Not Running", "Start backend first.")
            return

        assert self.process is not None
        self.process.write((command + "\n").encode("utf-8"))
        self.append_output(f"[gui] -> {command}\n")

    def send_peer_command(self, prefix: str) -> None:
        peer = self.peer_input.text().strip()
        if not peer:
            QMessageBox.warning(self, "Missing Peer", "Peer name is required.")
            return
        self.send_command(f"{prefix} {peer}")

    def send_msg(self) -> None:
        peer = self.peer_input.text().strip()
        text = self.msg_input.text().strip()
        if not peer or not text:
            QMessageBox.warning(self, "Missing Data", "Peer and message text are required.")
            return
        self.send_command(f"/msg {peer} {text}")
        self.msg_input.clear()

    def send_rtc_status(self) -> None:
        peer = self.peer_input.text().strip()
        if peer:
            self.send_command(f"/rtc status {peer}")
        else:
            self.send_command("/rtc status")

    def send_rtc_test(self) -> None:
        peer = self.peer_input.text().strip()
        text = self.rtc_test_input.text().strip()
        if not peer or not text:
            QMessageBox.warning(self, "Missing Data", "Peer and test text are required.")
            return
        self.send_command(f"/rtc test {peer} {text}")
        self.rtc_test_input.clear()

    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose File")
        if path:
            self.file_input.setText(path)

    def send_file(self) -> None:
        peer = self.peer_input.text().strip()
        path = self.file_input.text().strip()
        if not peer or not path:
            QMessageBox.warning(self, "Missing Data", "Peer and file path are required.")
            return
        self.send_command(f"/file {peer} {path}")

    def send_raw(self) -> None:
        cmd = self.raw_input.text().strip()
        if not cmd:
            return
        self.send_command(cmd)
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
