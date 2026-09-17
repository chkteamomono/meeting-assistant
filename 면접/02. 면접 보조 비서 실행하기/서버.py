"""
면접 보조 비서 웹앱 — 로컬 서버 (Flask)

구조:
- 백그라운드 스레드(오디오/STT/Claude)는 tkinter 버전과 동일하게 마이크 →
  faster-whisper(로컬 한국어) → `claude` CLI 판정을 수행한다.
- 화면(GUI)만 tkinter → 브라우저로 교체. 서버는 index.html을 띄우고,
  브라우저가 1초마다 /state 를 폴링해서 타이머·카운터·알림(토스트/팝업)을 갱신한다.
- 같은 PC에서 http://127.0.0.1:8000 으로 접속 (음성이 PC 밖으로 나가지 않음).

Claude Code Max 구독을 그대로 사용 → 별도 API 키 불필요.
"""

import os
import sys
import json
import time
import queue
import shutil
import tempfile
import threading
import webbrowser
import subprocess
from pathlib import Path
from datetime import datetime

# === 외부 라이브러리 ===
try:
    import sounddevice as sd
    import numpy as np
    from faster_whisper import WhisperModel
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as e:
    print(f"필수 라이브러리 누락: {e}")
    print("[해결] '1_설치하기.bat' 먼저 실행하세요.")
    input("Enter 누르면 닫힘...")
    sys.exit(1)

# PDF 이력서 읽기(선택) — 없으면 이력서 없이 동작
try:
    from pypdf import PdfReader
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

# 스캔 이력서 변환용(선택) — 없으면 변환만 건너뛰고 나머지는 정상 동작
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

# =============================================================
# 설정
# =============================================================
HERE = Path(__file__).resolve().parent

# 콘솔 창 없이(pythonw) 띄우면 Ctrl+C로 끌 창이 없다 — 실행 중 PID를 파일로 남겨서
# 화면의 "서버 종료" 버튼이나 3_종료하기.bat가 이걸 보고 종료할 수 있게 한다.
PID_PATH = HERE / "실행중.pid"
PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

# 폴더 구조(이름·깊이)는 사용자가 자주 바꾸므로 하드코딩하지 않고 찾아간다.
#   인사관리 보조/
#   ├── 참고자료/직무정보.txt          ← 이걸 기준으로 루트를 잡음
#   ├── 02. 면접 보조 비서(웹)/         ← 이 프로그램 (HERE)
#   └── 03. 면접자 정보/                ← 지원자 폴더들의 상위 (LOG_DIR)
#       ├── 01. 이력서 검토 대상/{이름}/
#       ├── 02. 면접 대상/{이름}/       ← 드롭다운은 여기만 읽음
#       └── 03. 면접 종료/{이름}/
LOG_PREFIX = "면접점검_"      # 이 프로그램이 만든 로그 파일 접두사(이력서로 오인하지 않기 위함)
QUESTION_PREFIX = "맞춤질문_"  # 미리 만들어 둔 맞춤 질문 캐시 파일 접두사
TRANSCRIPT_PREFIX = "면접대화록_"  # 면접 받아쓰기 전문 파일 접두사
REPORT_PREFIX = "면접요약_"     # 공유용 질문·답변 요약 파일 접두사
EVAL_PREFIX = "면접평가_"       # 면접관이 직접 남기는 점수·주관 평가 파일 접두사
NOTION_PREFIX = "면접Notion용_"  # Notion에 붙여넣을 수 있는 형태로 저장하는 파일 접두사

# 면접관이 직접 정한 10점 기준표 — 화면 선택지·저장 파일에 그대로 노출한다.
SCORE_LABELS = {
    10: "매우 만족하며 바로 채용의사 있음, 대부분의 조건 반영 가능",
    9: "약간 만족하며 채용의사 있음, 약간의 원하는 조건 반영 가능",
    8: "이정도면 괜찮을 것 같고, 제시한 조건이 있다면 고민해보겠음",
    7: "그럭저럭 괜찮은 것 같음, 특별한 이슈가 없다면 채용 안정권",
    6: "뽑기에는 애매하고 그렇다고 탈락이라고 하기엔 아쉬움",
    5: "인력이 급하다면 채용할 생각 있음",
    4: "당장 사람 안 뽑아주면 나간다고 하는 상황 아니면 안 뽑음",
    3: "인성or능력 중 하나가 많이 부족하여 뽑으면 문제가 생김",
    2: "인성or능력 모두 부족함",
    1: "채용불가, 두 번 다시 보고 싶지 않음",
}

# 스캔 이력서 자동 변환 설정
RENDER_DPI = 300        # 낮추면 오탈자가 늘어난다 (170dpi에서 대학 이름을 잘못 읽는 것 확인)
OCR_MODEL = "opus"      # sonnet은 한글 이력서에서 오탈자가 눈에 띄게 많았음
PAGE_TIMEOUT = 240      # 한 쪽당 최대 대기 시간(초)
TEXT_PDF_MIN = 50       # 이 이상 글자가 뽑히면 스캔본이 아니라고 판단
# 프롬프트에 넣을 이력서 텍스트 상한.
# 이력서+자기소개서 두 편이면 1만 자를 넘기기 쉬워 8000자에서는 뒷부분이 잘렸다.
# 늘리면 판정 1회당 프롬프트가 커져 응답이 조금 느려진다(8초마다 호출).
RESUME_MAX_CHARS = 16000


def is_stage_dir(name: str) -> bool:
    """'02. 면접 대상' 같은 이름인가. 번호·띄어쓰기가 달라져도 잡히도록 낱말로 판단."""
    return "면접" in name and "대상" in name


def find_root() -> Path:
    """'참고자료' 폴더를 품은 상위 폴더를 '인사관리 보조' 루트로 본다."""
    for d in (HERE, *HERE.parents):
        if (d / "참고자료").is_dir():
            return d
    return HERE.parent


def find_log_dir(root: Path) -> Path:
    """지원자 폴더들이 들어 있는 상위 폴더를 찾는다 (예: '03. 면접자 정보')."""
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    # 1순위: '면접 대상' 류의 단계 폴더를 품고 있는 폴더
    for d in dirs:
        try:
            if any(s.is_dir() and is_stage_dir(s.name) for s in d.iterdir()):
                return d
        except OSError:
            continue
    # 2순위: 이름으로 추정
    for d in dirs:
        if "면접자" in d.name or "면접로그" in d.name:
            return d
    return root / "03. 면접자 정보"


ROOT = find_root()
JOB_INFO_PATH = ROOT / "참고자료" / "직무정보.txt"     # 선택사항(없어도 동작)
QUESTION_PATH = ROOT / "참고자료" / "면접질문.txt"     # 공통 질문 (없으면 질문 탭이 비어 있음)
VOCAB_PATH = ROOT / "참고자료" / "받아쓰기_용어.txt"   # 받아쓰기에 미리 알려줄 업무 용어
REPORT_FORMAT_PATH = ROOT / "참고자료" / "면접요약_형식.txt"  # 요약본 작성 규칙(없으면 내장 규칙)
LOG_DIR = find_log_dir(ROOT)
LOG_DIR.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8000

SAMPLE_RATE = 16000
CHUNK_SECONDS = 10           # 몇 초마다 받아쓰고 Claude로 점검할지
SILENCE_THRESHOLD = 0.008

# 받아쓰기 정확도 설정
#   이 PC에서 실측한 처리 시간 (7.1초 음성 기준, 음성 길이 대비 배수):
#     small           2.6초 (0.37배)  — 빠르지만 고유명사·띄어쓰기 오류 많음
#     medium          7.9초 (1.11배)  — 실시간을 못 따라감. 쓰지 말 것
#     large-v3-turbo  5.9초 (0.83배)  — medium보다 빠르고 정확. 현재 선택
#   turbo가 medium보다 빠른 이유는 디코더 층이 훨씬 얇기 때문입니다.
#
#   되돌리려면 WHISPER_MODEL을 "small"로 바꾸세요.
#   터미널에 "⚠️밀림"이 자주 뜨면 실시간을 못 따라가는 것이니 small로 내리십시오.
WHISPER_MODEL = "large-v3-turbo"
WHISPER_THREADS = os.cpu_count() or 4   # 전 코어 사용 (8코어에서 6.6초 → 5.9초)
BEAM_SIZE = 5                # 1이면 빠르지만 오인식이 늘어난다. 5가 기본 권장값

# 받아쓰기가 밀릴 때 안전장치.
# 처리가 녹음 속도를 못 따라가면 대기 음성이 계속 쌓여, 면접 후반에는
# 몇 분 전 대화를 판정하게 되어 알림이 쓸모없어진다.
# 밀린 양이 이 배수를 넘으면 오래된 음성을 버리고 현재 시점으로 따라붙는다.
MAX_BACKLOG_CHUNKS = 2
CLAUDE_MODEL = "sonnet"      # claude -p --model. opus/sonnet/haiku
CLAUDE_TIMEOUT = 60

ASK_THRESHOLD = 0.8         # 🟡 추가 질문 제안: 0.8 이상만
WARN_THRESHOLD = 0.5        # 🔴 차별 질문 경고: 0.5 이상

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

