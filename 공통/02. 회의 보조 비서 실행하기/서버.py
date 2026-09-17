"""
회의 보조 비서 웹앱 — 로컬 서버 (Flask)

`인사관리 보조 에이전트`(면접 보조 비서)와 같은 구조를 그대로 따릅니다.
- 백그라운드 스레드(오디오/STT/Claude)가 마이크 → faster-whisper(로컬 한국어) →
  `claude` CLI 순으로 처리한다.
- 화면은 브라우저. 서버는 index.html을 띄우고, 브라우저가 1초마다 /api/state 를
  폴링해서 타이머·자막·안건판·알림을 갱신한다.
- 같은 PC에서 http://127.0.0.1:8001 으로 접속 (음성이 PC 밖으로 나가지 않음).

면접 보조 비서와 다른 점: 지원자 선택·이력서 대조·차별 질문 경고 같은 기능은 없고,
대신 "실시간으로 누적되는 안건판"이 핵심 기능이다 (회의 중 약 25~30초마다 갱신).

Claude Code Max 구독을 그대로 사용 → 별도 API 키 불필요.
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import threading
import webbrowser
import subprocess
import urllib.request
import urllib.error
import wave
from pathlib import Path
from datetime import datetime

# pythonw.exe로 실행하면(검은 콘솔 창 없이 띄우는 방식) 콘솔이 아예 없어서
# sys.stdin/stdout/stderr가 None이 되는 게 보통이지만, 실행 방식(터미널의 백그라운드
# 작업으로 띄우는 경우 등)에 따라 None이 아닌 채로 넘어올 수도 있어 그것만으로는
# 못 믿는다 — 실행 파일 이름(pythonw.exe인지)으로 직접 판단하는 게 더 확실하다.
NO_CONSOLE = Path(sys.executable).name.lower() == "pythonw.exe" or sys.stdout is None
if NO_CONSOLE:
    _log_path = Path(__file__).resolve().parent / "실행로그.txt"
    _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file
    print(f"\n===== 서버 시작 {datetime.now().isoformat(timespec='seconds')} =====")


def _fatal_message(msg: str):
    """콘솔이 없어도(pythonw) 시작 실패를 알아챌 수 있게 메시지 박스를 띄운다.
    콘솔이 있으면(python.exe로 직접 실행) 기존처럼 출력하고 Enter를 기다린다."""
    print(msg)
    if NO_CONSOLE:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "회의 보조 비서 — 시작 실패", 0x10)
        except Exception:
            pass
    else:
        input("Enter 누르면 닫힘...")


# === 외부 라이브러리 ===
try:
    import sounddevice as sd
    import numpy as np
    from scipy.signal import resample_poly
    from faster_whisper import WhisperModel
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as e:
    _fatal_message(f"필수 라이브러리 누락: {e}\n\n[해결] '1_설치하기.bat' 먼저 실행하세요.")
    sys.exit(1)

# =============================================================
# 설정
# =============================================================
HERE = Path(__file__).resolve().parent

# 콘솔 창 없이(pythonw) 띄우면 Ctrl+C로 끌 창이 없다 — 실행 중 PID를 파일로 남겨서
# 화면의 "서버 종료" 버튼이나 3_종료하기.bat가 이걸 보고 종료할 수 있게 한다.
PID_PATH = HERE / "실행중.pid"
PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

# 폴더 구조(이름·깊이)는 사용자가 자주 바꾸므로 하드코딩하지 않고 찾아간다.
#   회의 보조 에이전트/
#   ├── 참고자료/받아쓰기_용어.txt, 회의록_형식.txt   ← 이걸 기준으로 루트를 잡음
#   ├── 02. 회의 보조 비서 실행하기/                   ← 이 프로그램 (HERE)
#   └── 03. 회의 기록/                                  ← 회의별 결과 폴더가 쌓이는 곳
TRANSCRIPT_PREFIX = "회의녹취록_"
CORRECTED_TRANSCRIPT_PREFIX = "회의녹취록_보정_"
RECORDING_PREFIX = "원본음성_"
MINUTES_PREFIX = "회의록_"
BOARD_SNAPSHOT_PREFIX = "안건판_"
MEMO_PREFIX = "메모_"


def find_root() -> Path:
    """코드(공통/)와 데이터(회의관리/)가 분리된 구조 — 고정된 상대 위치로 데이터 루트를 가리킨다.
    HERE = <저장소 루트>/공통/02. 회의 보조 비서 실행하기"""
    return HERE.parent.parent / "회의관리"


ROOT = find_root()
VOCAB_PATH = ROOT / "참고자료" / "받아쓰기_용어.txt"        # 받아쓰기에 미리 알려줄 업무 용어
MINUTES_FORMAT_PATH = ROOT / "참고자료" / "회의록_형식.txt"  # 회의록 작성 규칙(없으면 내장 규칙)
DEFAULT_MEETING_LOG_DIR = ROOT / "03. 회의 기록"
DEFAULT_MEETING_LOG_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = ROOT / "설정.json"

HOST = "127.0.0.1"
PORT = 8001   # 면접 보조 비서(8000)와 동시에 켜둘 수 있도록 다른 포트 사용

SAMPLE_RATE = 16000
CHUNK_SECONDS = 10           # 몇 초마다 받아쓸지 (faster-whisper 호출 주기)
BOARD_INTERVAL = 25          # 몇 초 분량의 받아쓰기가 모이면 안건판을 갱신할지
SILENCE_THRESHOLD = 0.008

# 보정 작업 — 실시간 받아쓰기(빠르지만 10초씩 잘라서 처리 → 문장 경계에서 오탈자가
# 잘 남) 와 별개로, 녹음을 더 큰 덩어리(60초)로 모아서 한 번 더 받아쓰는 두 번째
# 파이프라인. 문맥이 더 넓어서(vad_filter + condition_on_previous_text=True) 실시간
# 보다 정확한 경향이 있지만, 그만큼 늦게(최대 60초+처리시간) 나온다. 회의가 끝나면
# 이 보정본이 준비될 때까지 잠깐 기다렸다가 회의록을 그걸로 작성한다.
CORRECTION_WINDOW_SECONDS = 60
CORRECTION_BEAM_SIZE = 8
CORRECTION_CATCHUP_TIMEOUT = 180   # 회의 종료 후 보정이 못 따라잡으면 이 시간 뒤 실시간본으로 대체

# 받아쓰기 정확도 설정 — 면접 보조 비서에서 실측한 값을 그대로 사용.
#   large-v3-turbo가 medium보다 빠르고 정확 (디코더 층이 얇아서).
#   터미널에 "⚠️밀림"이 자주 뜨면 WHISPER_MODEL을 "small"로 낮추세요.
WHISPER_MODEL = "large-v3-turbo"
WHISPER_THREADS = os.cpu_count() or 4
BEAM_SIZE = 5

MAX_BACKLOG_CHUNKS = 2        # 받아쓰기가 밀릴 때 오래된 음성을 버리고 따라붙는 배수

# 자리가 떨어진 회의(면접처럼 마이크 바로 앞이 아닌 상황)는 목소리가 작게 녹음돼
# 인식률이 크게 떨어진다. 무음 판정(SILENCE_THRESHOLD)은 원음 기준으로 그대로 하되,
# 그보다 크지만 여전히 작은 소리는 증폭해서 모델에 넣는다 (조용한 소리만 키우고,
# 이미 크게 들어온 소리는 건드리지 않음 — 왜곡 방지).
GAIN_TARGET_PEAK = 0.6         # 증폭 후 목표로 하는 최대 진폭
GAIN_MAX = 6.0                 # 과도하게 키워 잡음을 말소리로 오인하는 것을 막는 상한
CLAUDE_MODEL = "sonnet"       # claude -p --model. opus/sonnet/haiku
CLAUDE_TIMEOUT = 60           # 안건판 갱신 1회 타임아웃
MINUTES_TIMEOUT = 600         # 회의 종료 후 회의록 생성 타임아웃

# =============================================================
# 입력 장치 선택 — WASAPI 우선
#
# sounddevice(PortAudio)는 아무 지정 없이 열면 Windows의 가장 오래된 오디오 경로인
# MME로 기본 잡히는 경우가 많다. MME는 마이크 배열의 드라이버 단 처리(빔포밍·잡음
# 억제·자동 게인 — 예: 인텔 Smart Sound Technology)를 거치지 않은 날것 신호를 준다.
# 반면 Windows 기본 녹음기 같은 최신 앱은 WASAPI를 쓰고, 이 경로에서는 그 처리가
# 적용된 신호를 받는다. 같은 자리·같은 시각에 녹음했는데도 앱 쪽 받아쓰기 품질이
# 눈에 띄게 떨어졌던 원인이 바로 이것으로 확인됨(2026-09-10, 같은 회의를 Windows
# 녹음기로 동시에 녹음해 비교) — 그래서 이 앱도 WASAPI 기본 입력 장치를 명시적으로
# 골라서 연다. 장치 인덱스는 실행할 때마다 바뀔 수 있어 매번 새로 찾는다.
def find_capture_device() -> tuple[int | None, int]:
    try:
        hostapis = sd.query_hostapis()
        wasapi_idx = next(i for i, a in enumerate(hostapis) if "WASAPI" in a["name"])
        dev_idx = hostapis[wasapi_idx]["default_input_device"]
        if dev_idx is None or dev_idx < 0:
            raise RuntimeError("WASAPI 기본 입력 장치 없음")
        info = sd.query_devices(dev_idx)
        rate = int(info["default_samplerate"])
        print(f"[Audio] WASAPI 입력 장치 사용: {info['name']} ({rate}Hz, {info['max_input_channels']}채널)")
        return dev_idx, rate
    except Exception as e:
        print(f"[Audio] WASAPI 장치를 찾지 못해 시스템 기본 장치로 진행합니다 ({e})")
        return None, SAMPLE_RATE


CAPTURE_DEVICE, CAPTURE_RATE = find_capture_device()

# 오디오 캡처 상태 — 프론트엔드(/api/state)에 노출해서 마이크가 안 잡혀도
# 화면에 아무 표시가 없던 문제(2026-09-17 회의: 녹취록이 완전히 빔)를 막는다.
audio_status = {"ok": False, "error": "", "device": ""}
audio_status_lock = threading.Lock()


def set_audio_status(ok: bool, error: str = "", device: str = ""):
    global audio_status
    with audio_status_lock:
        audio_status = {"ok": ok, "error": error, "device": device}


# 마지막으로 무음이 아닌 소리가 잡힌 시각 — 장치는 열렸지만(에러 없음) 음소거되어
# 있거나 엉뚱한 장치라 계속 무음만 들어오는 경우를 잡아내기 위함.
last_sound_at: float = 0.0
NO_SOUND_WARN_AFTER = 40  # 초


def resample_to_model_rate(audio: np.ndarray) -> np.ndarray:
    """녹음 장치의 실제 표본화율(예: 48000Hz)을 모델이 기대하는 16000Hz로 맞춘다."""
    if CAPTURE_RATE == SAMPLE_RATE:
        return audio
    from math import gcd
    g = gcd(CAPTURE_RATE, SAMPLE_RATE)
    up, down = SAMPLE_RATE // g, CAPTURE_RATE // g
    return resample_poly(audio, up, down).astype(np.float32)


# =============================================================
# Claude CLI 자동 탐지
# =============================================================
def find_claude_cli() -> str | None:
    cmd = shutil.which("claude")
    if cmd:
        return cmd
    home = Path.home()
    candidates = [
        home / "AppData/Local/Programs/claude/claude.exe",
        home / "AppData/Roaming/npm/claude.cmd",
        home / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.cmd",
        home / ".local/bin/claude",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    vscode_ext = home / ".vscode/extensions"
    if vscode_ext.exists():
        for ext_dir in sorted(vscode_ext.glob("anthropic.claude-code-*"), reverse=True):
            for sub in ("resources/native-binary/claude.exe",
                        "resources/native-binary/claude",
                        "claude.exe"):
                p = ext_dir / sub
                if p.exists():
                    return str(p)
    return None


CLAUDE_CLI = find_claude_cli()

# claude CLI는 대개 .cmd 배치 파일이라(예: claude.CMD), 실행하려면 Windows가 내부적으로
# cmd.exe를 띄워야 한다. 이 서버 자체가 콘솔 없이(pythonw) 떠 있을 때는 물려받을 콘솔이
# 없어서, 호출할 때마다(25초 회의판 갱신 등) 새 콘솔 창이 잠깐씩 나타났다 사라진다 —
# CREATE_NO_WINDOW로 그 창 자체를 아예 안 만들게 막는다.
CLI_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# =============================================================
# 시스템 프롬프트 — 안건 진행 상황 갱신용
# =============================================================
SYSTEM_PROMPT = """당신은 "회의 보조 비서"입니다.

