import uvicorn
import json
import os
import socket
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()


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