# =============================================================
# 시스템 프롬프트 (tkinter 버전과 동일)
# =============================================================
SYSTEM_PROMPT = """당신은 "면접 보조 비서"입니다.

[역할]
면접관이 지원자(일반 사무·행정직)와 대화하는 동안, 양측 발언을 실시간으로 받아
점검 항목과 대조합니다. 면접관이 놓칠 수 있는 추가 질문 포인트를 제안하고,
차별 소지 질문을 즉시 경고하는 것이 임무입니다.
면접 진행·평가·합불 판정은 절대 하지 않습니다. 출력은 화면 알림용 JSON 한 개입니다.

[중요 — 입력 구조]
프롬프트에는 아래 부분들이 옵니다.
- [지원자 이력서]: 이번 면접 지원자의 이력서 텍스트(없을 수도 있음). 점검 대상 아님.
  답변이 이력서와 어긋나는지, 이력서상 확인이 필요한 항목이 아직 안 나왔는지 대조하는 데만 쓴다.
- [이전 대화 (맥락용)]: 이번 면접에서 지금까지 오간 발언. 점검 대상 아님. 맥락 파악용.
- [방금 발언]: 이것만 점검 대상. 한 마이크로 면접관·지원자 음성이 섞여 들어오므로
  화자 라벨이 없을 수 있습니다. 발언 내용으로 화자를 추론하세요.
  (질문 형태 → 면접관 / 답변·설명 형태 → 지원자)
- [이미 알림한 항목]: 이번 면접에서 이미 제안/경고한 category 목록. 같은 category는
  다시 알리지 말고 skip 하세요(중복 억제).

[점검 우선순위 — 위에서부터 더 민감하게]
1순위. 차별 소지 질문 (면접관 발언에서만 탐지) — category: 차별질문
   결혼·출산 계획 / 나이·연령 직접 확인 / 가족 사항(부모 직업·형제 수) /
   종교·정치 성향 / 외모·신체 정보
   예: "결혼 계획 있으세요?", "몇 년생이세요?", "부모님 직업이?", "종교 있으세요?"
   → status: warn (confidence 0.5 이상이면 출력)
2순위. STAR 미충족 (지원자 답변) — category: STAR
   답변에 S(상황)·A(본인 행동)·R(결과)이 모두 없이 "열심히 했습니다",
   "잘 해결했습니다", "노력했습니다" 등 추상 표현으로만 끝남
3순위. 경력 공백·이직 사유 (지원자) — category: 이직사유
   공백 기간 사유 미확인 / 이직 사유가 "발전 위해" 등 추상적
4순위. 지원 동기·회사 이해도 (지원자) — category: 지원동기
   "분위기 좋아서", "성장 가능성" 등 표면적이고 회사 이해가 없음
5순위. 회피성 답변 (지원자) — category: 회피성답변
   "상황에 따라", "케이스 바이 케이스", "잘 모르겠어요", 질문 회피·전환
6순위. 이력서와 답변 대조 — category: 이력서확인
   [지원자 이력서]가 제공된 경우에만 판정한다.
   - 이력서에 적힌 경력·기간·자격이 답변과 어긋나거나
   - 이력서에 드러난 확인 필요 사항(경력 공백, 재학 여부, 거주지, 짧은 근속 등)이
     면접 중반까지 한 번도 확인되지 않은 경우
   ※ 이력서에 없는 사실을 지어내지 말 것. 이력서에 실제로 적힌 내용만 근거로 삼는다.
   ※ 이력서 첫머리에 "자동 변환" 안내가 있으면 기계로 옮긴 글이라 사람 이름·학교명·
     숫자에 오탈자가 있을 수 있다. 이런 이력서에서는 글자 한두 개 차이나 표기 차이를
     불일치로 보지 말고, 경력 유무·기간처럼 분명한 차이일 때만 확인을 제안한다.
7순위. 핵심 역량 미검증 (누적 이력 기준 아직 안 나온 경우) — category: 핵심역량
   ※ [면접 직무정보]에 검증 역량이 명시돼 있으면 그쪽을 우선한다. 기본값은 아래 3축.
   - 학습 속도·질문 습관 [최우선]: 모르는 것을 얼마 만에 묻는가, 메모·기록 습관
   - 기본기: 문서·맞춤법 / 엑셀 / 전화·대면 응대 / 사람 앞 진행 경험
   - 자기주도성·책임감: 시키지 않은 일을 한 경험, 실수를 즉시 알리는가
   특히 파고들 답변 패턴:
   - "웬만하면 혼자 해결합니다" → 온보딩이 짧은 자리라 오히려 확인이 필요
   - "큰 실수는 없었어요" → 실수 처리 경험 미확인
   - "다들 저보고 ~하다고 해요" → 타인 평가로 대체, 본인 사례 없음
   - 주어가 "저희 팀은"·"우리가"로만 이어짐 → 본인 역할 미확인
8순위. 근무 조건·컷오프 미확인 (면접 중반 이후) — category: 근무조건
   ※ 불일치 시 채용 자체가 불가한 항목이므로, 면접 중반을 넘기면 7순위보다 먼저 제안한다.
   근무기간 완주 가능 / 주말·야근 가능 / 지방 출장 및 운전 가능 /
   급여 수용 여부 / 출근 가능 시기
   ※ 단, 이 조건을 개인사(결혼·가족·연애 계획 등)와 엮어 묻는 질문은
     조건 확인이 아니라 차별 질문이므로 1순위 warn으로 처리한다.

[판정 기준 및 임계값]
- warn (🔴): 면접관 발언에서 차별 주제 탐지. confidence 0.5 이상이면 출력.
- ask  (🟡): 점검 항목 미충족. confidence 0.8 이상일 때만 출력.
- ok        : 이번 발언에서 특별히 제안할 것 없음(STAR 충족 등).
- skip      : 인사·잡담·받아쓰기 오류·너무 짧은 발언·점검 무관.
              confidence 0.6 미만이면 무조건 skip.
              이미 알림한 category이면 skip.

[중복 억제]
- [이미 알림한 항목]에 있는 category는 다시 ask 하지 말 것 → skip.
- 지원자가 이미 답한 내용을 무시하는 질문은 제안하지 말 것.
- 한 입력에 여러 개 잡히면 1개만: warn > ask > ok > skip.

[출력 형식 — 반드시 아래 JSON 한 개만. 마크다운/설명 금지]
{
  "status": "ask" | "warn" | "ok" | "skip",
  "category": "STAR|이직사유|지원동기|회피성답변|이력서확인|핵심역량|근무조건|차별질문|기타",
  "summary": "왜 이 알림이 떴는지 1행 요약 (예: '직접 한 행동 + 결과 확인 필요')",
  "suggested_question": "면접관이 그대로 읽을 완성형 추가 질문 문장, 50자 이내 (status=ask일 때만, 그 외 \\"\\")",
  "warning": "차별 질문 경고 + 법적 근거 (status=warn일 때만, 그 외 \\"\\")",
  "confidence": 0.0
}

[톤 규칙]
- summary: 가장 짧게. 명사·동사 위주.
- suggested_question: "~말씀해주시겠어요?", "~여쭤봐도 될까요?" 등 면접 어미. 50자 이내.
- warning: "이 질문은 하지 마세요. [주제] — [법적 근거]" 형태. 우회 질문 제안 절대 금지.
- JSON 외 어떤 텍스트도 출력 금지.

[중요 제약]
- 합격·불합격 판단, 지원자 평가, 점수 부여 절대 금지.
- warn에 "이렇게 바꿔 물어보세요" 식 우회 제안 금지 — "묻지 말 것" 경고만.
- 점검 항목 외 사실을 추측해 판단 근거로 쓰지 말 것.
"""

# =============================================================
# 직무정보 로드 (선택사항)
# =============================================================
if JOB_INFO_PATH.exists():
    JOB_INFO_TEXT = JOB_INFO_PATH.read_text(encoding="utf-8")
    print(f"[직무정보] 로드 완료 ({len(JOB_INFO_TEXT):,}자)")
else:
    JOB_INFO_TEXT = "(직무정보 파일 없음 — 일반 사무·행정직 기준으로 점검)"
    print("[직무정보] 파일 없음 — 일반 사무·행정직 기준으로 진행")


