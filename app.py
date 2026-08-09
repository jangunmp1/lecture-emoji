import uvicorn
import json
import os
import random
import socket
import string
import hmac
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, field
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Header, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "lecture_emoji.db")
MASTER_KEY = os.environ.get("MASTER_KEY", "lecture2026")

app = FastAPI()

_SECRET = secrets.token_hex(32)   # 서버 토큰 생성용 시크릿


def _room_token(room_id: str, password: str) -> str:
    return hmac.new(_SECRET.encode(), f"{room_id}:{password}".encode(), hashlib.sha256).hexdigest()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        # 강사 계정 테이블
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS presenters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 세션 테이블
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            presenter_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (presenter_id) REFERENCES presenters(id) ON DELETE CASCADE
        );
        """)
        # 방 테이블
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY,
            presenter_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (presenter_id) REFERENCES presenters(id) ON DELETE CASCADE
        );
        """)
        # 질문 테이블
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            text TEXT NOT NULL,
            is_deleted INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (room_id) REFERENCES rooms(room_id) ON DELETE CASCADE
        );
        """)
        conn.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}${key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, key_hex = password_hash.split('$')
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def get_current_presenter(authorization: str = Header(default="")) -> dict | None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.username FROM sessions s
            JOIN presenters p ON s.presenter_id = p.id
            WHERE s.token = ?
        """, (token,))
        row = cursor.fetchone()
        if row:
            return {"id": row["id"], "username": row["username"]}
    return None


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
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.presenters: list[WebSocket] = []
        self.students: list[WebSocket] = []

    def get_bubbles_from_db(self) -> list[dict]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, text FROM questions WHERE room_id = ? AND is_deleted = 0 ORDER BY created_at ASC",
                (self.room_id,)
            )
            return [{"id": row["id"], "text": row["text"]} for row in cursor.fetchall()]

    async def connect_presenter(self, ws: WebSocket):
        await ws.accept()
        self.presenters.append(ws)
        await ws.send_text(json.dumps({"type": "count", "count": len(self.students)}))
        bubbles = self.get_bubbles_from_db()
        for bubble in bubbles:
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
            if ws in self.presenters:
                self.presenters.remove(ws)

    async def send_question_to_presenters(self, text: str, bubble_id: str):
        # DB에 저장
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO questions (id, room_id, text) VALUES (?, ?, ?)",
                (bubble_id, self.room_id, text)
            )
            conn.commit()

        dead = []
        for ws in self.presenters:
            try:
                await ws.send_text(json.dumps({"type": "question", "text": text, "id": bubble_id}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.presenters:
                self.presenters.remove(ws)

    async def delete_question_bubble(self, bubble_id: str):
        # DB에서 삭제 상태 업데이트
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE questions SET is_deleted = 1 WHERE id = ? AND room_id = ?",
                (bubble_id, self.room_id)
            )
            conn.commit()

        msg = {"type": "delete_bubble", "id": bubble_id}
        dead = []
        for ws in self.presenters:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.presenters:
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
    room_id: str
    presenter_id: int
    title: str
    password: str
    manager: ConnectionManager = field(init=False)

    def __post_init__(self):
        self.manager = ConnectionManager(self.room_id)


rooms: dict[str, Room] = {}


def load_rooms_from_db():
    rooms.clear()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT room_id, presenter_id, title, password FROM rooms")
        for row in cursor.fetchall():
            r_id = row["room_id"]
            rooms[r_id] = Room(
                room_id=r_id,
                presenter_id=row["presenter_id"],
                title=row["title"],
                password=row["password"]
            )


def generate_room_id() -> str:
    chars = ''.join(c for c in string.ascii_uppercase + string.digits if c not in 'O0I1')
    while True:
        rid = ''.join(random.choices(chars, k=6))
        if rid not in rooms:
            return rid


# ── Auth Request Models ───────────────────────────────────────────────────────
class VerifyMasterKeyBody(BaseModel):
    master_key: str


class RegisterBody(BaseModel):
    master_key: str
    username: str
    password: str


class AuthLoginBody(BaseModel):
    username: str
    password: str


class CreateRoomBody(BaseModel):
    title: str
    password: str


class LoginRoomBody(BaseModel):
    password: str


# ── Auth Endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/auth/verify-master-key")
async def verify_master_key(body: VerifyMasterKeyBody):
    if body.master_key != MASTER_KEY:
        return JSONResponse({"error": "마스터 키가 올바르지 않습니다."}, status_code=403)
    return {"ok": True}


@app.post("/api/auth/register")
async def register(body: RegisterBody):
    if body.master_key != MASTER_KEY:
        return JSONResponse({"error": "마스터 키가 올바르지 않습니다."}, status_code=403)

    username = body.username.strip()
    if len(username) < 3 or len(username) > 30:
        return JSONResponse({"error": "아이디는 3~30자 사이여야 합니다."}, status_code=400)
    if len(body.password) < 4:
        return JSONResponse({"error": "비밀번호는 4자 이상이어야 합니다."}, status_code=400)

    pw_hash = hash_password(body.password)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO presenters (username, password_hash) VALUES (?, ?)",
                (username, pw_hash)
            )
            conn.commit()
            presenter_id = cursor.lastrowid

            token = secrets.token_hex(32)
            cursor.execute(
                "INSERT INTO sessions (token, presenter_id) VALUES (?, ?)",
                (token, presenter_id)
            )
            conn.commit()
            return {"token": token, "username": username}
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "이미 존재하는 아이디입니다."}, status_code=400)


