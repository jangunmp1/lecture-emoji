# 강의 이모지 반응 (Lecture Emoji Reaction)

강의 중 학생들이 강사 화면에 실시간으로 이모지와 질문을 보낼 수 있는 앱입니다.
여러 수업이 동시에 독립적으로 운영되며, 학생은 별도 앱 설치 없이 브라우저만으로 참여할 수 있습니다.
강사는 화면 위에 이모지와 질문 말풍선을 띄우는 오버레이 앱을 실행해야 합니다.

## 구조

```
lecture-emoji/
├── app.py                    # FastAPI 서버 (WebSocket 포함)
├── overlay.py                # 데스크톱 이모지 오버레이 (선택 사항)
├── requirements.txt          # 서버 패키지 목록
├── requirements-overlay.txt  # 오버레이 패키지 목록
├── Procfile                  # Heroku 배포 설정
└── static/
    ├── presenter.html        # 강사 화면 (방 관리 + QR코드 + 질문 말풍선)
    └── student.html          # 학생 화면 (이모지 버튼 + 질문 입력)
```

## 실행 방법

### 서버

```bash
# 패키지 설치 (최초 1회)
pip install -r requirements.txt

# 서버 시작
python app.py
```

서버 시작 시 터미널에 접속 주소가 출력됩니다.

```
====================================================
  강의 이모지 반응 서버 시작!
====================================================
  강사 화면  →  http://서버IP:8000/presenter.html
  학생 접속  →  http://서버IP:8000/student.html
  (학생 기기에서 서버 IP에 접근 가능해야 합니다)
====================================================
```

### 데스크톱 오버레이

화면 위에 직접 이모지와 질문 말풍선을 띄우는 투명 오버레이 앱입니다.

```bash
# 패키지 설치 (최초 1회)
pip install -r requirements-overlay.txt

# 오버레이 실행 — 서버 주소·방 코드·비밀번호 입력창이 표시됩니다
python overlay.py

# 인수로 직접 지정 (입력창 생략)
python overlay.py --room ABC123 --password mypass

# 원격 서버에 연결할 때
python overlay.py --host 192.168.x.x --room ABC123 --password mypass

# 클라우드 서버에 연결할 때 (WSS)
python overlay.py --host example.com --ssl --room ABC123 --password mypass
```

실행하면 접속 정보 입력창이 표시됩니다.

- **서버 주소**: 기본값 `localhost:8000`. 원격 서버라면 해당 주소로 변경
- **HTTPS/WSS 사용**: 클라우드 서버(HTTPS)에 접속할 경우 체크
- **방 코드**: 강사 화면에 표시된 6자리 코드
- **비밀번호**: 방 개설 시 설정한 강의자 비밀번호

오버레이는 시스템 트레이에 🎉 아이콘으로 표시되며, **우클릭 → 종료**로 닫을 수 있습니다.
접속한 방 코드가 화면 오른쪽 상단에 표시됩니다.

> **Linux (Wayland)**: XWayland가 설치되어 있어야 합니다.

## 사용 방법

### 강사

1. `presenter.html`을 브라우저에서 열어 프로젝터/화면에 띄운다
2. **방 개설**: 방 제목과 비밀번호를 입력해 새 방을 만든다
   - 이미 개설된 방이 있으면 목록에서 선택 후 비밀번호를 입력해 입장
3. 생성된 QR코드 또는 URL을 학생들과 공유한다
4. 학생이 보낸 질문은 화면 왼쪽 아래에 말풍선으로 표시되며, X버튼으로 닫을 수 있다

### 학생

1. 강사 화면의 QR코드를 스캔하거나 `student.html`에 접속한다
2. 방 코드를 직접 입력하는 경우: 강사 화면에 표시된 6자리 코드를 입력한다
3. 이모지 버튼을 눌러 반응을 보내거나, 질문을 입력해 전송한다

> 강사와 학생이 **같은 Wi-Fi 네트워크**에 연결되어 있어야 합니다.

## 기능

- **다중 수업 동시 지원**: 수업마다 고유한 6자리 방 코드로 독립 운영
- **방별 비밀번호 인증**: 강사만 방을 개설·관리할 수 있도록 방 단위 비밀번호 설정
- **실시간 이모지 전송**: WebSocket으로 지연 없이 전달 (이모지 9종: 👍 👏 ❤️ 😂 🤔 😮 🔥 ✨ ❓)
- **질문 말풍선**: 학생 질문이 강사 화면에 말풍선으로 표시, 개별 삭제 가능
- **QR코드 자동 생성**: 방 코드가 포함된 학생 접속 QR코드 표시
- **실시간 접속자 수**: 강사/학생 화면 모두에 표시
- **스팸 방지**: 학생당 0.7초 쿨다운
- **자동 재연결**: 네트워크 끊김 시 자동으로 재연결 시도
- **데스크톱 오버레이**: 강사 화면 위에 이모지와 질문 말풍선을 직접 띄우는 투명 오버레이 (macOS/Linux/Windows 지원)

## 오버레이 바이너리 릴리스

Python 없이 바로 실행할 수 있는 바이너리를 [GitHub Releases](../../releases) 페이지에서 내려받을 수 있습니다.

### Linux (`overlay-linux`)

```bash
chmod +x overlay-linux
./overlay-linux
```

### macOS (`overlay-macos.zip`)

1. `overlay-macos.zip`을 내려받아 압축 해제
2. `overlay.app`을 더블클릭
3. "확인되지 않은 개발자" 경고가 뜨면: **시스템 설정 → 개인 정보 보호 및 보안 → 확인 없이 열기**

### Windows (`overlay-windows.exe`)

`overlay-windows.exe`를 더블클릭. SmartScreen 경고가 뜨면 **추가 정보 → 그래도 실행**을 클릭.

---

새 릴리스를 빌드하려면 태그를 푸시하면 GitHub Actions가 자동으로 빌드합니다.

```bash
git tag v1.0.0
git push origin v1.0.0
```

## 기술 스택

- **백엔드**: Python, FastAPI, WebSocket
- **프론트엔드**: HTML/CSS/JavaScript (바닐라, 프레임워크 없음)
- **QR코드**: qrcodejs (CDN)
- **오버레이**: PyQt6, python-xlib (Linux), pyobjc (macOS)
