"""
강의 이모지 오버레이 — 화면 위에 직접 이모지를 띄우는 투명 오버레이 앱
실행 전에 app.py(서버)가 먼저 실행되어 있어야 합니다.

사용법:
    python overlay.py                                        # 접속 정보 입력창 표시
    python overlay.py --room ABC123 --password mypass        # 인수로 직접 지정 (창 생략)
    python overlay.py --host 192.168.x.x --room ABC123 --password mypass
    python overlay.py --host example.com --ssl --room ABC123 --password mypass
"""

import sys
import os
import asyncio
import json
import random
import threading
import argparse
import urllib.request
import urllib.error

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QDialogButtonBox, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint, QEasingCurve, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QPixmap, QColor, QPainter, QIcon

import websockets


# ── 접속 정보 입력 다이얼로그 ─────────────────────────────────────────────────
class _ConnectDialog(QDialog):
    def __init__(self, room: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("강의 이모지 오버레이")
        self.setMinimumWidth(340)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(28, 28, 28, 20)

        title = QLabel("🎓 수업 연결")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._room_input = QLineEdit()
        self._room_input.setPlaceholderText("예: ABC123")
        self._room_input.setMaxLength(6)
        if room:
            self._room_input.setText(room.upper())
        self._room_input.textEdited.connect(self._force_upper)
        form.addRow("방 코드:", self._room_input)

        self._pw_input = QLineEdit()
        self._pw_input.setPlaceholderText("강의자 비밀번호")
        self._pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        if password:
            self._pw_input.setText(password)
        self._pw_input.returnPressed.connect(self.accept)
        form.addRow("비밀번호:", self._pw_input)

        layout.addLayout(form)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("연결")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        (self._pw_input if room else self._room_input).setFocus()

    def _force_upper(self, text: str):
        upper = text.upper()
        if text != upper:
            pos = self._room_input.cursorPosition()
            self._room_input.setText(upper)
            self._room_input.setCursorPosition(pos)

    def get_values(self) -> tuple[str, str]:
        return self._room_input.text().strip(), self._pw_input.text()


# ── Qt 시그널 브릿지 (asyncio 스레드 → Qt 메인 스레드) ──────────────────────
class _Bridge(QObject):
    emoji_received    = pyqtSignal(str)
    question_received = pyqtSignal(str, str)  # text, id
    bubble_deleted    = pyqtSignal(str)        # id
    room_not_found    = pyqtSignal()           # 잘못된 방 코드 (4004)
    auth_failed       = pyqtSignal()           # 토큰 만료 (4001)


bridge = _Bridge()


# ── 투명 오버레이 윈도우 ──────────────────────────────────────────────────────
class EmojiOverlay(QWidget):
    def __init__(self):
        super().__init__()

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._bubbles: dict[str, QLabel] = {}

        bridge.emoji_received.connect(self._spawn)
        bridge.question_received.connect(self._spawn_bubble)
        bridge.bubble_deleted.connect(self._delete_bubble)

    def _spawn(self, emoji: str):
        label = QLabel(emoji, self)
        font = QFont()
        font.setPointSize(52)
        label.setFont(font)
        label.setStyleSheet("background: transparent;")
        label.adjustSize()

        x     = random.randint(40, max(41, self.width() - label.width() - 40))
        start = QPoint(x, self.height() + 20)
        end   = QPoint(x + random.randint(-40, 40), -label.height() - 20)

        label.move(start)
        label.show()
        label.raise_()

        anim = QPropertyAnimation(label, b"pos")
        anim.setDuration(random.randint(3200, 4400))
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.finished.connect(label.deleteLater)
        anim.start()

        label._anim = anim  # GC 방지

    def _spawn_bubble(self, text: str, bubble_id: str):
        MARGIN_RIGHT  = 32
        MARGIN_BOTTOM = 32
        BUBBLE_WIDTH  = 300
        GAP           = 10

        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setFixedWidth(BUBBLE_WIDTH)
        label.setFont(QFont("sans-serif", 13))
        label.setStyleSheet("""
            QLabel {
                background: rgba(255, 255, 255, 190);
                border-radius: 16px;
                padding: 10px 14px;
                color: #111111;
            }
        """)
        label.adjustSize()
        new_h = label.height()

        for b in self._bubbles.values():
            b.move(b.x(), b.y() - new_h - GAP)

        x = self.width() - MARGIN_RIGHT - BUBBLE_WIDTH
        y = self.height() - MARGIN_BOTTOM - new_h
        label.move(x, y)
        label.show()
        label.raise_()
        self._bubbles[bubble_id] = label

    def show_room_code(self, room_code: str):
        label = QLabel(f"방 코드  {room_code}", self)
        font = QFont("monospace", 12)
        font.setBold(True)
        label.setFont(font)
        label.setStyleSheet("""
            QLabel {
                background: rgba(0, 0, 0, 150);
                color: rgba(255, 255, 255, 210);
                border-radius: 8px;
                padding: 6px 14px;
            }
        """)
        label.adjustSize()
        x = self.width() - label.width() - 20
        label.move(x, 52)
        label.show()
        label.raise_()

    def _delete_bubble(self, bubble_id: str):
        label = self._bubbles.pop(bubble_id, None)
        if not label:
            return

        deleted_y = label.y()
        deleted_h = label.height()
        label.deleteLater()

        # 삭제된 말풍선보다 위에 있던 것들을 아래로 내림
        for b in self._bubbles.values():
            if b.y() < deleted_y:
                b.move(b.x(), b.y() + deleted_h + 10)  # GAP=10


# ── Linux 전용 설정 ───────────────────────────────────────────────────────────
def _setup_linux(overlay: QWidget):
    """
    Wayland는 앱의 Z-order 제어를 막으므로 XWayland(xcb)로 강제 실행.
    X11 _NET_WM_STATE_ABOVE 속성을 WM에 직접 전달해 항상 최상단 유지.
    """
    try:
        from Xlib import display as xdisplay, X
        from Xlib.protocol import event as xevent

        def _apply():
            d = xdisplay.Display()
            root = d.screen().root
            win_id = int(overlay.winId())
            window = d.create_resource_object('window', win_id)

            _NET_WM_STATE       = d.intern_atom('_NET_WM_STATE')
            _NET_WM_STATE_ABOVE = d.intern_atom('_NET_WM_STATE_ABOVE')

            ev = xevent.ClientMessage(
                window=window,
                client_type=_NET_WM_STATE,
                data=(32, [1, _NET_WM_STATE_ABOVE, 0, 1, 0]),
            )
            root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            d.flush()

        QTimer.singleShot(200, _apply)
    except ImportError:
        print("ℹ️  python-xlib 미설치 — pip install python-xlib")


# ── macOS 전용 설정 ───────────────────────────────────────────────────────────
def _setup_macos(overlay: QWidget):
    """
    - setIgnoresMouseEvents_: 클릭이 오버레이를 뚫고 뒤 창으로 전달
    - setHidesOnDeactivate_:  NSPanel 기본값(True)을 끄면 앱이 포커스를 잃어도 창이 유지됨
    - setLevel_(25):          NSStatusWindowLevel — 어떤 앱이 활성화돼도 항상 최상단
    - setCollectionBehavior_: 모든 스페이스에서 유지, 위치 고정, Cmd+Tab 제외
    """
    try:
        from AppKit import NSApp

        def _apply():
            for win in NSApp.windows():
                win.setIgnoresMouseEvents_(True)
                win.setHidesOnDeactivate_(False)
                win.setLevel_(25)
                win.setCollectionBehavior_(
                    (1 << 0) |  # CanJoinAllSpaces
                    (1 << 4) |  # Stationary
                    (1 << 6)    # IgnoresCycle
                )

        QTimer.singleShot(100, _apply)

    except ImportError:
        print("ℹ️  PyObjC 미설치 — pip install pyobjc-framework-Cocoa")


# ── WebSocket 클라이언트 ──────────────────────────────────────────────────────
async def _ws_loop(ws_url: str):
    print(f"서버 연결 중… ({ws_url})")
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                print("✅ 서버 연결됨 — 오버레이 활성")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") == "emoji":
                            bridge.emoji_received.emit(msg["emoji"])
                        elif msg.get("type") == "question":
                            bridge.question_received.emit(msg["text"], msg.get("id", ""))
                        elif msg.get("type") == "delete_bubble":
                            bridge.bubble_deleted.emit(msg.get("id", ""))
                    except json.JSONDecodeError:
                        pass
        except websockets.exceptions.ConnectionClosedError as e:
            code = getattr(e.rcvd, 'code', None)
            if code == 4004:
                print("❌ 방 코드를 찾을 수 없습니다.")
                bridge.room_not_found.emit()
                return
            if code == 4001:
                print("❌ 인증 실패 — 토큰이 만료되었습니다.")
                bridge.auth_failed.emit()
                return
            print(f"⚠️  연결 끊김 ({e}) — 2초 후 재연결…")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"⚠️  연결 끊김 ({e}) — 2초 후 재연결…")
            await asyncio.sleep(2)