# =============================================================
# 공통 질문 로드 (참고자료/면접질문.txt)
# =============================================================
def load_common_questions() -> list[dict]:
    """면접질문.txt를 묶음(section) → 질문(q) → 메모(notes) 구조로 읽는다.

    형식:
        [묶음 제목]
        - 질문 문장
        > 꼬리질문이나 판단 기준 메모
    """
    if not QUESTION_PATH.exists():
        return []
    sections: list[dict] = []
    for raw in QUESTION_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("※"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sections.append({"title": line[1:-1].strip(), "items": []})
        elif line.startswith("-") and sections:
            sections[-1]["items"].append({"q": line[1:].strip(), "notes": []})
        elif line.startswith(">") and sections and sections[-1]["items"]:
            sections[-1]["items"][-1]["notes"].append(line[1:].strip())
    return [s for s in sections if s["items"]]


# =============================================================
# 받아쓰기 용어 로드 (참고자료/받아쓰기_용어.txt)
#
# Whisper에 고유명사를 미리 알려주면 훨씬 잘 잡는다. 단, 많이 넣으면 역효과다.
# 실측(TTS 문장 "(회사명)에서 (사업명) 사업과 선도교사 연수를…"):
#   · 힌트 없음      → "(회사 명)에서 (사업 명) 사업과"       (띄어쓰기 틀림)
#   · 72개 목록      → "(사업명 오인식)"                      (오히려 악화)
#   · 20개 안쪽      → "(회사명)에서 (사업명) 사업과"         (정확)
# 그래서 상한을 두고 앞에서부터 잘라 쓴다.
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
        # 설명 문장이 섞여 들어가면 인식이 흐트러진다 — 낱말 길이를 넘으면 버린다
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

COMMON_QUESTIONS = load_common_questions()
_qcount = sum(len(s["items"]) for s in COMMON_QUESTIONS)
if COMMON_QUESTIONS:
    print(f"[면접질문] 공통 질문 {_qcount}개 / {len(COMMON_QUESTIONS)}묶음 로드 완료")
else:
    print("[면접질문] 참고자료/면접질문.txt 없음 — 질문 탭은 실시간 제안만 표시됩니다")

# =============================================================
# 글로벌 상태
# =============================================================
audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
transcript_q: "queue.Queue[str]" = queue.Queue()
verdict_q: "queue.Queue[dict]" = queue.Queue()    # 브라우저로 보낼 알림(ask/warn)
caption_q: "queue.Queue[str]" = queue.Queue()     # 브라우저 실시간 자막

running = threading.Event()
shutdown = threading.Event()

# 일시정지 — 면접을 끝내지 않고 마이크만 잠깐 끈다 (화장실·전화 등). 종료와 달리
# 대화록·질문 상태를 그대로 두고, 경과 시간 계산에서 정지해 있던 구간만 뺀다.
paused = threading.Event()
pause_started: float = 0.0
total_paused: float = 0.0

session_log: list = []
session_start: float = 0.0
last_elapsed: int = 0          # 면접 종료 시점의 실제 소요 시간(초)
last_summary_file: str = ""
last_score: int | None = None   # 면접관이 종료 시 남긴 1~10점 (안 남기면 None)
last_comment: str = ""          # 면접관이 종료 시 남긴 주관적 평가
counters = {"ok": 0, "ask": 0, "warn": 0, "skip": 0}

session_transcripts: list[str] = []
alerted_categories: list[str] = []
session_lock = threading.Lock()

# 이번 면접의 지원자 (선택 안 하면 None → 면접로그 최상위에 저장)
current_candidate: dict | None = None      # {"name","stage","rel","dir"}
resume_text: str = ""
resume_files: list[str] = []               # 실제로 읽힌 이력서 파일
resume_skipped: list[str] = []             # 글자 추출이 안 된 파일(스캔 PDF 등)

# 이력서 기반 맞춤 질문 (면접 시작 시 백그라운드로 1회 생성)
custom_questions: dict = {"status": "idle", "items": [], "error": ""}
# status: idle(아직) | generating(만드는 중) | ready(완성) | error | none(이력서 없음)

# =============================================================
# 지원자 폴더 스캔 / 이력서 읽기
# =============================================================
def find_stage_dir() -> Path | None:
    """지원자를 읽어올 단계 폴더('02. 면접 대상')를 찾는다. 이름이 바뀌어도 견디도록 2단 탐색."""
    if not LOG_DIR.exists():
        return None
    dirs = [d for d in sorted(LOG_DIR.iterdir()) if d.is_dir()]
    for d in dirs:
        if is_stage_dir(d.name):
            return d
    for d in dirs:
        if d.name.startswith("02"):
            return d
    return None


def scan_candidates() -> list[dict]:
    """'02. 면접 대상' 폴더의 하위 폴더명을 오늘 면접볼 지원자 목록으로 읽는다."""
    stage = find_stage_dir()
    if stage is None:
        return []
    out = []
    for cand in sorted(stage.iterdir()):
        if not cand.is_dir():
            continue
        files = sorted(f.name for f in cand.iterdir() if f.is_file())
        out.append({
            "name": cand.name,
            "stage": stage.name,
            "rel": f"{stage.name}/{cand.name}",
            "files": files,
            "has_resume": any(is_resume_file(cand / f) for f in files),
            "has_questions": bool(load_cached_questions(cand)),
        })
    return out


def is_resume_file(p: Path) -> bool:
    """이력서로 읽어들일 파일인가. 이 프로그램이 만든 파일은 제외한다."""
    if p.name.startswith((LOG_PREFIX, QUESTION_PREFIX, TRANSCRIPT_PREFIX, REPORT_PREFIX)):
        return False
    return p.suffix.lower() in (".pdf", ".txt", ".md")


def read_resume(cand_dir: Path) -> tuple[str, list[str], list[str]]:
    """지원자 폴더의 이력서를 텍스트로 합친다. (텍스트, 읽은 파일, 못 읽은 파일)

    스캔 이미지로 만든 PDF는 글자 레이어가 없어 추출되지 않는다(0자).
    조용히 넘기면 이력서를 읽은 줄 알게 되므로 못 읽은 파일을 따로 돌려준다.
    """
    chunks, used, skipped = [], [], []
    for f in sorted(cand_dir.iterdir()):
        if not f.is_file() or not is_resume_file(f):
            continue
        try:
            if f.suffix.lower() == ".pdf":
                if not HAS_PDF:
                    skipped.append(f.name)
                    continue
                reader = PdfReader(str(f))
                text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            else:
                text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[이력서] {f.name} 읽기 실패: {e}")
            skipped.append(f.name)
            continue
        text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        if len(text) < 20:
            print(f"[이력서] {f.name} — 글자가 추출되지 않음(스캔 이미지 PDF로 보임)")
            skipped.append(f.name)
            continue
        chunks.append(f"--- {f.name} ---\n{text}")
        used.append(f.name)

    # 스캔 PDF라도 변환된 .txt를 읽었으면 못 읽은 것이 아니므로 경고에서 뺀다
    #   예: 이력서_및_자기소개서_홍길동.pdf(0자) + 같은 이름의 .txt(변환본)
    converted = {Path(u).stem for u in used}
    skipped = [s for s in skipped if Path(s).stem not in converted]

    joined = "\n\n".join(chunks)
    if len(joined) > RESUME_MAX_CHARS:
        joined = joined[:RESUME_MAX_CHARS] + "\n...[이력서 뒷부분 생략]"
    return joined, used, skipped

# =============================================================
# Whisper lazy init
# =============================================================
_whisper = None
whisper_ready = threading.Event()
VOCAB_KW: dict = {}     # transcribe에 넘길 힌트 인자 (모델 로드 후 결정)


def get_whisper():
    global _whisper, VOCAB_KW
    if _whisper is None:
        print(f"[Whisper] {WHISPER_MODEL} 모델 로드 중... "
              f"(스레드 {WHISPER_THREADS}개, 처음 한 번은 내려받느라 몇 분 걸립니다)")
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                cpu_threads=WHISPER_THREADS)
        # hotwords는 고유명사 전용 인자라 initial_prompt보다 정확하다.
        # 옛 버전에는 없으므로 있는지 확인하고 고른다.
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


def call_claude_cli(user_text: str, history: list[str], alerted: list[str]) -> str:
    if history:
        joined = " ".join(history)
        if len(joined) > 30000:
            joined = "...[앞부분 생략]... " + joined[-30000:]
        history_block = f"[이전 대화 (맥락용, 점검 대상 아님)]\n{joined}\n\n"
    else:
        history_block = "[이전 대화 (맥락용, 점검 대상 아님)]\n(면접 시작 직후 — 이전 대화 없음)\n\n"

    alerted_block = (
        f"[이미 알림한 항목 (이 category는 다시 알리지 말 것)]\n"
        f"{', '.join(alerted) if alerted else '(아직 없음)'}\n\n"
    )
    if resume_text:
        who = current_candidate["name"] if current_candidate else "지원자"
        resume_block = (
            f"[지원자 이력서 — {who} (점검 대상 아님, 대조용)]\n{resume_text}\n\n"
        )
    else:
        resume_block = "[지원자 이력서]\n(없음 — 이력서 대조는 하지 말 것)\n\n"

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"[면접 직무정보]\n{JOB_INFO_TEXT}\n\n"
        f"{resume_block}"
        f"{history_block}"
        f"{alerted_block}"
        f"[방금 발언]\n{user_text}\n\n"
        "위 [방금 발언]을 JSON 한 객체로만 응답하세요. 설명/마크다운 금지."
    )
    result = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", CLAUDE_MODEL],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=CLAUDE_TIMEOUT, shell=False,
    )
    return result.stdout or ""

# =============================================================
# 스캔 이력서 자동 변환 (프로그램 실행 시 1회, 백그라운드)
#
# 스캔해서 만든 PDF는 글자 레이어가 없어 그냥 읽으면 0자가 나온다.
# 그런 PDF만 골라 각 쪽을 이미지로 만든 뒤 Claude에게 읽혀 같은 폴더에 .txt로 저장한다.
# 저장된 .txt는 read_resume()이 이력서로 그대로 읽어간다.
# =============================================================
convert_state: dict = {
    "status": "idle",   # idle | running | done | skipped | error
    "total": 0, "done": 0, "current": "",
    "converted": [], "failed": [], "message": "",
}


def pdf_has_text(pdf: Path) -> bool:
    """글자 레이어가 있는 PDF인가 (스캔본이 아닌가)."""
    try:
        doc = fitz.open(str(pdf))
        return len("".join(pg.get_text() for pg in doc).strip()) >= TEXT_PDF_MIN
    except Exception:
        return True     # 못 열면 건드리지 않는다