@app.post("/api/auth/login")
async def auth_login(body: AuthLoginBody):
    username = body.username.strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash FROM presenters WHERE username = ?", (username,))
        row = cursor.fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            return JSONResponse({"error": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401)

        presenter_id = row["id"]
        token = secrets.token_hex(32)
        cursor.execute("INSERT INTO sessions (token, presenter_id) VALUES (?, ?)", (token, presenter_id))
        conn.commit()
        return {"token": token, "username": username}


@app.post("/api/auth/logout")
async def auth_logout(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if token:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(authorization: str = Header(default="")):
    presenter = get_current_presenter(authorization)
    if not presenter:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return presenter


# ── Room Endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/room")
async def create_room(body: CreateRoomBody, authorization: str = Header(default="")):
    presenter = get_current_presenter(authorization)
    if not presenter:
        return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401)

    title = body.title.strip()[:50]
    if not title or not body.password:
        return JSONResponse({"error": "title and password required"}, status_code=400)

    room_id = generate_room_id()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO rooms (room_id, presenter_id, title, password) VALUES (?, ?, ?, ?)",
            (room_id, presenter["id"], title, body.password)
        )
        conn.commit()

    r = Room(room_id=room_id, presenter_id=presenter["id"], title=title, password=body.password)
    rooms[room_id] = r
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
async def list_rooms(authorization: str = Header(default="")):
    presenter = get_current_presenter(authorization)
    if not presenter:
        # 로그인하지 않은 경우 (공개 목록 또는 빈 목록)
        return []
    
    # 로그인한 강사 본인이 개설한 방만 반환 (질문 개수 포함)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT r.room_id, r.title, r.created_at,
                   (SELECT COUNT(*) FROM questions q WHERE q.room_id = r.room_id) as question_count
            FROM rooms r
            WHERE r.presenter_id = ?
            ORDER BY r.created_at DESC
            """,
            (presenter["id"],)
        )
        return [
            {
                "room_id": row["room_id"],
                "title": row["title"],
                "question_count": row["question_count"]
            }
            for row in cursor.fetchall()
        ]


@app.get("/api/room/{room_id}")
async def get_room_info(room_id: str):
    room_id = room_id.upper().strip()[:6]
    r = rooms.get(room_id)
    if not r:
        return JSONResponse({"error": "room not found"}, status_code=404)
    return {"title": r.title}


@app.get("/api/room/{room_id}/questions")
async def get_room_questions(
    room_id: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100),
    filter_type: str = Query(default="all"),
    search: str = Query(default=""),
    authorization: str = Header(default="")
):
    room_id = room_id.upper().strip()[:6]
    presenter = get_current_presenter(authorization)
    token = authorization.removeprefix("Bearer ").strip()

    is_owner = False
    is_token_valid = False
    room_title = ""

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT presenter_id, title, password FROM rooms WHERE room_id = ?", (room_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"error": "room not found"}, status_code=404)
        
        room_title = row["title"]
        if presenter and presenter["id"] == row["presenter_id"]:
            is_owner = True
        elif token and hmac.compare_digest(token, _room_token(room_id, row["password"])):
            is_token_valid = True

        if not is_owner and not is_token_valid:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # 전체 통계 계산
        cursor.execute("SELECT COUNT(*) FROM questions WHERE room_id = ?", (room_id,))
        total_all = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM questions WHERE room_id = ? AND is_deleted = 0", (room_id,))
        total_active = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM questions WHERE room_id = ? AND is_deleted = 1", (room_id,))
        total_deleted = cursor.fetchone()[0]

        # 필터 및 검색 조건 적용
        where_clauses = ["room_id = ?"]
        params = [room_id]

        if filter_type == "active":
            where_clauses.append("is_deleted = 0")
        elif filter_type == "deleted":
            where_clauses.append("is_deleted = 1")

        if search.strip():
            where_clauses.append("text LIKE ?")
            params.append(f"%{search.strip()}%")

        where_sql = " WHERE " + " AND ".join(where_clauses)

        # 필터링된 총 개수 계산
        cursor.execute(f"SELECT COUNT(*) FROM questions {where_sql}", params)
        filtered_count = cursor.fetchone()[0]

        total_pages = (filtered_count + limit - 1) // limit if filtered_count > 0 else 1
        page = min(page, total_pages)
        offset = (page - 1) * limit

        query_sql = f"""
            SELECT id, text, is_deleted, strftime('%Y-%m-%d %H:%M:%S', created_at) as created_at
            FROM questions
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query_sql, params + [limit, offset])

        questions = [
            {
                "id": r["id"],
                "text": r["text"],
                "is_deleted": bool(r["is_deleted"]),
                "created_at": r["created_at"]
            }
            for r in cursor.fetchall()
        ]

        return {
            "room_id": room_id,
            "room_title": room_title,
            "page": page,
            "limit": limit,
            "total_count": filtered_count,
            "total_pages": total_pages,
            "stats": {
                "total": total_all,
                "active": total_active,
                "deleted": total_deleted
            },
            "questions": questions
        }