[역할]
회의 중 발언을 실시간으로 받아쓴 것을 바탕으로, 오늘 회의 안건의 진행 상황(논의 요약·
결정 사항·액션 아이템)을 계속 갱신합니다.
안건 자체(제목)는 사용자가 미리 정하거나 회의 중 직접 수정하는 것이므로, 당신은 기존
안건의 제목을 만들거나 바꾸지 않습니다. 다만 목록 어디에도 없는 새로운 주제가 뚜렷하게
등장하면 새 항목으로 제안할 수 있습니다.
회의 진행·결론 도출·의사결정에는 관여하지 않고, 오간 내용을 있는 그대로 구조화해서
보여주는 것이 임무입니다.

[중요 — 입력 구조]
- [오늘 안건 목록]: 지금까지의 안건과 진행 상황 JSON (id·title·status·summary·decision·
  action_items 포함). 회의 시작 전 사용자가 미리 적어둔 것일 수도, 비어 있을 수도 있습니다.
- [새로 들린 내용]: 방금 들린 발언들 (약 25~30초 분량, 화자 구분 없음)

[작업 방식]
- [새로 들린 내용]이 [오늘 안건 목록] 중 하나와 관련 있으면, 그 항목의 id를 그대로 써서
  "updates"에 최신 상태를 담으세요. summary는 기존 내용에 새로 알게 된 것을 더해 처음부터
  다시 쓰세요 (이어붙이지 말 것). 2~4문장 이내.
- updates에는 id·status·summary·decision·action_items만 넣으세요. title은 절대 넣거나
  바꾸지 마세요 — 안건 제목은 사용자 소유입니다.
- 실제로 논의되지 않은 안건은 언급하지 마세요 (updates에 넣지 말고 그대로 둘 것).
- [오늘 안건 목록] 어디에도 해당하지 않는, 뚜렷이 새로운 주제가 등장하면 "new_items"에
  담으세요 (title은 8자 내외로 새로 지어서 포함).
- 결론·합의·숫자·기한이 나오면 decision을 채우고 status를 decided로 바꾸세요.
- 담당자와 할 일이 언급되면 action_items에 추가하세요 (owner·due는 언급 안 되면 빈 값).
- 확실하지 않은 내용은 지어내지 말고 비워두세요.
- 잡담·인사말 등 안건과 무관한 내용은 무시하세요.

[출력 형식 — 반드시 아래 JSON 한 개만. 마크다운/설명 금지]
{
  "updates": [
    {
      "id": "m1",
      "status": "discussing" | "decided" | "pending",
      "summary": "지금까지 논의 요약 2~4문장",
      "decision": "결정된 경우만 채움, 그 외 \\"\\"",
      "action_items": [
        {"task": "할 일", "owner": "담당자(빈 값 가능)", "due": "기한(빈 값 가능)"}
      ]
    }
  ],
  "new_items": [
    {
      "title": "8자 내외 새 안건명",
      "status": "discussing",
      "summary": "...",
      "decision": "",
      "action_items": []
    }
  ]
}