def find_scan_targets() -> list[Path]:
    """변환이 필요한 스캔 PDF 목록. 이미 .txt가 있으면 제외."""
    if not (HAS_FITZ and CLAUDE_CLI and LOG_DIR.exists()):
        return []
    out = []
    for stage in sorted(p for p in LOG_DIR.iterdir() if p.is_dir()):
        for cand in sorted(p for p in stage.iterdir() if p.is_dir()):
            for f in sorted(cand.iterdir()):
                if (f.is_file() and f.suffix.lower() == ".pdf"
                        and not f.name.startswith(LOG_PREFIX)
                        and not f.with_suffix(".txt").exists()
                        and not pdf_has_text(f)):
                    out.append(f)
    return out


def ocr_page(png: Path) -> str:
    """이미지 한 장을 Claude에게 읽힌다."""
    prompt = (
        "이 이미지를 Read 도구로 열어 보이는 글자를 그대로 텍스트로 옮겨줘. "
        "추측해서 채우지 말고 보이는 대로만 쓰고, 표는 읽기 쉽게 줄로 풀어줘. "
        f"설명 없이 본문만 출력해: {png}"
    )
    r = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", OCR_MODEL, "--allowedTools", "Read"],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=PAGE_TIMEOUT, shell=False,
    )
    return (r.stdout or "").strip()


def convert_scanned_pdf(pdf: Path) -> bool:
    """스캔 PDF 하나를 .txt로 변환. 변환했으면 True."""
    out = pdf.with_suffix(".txt")
    if out.exists():
        return False
    doc = fitz.open(str(pdf))
    pages = []
    with tempfile.TemporaryDirectory() as td:
        for i, pg in enumerate(doc, start=1):
            png = Path(td) / f"p{i}.png"
            pg.get_pixmap(dpi=RENDER_DPI).save(str(png))
            print(f"    {pdf.name} {i}/{doc.page_count}쪽 읽는 중…", flush=True)
            try:
                text = ocr_page(png)
            except subprocess.TimeoutExpired:
                print(f"    {pdf.name} {i}쪽 시간 초과 — 건너뜀")
                continue
            if text:
                pages.append(f"[{i}쪽]\n{text}")
    if not pages:
        return False
    header = (
        f"※ 이 파일은 「{pdf.name}」가 글자 레이어 없는 이미지 PDF라\n"
        f"   화면을 읽어 텍스트로 옮긴 것입니다. (자동 변환)\n"
        f"   기계 변환이라 사람 이름·학교명·숫자에 오탈자가 있을 수 있습니다.\n"
        f"   원본 PDF가 정본이며, 중요한 수치·날짜는 원본과 대조해 확인하세요.\n\n"
    )
    out.write_text(header + "\n\n".join(pages) + "\n", encoding="utf-8")
    return True


def auto_convert(quiet: bool = False):
    """실행 시 1회 — 변환할 게 있는지 점검하고, 있으면 변환한다."""
    global convert_state
    if not HAS_FITZ:
        convert_state = {**convert_state, "status": "skipped",
                         "message": "PyMuPDF 미설치 — '1_설치하기.bat'을 다시 실행하면 스캔 이력서도 읽습니다"}
        print("[이력서변환] PyMuPDF 없음 — 건너뜀")
        return
    if not CLAUDE_CLI:
        convert_state = {**convert_state, "status": "skipped",
                         "message": "Claude CLI를 찾지 못해 변환을 건너뜁니다"}
        return

    targets = find_scan_targets()
    if not targets:
        convert_state = {**convert_state, "status": "done", "total": 0, "done": 0,
                         "message": "변환할 스캔 이력서 없음"}
        if not quiet:
            print("[이력서변환] 변환할 스캔 이력서 없음 — 통과")
        return

    convert_state = {"status": "running", "total": len(targets), "done": 0,
                     "current": "", "converted": [], "failed": [], "message": ""}
    print(f"[이력서변환] 스캔 이력서 {len(targets)}개 발견 — 백그라운드 변환 시작")
    for pdf in targets:
        convert_state["current"] = f"{pdf.parent.name} / {pdf.name}"
        try:
            if convert_scanned_pdf(pdf):
                convert_state["converted"].append(f"{pdf.parent.name}/{pdf.name}")
                print(f"[이력서변환] 완료: {pdf.parent.name}/{pdf.with_suffix('.txt').name}")
            else:
                convert_state["failed"].append(f"{pdf.parent.name}/{pdf.name}")
                print(f"[이력서변환] 글자를 읽지 못함: {pdf.name}")
        except Exception as e:
            convert_state["failed"].append(f"{pdf.parent.name}/{pdf.name}")
            print(f"[이력서변환] 실패 {pdf.name}: {e}")
        convert_state["done"] += 1
    convert_state["status"] = "done"
    convert_state["current"] = ""
    n, f = len(convert_state["converted"]), len(convert_state["failed"])
    convert_state["message"] = f"변환 {n}개 완료" + (f", {f}개 실패" if f else "")
    print(f"[이력서변환] 끝 — {convert_state['message']}")


# =============================================================
# 이력서 기반 맞춤 질문 생성 (면접 시작 시 1회, 백그라운드)
# =============================================================
CUSTOM_Q_PROMPT = """당신은 채용 면접을 설계하는 헤드헌팅 전문가입니다.
아래 [채용 기준]과 [지원자 이력서]를 읽고, 이 지원자에게만 해당하는 맞춤 질문을 만드세요.

[반드시 지킬 것]
- 이력서에 실제로 적힌 내용에서만 질문을 뽑을 것. 없는 사실을 지어내지 말 것.
- 공통 질문(학습 속도, 기본기, 자기주도성 등 일반 항목)은 이미 준비돼 있으므로 만들지 말 것.
  이 지원자의 이력서를 안 읽으면 나올 수 없는 질문만 만들 것.
- 특히 아래를 놓치지 말 것:
  · 경력 공백 기간, 짧은 근속, 재학·졸업 예정 여부(근무기간과 충돌하는지)
  · 이력서에 쓴 강점이 이 직무와 실제로 연결되는지
  · 전공·경력이 이 직무와 달라 보이는 지점
  · 거주지·운전 가능 여부 등 근무 조건과 관련된 단서
- 질문은 면접관이 그대로 읽을 수 있는 완성형 한국어 문장. 60자 이내.
- 차별 금지 항목(결혼·출산·나이·가족·종교·외모)은 절대 만들지 말 것.
  졸업 예정 여부는 근무 가능 기간 확인 목적이라면 가능하지만, 나이를 묻는 형태는 금지.
- 6~9개만 만들 것. 중요한 순서대로.

[출력 형식 — JSON 한 개만. 설명·마크다운 금지]
{
  "items": [
    {"q": "질문 문장", "why": "이력서의 어떤 대목 때문에 묻는지 25자 이내"}
  ]
}
"""


# 맞춤 질문 미리 만들기 진행 상황 (프로그램 실행 시 백그라운드)
prep_state: dict = {"status": "idle", "total": 0, "done": 0, "current": "", "ready": []}

# 공유용 면접 요약 생성 상태
report_state: dict = {"status": "idle", "file": "", "error": ""}


def question_cache_path(cand_dir: Path) -> Path:
    return cand_dir / f"{QUESTION_PREFIX}{cand_dir.name}.md"


def resume_fingerprint(cand_dir: Path) -> str:
    """이력서 파일 구성이 바뀌면 캐시를 다시 만들도록 지문을 만든다."""
    parts = [f"{f.name}:{f.stat().st_size}"
             for f in sorted(cand_dir.iterdir())
             if f.is_file() and is_resume_file(f)]
    return "|".join(parts)


def load_cached_questions(cand_dir: Path) -> list | None:
    """캐시가 있고 이력서가 그대로면 질문 목록을 돌려준다."""
    p = question_cache_path(cand_dir)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        marker = "<!-- data: "
        i = text.rfind(marker)
        if i < 0:
            return None
        data = json.loads(text[i + len(marker):text.rfind("-->")].strip())
        if data.get("fp") != resume_fingerprint(cand_dir):
            print(f"[면접질문] {cand_dir.name} — 이력서가 바뀌어 다시 만듭니다")
            return None
        return data.get("items") or None
    except Exception:
        return None


def save_cached_questions(cand_dir: Path, items: list):
    """사람이 읽을 수 있는 .md로 저장하고, 끝에 기계용 데이터를 주석으로 붙인다."""
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 맞춤 질문 — {cand_dir.name}", "",
             f"이력서를 읽고 자동으로 만든 질문입니다. (생성 {when})",
             "면접 프로그램의 「📋 면접 질문」 탭에 그대로 표시됩니다.",
             "질문을 고치고 싶으면 이 파일을 지우고 프로그램을 다시 켜면 새로 만듭니다.", ""]
    for it in items:
        why = f"  ↳ {it.get('why','')}" if it.get("why") else ""
        lines.append(f"- {it.get('q','')}{why}")
    lines += ["", "<!-- data: " + json.dumps(
        {"fp": resume_fingerprint(cand_dir), "items": items}, ensure_ascii=False) + " -->"]
    question_cache_path(cand_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_questions(resume: str, who: str) -> list:
    """Claude를 불러 맞춤 질문을 만든다 (2분 안팎 걸림)."""
    prompt = (
        f"{CUSTOM_Q_PROMPT}\n\n"
        f"[채용 기준]\n{JOB_INFO_TEXT}\n\n"
        f"[지원자 이력서 — {who}]\n{resume}\n\n"
        "위 이력서를 근거로 JSON 한 객체만 출력하세요."
    )
    r = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", CLAUDE_MODEL],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, shell=False,
    )
    data = json.loads(extract_json(r.stdout or ""))
    return [
        {"q": str(it.get("q", "")).strip(), "why": str(it.get("why", "")).strip()}
        for it in data.get("items", []) if str(it.get("q", "")).strip()
    ]


