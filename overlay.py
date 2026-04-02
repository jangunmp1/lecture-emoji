"""
강의 이모지 오버레이 — 화면 위에 직접 이모지를 띄우는 투명 오버레이 앱
실행 전에 app.py(서버)가 먼저 실행되어 있어야 합니다.

사용법:
    python overlay.py
    python overlay.py --host 192.168.x.x  # 원격 서버에 연결할 때
"""

import sys
import os
import asyncio
import json
import random
import threading
import argparse

from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QSystemTrayIcon, QMenu
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint, QEasingCurve, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QPixmap, QColor, QPainter, QIcon

import websockets


# ── Qt 시그널 브릿지 (asyncio 스레드 → Qt 메인 스레드) ──────────────────────
class _Bridge(QObject):
    emoji_received = pyqtSignal(str)


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

        bridge.emoji_received.connect(self._spawn)

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
                    except json.JSONDecodeError:
                        pass
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
    parser.add_argument("--host", default="localhost", help="서버 호스트 (기본값: localhost)")
    parser.add_argument("--port", default=None, type=int, help="서버 포트 (로컬 기본값: 8000)")
    parser.add_argument("--ssl", action="store_true", help="WSS 사용 (클라우드 서버 접속 시)")
    args = parser.parse_args()

    scheme = "wss" if args.ssl else "ws"
    if args.port:
        ws_url = f"{scheme}://{args.host}:{args.port}/ws/presenter"
    elif args.ssl:
        ws_url = f"{scheme}://{args.host}/ws/presenter"   # 클라우드: 포트 생략 (443)
    else:
        ws_url = f"{scheme}://{args.host}:8000/ws/presenter"  # 로컬 기본값

    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # XWayland 강제 사용

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = EmojiOverlay()
    overlay.show()

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

    _start_ws_thread(ws_url)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