[중요 제약]
- 회의 내용을 평가·판단하지 마세요. 사실을 구조화하는 것이 전부입니다.
- 없는 사실을 지어내 채우지 마세요.
- JSON 외 어떤 텍스트도 출력 금지.
"""

# =============================================================
# 받아쓰기 용어 로드 (참고자료/받아쓰기_용어.txt)
# =============================================================
VOCAB_MAX = 25


def load_vocab_words() -> list[str]:
    if not VOCAB_PATH.exists():
        return []
    words = []
    for raw in VOCAB_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("※") or line.startswith("·"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if len(line) > 25 or line.endswith(("다.", "요.", "니다", "세요")):
            continue
        words.append(line)
    return words


_all_vocab = load_vocab_words()
VOCAB_WORDS = _all_vocab[:VOCAB_MAX]
VOCAB_HINT = ", ".join(VOCAB_WORDS) + "." if VOCAB_WORDS else ""

if VOCAB_WORDS:
    msg = f"[받아쓰기] 고유명사 {len(VOCAB_WORDS)}개 적용"
    if len(_all_vocab) > VOCAB_MAX:
        msg += f" (파일에 {len(_all_vocab)}개 있으나 앞 {VOCAB_MAX}개만 사용 — 많으면 인식이 흐트러짐)"
    print(msg)
else:
    print("[받아쓰기] 참고자료/받아쓰기_용어.txt 없음 — 기본 인식으로 진행")

# =============================================================
# 글로벌 상태
# =============================================================
audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
transcript_q: "queue.Queue[dict]" = queue.Queue()   # {"t": iso, "text": ...}
caption_q: "queue.Queue[str]" = queue.Queue()       # 브라우저 실시간 자막
notice_q: "queue.Queue[dict]" = queue.Queue()       # 새 안건/결정 알림

running = threading.Event()
shutdown = threading.Event()

# 일시정지 — 회의를 끝내지 않고 마이크만 잠깐 끈다 (휴식·비공개 대화 등). 종료와 달리
# 안건·메모를 그대로 두고, 경과 시간 계산에서 정지해 있던 구간만 뺀다.
paused = threading.Event()
pause_started: float = 0.0
total_paused: float = 0.0

session_start: float = 0.0
last_elapsed: int = 0
session_transcripts: list[dict] = []
session_lock = threading.Lock()

# 오늘의 안건 목록 — 사용자가 언제든(시작 전/중/후) 추가·수정·삭제할 수 있는 단일 소스.
# Claude는 title을 절대 바꾸지 않고, status/summary/decision/action_items만 채운다.
agenda_items: list[dict] = []
agenda_lock = threading.Lock()
next_manual_id = 1     # 사용자가 직접 추가한 안건 (mN)
next_auto_id = 1        # 대화 중 Claude가 새로 감지한 안건 (aN)

minutes_state: dict = {"status": "idle", "file": "", "error": ""}
# status: idle | running | ready | error

notion_status: dict = {"status": "idle", "error": ""}
# status: idle(설정 안 됨) | running | ready | error

# 보정 작업 상태 — 실시간과 별개로 돌아가는 2차 받아쓰기 파이프라인
correction_status: dict = {"status": "idle"}
# status: idle | running (off는 설정에서 껐을 때 — 화면에는 idle과 동일하게 보여줌)
correction_q: "queue.Queue[np.ndarray]" = queue.Queue()   # stt_loop가 리샘플된 조각을 실시간용과 별도로 여기에도 흘림
corrected_transcript: list[dict] = []   # [{"t": iso, "text": ...}] — 보정 완료된 구간들
correction_lock = threading.Lock()
correction_finalize = threading.Event()   # api_stop이 세팅: "지금까지 쌓인 것만 마저 처리해줘"
correction_caught_up = threading.Event()  # correction_loop이 세팅: "더 처리할 오디오가 없다"
correction_caught_up.set()  # 시작 시점엔 처리할 게 없으니 True

stt_finalize = threading.Event()  # api_stop이 세팅: "타이머 기다리지 말고 지금 버퍼 바로 처리해줘"
stt_flushed = threading.Event()   # stt_loop이 세팅: "버퍼에 남은 게 없다" — api_stop이 이걸 기다렸다가 녹음을 닫음
stt_flushed.set()

# 녹음(원본음성) — 보정 작업의 재료. 회의 중에만 연다.
wav_writer = None
wav_writer_lock = threading.Lock()

# 자유 메모 — 안건에 묶이지 않는 내용을 적어두는 공간. 사용자가 직접 쓰고 고치는 것이라
# Claude는 건드리지 않는다.
notes_text: str = ""
notes_lock = threading.Lock()

current_meeting: dict | None = None   # {"title","company","topic","dir"}

# =============================================================
# Whisper lazy init
# =============================================================
_whisper = None
whisper_ready = threading.Event()
VOCAB_KW: dict = {}


def get_whisper():
    global _whisper, VOCAB_KW
    if _whisper is None:
        print(f"[Whisper] {WHISPER_MODEL} 모델 로드 중... "
              f"(스레드 {WHISPER_THREADS}개, 처음 한 번은 내려받느라 몇 분 걸립니다)")
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                cpu_threads=WHISPER_THREADS)
        if VOCAB_HINT:
            import inspect
            if "hotwords" in inspect.signature(_whisper.transcribe).parameters:
                VOCAB_KW = {"hotwords": VOCAB_HINT}
            else:
                VOCAB_KW = {"initial_prompt": VOCAB_HINT}
            print(f"[받아쓰기] 고유명사 힌트 방식: {list(VOCAB_KW)[0]}")
        whisper_ready.set()
        print("[Whisper] 준비 완료")
    return _whisper


def extract_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        return text[first:last + 1]
    return text


def sanitize_filename(s: str) -> str:
    """파일·폴더명에 못 쓰는 문자를 지운다. 빈 값이면 빈 문자열을 그대로 돌려준다
    (회사명처럼 선택 입력 항목은 비어 있는 게 정상이므로 여기서 기본값을 채우지 않음)."""
    s = (s or "").strip()
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# =============================================================
# 사용자 설정 — 저장 위치 / 노션 연동 (설정.json, 설정 페이지에서 편집)
# =============================================================
def load_settings() -> dict:
    data = {"save_dir": "", "notion_page_url": "", "notion_api_key": "", "correction_enabled": True}
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[설정] 설정.json을 읽지 못해 기본값으로 진행합니다: {e}")
    return data


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


settings_lock = threading.Lock()
settings = load_settings()


def current_meeting_log_dir() -> Path:
    """회의 기록 저장 위치. 설정 페이지에서 바꿀 수 있다. 지정한 폴더를 못 쓰게 되면
    (외장 드라이브 분리 등) 조용히 기본 위치로 돌아간다 — 회의록을 통째로 못 만드는
    것보다 낫다."""
    with settings_lock:
        custom = (settings.get("save_dir") or "").strip()
    if custom:
        p = Path(custom)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception as e:
            print(f"[설정] 지정한 저장 위치({custom})를 쓸 수 없어 기본 위치로 대체합니다: {e}")
    DEFAULT_MEETING_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_MEETING_LOG_DIR

# =============================================================
# 노션 연동 — 회의록을 지정한 노션 페이지에 그대로 작성
# =============================================================
NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


def extract_notion_page_id(url_or_id: str) -> str | None:
    """노션 페이지 URL 또는 ID 문자열에서 32자리 페이지 ID를 뽑아 UUID 형식(대시 포함)으로
    돌려준다. 못 찾으면 None."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", s)
    if m:
        return m.group(1).lower()
    hex_only = re.sub(r"[^0-9a-fA-F]", "", s)
    # URL 맨 끝 32자리(하이픈 없는 형태)를 페이지 ID로 본다 — 노션 URL 관례
    m2 = re.search(r"([0-9a-fA-F]{32})$", hex_only)
    if not m2:
        return None
    h = m2.group(1).lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def notion_request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{NOTION_API}{path}",
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        if e.code == 404:
            raise RuntimeError("페이지를 찾을 수 없습니다 — 노션 페이지에서 '연결 추가'로 이 통합(integration)을 공유했는지 확인하세요.")
        if e.code == 401:
            raise RuntimeError("API 키가 올바르지 않습니다.")
        raise RuntimeError(detail or f"노션 API 오류 (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"노션에 연결할 수 없습니다: {e.reason}")


def _rich_text(text: str, bold: bool = False) -> list:
    # 노션 rich_text 한 조각은 2000자 제한 — 넘으면 잘라서 여러 조각으로 나눈다.
    text = text or ""
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
    return [{"type": "text", "text": {"content": c}, "annotations": {"bold": bold}} for c in chunks]


def _inline_richtext(line: str) -> list:
    """**볼드**만 지원하는 가벼운 인라인 변환 — 이 앱이 만드는 회의록 형식에서
    굵게는 이 정도만 쓰인다."""
    parts = re.split(r"(\*\*[^*]+\*\*)", line)
    out = []
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**") and len(p) > 4:
            out.extend(_rich_text(p[2:-2], bold=True))
        else:
            out.extend(_rich_text(p))
    return out or _rich_text("")


def markdown_to_notion_blocks(md: str) -> list:
    """회의록 마크다운을 노션 블록 배열로 바꾼다. 범용 마크다운 변환기가 아니라,
    이 앱이 실제로 만드는 형식(#/##/###, -, |표|, **볼드**, > 인용, ---)만 정확히
    처리하는 것이 목표다."""
    blocks: list = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        if stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _inline_richtext(stripped[4:])}})
            i += 1
            continue

        if stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _inline_richtext(stripped[3:])}})
            i += 1
            continue

        if stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _inline_richtext(stripped[2:])}})
            i += 1
            continue

        if stripped.startswith("> "):
            blocks.append({"object": "block", "type": "quote",
                           "quote": {"rich_text": _inline_richtext(stripped[2:])}})
            i += 1
            continue

        if stripped.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _inline_richtext(stripped[2:])}})
            i += 1
            continue

        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows = [[c.strip() for c in ln.strip("|").split("|")] for ln in table_lines]
            if len(rows) >= 2 and all(re.fullmatch(r":?-{1,}:?", c or "-") for c in rows[1]):
                rows.pop(1)  # |---|---| 구분선 제거
            if rows:
                width = max(len(r) for r in rows)
                table_rows = []
                for r in rows:
                    r = r + [""] * (width - len(r))
                    table_rows.append({"object": "block", "type": "table_row",
                                       "table_row": {"cells": [_inline_richtext(c) for c in r]}})
                blocks.append({"object": "block", "type": "table",
                              "table": {"table_width": width, "has_column_header": True,
                                       "has_row_header": False, "children": table_rows}})
            continue

        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": _inline_richtext(stripped)}})
        i += 1

    return blocks