def ensure_questions(cand_dir: Path) -> list:
    """캐시가 있으면 즉시, 없으면 만들어서 저장한 뒤 돌려준다."""
    cached = load_cached_questions(cand_dir)
    if cached:
        return cached
    resume, used, _ = read_resume(cand_dir)
    if not resume:
        return []
    items = build_questions(resume, cand_dir.name)
    if items:
        save_cached_questions(cand_dir, items)
    return items


def prewarm_questions():
    """프로그램 실행 시 — 면접 대상 전원의 맞춤 질문을 미리 만들어 둔다.

    면접 중에 만들면 8초마다 도는 점검 호출과 겹쳐 몇 분씩 걸린다(실측 9분).
    미리 만들어 파일로 캐시해 두면 지원자를 고르는 순간 바로 뜬다.
    """
    global prep_state
    if not CLAUDE_CLI:
        prep_state = {**prep_state, "status": "skipped"}
        return
    cands = scan_candidates()
    if not cands:
        prep_state = {**prep_state, "status": "done", "total": 0, "done": 0}
        return

    todo = []
    ready = []
    for c in cands:
        d = LOG_DIR / c["rel"]
        if load_cached_questions(d):
            ready.append(c["name"])
        else:
            todo.append(d)

    prep_state = {"status": "running" if todo else "done",
                  "total": len(todo), "done": 0, "current": "", "ready": ready}
    if not todo:
        print(f"[면접질문] 맞춤 질문 준비 완료 (모두 캐시됨: {', '.join(ready)})")
        return

    print(f"[면접질문] 맞춤 질문을 미리 만듭니다 — {len(todo)}명 "
          f"(1명당 2분 안팎, 한 번 만들면 다음부터는 즉시 표시)")
    for d in todo:
        prep_state["current"] = d.name
        t0 = time.time()
        # CLI 호출이 드물게 일시적으로 실패한다(타임아웃 등). 한 번 더 시도한다.
        for attempt in (1, 2):
            try:
                items = ensure_questions(d)
                if items:
                    prep_state["ready"].append(d.name)
                    print(f"[면접질문] {d.name} — {len(items)}개 준비 완료 ({time.time()-t0:.0f}초)")
                else:
                    print(f"[면접질문] {d.name} — 이력서가 없어 건너뜀")
                break
            except Exception as e:
                if attempt == 1:
                    print(f"[면접질문] {d.name} 생성 실패({e}) — 다시 시도합니다")
                    time.sleep(3)
                else:
                    print(f"[면접질문] {d.name} 생성 실패 — 면접 시작 시 다시 시도합니다")
        prep_state["done"] += 1
    prep_state["status"] = "done"
    prep_state["current"] = ""
    print("[면접질문] 미리 만들기 끝")


def load_custom_questions(cand_dir: Path, who: str):
    """면접 시작 시 — 캐시가 있으면 즉시, 없으면 그때 만든다(느림)."""
    global custom_questions
    cached = load_cached_questions(cand_dir)
    if cached:
        custom_questions = {"status": "ready", "items": cached, "error": ""}
        print(f"[면접질문] {who} — 미리 만들어 둔 질문 {len(cached)}개 즉시 표시")
        return

    custom_questions = {"status": "generating", "items": [], "error": ""}
    print(f"[면접질문] {who} — 미리 만든 질문이 없어 지금 만듭니다 (몇 분 걸릴 수 있음)")
    try:
        items = ensure_questions(cand_dir)
        if items:
            custom_questions = {"status": "ready", "items": items, "error": ""}
            print(f"[면접질문] {who} 맞춤 질문 {len(items)}개 생성 완료")
        else:
            custom_questions = {"status": "error", "items": [],
                                "error": "질문을 만들지 못했습니다."}
    except subprocess.TimeoutExpired:
        custom_questions = {"status": "error", "items": [],
                            "error": "시간이 초과됐습니다. 공통 질문으로 진행하세요."}
        print("[면접질문] 생성 타임아웃")
    except Exception as e:
        custom_questions = {"status": "error", "items": [], "error": f"생성 실패: {e}"}
        print(f"[면접질문] 생성 실패: {e}")


# =============================================================
# 오디오 / STT / Claude 스레드 (tkinter 버전과 동일)
# =============================================================
def audio_callback(indata, frames, time_info, status):
    if running.is_set() and not paused.is_set():
        audio_q.put(indata.copy().flatten())

def audio_loop():
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            callback=audio_callback,
                            blocksize=int(SAMPLE_RATE * 0.5)):
            while not shutdown.is_set():
                time.sleep(0.1)
    except Exception as e:
        print(f"[Audio] 에러: {e}")

def stt_loop():
    model = get_whisper()
    buffer = []
    last_process = time.time()
    while not shutdown.is_set():
        try:
            chunk = audio_q.get(timeout=0.3)
            buffer.append(chunk)
        except queue.Empty:
            pass
        now = time.time()
        if now - last_process >= CHUNK_SECONDS and buffer:
            audio = np.concatenate(buffer).astype(np.float32)
            buffer = []
            last_process = now

            # 처리가 녹음을 못 따라가 음성이 밀렸으면 오래된 부분을 버리고 따라붙는다
            limit = int(SAMPLE_RATE * CHUNK_SECONDS * MAX_BACKLOG_CHUNKS)
            if len(audio) > limit:
                dropped = (len(audio) - limit) / SAMPLE_RATE
                audio = audio[-limit:]
                print(f"[STT] ⚠️밀림 — 오래된 음성 {dropped:.0f}초를 버리고 현재 시점으로 따라붙습니다. "
                      f"자주 뜨면 WHISPER_MODEL을 'small'로 낮추세요")
            if np.abs(audio).max() < SILENCE_THRESHOLD:
                continue
            try:
                t0 = time.time()
                segments, _ = model.transcribe(
                    audio, language="ko",
                    beam_size=BEAM_SIZE,
                    vad_filter=True,
                    **VOCAB_KW,          # 고유명사 힌트 (hotwords 또는 initial_prompt)
                )
                text = " ".join(s.text for s in segments).strip()
                took = time.time() - t0
                if len(text) > 3:
                    # 처리 시간이 CHUNK_SECONDS에 가까워지면 뒤로 밀리기 시작한다
                    warn = "  ⚠️밀림주의(모델을 낮추세요)" if took > CHUNK_SECONDS * 0.85 else ""
                    print(f"[STT] ({took:.1f}초{warn}) {text}")
                    transcript_q.put(text)
                    caption_q.put(text)
            except Exception as e:
                print(f"[STT] 에러: {e}")

def claude_loop():
    if not CLAUDE_CLI:
        print("[Claude] claude 명령어를 찾지 못함 — 점검 스레드 비활성")
        return
    while not shutdown.is_set():
        try:
            text = transcript_q.get(timeout=0.3)
        except queue.Empty:
            continue
        with session_lock:
            history_snapshot = list(session_transcripts)
            alerted_snapshot = list(alerted_categories)
        content = ""
        try:
            raw = call_claude_cli(text, history_snapshot, alerted_snapshot)
            content = extract_json(raw)
            if not content:
                continue
            verdict = json.loads(content)
            verdict["timestamp"] = datetime.now().isoformat()
            verdict["raw_input"] = text

            status = verdict.get("status", "skip")
            conf = float(verdict.get("confidence", 0) or 0)
            category = verdict.get("category", "기타")
            if status == "ask" and conf < ASK_THRESHOLD:
                status = "skip"
            elif status == "warn" and conf < WARN_THRESHOLD:
                status = "skip"
            verdict["status"] = status

            counters[status] = counters.get(status, 0) + 1
            session_log.append(verdict)

            if status in ("ask", "warn"):
                verdict_q.put(verdict)
                with session_lock:
                    if category not in alerted_categories:
                        alerted_categories.append(category)

            with session_lock:
                session_transcripts.append(text)

            note = verdict.get("summary", "") or verdict.get("warning", "")
            print(f"[Claude] {status} ({category}, {conf:.2f}) | {note}")
        except subprocess.TimeoutExpired:
            print(f"[Claude] 타임아웃 ({CLAUDE_TIMEOUT}s) — 다음 청크로 계속")
        except json.JSONDecodeError:
            print(f"[Claude] JSON 파싱 실패: {content[:200] if content else '(빈 응답)'}")
        except Exception as e:
            print(f"[Claude] 에러: {e}")