def _start_ws_thread(ws_url: str):
    def run():
        asyncio.run(_ws_loop(ws_url))
    t = threading.Thread(target=run, daemon=True)
    t.start()


# ── 진입점 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="강의 이모지 오버레이")
    parser.add_argument("--host",     default="localhost", help="서버 호스트 (기본값: localhost)")
    parser.add_argument("--port",     default=None, type=int, help="서버 포트 (로컬 기본값: 8000)")
    parser.add_argument("--ssl",      action="store_true",  help="WSS 사용 (클라우드 서버 접속 시)")
    parser.add_argument("--room",     default="",           help="방 코드 (예: ABC123)")
    parser.add_argument("--password", default="",           help="강의자 비밀번호")
    args = parser.parse_args()

    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # XWayland 강제 사용

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # ── 접속 정보 결정 ──────────────────────────────────────────────────────────
    room_code = args.room.strip().upper()
    password  = args.password

    if not room_code or not password:
        dlg = _ConnectDialog(room=room_code, password=password)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        room_code, password = dlg.get_values()
        if not room_code or not password:
            QMessageBox.warning(None, "입력 오류", "방 코드와 비밀번호를 모두 입력하세요.")
            sys.exit(0)

    # ── URL 구성 ────────────────────────────────────────────────────────────────
    port_part   = f":{args.port}" if args.port else ("" if args.ssl else ":8000")
    host_base   = f"{args.host}{port_part}"
    http_scheme = "https" if args.ssl else "http"
    ws_scheme   = "wss"   if args.ssl else "ws"

    # ── HTTP 로그인 → 토큰 취득 ─────────────────────────────────────────────────
    api_url = f"{http_scheme}://{host_base}/api/room/{room_code}/login"
    req = urllib.request.Request(
        api_url,
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            token = json.loads(resp.read())["token"]
    except urllib.error.HTTPError as e:
        if e.code == 401:
            QMessageBox.critical(None, "인증 오류", "비밀번호가 틀렸습니다.")
        elif e.code == 404:
            QMessageBox.critical(None, "방 오류",
                f"방 코드 '{room_code}'를 찾을 수 없습니다.\n강사가 수업을 먼저 시작했는지 확인하세요.")
        else:
            QMessageBox.critical(None, "서버 오류", f"서버 응답 오류: {e.code}")
        sys.exit(1)
    except Exception as e:
        QMessageBox.critical(None, "연결 오류", f"서버에 연결할 수 없습니다.\n{e}")
        sys.exit(1)

    ws_url = f"{ws_scheme}://{host_base}/ws/presenter?room={room_code}&token={token}"

    overlay = EmojiOverlay()
    overlay.show()
    overlay.show_room_code(room_code)

    if sys.platform == "darwin":
        _setup_macos(overlay)
    elif sys.platform.startswith("linux"):
        _setup_linux(overlay)

    # ── 트레이 아이콘 ──────────────────────────────────────────────────────────
    icon_pixmap = QPixmap(32, 32)
    icon_pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(icon_pixmap)
    painter.setFont(QFont("serif", 22))
    painter.drawText(icon_pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "🎉")
    painter.end()

    tray = QSystemTrayIcon(QIcon(icon_pixmap), app)
    menu = QMenu()
    menu.addAction("종료").triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("강의 이모지 오버레이")
    tray.show()

    def _on_room_not_found():
        QMessageBox.critical(None, "방 코드 오류",
            f"방 코드 '{room_code}'를 찾을 수 없습니다.\n강사가 수업을 먼저 시작했는지 확인하세요.")
        app.quit()

    def _on_auth_failed():
        QMessageBox.critical(None, "인증 만료",
            "서버가 재시작되어 인증이 만료되었습니다.\n오버레이를 다시 실행하세요.")
        app.quit()

    bridge.room_not_found.connect(_on_room_not_found)
    bridge.auth_failed.connect(_on_auth_failed)

    _start_ws_thread(ws_url)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