def push_minutes_to_notion(markdown_text: str, display_title: str) -> dict:
    """settings에 페이지 주소·API 키가 모두 설정돼 있을 때만 실제로 시도한다.
    실패해도 예외를 밖으로 던지지 않는다 — 로컬 회의록 저장은 이미 끝난 뒤라
    노션 쪽 문제가 그걸 되돌리면 안 된다."""
    with settings_lock:
        page_url = (settings.get("notion_page_url") or "").strip()
        api_key = (settings.get("notion_api_key") or "").strip()
    if not page_url or not api_key:
        return {"status": "idle", "error": ""}
    page_id = extract_notion_page_id(page_url)
    if not page_id:
        return {"status": "error", "error": "설정에 저장된 노션 페이지 주소에서 페이지 ID를 찾지 못했습니다."}
    try:
        blocks = markdown_to_notion_blocks(markdown_text)
        for start in range(0, len(blocks), 100):  # 노션은 한 번에 최대 100개 children
            notion_request("PATCH", f"/blocks/{page_id}/children", api_key,
                           {"children": blocks[start:start + 100]})
        print(f"[Notion] {display_title} 회의록을 페이지에 작성했습니다: {page_url}")
        return {"status": "ready", "error": ""}
    except Exception as e:
        print(f"[Notion] 작성 실패: {e}")
        return {"status": "error", "error": str(e)}

# =============================================================
# 녹음 저장 — 보정 작업의 재료 (16kHz 모노 WAV, 실시간 STT가 이미 쓰는 표본화율
# 그대로라 별도 변환 없이 그대로 저장한다 — Whisper 자체가 16kHz로 처리하므로
# 더 높은 표본화율로 저장해도 보정 품질에는 도움이 안 되고 파일만 커진다)
# =============================================================
def open_recording(out_dir: Path, stem: str) -> str:
    global wav_writer
    path = out_dir / f"{RECORDING_PREFIX}{stem}.wav"
    w = wave.open(str(path), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)  # 16-bit PCM
    w.setframerate(SAMPLE_RATE)
    with wav_writer_lock:
        wav_writer = w
    return path.name


def write_recording(audio: np.ndarray):
    with wav_writer_lock:
        if wav_writer is None:
            return
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        try:
            wav_writer.writeframes(pcm16.tobytes())
        except Exception as e:
            print(f"[녹음] 쓰기 실패: {e}")


def close_recording():
    global wav_writer
    with wav_writer_lock:
        if wav_writer is not None:
            try:
                wav_writer.close()
            except Exception as e:
                print(f"[녹음] 닫기 실패: {e}")
            wav_writer = None


def set_correction_status(status: str):
    global correction_status
    correction_status = {"status": status}

# =============================================================
# 오디오 / STT 스레드
# =============================================================
def audio_callback(indata, frames, time_info, status):
    if running.is_set() and not paused.is_set():
        audio_q.put(indata.copy().flatten())


def audio_loop():
    """마이크 장치를 열어 계속 듣는다. 실행 중 실패하면(장치가 뽑혔다/다른 앱이
    점유했다/처음부터 못 열었다) 조용히 죽지 않고, 장치를 새로 찾아 재시도한다 —
    장치 인덱스는 재부팅·USB 재연결 등으로 실행마다 바뀔 수 있어 최초 1회만 찾으면
    한 번 실패한 뒤 영영 복구되지 않았었다."""
    global CAPTURE_DEVICE, CAPTURE_RATE
    retry_delay = 3
    consecutive_failures = 0
    # WASAPI 마이크(특히 인텔 스마트 사운드의 가상 "마이크 배열")가 다른 프로그램에
    # 단독 모드로 점유돼 있으면 몇 번을 재시도해도 같은 이유로 계속 실패한다.
    # 그럴 땐 품질이 낮아지더라도(빔포밍 등 드라이버 처리 없이 날것 신호) 아예 안
    # 잡히는 것보단 나으므로, 연속 실패 시 시스템 기본 장치로 폴백해서라도 듣는다.
    WASAPI_FAIL_FALLBACK_AFTER = 3
    while not shutdown.is_set():
        use_fallback = consecutive_failures >= WASAPI_FAIL_FALLBACK_AFTER
        if use_fallback:
            CAPTURE_DEVICE, CAPTURE_RATE = None, SAMPLE_RATE
        else:
            CAPTURE_DEVICE, CAPTURE_RATE = find_capture_device()
        try:
            device_label = (sd.query_devices(CAPTURE_DEVICE)["name"]
                            if CAPTURE_DEVICE is not None else "시스템 기본 장치")
            with sd.InputStream(samplerate=CAPTURE_RATE, channels=1, device=CAPTURE_DEVICE,
                                callback=audio_callback,
                                blocksize=int(CAPTURE_RATE * 0.5)):
                consecutive_failures = 0
                note = " (WASAPI 실패로 폴백, 인식 품질이 낮을 수 있음)" if use_fallback else ""
                set_audio_status(True, "", device_label + note)
                print(f"[Audio] 캡처 시작: {device_label}{note} ({CAPTURE_RATE}Hz)")
                while not shutdown.is_set():
                    time.sleep(0.1)
            return  # shutdown으로 정상 종료
        except Exception as e:
            consecutive_failures += 1
            hint = ("다른 프로그램(Zoom/Teams/Discord/브라우저 등)이 마이크를 점유하고 "
                    "있는지 확인하세요. " if not use_fallback else "")
            print(f"[Audio] 에러: {e} — {hint}{retry_delay}초 후 재시도합니다 "
                  f"(연속 {consecutive_failures}회)")
            set_audio_status(False, f"{hint}{e}".strip())
            time.sleep(retry_delay)


