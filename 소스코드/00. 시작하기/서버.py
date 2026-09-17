"""
런처 — "오늘은 어떤 작업을 할까요?" 화면 하나로 회의/면접 두 비서를 띄워주는 허브.

회의 보조 비서(포트 8001)와 면접 보조 비서(포트 8000)는 각자 마이크·faster-whisper·
독립된 상태를 갖는 무거운 앱이라 하나의 서버로 합치지 않았다. 대신 이 런처가 버튼 클릭
시 필요한 쪽만 백그라운드로 켜고, 켜질 때까지 기다렸다가 브라우저를 그 주소로 이동시킨다.
두 앱은 코드를 전혀 건드리지 않는다 — 원래부터 QP_NO_BROWSER 환경변수를 지원해서
(자기가 알아서 브라우저를 열지 않도록) 그대로 재사용할 수 있다.
"""

import os
import sys
import time
import socket
import subprocess
import threading
import webbrowser
from pathlib import Path

try:
    from flask import Flask, jsonify, send_from_directory
except ImportError as e:
    print(f"필수 라이브러리 누락: {e}")
    print("[해결] '1_설치하기.bat' 먼저 실행하세요.")
    input("Enter 누르면 닫힘...")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 콘솔 창 없이(pythonw) 띄우면 Ctrl+C로 끌 창이 없다 — 실행 중 PID를 파일로 남겨서
# 3_종료하기.bat가 이걸 보고 종료할 수 있게 한다.
PID_PATH = HERE / "실행중.pid"
PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

HOST = "127.0.0.1"
PORT = 7999

APPS = {
    "meeting": {
        "label": "회의 보조 비서",
        "server": ROOT / "02. 회의 보조 비서 실행하기" / "서버.py",
        "port": 8001,
    },
    "interview": {
        "label": "면접 보조 비서",
        "server": ROOT / "02. 면접 보조 비서 실행하기" / "서버.py",
        "port": 8000,
    },
}

LAUNCH_TIMEOUT = 40  # 초 — whisper 모델 로딩까지 감안한 여유 시간


def port_open(port: str, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch_app(key: str) -> tuple[bool, str]:
    app = APPS.get(key)
    if not app:
        return False, "알 수 없는 앱입니다."
    if not app["server"].exists():
        return False, f"{app['label']} 서버 파일을 찾을 수 없습니다: {app['server']}"

    if port_open(app["port"]):
        return True, ""  # 이미 켜져 있음(수동 실행 포함) — 그대로 이동

    subprocess.Popen(
        ["pythonw.exe", str(app["server"])],
        cwd=str(app["server"].parent),
        env={**os.environ, "QP_NO_BROWSER": "1"},
    )

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if port_open(app["port"]):
            return True, ""
        time.sleep(0.5)
    return False, f"{app['label']}가 {LAUNCH_TIMEOUT}초 안에 켜지지 않았습니다. 잠시 후 다시 시도해주세요."


# =============================================================
# Flask 웹 서버
# =============================================================
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/launch/<key>", methods=["POST"])
def api_launch(key):
    ok, error = launch_app(key)
    if not ok:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "port": APPS[key]["port"]})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """런처만 끈다 — 회의/면접 비서는 각자 탭을 닫으면 알아서(유휴 자동종료) 꺼지거나,
    각자의 설정 화면에서 따로 끄면 된다."""
    def _die():
        time.sleep(0.3)
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


def main():
    print("=" * 50)
    print("🏠 업무 보조 비서 — 런처")
    print("=" * 50)
    for key, app_info in APPS.items():
        exists = "✅" if app_info["server"].exists() else "❌ 못 찾음"
        print(f"  {app_info['label']} (포트 {app_info['port']}): {exists}")
    print(f"🌐 주소: http://{HOST}:{PORT}")
    print("=" * 50)

    if not os.environ.get("QP_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
