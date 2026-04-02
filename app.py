import uvicorn
import json
import os
import socket
import hmac
import hashlib
import secrets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Cookie, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# ── 발표자 화면 암호 ──────────────────────────────────────────────────────────
_PASSWORD    = os.environ.get("PRESENTER_PASSWORD", "")
_SECRET      = secrets.token_hex(32)          # 서버 재시작마다 갱신
_COOKIE_NAME = "presenter_token"

def _make_token() -> str:
    return hmac.new(_SECRET.encode(), _PASSWORD.encode(), hashlib.sha256).hexdigest()

def _is_authorized(token: str | None) -> bool:
    if not _PASSWORD:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, _make_token())

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>강사 화면 로그인</title>
  <style>
    *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      background: #080812; color: #e2e8f0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
      border-radius: 24px; padding: 48px 40px; width: 100%; max-width: 360px;
      display: flex; flex-direction: column; gap: 20px; text-align: center;
    }}
    h1 {{ font-size: 1.3rem; color: #a78bfa; }}
    p  {{ font-size: 0.85rem; color: #64748b; }}
    input[type=password] {{
      width: 100%; background: rgba(255,255,255,0.05);
      border: 2px solid rgba(255,255,255,0.09); border-radius: 12px;
      padding: 12px 16px; color: #e2e8f0; font-size: 1rem; outline: none;
      transition: border-color 0.15s;
    }}
    input[type=password]:focus {{ border-color: #a78bfa; }}
    button {{
      width: 100%; background: rgba(167,139,250,0.2);
      border: 2px solid rgba(167,139,250,0.5); border-radius: 12px;
      padding: 12px; color: #a78bfa; font-size: 1rem; cursor: pointer;
      transition: background 0.15s;
    }}
    button:hover {{ background: rgba(167,139,250,0.35); }}
    .error {{ color: #f87171; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🔒 강사 화면</h1>
    <p>접근하려면 암호를 입력하세요.</p>
    {error}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{next}"/>
      <input type="password" name="password" placeholder="암호" autofocus/>
      <br/><br/>
      <button type="submit">입장</button>
    </form>
  </div>
</body>
</html>
"""


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class ConnectionManager:
    def __init__(self):
        self.presenters: list[WebSocket] = []
        self.students: list[WebSocket] = []
        self.bubbles: list[dict] = []  # {"id": ..., "text": ...}

    async def connect_presenter(self, ws: WebSocket):
        await ws.accept()
        self.presenters.append(ws)
        await ws.send_text(json.dumps({"type": "count", "count": len(self.students)}))
        for bubble in self.bubbles:
            await ws.send_text(json.dumps({"type": "question", "text": bubble["text"], "id": bubble["id"]}))

    async def connect_student(self, ws: WebSocket):
        await ws.accept()
        self.students.append(ws)
        await self._broadcast_count()

    async def disconnect(self, ws: WebSocket):
        if ws in self.presenters:
            self.presenters.remove(ws)
        if ws in self.students:
            self.students.remove(ws)
            await self._broadcast_count()

    async def send_emoji_to_presenters(self, emoji: str):
        dead = []
        for ws in self.presenters:
            try:
                await ws.send_text(json.dumps({"type": "emoji", "emoji": emoji}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.presenters.remove(ws)

    async def send_question_to_presenters(self, text: str, bubble_id: str):
        self.bubbles.append({"id": bubble_id, "text": text})
        dead = []
        for ws in self.presenters:
            try:
                await ws.send_text(json.dumps({"type": "question", "text": text, "id": bubble_id}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.presenters.remove(ws)

    async def broadcast_to_presenters(self, msg: dict):
        if msg.get("type") == "delete_bubble":
            self.bubbles = [b for b in self.bubbles if b["id"] != msg.get("id")]
        dead = []
        for ws in self.presenters:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.presenters.remove(ws)

    async def _broadcast_count(self):
        msg = json.dumps({"type": "count", "count": len(self.students)})
        for ws in self.presenters + self.students:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


manager = ConnectionManager()


@app.websocket("/ws/presenter")
async def presenter_ws(ws: WebSocket):
    await manager.connect_presenter(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "delete_bubble" and msg.get("id"):
                    await manager.broadcast_to_presenters({
                        "type": "delete_bubble",
                        "id": str(msg["id"])[:64],
                    })
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await manager.disconnect(ws)


@app.websocket("/ws/student")
async def student_ws(ws: WebSocket):
    await manager.connect_student(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "emoji" and msg.get("emoji"):
                    await manager.send_emoji_to_presenters(msg["emoji"])
                elif msg.get("type") == "question" and msg.get("text"):
                    text = str(msg["text"])[:100]
                    bubble_id = str(msg.get("id", ""))[:64]
                    await manager.send_question_to_presenters(text, bubble_id)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await manager.disconnect(ws)


@app.get("/presenter.html")
async def presenter_page(presenter_token: str | None = Cookie(default=None)):
    if not _is_authorized(presenter_token):
        return RedirectResponse("/login?next=/presenter.html")
    return FileResponse("static/presenter.html")


@app.get("/login")
async def login_page(next: str = "/presenter.html", error: str = ""):
    error_html = '<p class="error">암호가 틀렸습니다.</p>' if error else ""
    return HTMLResponse(_LOGIN_HTML.format(next=next, error=error_html))


@app.post("/login")
async def do_login(password: str = Form(...), next: str = Form(default="/presenter.html")):
    next_url = next if next.startswith("/") else "/presenter.html"
    if _PASSWORD and password == _PASSWORD:
        response = RedirectResponse(next_url, status_code=303)
        response.set_cookie(_COOKIE_NAME, _make_token(), httponly=True, samesite="strict")
        return response
    return RedirectResponse(f"/login?next={next_url}&error=1", status_code=303)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    is_cloud = "PORT" in os.environ

    print(f"\n{'=' * 52}")
    print(f"  강의 이모지 반응 서버 시작!")
    print(f"{'=' * 52}")
    if is_cloud:
        print(f"  클라우드 모드 (PORT={port})")
    else:
        ip = get_local_ip()
        print(f"  강사 화면  →  http://{ip}:{port}/presenter.html")
        print(f"  학생 접속  →  http://{ip}:{port}/student.html")
        print(f"  (학생들과 같은 Wi-Fi 네트워크여야 합니다)")
    if _PASSWORD:
        print(f"  발표자 암호 설정됨 (PRESENTER_PASSWORD)")
    else:
        print(f"  ⚠️  발표자 암호 미설정 — PRESENTER_PASSWORD 환경변수로 설정하세요")
    print(f"{'=' * 52}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