def stt_loop():
    model = get_whisper()
    buffer = []
    last_process = time.time()
    while not shutdown.is_set():
        try:
            chunk = audio_q.get(timeout=0.3)
            buffer.append(chunk)
            stt_flushed.clear()
        except queue.Empty:
            pass
        now = time.time()
        # 평소엔 CHUNK_SECONDS마다 처리하지만, 회의가 막 끝난 직후(stt_finalize)는
        # 타이머를 기다리지 않고 바로 처리한다 — 안 그러면 끄기 직전 몇 초 분량이
        # 녹음·보정에도 전혀 안 남고 통째로 버려진다.
        if buffer and (now - last_process >= CHUNK_SECONDS or stt_finalize.is_set()):
            audio = np.concatenate(buffer).astype(np.float32)
            buffer = []
            last_process = now

            limit = int(CAPTURE_RATE * CHUNK_SECONDS * MAX_BACKLOG_CHUNKS)
            if len(audio) > limit:
                dropped = (len(audio) - limit) / CAPTURE_RATE
                audio = audio[-limit:]
                print(f"[STT] ⚠️밀림 — 오래된 음성 {dropped:.0f}초를 버리고 현재 시점으로 따라붙습니다. "
                      f"자주 뜨면 WHISPER_MODEL을 'small'로 낮추세요")
            audio = resample_to_model_rate(audio)

            # 녹음 저장 + 보정 작업용 큐 — 회의 중에 실제로 잡힌 소리는 화면에 실시간
            # 자막으로 보여줄지와 무관하게 항상 남긴다(무음 구간 포함, 보정 파이프라인이
            # 스스로 vad_filter로 걸러내므로 여기서 미리 자르지 않음).
            write_recording(audio)
            correction_q.put(audio.copy())

            # 반면 실시간 자막은 "지금도 회의 중인가"를 확인한다 — 종료 버튼을 누른
            # 뒤에 뒤늦게 자막이 뜨는 것(위 녹음과 달리 사람이 바로 보는 화면이라
            # 혼란을 준다)을 막기 위함.
            if not (running.is_set() and not paused.is_set()):
                continue

            peak = np.abs(audio).max()
            if peak < SILENCE_THRESHOLD:
                continue
            global last_sound_at
            last_sound_at = time.time()
            if peak < GAIN_TARGET_PEAK:
                gain = min(GAIN_TARGET_PEAK / peak, GAIN_MAX)
                audio = np.clip(audio * gain, -1.0, 1.0)
            try:
                t0 = time.time()
                segments, _ = model.transcribe(
                    audio, language="ko",
                    beam_size=BEAM_SIZE,
                    vad_filter=True,
                    # 조용하거나 잡음 섞인 구간에서 같은 구절(특히 받아쓰기 힌트 단어)을
                    # 계속 반복하는 환각 현상을 줄인다 — 실제 회의에서 "한국교육학술정보원"
                    # 같은 힌트 단어가 근거 없이 반복 출력되는 것을 확인해 추가함.
                    condition_on_previous_text=False,
                    **VOCAB_KW,
                )
                text = " ".join(s.text for s in segments).strip()
                took = time.time() - t0
                if len(text) > 3:
                    warn = "  ⚠️밀림주의(모델을 낮추세요)" if took > CHUNK_SECONDS * 0.85 else ""
                    print(f"[STT] ({took:.1f}초{warn}) {text}")
                    transcript_q.put({"t": datetime.now().isoformat(), "text": text})
                    caption_q.put(text)
            except Exception as e:
                print(f"[STT] 에러: {e}")
        if not buffer:
            stt_flushed.set()

# =============================================================
# 보정 스레드 — 실시간(stt_loop)과 동시에 돌아가는 2차 받아쓰기
#
# 실시간은 "빠르게 텍스트를 보여주는 것"이 목적이라 10초씩 잘라 처리한다 — 그 경계에서
# 단어가 잘리거나 문맥이 짧아 오탈자가 나기 쉽다. 이 스레드는 같은 오디오를 60초 단위로
# 더 크게 모아서, condition_on_previous_text=True(앞뒤 문맥 참고)로 한 번 더 받아써서
# 더 정확한 버전을 따로 만든다. 실시간 화면에는 반영하지 않고(그러면 자막이 몇 초 전
# 내용으로 갑자기 바뀌어 혼란스러움), 별도 파일로 저장하고 회의록 작성 시 이 보정본을
# 우선 사용한다.
# =============================================================
def correction_loop():
    buffer: list[np.ndarray] = []
    buffered_samples = 0
    while not shutdown.is_set():
        try:
            chunk = correction_q.get(timeout=0.5)
            buffer.append(chunk)
            buffered_samples += len(chunk)
            correction_caught_up.clear()
        except queue.Empty:
            pass

        with settings_lock:
            enabled = bool(settings.get("correction_enabled", True))

        if not enabled:
            # 꺼져 있으면 큐만 비우고(밀리지 않게) 처리는 하지 않는다
            buffer = []
            buffered_samples = 0
            correction_caught_up.set()
            continue

        window_ready = buffered_samples >= CORRECTION_WINDOW_SECONDS * SAMPLE_RATE
        finalize_ready = correction_finalize.is_set() and buffer

        if buffer and (window_ready or finalize_ready):
            audio = np.concatenate(buffer).astype(np.float32)
            buffer = []
            buffered_samples = 0
            if np.abs(audio).max() >= SILENCE_THRESHOLD:
                set_correction_status("running")
                try:
                    model = get_whisper()
                    t0 = time.time()
                    segments, _ = model.transcribe(
                        audio, language="ko",
                        beam_size=CORRECTION_BEAM_SIZE,
                        vad_filter=True,
                        condition_on_previous_text=True,
                        **VOCAB_KW,
                    )
                    text = " ".join(s.text for s in segments).strip()
                    if text:
                        with correction_lock:
                            corrected_transcript.append({"t": datetime.now().isoformat(), "text": text})
                        print(f"[보정] ({time.time()-t0:.1f}초, {len(audio)/SAMPLE_RATE:.0f}초 분량) {text[:60]}")
                except Exception as e:
                    print(f"[보정] 에러: {e}")
                set_correction_status("idle")

        if not buffer:
            correction_caught_up.set()

# =============================================================
# 안건 진행 상황 갱신 스레드 — 이 프로그램의 핵심
#
# 안건 "제목"은 언제나 사용자(또는 이 함수가 새로 감지해 추가한 것)가 소유한다.
# Claude 호출 결과는 status/summary/decision/action_items만 병합하고, title은 절대
# 덮어쓰지 않는다 — 그래야 사용자가 회의 중 제목을 고쳐도 안전하게 유지된다.
# =============================================================
def update_board(new_text: str):
    global next_auto_id
    if not new_text.strip():
        return
    with agenda_lock:
        snapshot = [
            {"id": it["id"], "title": it["title"], "status": it["status"],
             "summary": it["summary"], "decision": it["decision"],
             "action_items": it["action_items"]}
            for it in agenda_items
        ]
    agenda_json = json.dumps(snapshot, ensure_ascii=False)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"[오늘 안건 목록]\n{agenda_json}\n\n"
        f"[새로 들린 내용]\n{new_text}\n\n"
        "위 내용을 반영해 JSON 한 개로만 응답하세요."
    )
    content = ""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", "--model", CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT, shell=False,
            creationflags=CLI_CREATIONFLAGS,
        )
        content = extract_json(result.stdout or "")
        if not content:
            return
        data = json.loads(content)
        updates = data.get("updates", [])
        new_items = data.get("new_items", [])

        with agenda_lock:
            live_by_id = {it["id"]: it for it in agenda_items}
            if isinstance(updates, list):
                for u in updates:
                    it = live_by_id.get(u.get("id"))
                    if it is None:
                        continue   # 그 사이 삭제됐거나 알 수 없는 id — 무시(안전)
                    was_decided = it.get("status") == "decided"
                    if u.get("status") in ("discussing", "decided", "pending"):
                        it["status"] = u["status"]
                    if u.get("summary"):
                        it["summary"] = u["summary"]
                    if u.get("decision"):
                        it["decision"] = u["decision"]
                    if u.get("action_items"):
                        it["action_items"] = u["action_items"]
                    if not was_decided and it.get("status") == "decided":
                        notice_q.put({"type": "decided", "title": it["title"], "decision": it.get("decision", "")})
                        print(f"[안건정리] ✅ 결정: {it['title']}")
            if isinstance(new_items, list):
                for n in new_items:
                    title = (n.get("title") or "").strip()
                    if not title:
                        continue
                    iid = f"a{next_auto_id}"
                    next_auto_id += 1
                    agenda_items.append({
                        "id": iid, "title": title, "source": "auto",
                        "status": n.get("status") or "discussing",
                        "summary": n.get("summary", ""), "decision": n.get("decision", ""),
                        "action_items": n.get("action_items") or [],
                    })
                    notice_q.put({"type": "new", "title": title})
                    print(f"[안건정리] 🆕 새 안건: {title}")
    except subprocess.TimeoutExpired:
        print("[안건정리] 타임아웃 — 다음 갱신에서 계속")
    except json.JSONDecodeError:
        print(f"[안건정리] JSON 파싱 실패: {content[:200] if content else '(빈 응답)'}")
    except Exception as e:
        print(f"[안건정리] 에러: {e}")


def board_loop():
    if not CLAUDE_CLI:
        print("[Claude] claude 명령어를 찾지 못함 — 안건 정리 스레드 비활성")
        return
    buffer: list[str] = []
    last_update = time.time()
    while not shutdown.is_set():
        try:
            item = transcript_q.get(timeout=0.3)
            # stt_loop가 정상이라면 회의 중이 아닐 땐 여기로 아무것도 안 들어와야
            # 하지만, 혹시 몰라 한 번 더 막아둔다 — 회의록에 엉뚱한 내용이 섞이는 것
            # (다른 회의의 발언이 이번 회의 기록에 끼는 것)보다는 조용히 버리는 게 낫다.
            if running.is_set() and not paused.is_set():
                buffer.append(item["text"])
                with session_lock:
                    session_transcripts.append(item)
        except queue.Empty:
            pass
        now = time.time()
        if buffer and (now - last_update >= BOARD_INTERVAL):
            chunk_text = " ".join(buffer)
            buffer = []
            last_update = now
            update_board(chunk_text)