# =============================================================
# 요약 저장 (tkinter 버전과 동일)
# =============================================================
BUILTIN_REPORT_PROMPT = """당신은 채용 면접을 기록·정리하는 전문가입니다.
아래 면접 받아쓰기 전문을 읽고, 다른 사람과 공유할 수 있는 요약을 작성하세요.

[반드시 지킬 것]
- 받아쓰기는 음성 자동 변환이라 오탈자가 있습니다. 앞뒤 맥락으로 자연스럽게 고쳐 읽되,
  없는 내용을 지어내지 마세요. 알아들을 수 없는 대목은 억지로 채우지 말고 넘어가세요.
- 한 마이크로 두 사람 목소리가 섞여 화자 구분이 없습니다.
  질문하는 쪽이 면접관, 답하는 쪽이 지원자입니다. 내용으로 판단하세요.
- 합격·불합격을 판단하지 마세요. 점수도 매기지 마세요.
  오간 내용을 사실대로 정리하는 것이 전부입니다.
- 지원자가 실제로 한 말을 근거로 쓰고, 추측이면 "~라고 언급"처럼 표시하세요.
- 확인되지 않은 항목은 비워두지 말고 "확인 안 됨"이라고 분명히 적으세요.

[출력 형식 — 아래 마크다운 그대로. 다른 말 붙이지 마세요]
## 한눈에 보기
- **지원 동기:** (지원자가 말한 지원 동기를 한 문장으로)
- **내세운 강점:** (지원자가 내세운 강점을 한 문장으로)
- **확인 필요:** (면접에서 드러난 확인 필요 사항을 한 문장으로)

## 주요 질문과 답변
(오간 순서대로. 중요한 것 6~10개. 잡담·인사는 빼세요)

### Q. (면접관 질문을 한 문장으로)
**A.** (지원자 답변 요약 2~4줄. 구체적 수치·기간·기관명이 나왔으면 반드시 포함)

## 근무 조건 확인 결과
| 항목 | 지원자 답변 |
|---|---|
| 급여 | |
| 근무 기간 | |
| 주말·야근 | |
| 출장·운전 | |
| 출근 가능 시기 | |
(대화에서 다뤄지지 않은 항목은 "확인 안 됨"으로 적으세요)

## 확인된 것
- (면접에서 분명히 확인된 사실만)

## 확인하지 못한 것
- (물어보려 했으나 답이 없었거나, 아예 다루지 못한 항목)
"""


def load_report_prompt() -> str:
    """요약본 작성 규칙을 파일에서 읽는다. 없으면 내장 규칙을 쓴다.

    파일 맨 위의 안내문([대괄호] 제목, ※ 줄, ─── 구분선)은 지시문이 아니므로 걸러낸다.
    """
    if not REPORT_FORMAT_PATH.exists():
        return BUILTIN_REPORT_PROMPT
    raw = REPORT_FORMAT_PATH.read_text(encoding="utf-8")
    # 마지막 구분선 뒤부터가 실제 지시문
    marker = "─────"
    if marker in raw:
        raw = raw.rsplit(marker, 1)[-1]
    body = chr(10).join(ln for ln in raw.splitlines() if not ln.strip().startswith("※"))
    body = body.strip()
    if len(body) < 100:
        print("[면접요약] 형식 파일이 너무 짧아 내장 규칙을 씁니다")
        return BUILTIN_REPORT_PROMPT
    return body


REPORT_PROMPT = load_report_prompt()



def build_report(transcript_text: str, who: str) -> str:
    """받아쓰기 전문 → 공유용 질문·답변 요약 (Claude 호출, 2분 안팎)."""
    prompt = (f"{REPORT_PROMPT}\n\n[채용 기준 참고]\n{JOB_INFO_TEXT}\n\n"
              f"[면접 받아쓰기 전문 — {who}]\n{transcript_text}\n\n"
              "위 형식대로 요약을 작성하세요.")
    r = subprocess.run(
        [CLAUDE_CLI, "-p", "--model", CLAUDE_MODEL],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, shell=False,
    )
    return (r.stdout or "").strip()


def save_report(out_dir, who: str, ts: str, when_label: str, dur: str, log_snapshot: list) -> str:
    """공유용 요약을 만들어 저장한다. 실패하면 빈 문자열.

    log_snapshot은 종료 시점에 뜬 session_log 스냅샷이다 — 이 배경 스레드가 도는 동안
    다음 면접이 바로 시작돼 session_log가 초기화돼도 이전 면접 내용이 섞이지 않는다.
    """
    global report_state
    lines = []
    for v in log_snapshot:
        text = (v.get("raw_input") or "").strip()
        if text:
            lines.append(f"[{(v.get('timestamp','') or '')[11:19]}] {text}")
    if not lines:
        report_state = {"status": "error", "file": "", "error": "받아쓴 내용이 없습니다."}
        return ""

    report_state = {"status": "running", "file": "", "error": ""}
    print(f"[면접요약] {who} — 질문·답변 요약을 만드는 중입니다 (2분 안팎)")
    try:
        body = build_report(chr(10).join(lines), who)
        if not body:
            raise RuntimeError("응답이 비었습니다")
        head = (f"# {who} 면접 요약{chr(10)}{chr(10)}"
                f"**일시** {when_label}  ·  **소요** {dur}  ·  **면접관** 운영팀{chr(10)}{chr(10)}"
                f"> 음성 자동 변환 기록을 바탕으로 정리한 요약입니다. "
                f"수치·기관명은 원본 대화록과 대조해 확인하세요.{chr(10)}"
                f"> 합격 여부 판단은 들어 있지 않습니다.{chr(10)}{chr(10)}---{chr(10)}{chr(10)}")
        path = out_dir / f"{REPORT_PREFIX}{who}_{ts}.md"
        path.write_text(head + body + chr(10), encoding="utf-8")
        report_state = {"status": "ready", "file": path.name, "error": ""}
        print(f"[저장] {path}")
        return path.name
    except Exception as e:
        report_state = {"status": "error", "file": "", "error": str(e)}
        print(f"[면접요약] 실패: {e}")
        return ""


def build_notion_lines(when_label: str, dur: str, count: int, asks: list, warns: list,
                        custom_items: list, report_body: str, score: int | None,
                        comment: str, pending_note: str = "") -> list[str]:
    """Notion 붙여넣기용 본문을 줄 단위로 만든다. 실시간 미리보기(api_notion)와
    종료 후 저장 파일(save_notion_doc)이 이 함수 하나를 같이 써서 내용이 갈라지지 않게 한다."""
    L = [f"**면접 일시** {when_label} · **소요** {dur} · **점검 발언** {count}건", ""]
    if pending_note:
        L += [pending_note, ""]

    if report_body:
        i = report_body.find("## 한눈에 보기")
        if i >= 0:
            L += [report_body[i:].rstrip(), "", "---", ""]

    if custom_items:
        L.append("## 이력서 기반 확인 항목")
        for it in custom_items:
            why = f" — {it.get('why','')}" if it.get("why") else ""
            L.append(f"- {it.get('q','')}{why}")
        L.append("")

    L.append(f"## 면접 중 추가로 짚은 질문 ({len(asks)}건)")
    if asks:
        for v in asks:
            t = (v.get("timestamp", "") or "")[11:16]
            L.append(f"- [{t}] {v.get('summary','')} → {v.get('suggested_question','')}")
    else:
        L.append("- 없음")
    L.append("")

    L.append(f"## 차별 질문 경고 ({len(warns)}건)")
    if warns:
        for v in warns:
            t = (v.get("timestamp", "") or "")[11:16]
            L.append(f"- [{t}] {v.get('warning','')}")
    else:
        L.append("- 없음")
    L.append("")

    L.append("## 면접관 평가")
    if score is not None:
        label = SCORE_LABELS.get(score, "")
        L.append(f"- 점수: {score}/10" + (f" — {label}" if label else ""))
    else:
        L.append("- 점수: (기록 없음)")
    L.append(f"- 주관적 평가: {comment.strip() if comment and comment.strip() else '(작성 안 함)'}")
    L += ["", "## 다음 단계", "- (채용 / 보류 / 불채용)", "- 다음 단계:"]
    return L


def save_notion_doc(out_dir: Path, who: str, ts: str, when_label: str, dur: str,
                     log_snapshot: list, custom_items: list, report_body: str,
                     score: int | None, comment: str) -> str:
    """면접 종료 후 Notion에 바로 붙여넣을 수 있는 문서를 파일로 저장해 둔다.
    (예전엔 화면의 '📋 Notion용 정리' 버튼을 눌러야만 볼 수 있어서, 그 세션이 끝나면
    다시 볼 방법이 없었다 — 그래서 종료할 때마다 항상 파일로 남긴다.)"""
    asks = [v for v in log_snapshot if v.get("status") == "ask"]
    warns = [v for v in log_snapshot if v.get("status") == "warn"]
    when_date = when_label.split(" ")[0]
    lines = build_notion_lines(when_label, dur, len(log_snapshot), asks, warns,
                               custom_items, report_body, score, comment)
    head = f"# 면접결과 — {who} ({when_date})\n\n"
    path = out_dir / f"{NOTION_PREFIX}{who}_{ts}.md"
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def finalize_after_stop(out_dir: Path, who: str, ts: str, when_label: str, dur: str,
                         log_snapshot: list, custom_items: list,
                         score: int | None, comment: str):
    """종료 직후 백그라운드에서: 공유용 요약을 만들고, 그 내용까지 합쳐 Notion 문서를 저장한다."""
    save_report(out_dir, who, ts, when_label, dur, log_snapshot)
    report_body = ""
    if report_state.get("status") == "ready" and report_state.get("file"):
        rp = out_dir / report_state["file"]
        if rp.exists():
            report_body = rp.read_text(encoding="utf-8")
    save_notion_doc(out_dir, who, ts, when_label, dur, log_snapshot, custom_items,
                    report_body, score, comment)