@app.get("/history.html")
async def history_page():
    return FileResponse("static/history.html")


@app.delete("/api/room/{room_id}")
async def delete_room(room_id: str, authorization: str = Header(default="")):
    room_id = room_id.upper().strip()[:6]
    r = rooms.get(room_id)
    if not r:
        return JSONResponse({"error": "room not found"}, status_code=404)

    token = authorization.removeprefix("Bearer ").strip()
    presenter = get_current_presenter(authorization)
    
    # 방 작성자이거나 올바른 방 토큰을 가진 경우 삭제 허용
    is_owner = presenter and presenter["id"] == r.presenter_id
    expected_token = _room_token(room_id, r.password)
    is_token_valid = token and hmac.compare_digest(token, expected_token)

    if not is_owner and not is_token_valid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
        conn.commit()

    if room_id in rooms:
        del rooms[room_id]
    return {"ok": True}


# ── WebSockets ────────────────────────────────────────────────────────────────
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
                    await mgr.delete_question_bubble(str(msg["id"])[:64])
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
                    if not bubble_id:
                        bubble_id = secrets.token_hex(16)
                    await mgr.send_question_to_presenters(text, bubble_id)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        await mgr.disconnect(ws)


@app.get("/presenter.html")
async def presenter_page():
    return FileResponse("static/presenter.html")


app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
def startup_event():
    init_db()
    load_rooms_from_db()


if __name__ == "__main__":
    init_db()
    load_rooms_from_db()

    port = int(os.environ.get("PORT", 8000))
    is_cloud = "PORT" in os.environ

    print(f"\n{'=' * 52}")
    print(f"  강의 이모지 반응 서버 시작! (DB & 회원가입 기능 적용)")
    print(f"{'=' * 52}")
    if is_cloud:
        print(f"  클라우드 모드 (PORT={port})")
    else:
        ip = get_local_ip()
        print(f"  강사 화면  →  http://{ip}:{port}/presenter.html")
        print(f"  학생 접속  →  http://{ip}:{port}/student.html")
        print(f"  마스터 키  →  {MASTER_KEY}")
    print(f"{'=' * 52}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