# =============================================================
# 회의록 생성 (회의 종료 시 1회)
# =============================================================
BUILTIN_MINUTES_PROMPT = """당신은 회의를 기록·정리하는 전문가입니다.
아래 [안건판](회의 중 실시간으로 정리된 마지막 상태)과 [회의 받아쓰기 전문]을 읽고,
다른 사람과 공유할 수 있는 회의록을 작성하세요.

[반드시 지킬 것]
- 받아쓰기는 음성 자동 변환이라 오탈자가 있습니다. 앞뒤 맥락으로 자연스럽게 고쳐 읽되,
  없는 내용을 지어내지 마세요. 알아들을 수 없는 대목은 억지로 채우지 말고 넘어가세요.
- 한 마이크로 여러 사람 목소리가 섞여 화자 구분이 없습니다.
  발언 내용으로 누가 말했는지 짐작할 수 있으면 짐작하되, 확실하지 않으면 특정하지 마세요.
- [안건판]은 실시간으로 정리된 것이라 다소 거칠 수 있습니다. 받아쓰기 전문을 함께 참고해
  빠지거나 부정확한 내용을 보완하세요.
- [참석자가 직접 남긴 메모]가 있다면, 이는 사람이 직접 쓴 것이라 받아쓰기보다 정확합니다.
  받아쓰기 내용과 다르면 메모 쪽을 우선하세요.
- 결정되지 않고 넘어간 사항은 "미결" 또는 "보류"로 분명히 표시하세요.
- 참석자 명단은 음성만으로 알 수 없으니 임의로 적지 말고 "확인 필요"로 표시하세요.
- 회의 내용을 평가하거나 잘잘못을 판단하지 마세요. 오간 내용을 사실대로 정리하는 것이 전부입니다.

[출력 형식 — 아래 마크다운 그대로. 다른 말 붙이지 마세요]
## 한눈에 보기
- (이 회의를 한두 문장으로 요약)

## 안건별 정리
### 1. (안건명) — (결정 / 보류 / 진행중)
- 논의 내용: (2~4줄)
- 결정 사항: (없으면 "결정 없음")
- 액션 아이템: (담당자) (할 일) (기한) — (없으면 생략)

## 전체 액션 아이템
| 할 일 | 담당자 | 기한 |
|---|---|---|
(없으면 "없음"이라고만 적으세요)

## 다음 회의 전 확인할 것
- (미결·보류 사항, 확인이 더 필요한 것)
"""


def load_minutes_prompt() -> str:
    if not MINUTES_FORMAT_PATH.exists():
        return BUILTIN_MINUTES_PROMPT
    raw = MINUTES_FORMAT_PATH.read_text(encoding="utf-8")
    marker = "─────"
    if marker in raw:
        raw = raw.rsplit(marker, 1)[-1]
    body = chr(10).join(ln for ln in raw.splitlines() if not ln.strip().startswith("※"))
    body = body.strip()
    if len(body) < 100:
        print("[회의록] 형식 파일이 너무 짧아 내장 규칙을 씁니다")
        return BUILTIN_MINUTES_PROMPT
    return body


MINUTES_PROMPT = load_minutes_prompt()


def build_minutes(transcript_text: str, board_items: list, notes: str, display_title: str) -> str:
    notes_block = f"\n[참석자가 회의 중 직접 남긴 메모 — 받아쓰기와 무관하게 참고할 것]\n{notes}\n" if notes.strip() else ""
    prompt = (f"{MINUTES_PROMPT}\n\n"
              f"[안건판 — 회의 종료 시점]\n{json.dumps(board_items, ensure_ascii=False)}\n"
              f"{notes_block}\n"
              f"[회의 받아쓰기 전문 — {display_title}]\n{transcript_text}\n\n"
              "위 형식대로 회의록을 작성하세요.")
    r = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", CLAUDE_MODEL],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=MINUTES_TIMEOUT, shell=False,
        creationflags=CLI_CREATIONFLAGS,
    )
    return (r.stdout or "").strip()


def save_transcript(out_dir: Path, stem: str, display_title: str) -> str:
    lines = [f"# {display_title} 회의 녹취록 — {datetime.fromtimestamp(session_start).strftime('%Y-%m-%d %H:%M')}",
             "",
             "마이크로 받아쓴 전문입니다. 음성 자동 변환이라 오탈자가 있을 수 있고,",
             "여러 사람 목소리가 한 마이크로 섞여 들어와 화자 구분은 되어 있지 않습니다.",
             "", "---", ""]
    with session_lock:
        items = list(session_transcripts)
    for it in items:
        t = (it.get("t", "") or "")[11:19]
        lines.append(f"**{t}**  {it.get('text', '')}")
    path = out_dir / f"{TRANSCRIPT_PREFIX}{stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_corrected_transcript(out_dir: Path, stem: str, display_title: str) -> str:
    with correction_lock:
        items = list(corrected_transcript)
    lines = [f"# {display_title} 회의 녹취록 — 보정본 ({datetime.fromtimestamp(session_start).strftime('%Y-%m-%d %H:%M')})",
             "",
             "실시간 받아쓰기본을 저장된 녹음으로 다시 확인해 오탈자를 줄인 버전입니다. "
             "60초 단위로 더 넓은 문맥을 보고 다시 받아썼습니다.",
             "", "---", ""]
    for it in items:
        t = (it.get("t", "") or "")[11:19]
        lines.append(f"**{t}**  {it.get('text', '')}")
    path = out_dir / f"{CORRECTED_TRANSCRIPT_PREFIX}{stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_board_snapshot(out_dir: Path, stem: str) -> str:
    with agenda_lock:
        snapshot = list(agenda_items)
    path = out_dir / f"{BOARD_SNAPSHOT_PREFIX}{stem}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_notes_file(out_dir: Path, stem: str) -> str:
    with notes_lock:
        text = notes_text
    if not text.strip():
        return ""
    path = out_dir / f"{MEMO_PREFIX}{stem}.md"
    path.write_text(text, encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_minutes(out_dir: Path, stem: str, display_title: str, when_label: str, dur: str,
                  transcript_text: str, board_snapshot: list, notes: str):
    global minutes_state, notion_status
    if not transcript_text.strip():
        minutes_state = {"status": "error", "file": "", "error": "받아쓴 내용이 없습니다."}
        return
    minutes_state = {"status": "running", "file": "", "error": ""}
    notion_status = {"status": "idle", "error": ""}
    print(f"[회의록] {display_title} — 회의록을 만드는 중입니다 (1~2분 안팎)")

    with settings_lock:
        correction_enabled = bool(settings.get("correction_enabled", True))
    final_transcript_text = transcript_text
    if correction_enabled:
        print("[보정] 남은 구간을 마저 확인하는 중...")
        caught_up = correction_caught_up.wait(timeout=CORRECTION_CATCHUP_TIMEOUT)
        if not caught_up:
            print(f"[보정] {CORRECTION_CATCHUP_TIMEOUT}초 안에 못 끝나서 실시간본으로 회의록을 작성합니다")
        with correction_lock:
            corrected_items = list(corrected_transcript)
        if corrected_items:
            corrected_text = "\n".join(
                f"[{it.get('t', '')[11:19]}] {it.get('text', '')}" for it in corrected_items
            )
            if corrected_text.strip():
                final_transcript_text = corrected_text
                print("[보정] 보정된 녹취록으로 회의록을 작성합니다")
        save_corrected_transcript(out_dir, stem, display_title)

    try:
        body = build_minutes(final_transcript_text, board_snapshot, notes, display_title)
        if not body:
            raise RuntimeError("응답이 비었습니다")
        head = (f"# {display_title} 회의록\n\n"
                f"**일시** {when_label}  ·  **소요** {dur}\n\n"
                f"> 음성 자동 변환 기록을 바탕으로 정리한 회의록입니다. "
                f"수치·날짜·담당자는 원본 대화록과 대조해 확인하세요.\n\n---\n\n")
        full_text = head + body + "\n"
        path = out_dir / f"{MINUTES_PREFIX}{stem}.md"
        path.write_text(full_text, encoding="utf-8")
        minutes_state = {"status": "ready", "file": path.name, "error": ""}
        print(f"[저장] {path}")

        with settings_lock:
            notion_ready = bool((settings.get("notion_page_url") or "").strip()
                                and (settings.get("notion_api_key") or "").strip())
        if notion_ready:
            notion_status = {"status": "running", "error": ""}
            notion_status = push_minutes_to_notion(full_text, display_title)
    except Exception as e:
        minutes_state = {"status": "error", "file": "", "error": str(e)}
        print(f"[회의록] 실패: {e}")

# =============================================================
# 탭을 닫으면 자동 종료
#
# 화면(index.html)은 1초마다 /api/state를 폴링한다 — 역으로, 한동안 어떤
# 요청도 안 들어오면 "탭이 다 닫혔다"고 볼 수 있다. 탭을 닫는 즉시 끄면 실수로
# 닫았을 때·새로고침할 때 위험하니 여유 시간을 두고, 회의 중이거나 회의록·노션
# 작성이 진행 중이면 그것부터 끝날 때까지 기다린다.
# =============================================================
SHUTDOWN_IDLE_GRACE = 20  # 초 — 이만큼 요청이 없으면 탭이 닫힌 것으로 봄
last_activity_at = time.time()


def shutdown_watchdog_loop():
    global last_activity_at
    while not shutdown.is_set():
        time.sleep(5)
        idle_for = time.time() - last_activity_at
        if idle_for < SHUTDOWN_IDLE_GRACE:
            continue
        busy = (
            running.is_set()
            or minutes_state.get("status") == "running"
            or notion_status.get("status") == "running"
            or correction_status.get("status") == "running"
        )
        if busy:
            continue  # 작업이 남아있으면 기다린다 — idle_for는 다음 루프에서 다시 확인
        print(f"[자동종료] {idle_for:.0f}초간 접속이 없어 서버를 종료합니다")
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        os._exit(0)

# =============================================================
# Flask 웹 서버
# =============================================================
app = Flask(__name__)


@app.before_request
def _mark_activity():
    global last_activity_at
    last_activity_at = time.time()


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/agenda")
def api_agenda_get():
    with agenda_lock:
        items = list(agenda_items)
    return jsonify({"items": items})


@app.route("/api/agenda/add", methods=["POST"])
def api_agenda_add():
    global next_manual_id
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "안건 제목을 입력하세요."})
    with agenda_lock:
        iid = f"m{next_manual_id}"
        next_manual_id += 1
        agenda_items.append({"id": iid, "title": title, "source": "manual",
                             "status": "pending", "summary": "", "decision": "", "action_items": []})
    return jsonify({"ok": True, "id": iid})


