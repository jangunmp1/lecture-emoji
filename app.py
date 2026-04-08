import uvicorn
import json
import os
import random
import socket
import string
import hmac
import hashlib
import secrets
from dataclasses import dataclass, field
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

_SECRET = secrets.token_hex(32)   # 서버 재시작마다 갱신 → 기존 토큰 자동 무효화


def _room_token(room_id: str, password: str) -> str:
    return hmac.new(_SECRET.encode(), f"{room_id}:{password}".encode(), hashlib.sha256).hexdigest()


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


# ── 방(Room) 관리 ─────────────────────────────────────────────────────────────
@dataclass
class Room:
    title: str
    password: str
    manager: ConnectionManager = field(default_factory=ConnectionManager)


rooms: dict[str, Room] = {}


def generate_room_id() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        rid = ''.join(random.choices(chars, k=6))
        if rid not in rooms:
            return rid


class CreateRoomBody(BaseModel):
    title: str
    password: str


class LoginRoomBody(BaseModel):
    password: str


@app.post("/api/room")
async def create_room(body: CreateRoomBody):
    title = body.title.strip()[:50]
    if not title or not body.password:
        return JSONResponse({"error": "title and password required"}, status_code=400)
    room_id = generate_room_id()
    rooms[room_id] = Room(title=title, password=body.password)
    token = _room_token(room_id, body.password)
    return {"room_id": room_id, "token": token}


@app.post("/api/room/{room_id}/login")
async def login_room(room_id: str, body: LoginRoomBody):
    room_id = room_id.upper().strip()[:6]
    r = rooms.get(room_id)
    if not r:
        return JSONResponse({"error": "room not found"}, status_code=404)
    if not hmac.compare_digest(body.password, r.password):
        return JSONResponse({"error": "wrong password"}, status_code=401)
    token = _room_token(room_id, body.password)
    return {"token": token}


@app.get("/api/rooms")
async def list_rooms():
    return [{"room_id": rid, "title": r.title} for rid, r in rooms.items()]


@app.get("/api/room/{room_id}")
async def get_room_info(room_id: str):
    room_id = room_id.upper().strip()[:6]
    r = rooms.get(room_id)
    if not r:
        return JSONResponse({"error": "room not found"}, status_code=404)
    return {"title": r.title}


@app.websocket("/ws/presenter")
async def presenter_ws(ws: WebSocket, room: str = Query(default=""), token: str = Query(default="")):
    room = room.upper().strip()[:6]
    if not room or room not in rooms:
        await ws.accept()
        await ws.close(code=4004)
        return
    r = rooms[room]
    expected = _room_token(room, r.password)
    if not token or not hmac.compare_digest(token, expected):
        await ws.accept()
        await ws.close(code=4001)
        return
    mgr = r.manager
    await mgr.connect_presenter(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "delete_bubble" and msg.get("id"):
                    await mgr.broadcast_to_presenters({
                        "type": "delete_bubble",
                        "id": str(msg["id"])[:64],
                    })
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await mgr.disconnect(ws)


@app.websocket("/ws/student")
async def student_ws(ws: WebSocket, room: str = Query(default="")):
    room = room.upper().strip()[:6]
    if not room or room not in rooms:
        await ws.accept()
        await ws.close(code=4004)
        return
    mgr = rooms[room].manager
    await mgr.connect_student(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "emoji" and msg.get("emoji"):
                    await mgr.send_emoji_to_presenters(msg["emoji"])
                elif msg.get("type") == "question" and msg.get("text"):
                    text = str(msg["text"])[:100]
                    bubble_id = str(msg.get("id", ""))[:64]
                    await mgr.send_question_to_presenters(text, bubble_id)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await mgr.disconnect(ws)


@app.get("/presenter.html")
async def presenter_page():
    return FileResponse("static/presenter.html")


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
    print(f"{'=' * 52}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