def save_transcript(out_dir, who: str, ts: str) -> str:
    """면접 받아쓰기 전문을 사람이 읽을 수 있는 .md로 저장한다.

    지금까지는 대화 내용이 .json 안에만 들어 있어 다시 읽기 어려웠다.
    알림이 뜬 대목에는 그 자리에 무슨 제안이 나왔는지 함께 표시한다.
    """
    lines = [f"# {who} 면접 대화록 — {datetime.fromtimestamp(session_start).strftime('%Y-%m-%d %H:%M')}",
             "",
             "마이크로 받아쓴 전문입니다. 음성 자동 변환이라 오탈자가 있을 수 있고,",
             "한 마이크로 두 사람 목소리가 같이 들어와 화자 구분은 되어 있지 않습니다.",
             "🟡·🔴 표시는 그 대목에서 화면에 뜬 알림입니다.",
             "",
             "---",
             ""]
    for v in session_log:
        text = (v.get("raw_input") or "").strip()
        if not text:
            continue
        t = (v.get("timestamp", "") or "")[11:19]
        lines.append(f"**{t}**  {text}")
        st = v.get("status")
        if st == "ask":
            lines.append(f"> 🟡 {v.get('summary','')}")
            lines.append(f"> → {v.get('suggested_question','')}")
        elif st == "warn":
            lines.append(f"> 🔴 {v.get('warning','')}")
        lines.append("")

    path = out_dir / f"{TRANSCRIPT_PREFIX}{who}_{ts}.md"
    path.write_text(chr(10).join(lines), encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_evaluation(out_dir: Path, who: str, ts: str, when_label: str,
                     score: int | None, comment: str) -> str:
    """면접관이 종료 시 남긴 점수·주관 평가를 저장한다. 자동 기록(받아쓰기·AI 요약)과
    섞이지 않도록 별도 파일로 둔다. 둘 다 비어 있으면 파일을 만들지 않는다."""
    comment = (comment or "").strip()
    if score is None and not comment:
        return ""
    lines = [f"# {who} 면접관 평가 — {when_label}", ""]
    if score is not None:
        label = SCORE_LABELS.get(score, "")
        lines.append(f"**점수** {score}/10" + (f" — {label}" if label else ""))
        lines.append("")
    lines.append("## 주관적 평가")
    lines.append(comment if comment else "(작성 안 함)")
    path = out_dir / f"{EVAL_PREFIX}{who}_{ts}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[저장] {path}")
    return path.name


def save_summary() -> str:
    if not session_log:
        return ""
    ts = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d_%H-%M")

    # 지원자를 골랐으면 그 사람 폴더에, 아니면 면접로그 최상위에 저장
    if current_candidate:
        out_dir = Path(current_candidate["dir"])
        stem = f"{LOG_PREFIX}{current_candidate['name']}_{ts}"
        shown = f"{current_candidate['rel']}/{stem}.md"
    else:
        out_dir = LOG_DIR
        stem = f"{LOG_PREFIX}{ts}"
        shown = f"{stem}.md"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(session_log, ensure_ascii=False, indent=2), encoding="utf-8")

    # 받아쓰기 전문은 따로 파일로 (요약이 길어지지 않게)
    who_for_file = current_candidate["name"] if current_candidate else "지원자"
    transcript_name = save_transcript(out_dir, who_for_file, ts)

    elapsed = int(time.time() - session_start)
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    ask_items = [v for v in session_log if v.get("status") == "ask"]
    warn_items = [v for v in session_log if v.get("status") == "warn"]

    who = current_candidate["name"] if current_candidate else "(지원자 미지정)"
    resume_line = f"📄 대조한 이력서: {', '.join(resume_files) if resume_files else '(없음)'}"
    if resume_skipped:
        resume_line += f"\n⚠️ 글자 추출 실패(대조 못 함): {', '.join(resume_skipped)}"

    md = f"""# {who} 면접 점검 요약 — {datetime.fromtimestamp(session_start).strftime("%Y-%m-%d %H:%M")}

⏱️ 진행 시간: {h:02d}:{m:02d}:{s:02d} / 점검 발언: {len(session_log)}건
{resume_line}
🎤 대화 전문: [{transcript_name}](./{transcript_name})
📝 공유용 요약: `{REPORT_PREFIX}{who}_{ts}.md` (면접 종료 후 2분쯤 뒤 생성)

- ✅ 점검 통과: {counters.get('ok', 0)}건
- 🟡 추가 질문 제안: {counters.get('ask', 0)}건
- 🔴 차별 질문 경고: {counters.get('warn', 0)}건
"""
    if warn_items:
        md += "\n## 🔴 차별 질문 경고 내역\n\n"
        for v in warn_items:
            t = v.get("timestamp", "")[11:16]
            md += f"- **{t}** [{v.get('category', '')}] {v.get('warning', '')}\n"
    if ask_items:
        md += "\n## 🟡 추가 질문 제안 내역\n\n"
        for v in ask_items:
            t = v.get("timestamp", "")[11:16]
            md += (f"- **{t}** [{v.get('category', '')}] {v.get('summary', '')}\n"
                   f"  → {v.get('suggested_question', '')}\n")
    if custom_questions.get("items"):
        md += "\n## 📋 이력서 기반 맞춤 질문 (면접 시작 시 생성됨)\n\n"
        for it in custom_questions["items"]:
            why = f"  _{it.get('why', '')}_" if it.get("why") else ""
            md += f"- {it.get('q', '')}{why}\n"

    md += ("\n## 다음 면접 전 참고\n\n"
           "- 이번 면접에서 자주 비어 있던 점검 항목을 다음 면접 시작 전 미리 떠올려 보세요.\n")

    md_path.write_text(md, encoding="utf-8")
    print(f"[저장] {md_path}")
    return shown

# =============================================================
# 탭을 닫으면 자동 종료
#
# 화면(index.html)은 1초마다 /api/state를 폴링한다 — 역으로, 한동안 어떤
# 요청도 안 들어오면 "탭이 다 닫혔다"고 볼 수 있다. 탭을 닫는 즉시 끄면 실수로
# 닫았을 때·새로고침할 때 위험하니 여유 시간을 두고, 면접 중이거나 스캔 이력서
# 변환·맞춤 질문 생성·면접 요약 작성이 진행 중이면 그것부터 끝날 때까지 기다린다.
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
            or report_state.get("status") == "running"
            or convert_state.get("status") == "running"
            or prep_state.get("status") == "running"
            or custom_questions.get("status") == "generating"
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


@app.route("/settings")
def settings_page():
    return send_from_directory(HERE, "settings.html")

@app.route("/api/candidates")
def api_candidates():
    stage = find_stage_dir()
    return jsonify({
        "candidates": scan_candidates(),
        "stage": stage.name if stage else "",
        "pdf_ok": HAS_PDF,
    })


@app.route("/api/report")
def api_report():
    """공유용 면접 요약 본문 (면접 종료 후 생성됨)."""
    if report_state.get("status") != "ready" or not report_state.get("file"):
        return jsonify({"ok": False, "status": report_state.get("status", "idle"),
                        "error": report_state.get("error", ""), "body": ""})
    out_dir = Path(current_candidate["dir"]) if current_candidate else LOG_DIR
    path = out_dir / report_state["file"]
    if not path.exists():
        return jsonify({"ok": False, "status": "error",
                        "error": "요약 파일을 찾지 못했습니다.", "body": ""})
    return jsonify({"ok": True, "status": "ready", "error": "",
                    "file": report_state["file"],
                    "body": path.read_text(encoding="utf-8")})


@app.route("/api/notion")
def api_notion():
    """Notion에 붙여넣기 좋은 형태로 면접 결과를 만들어 준다.

    Notion은 붙여넣을 때 마크다운을 알아서 해석한다(## → 제목, - → 불릿).
    다만 토글은 붙여넣기로 안 만들어지므로, 토글 제목은 따로 돌려주고
    본문만 토글 안에 붙여넣도록 안내한다.
    """
    if not session_log:
        return jsonify({"ok": False, "title": "", "body": "",
                        "error": "아직 저장된 면접 내용이 없습니다."})

    who = current_candidate["name"] if current_candidate else "지원자"
    when = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d") if session_start else ""
    when_label = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d %H:%M") if session_start else when
    elapsed = current_elapsed() if running.is_set() else last_elapsed
    dur = f"{elapsed // 60}분 {elapsed % 60}초"

    asks = [v for v in session_log if v.get("status") == "ask"]
    warns = [v for v in session_log if v.get("status") == "warn"]

    report_body = ""
    pending_note = ""
    if report_state.get("status") == "ready" and report_state.get("file"):
        out_dir = Path(current_candidate["dir"]) if current_candidate else LOG_DIR
        rp = out_dir / report_state["file"]
        if rp.exists():
            report_body = rp.read_text(encoding="utf-8")
    elif report_state.get("status") == "running":
        pending_note = "> ⏳ 질문·답변 요약을 만드는 중입니다. 잠시 후 다시 눌러주세요."

    L = build_notion_lines(when_label, dur, len(session_log), asks, warns,
                           custom_questions.get("items") or [], report_body,
                           last_score, last_comment, pending_note)

    return jsonify({
        "ok": True,
        "title": f"면접결과 — {who} ({when})",
        "body": "\n".join(L),
    })