@app.route("/api/agenda/update", methods=["POST"])
def api_agenda_update():
    """제목·요약·결정·상태 중 보내온 필드만 고친다.
    받아쓰기·AI 요약에 오탈자가 섞이는 경우가 많아, 사용자가 화면에서 직접 고칠 수 있게 한다."""
    body = request.get_json(silent=True) or {}
    iid = (body.get("id") or "").strip()
    if not iid:
        return jsonify({"ok": False, "error": "잘못된 요청입니다."})
    with agenda_lock:
        for it in agenda_items:
            if it["id"] == iid:
                if "title" in body:
                    title = (body.get("title") or "").strip()
                    if title:
                        it["title"] = title
                if "summary" in body:
                    it["summary"] = (body.get("summary") or "").strip()
                if "decision" in body:
                    it["decision"] = (body.get("decision") or "").strip()
                if body.get("status") in ("discussing", "decided", "pending"):
                    it["status"] = body["status"]
                return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "안건을 찾지 못했습니다."})


@app.route("/api/agenda/delete", methods=["POST"])
def api_agenda_delete():
    iid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    with agenda_lock:
        before = len(agenda_items)
        agenda_items[:] = [it for it in agenda_items if it["id"] != iid]
        removed = before - len(agenda_items)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/agenda/clear", methods=["POST"])
def api_agenda_clear():
    with agenda_lock:
        agenda_items.clear()
    return jsonify({"ok": True})


@app.route("/api/notes")
def api_notes_get():
    with notes_lock:
        return jsonify({"text": notes_text})


@app.route("/api/notes", methods=["POST"])
def api_notes_save():
    global notes_text
    text = (request.get_json(silent=True) or {}).get("text", "")
    with notes_lock:
        notes_text = text
    return jsonify({"ok": True})


def current_elapsed() -> int:
    """경과 시간 — 일시정지돼 있던 구간은 빼고 계산한다."""
    if not (running.is_set() and session_start):
        return 0
    now = time.time()
    paused_extra = (now - pause_started) if paused.is_set() else 0.0
    return max(0, int((now - session_start) - total_paused - paused_extra))


@app.route("/api/pause", methods=["POST"])
def api_pause():
    global pause_started
    if running.is_set() and not paused.is_set():
        pause_started = time.time()
        paused.set()
    return jsonify({"ok": True, "paused": paused.is_set()})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    global total_paused
    if running.is_set() and paused.is_set():
        total_paused += time.time() - pause_started
        paused.clear()
    return jsonify({"ok": True, "paused": paused.is_set()})


@app.route("/api/start", methods=["POST"])
def api_start():
    """claude CLI가 없어도 회의는 시작할 수 있다 — 실시간 받아쓰기(Whisper)는 클로드와
    무관하게 동작하는 별개 기능이라, CLI 하나 없다고 이 앱 전체를 못 쓰게 막을 이유가
    없다. 다만 안건 자동 정리·회의록 자동 생성은 claude CLI가 있어야만 되고(board_loop,
    save_minutes에서 각각 확인), 그건 화면 배너("claude_ok": false)로 안내된다."""
    global session_start, session_transcripts, minutes_state, current_meeting, total_paused
    if not running.is_set():
        paused.clear()
        total_paused = 0.0
        body = request.get_json(silent=True) or {}
        company = sanitize_filename(body.get("company") or "")
        topic = sanitize_filename(body.get("topic") or "") or "회의"
        date_str = datetime.now().strftime("%Y-%m-%d")

        log_dir = current_meeting_log_dir()
        stem = f"{company}_{topic}" if company else topic
        folder_name = f"{date_str}_{stem}"
        out_dir = log_dir / folder_name
        if out_dir.exists():
            # 같은 날 같은 기업·주제로 이미 회의를 저장한 적이 있으면 시각을 덧붙여 구분
            folder_name = f"{date_str}_{stem}_{datetime.now().strftime('%H%M')}"
            out_dir = log_dir / folder_name

        display_title = f"{company} · {topic}" if company else topic
        current_meeting = {"company": company, "topic": topic, "stem": stem,
                           "display_title": display_title, "dir": out_dir}

        with session_lock:
            session_transcripts = []
        minutes_state = {"status": "idle", "file": "", "error": ""}
        # 안건 목록(agenda_items)·메모(notes_text)는 회의 시작 전에 미리 적어둔 것이므로
        # 여기서 지우지 않는다.

        for q in (audio_q, transcript_q, caption_q, notice_q, correction_q):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        # 보정 작업 + 녹음은 여기서부터 새로 시작 — 폴더를 미리 만들어야 녹음 파일을
        # 바로 쓰기 시작할 수 있다(원래는 종료 시점에만 폴더를 만들었음).
        out_dir.mkdir(parents=True, exist_ok=True)
        with correction_lock:
            corrected_transcript.clear()
        correction_finalize.clear()
        correction_caught_up.clear()
        stt_finalize.clear()
        open_recording(out_dir, stem)

        session_start = time.time()
        running.set()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global last_elapsed
    if running.is_set():
        last_elapsed = current_elapsed()
        running.clear()
        paused.clear()
        # stt_loop에게 "타이머 기다리지 말고 지금 버퍼를 바로 처리해줘"라고 신호를
        # 보내고 끝날 때까지 잠깐 기다린다 — 안 그러면 끄기 직전 몇 초 분량이 녹음
        # 파일에도, 보정본에도 전혀 안 남고 그냥 사라진다.
        stt_finalize.set()
        stt_flushed.wait(timeout=CHUNK_SECONDS + 3)
        stt_finalize.clear()

        # 남은 음성이 audio_q에 더 있으면(위 flush 대기 중에 새로 들어왔을 수 있음)
        # 이제는 확실히 회의가 끝났으니 비운다 — stt_loop가 회의가 끝난 뒤에도 그걸
        # 계속 받아써서 "회의를 안 하는데 혼자 받아쓴다"처럼 보이는 원인이 된다.
        while not audio_q.empty():
            try:
                audio_q.get_nowait()
            except queue.Empty:
                break
        close_recording()
        # 보정 스레드에게 "지금까지 쌓인 것만 마저 처리해줘"라고 알린다 — save_minutes가
        # 이 결과를 기다렸다가 회의록에 쓴다.
        correction_finalize.set()

        if current_meeting is not None:
            out_dir = current_meeting["dir"]
            out_dir.mkdir(parents=True, exist_ok=True)
            stem, display_title = current_meeting["stem"], current_meeting["display_title"]

            transcript_name = save_transcript(out_dir, stem, display_title)
            save_board_snapshot(out_dir, stem)
            save_notes_file(out_dir, stem)

            with session_lock:
                transcript_text = "\n".join(
                    f"[{it.get('t', '')[11:19]}] {it.get('text', '')}" for it in session_transcripts
                )
            when_label = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d %H:%M")
            dur = f"{last_elapsed // 60}분 {last_elapsed % 60}초"
            with agenda_lock:
                board_snapshot = list(agenda_items)
            with notes_lock:
                notes_snapshot = notes_text

            if transcript_text.strip() and CLAUDE_CLI:
                threading.Thread(target=save_minutes,
                                 args=(out_dir, stem, display_title, when_label, dur,
                                       transcript_text, board_snapshot, notes_snapshot),
                                 daemon=True).start()
            return jsonify({"ok": True, "dir": str(out_dir), "transcript_file": transcript_name})
    return jsonify({"ok": True})