@app.route("/api/questions")
def api_questions():
    """면접 질문 탭 — 공통 질문 + 이력서 맞춤 질문 + 면접 중 실시간 제안."""
    live = [
        {"q": v.get("suggested_question", ""),
         "why": v.get("summary", ""),
         "category": v.get("category", ""),
         "time": (v.get("timestamp", "") or "")[11:16]}
        for v in session_log
        if v.get("status") == "ask" and v.get("suggested_question")
    ]
    return jsonify({
        "candidate": current_candidate["name"] if current_candidate else "",
        "common": COMMON_QUESTIONS,
        "custom": custom_questions,
        "live": live,
    })


@app.route("/api/history")
def api_history():
    """면접 중/후에 지난 알림을 다시 볼 수 있도록 이번 세션 판정 전체를 돌려준다."""
    return jsonify({
        "candidate": current_candidate["name"] if current_candidate else "",
        "items": session_log,
    })


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
    """claude CLI가 없어도 면접은 시작할 수 있다 — 실시간 받아쓰기(Whisper)는 클로드와
    무관하게 동작하는 별개 기능이라, CLI 하나 없다고 이 앱 전체를 못 쓰게 막을 이유가
    없다. 다만 실시간 판정(claude_loop)·맞춤 질문 생성·면접 요약은 claude CLI가 있어야만
    되고(claude_loop는 이미 자체적으로 건너뜀), 그건 화면 배너("claude_ok": false)로
    안내된다."""
    global session_start, session_log, current_candidate
    global resume_text, resume_files, resume_skipped, custom_questions
    global total_paused, last_score, last_comment
    if not running.is_set():
        paused.clear()
        total_paused = 0.0
        last_score, last_comment = None, ""   # 지난 면접 평가가 새 면접에 섞이지 않게
        # 지원자 선택 처리 (rel = "01. 이력서 검토 대상/홍길동")
        rel = ((request.get_json(silent=True) or {}).get("candidate") or "").strip()
        current_candidate, resume_text, resume_files, resume_skipped = None, "", [], []
        custom_questions = {"status": "idle", "items": [], "error": ""}   # 지난 면접 것 지우기
        if rel:
            cand_dir = (LOG_DIR / rel).resolve()
            # 면접로그 밖 경로 차단
            if LOG_DIR.resolve() not in cand_dir.parents or not cand_dir.is_dir():
                return jsonify({"ok": False, "error": f"지원자 폴더를 찾을 수 없어요: {rel}"})
            resume_text, resume_files, resume_skipped = read_resume(cand_dir)
            current_candidate = {
                "name": cand_dir.name,
                "stage": cand_dir.parent.name,
                "rel": rel,
                "dir": str(cand_dir),
            }
            if resume_files:
                print(f"[이력서] {cand_dir.name} — {', '.join(resume_files)} ({len(resume_text):,}자)")
            elif not HAS_PDF:
                print(f"[이력서] {cand_dir.name} — PDF 읽기 모듈(pypdf) 없음. 이력서 없이 진행")
            else:
                print(f"[이력서] {cand_dir.name} — 읽을 이력서 파일 없음. 이력서 없이 진행")
            if resume_skipped:
                print(f"[이력서] 못 읽은 파일: {', '.join(resume_skipped)} (스캔 이미지 PDF는 글자 추출 불가)")

        session_log = []
        counters.update({"ok": 0, "ask": 0, "warn": 0, "skip": 0})
        with session_lock:
            session_transcripts.clear()
            alerted_categories.clear()
        # 남은 알림/자막 비우기
        for q in (verdict_q, caption_q):
            while not q.empty():
                try: q.get_nowait()
                except queue.Empty: break
        session_start = time.time()
        running.set()

        # 맞춤 질문은 프로그램 실행 시 미리 만들어 두므로 보통 즉시 뜬다.
        # 캐시가 없으면 그때 만드는데 몇 분 걸리므로, 면접 시작은 막지 않는다.
        if resume_text and current_candidate:
            threading.Thread(
                target=load_custom_questions,
                args=(Path(current_candidate["dir"]), current_candidate["name"]),
                daemon=True,
            ).start()
        else:
            custom_questions = {"status": "none", "items": [],
                                "error": "이력서가 없어 맞춤 질문을 만들지 못했습니다."}
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global last_summary_file, last_elapsed, last_score, last_comment
    if running.is_set():
        body = request.get_json(silent=True) or {}
        raw_score = body.get("score")
        try:
            last_score = int(raw_score) if raw_score is not None else None
            if last_score is not None and not (1 <= last_score <= 10):
                last_score = None
        except (TypeError, ValueError):
            last_score = None
        last_comment = str(body.get("comment") or "")

        last_elapsed = current_elapsed()
        running.clear()
        paused.clear()
        last_summary_file = save_summary()

        out_dir = Path(current_candidate["dir"]) if current_candidate else LOG_DIR
        who = current_candidate["name"] if current_candidate else "지원자"
        ts = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d_%H-%M")
        when_label = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d %H:%M")
        eval_file = save_evaluation(out_dir, who, ts, when_label, last_score, last_comment)

        # 다음 면접이 이 스레드가 끝나기 전에 시작돼도 안 섞이도록 지금 상태를 스냅샷으로 떠둔다
        log_snapshot = list(session_log)
        custom_snapshot = list(custom_questions.get("items") or [])

        # 공유용 질문·답변 요약 + Notion 문서는 2분쯤 걸리므로 화면을 붙잡지 않고 뒤에서 만든다
        if CLAUDE_CLI:
            dur = f"{last_elapsed // 60}분 {last_elapsed % 60}초"
            threading.Thread(target=finalize_after_stop,
                             args=(out_dir, who, ts, when_label, dur,
                                   log_snapshot, custom_snapshot, last_score, last_comment),
                             daemon=True).start()
        return jsonify({"ok": True, "summary_file": last_summary_file, "evaluation_file": eval_file})
    return jsonify({"ok": True, "summary_file": last_summary_file})

@app.route("/api/state")
def api_state():
    verdicts = []
    while not verdict_q.empty():
        try:
            verdicts.append(verdict_q.get_nowait())
        except queue.Empty:
            break
    captions = []
    while not caption_q.empty():
        try:
            captions.append(caption_q.get_nowait())
        except queue.Empty:
            break
    elapsed = current_elapsed()
    return jsonify({
        "running": running.is_set(),
        "paused": paused.is_set(),
        "whisper_ready": whisper_ready.is_set(),
        "claude_ok": bool(CLAUDE_CLI),
        "elapsed": elapsed,
        "counters": counters,
        "verdicts": verdicts,
        "captions": captions,
        "summary_file": last_summary_file,
        "candidate": current_candidate["name"] if current_candidate else "",
        "candidate_rel": current_candidate["rel"] if current_candidate else "",
        "resume_files": resume_files,
        "resume_skipped": resume_skipped,
        "convert": convert_state,
        "prep": prep_state,
        "report": report_state,
    })


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
    print("🔍 면접 보조 비서 웹앱 — 면접 질문 점검 보조")
    print("=" * 50)
    print(f"📋 직무정보: {JOB_INFO_PATH.name if JOB_INFO_PATH.exists() else '(없음 — 일반 사무·행정직 기준)'}")
    print(f"💾 로그 위치: {LOG_DIR}")
    print(f"🤖 모델: claude -p --model {CLAUDE_MODEL}")
    print(f"🎤 STT: faster-whisper ({WHISPER_MODEL})")
    print(f"🔗 Claude CLI: {CLAUDE_CLI or '❌ 찾지 못함'}")
    stage = find_stage_dir()
    cands = scan_candidates()
    if cands:
        print(f"👤 오늘 면접 대상: {len(cands)}명 — {', '.join(c['name'] for c in cands)}  (출처: 면접로그/{stage.name}/)")
    elif stage:
        print(f"👤 오늘 면접 대상: 없음 — 면접로그/{stage.name}/ 안에 지원자 이름 폴더를 넣어주세요")
    else:
        print("👤 오늘 면접 대상: '02. 면접 대상' 폴더를 찾지 못했습니다 (면접로그 안에 만들어 주세요)")
    if not HAS_PDF:
        print("📄 PDF 이력서: pypdf 미설치 — '1_설치하기.bat'를 다시 실행하면 이력서까지 읽습니다")
    print(f"🌐 주소: http://{HOST}:{PORT}")
    print("=" * 50)

    if not CLAUDE_CLI:
        print("\n⚠️  Claude Code CLI를 찾지 못했습니다 — 실시간 받아쓰기는 그대로 되지만, "
              "실시간 판정·맞춤 질문 생성·면접 요약은 안 됩니다. 쓰려면 Claude Code 설치·로그인 "
              "후 다시 실행하세요.\n")

    threading.Thread(target=audio_loop, daemon=True).start()
    threading.Thread(target=stt_loop, daemon=True).start()
    threading.Thread(target=claude_loop, daemon=True).start()
    threading.Thread(target=shutdown_watchdog_loop, daemon=True).start()
    # 켜지자마자 준비 작업 (면접 시작을 막지 않음)
    #  1) 스캔 이력서를 텍스트로 변환  →  2) 그 이력서로 맞춤 질문을 미리 생성
    #  순서가 중요하다. 변환이 끝나야 그 내용까지 넣어 질문을 만든다.
    def warmup():
        auto_convert()
        prewarm_questions()

    threading.Thread(target=warmup, daemon=True).start()

    # 잠시 후 브라우저 자동 열기 (QP_NO_BROWSER=1 이면 생략)
    if not os.environ.get("QP_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