@app.route("/api/minutes")
def api_minutes():
    if minutes_state.get("status") != "ready" or not minutes_state.get("file"):
        return jsonify({"ok": False, "status": minutes_state.get("status", "idle"),
                        "error": minutes_state.get("error", ""), "body": ""})
    out_dir = current_meeting["dir"] if current_meeting else current_meeting_log_dir()
    path = out_dir / minutes_state["file"]
    if not path.exists():
        return jsonify({"ok": False, "status": "error",
                        "error": "회의록 파일을 찾지 못했습니다.", "body": ""})
    return jsonify({"ok": True, "status": "ready", "error": "",
                    "file": minutes_state["file"], "body": path.read_text(encoding="utf-8")})


@app.route("/api/state")
def api_state():
    notices = []
    while not notice_q.empty():
        try:
            notices.append(notice_q.get_nowait())
        except queue.Empty:
            break
    captions = []
    while not caption_q.empty():
        try:
            captions.append(caption_q.get_nowait())
        except queue.Empty:
            break

    elapsed = current_elapsed()
    with agenda_lock:
        items = list(agenda_items)
    decided = sum(1 for it in items if it.get("status") == "decided")
    actions = sum(len(it.get("action_items") or []) for it in items)

    with audio_status_lock:
        audio = dict(audio_status)

    no_sound_warning = ""
    if running.is_set() and not paused.is_set() and audio["ok"] and session_start:
        now = time.time()
        since_start = now - session_start
        since_sound = (now - last_sound_at) if last_sound_at else since_start
        if since_start > NO_SOUND_WARN_AFTER and since_sound > NO_SOUND_WARN_AFTER:
            no_sound_warning = "마이크가 열려 있지만 소리가 전혀 잡히지 않고 있습니다. 음소거되었거나 잘못된 입력 장치일 수 있습니다."

    return jsonify({
        "running": running.is_set(),
        "paused": paused.is_set(),
        "whisper_ready": whisper_ready.is_set(),
        "claude_ok": bool(CLAUDE_CLI),
        "audio": audio,
        "no_sound_warning": no_sound_warning,
        "elapsed": elapsed,
        "title": current_meeting["display_title"] if current_meeting else "",
        "board": items,
        "counters": {"items": len(items), "decided": decided, "actions": actions},
        "captions": captions,
        "notices": notices,
        "minutes": minutes_state,
        "notion": notion_status,
        "correction": correction_status,
    })


@app.route("/settings")
def settings_page():
    return send_from_directory(HERE, "settings.html")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with settings_lock:
        s = dict(settings)
    key = s.get("notion_api_key") or ""
    masked = ("•" * max(len(key) - 4, 0) + key[-4:]) if key else ""
    return jsonify({
        "save_dir": s.get("save_dir", ""),
        "default_save_dir": str(DEFAULT_MEETING_LOG_DIR),
        "current_save_dir": str(current_meeting_log_dir()),
        "notion_page_url": s.get("notion_page_url", ""),
        "notion_api_key_masked": masked,
        "notion_api_key_set": bool(key),
        "correction_enabled": bool(s.get("correction_enabled", True)),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    global settings
    body = request.get_json(silent=True) or {}
    with settings_lock:
        new_settings = dict(settings)
        if "save_dir" in body:
            new_settings["save_dir"] = (body.get("save_dir") or "").strip()
        if "notion_page_url" in body:
            new_settings["notion_page_url"] = (body.get("notion_page_url") or "").strip()
        # API 키는 화면에서 안 건드리면(빈 값으로 옴) 기존 값을 그대로 유지한다 —
        # 매번 다시 붙여넣지 않아도 되고, 화면에 평문으로 남겨두지도 않기 위함.
        new_key = (body.get("notion_api_key") or "").strip()
        if new_key:
            new_settings["notion_api_key"] = new_key
        if "correction_enabled" in body:
            new_settings["correction_enabled"] = bool(body.get("correction_enabled"))
        settings = new_settings
        save_settings(settings)
    return jsonify({"ok": True, "current_save_dir": str(current_meeting_log_dir())})


@app.route("/api/settings/reset_save_dir", methods=["POST"])
def api_settings_reset_save_dir():
    global settings
    with settings_lock:
        settings["save_dir"] = ""
        save_settings(settings)
    return jsonify({"ok": True, "current_save_dir": str(DEFAULT_MEETING_LOG_DIR)})


@app.route("/api/settings/test_notion", methods=["POST"])
def api_settings_test_notion():
    """저장 전에도 지금 입력창 값으로 바로 테스트할 수 있게 한다 — 빈 값으로 온
    필드는(특히 API 키) 저장된 값을 대신 쓴다."""
    body = request.get_json(silent=True) or {}
    with settings_lock:
        page_url = (body.get("notion_page_url") or settings.get("notion_page_url") or "").strip()
        api_key = (body.get("notion_api_key") or settings.get("notion_api_key") or "").strip()
    if not page_url or not api_key:
        return jsonify({"ok": False, "error": "페이지 주소와 API 키를 모두 입력하세요."})
    page_id = extract_notion_page_id(page_url)
    if not page_id:
        return jsonify({"ok": False, "error": "페이지 주소에서 페이지 ID를 찾지 못했습니다. 노션에서 페이지를 열고 주소창 URL을 그대로 복사해 붙여넣어 주세요."})
    try:
        info = notion_request("GET", f"/pages/{page_id}", api_key)
        title = ""
        for v in (info.get("properties") or {}).values():
            if v.get("type") == "title" and v.get("title"):
                title = "".join(t.get("plain_text", "") for t in v["title"])
                break
        return jsonify({"ok": True, "page_title": title or "(제목 없음)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """콘솔 창이 없어(pythonw) 닫을 창이 없으므로, 화면의 '서버 종료' 버튼으로
    프로세스를 직접 끝낸다. 응답을 먼저 돌려주고 잠깐 뒤에 죽어야 브라우저가
    '연결 실패'로 보지 않고 정상 응답을 받는다."""
    def _die():
        time.sleep(0.3)
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})

# =============================================================
# 메인
# =============================================================
def main():
    print("=" * 50)
    print("📝 회의 보조 비서 — 실시간 받아쓰기 + 안건 정리")
    print("=" * 50)
    print(f"💾 저장 위치: {current_meeting_log_dir()}")
    print(f"🤖 모델: claude -p --model {CLAUDE_MODEL}")
    print(f"🎤 STT: faster-whisper ({WHISPER_MODEL})")
    print(f"🔗 Claude CLI: {CLAUDE_CLI or '❌ 찾지 못함'}")
    print(f"🌐 주소: http://{HOST}:{PORT}")
    print("=" * 50)

    if not CLAUDE_CLI:
        print("\n⚠️  Claude Code CLI를 찾지 못했습니다 — 실시간 받아쓰기는 그대로 되지만, "
              "안건 자동 정리·회의록 자동 생성은 안 됩니다. 쓰려면 Claude Code 설치·로그인 "
              "후 다시 실행하세요.\n")

    threading.Thread(target=audio_loop, daemon=True).start()
    threading.Thread(target=stt_loop, daemon=True).start()
    threading.Thread(target=correction_loop, daemon=True).start()
    threading.Thread(target=board_loop, daemon=True).start()
    threading.Thread(target=shutdown_watchdog_loop, daemon=True).start()

    if not os.environ.get("QP_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
