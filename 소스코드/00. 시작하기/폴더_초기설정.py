# coding: utf-8
"""
처음 이 도구를 내려받아 실행할 때, 회의·면접 데이터 폴더가 하나도 없으면 기본
폴더트리(빈 템플릿)를 만들어준다. 이미 폴더가 있으면(기존 사용자) 아무것도 하지
않는다 — 실제 운영 데이터가 담긴 폴더는 절대 건드리지 않는다.

각 서버.py의 find_root()와 똑같이 "이름에 회의관리/면접이 들어간 폴더가 이미
있는지"로만 판단하므로, 폴더 이름 앞에 번호를 붙이거나(예: "2. 회의관리") 뒤에
말을 붙여도 새로 만들지 않고 기존 폴더를 그대로 쓴다.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # 저장소 루트 (소스코드/의 부모)


def has_folder(keyword: str) -> bool:
    return any(d.is_dir() and keyword in d.name for d in ROOT.iterdir())


def write_if_missing(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


MEETING_PERSONA_README = """# 01. 페르소나설정

회의 보조 비서 프로그램은 이 폴더를 직접 읽지 않습니다 — 팀이 참고할 기획 문서를
자유롭게 남겨두는 자리입니다. 예를 들어 이런 파일을 만들어볼 수 있습니다.

- 기획안.md — 이 도구를 왜, 어떻게 쓰기로 했는지 정리한 문서
- 시스템-프롬프트.md — 안건 정리·회의록 생성 방식에 대해 팀이 합의한 메모

비워둬도 프로그램은 정상 동작합니다.
"""

MEETING_VOCAB_TEMPLATE = """# 회의에서 자주 나오는 고유명사(회사명·사업명·기관명 등)를 한 줄에 하나씩
# 적어두면 받아쓰기 정확도가 올라갑니다. 너무 많이 넣으면 오히려 인식이 나빠지니
# (실측상 20개 안쪽 권장) 자주 쓰는 것부터 위에 적으세요.
#
# 예시 (앞의 #을 지우고 실제 용어로 바꿔서 쓰세요):
# (주)우리회사
# 우리 프로젝트명
"""

MEETING_FORMAT_TEMPLATE = """# 팀 고유의 회의록 양식이 있으면 이 파일에 적어두세요.
# 비워두면 프로그램에 내장된 기본 형식을 씁니다.
"""

INTERVIEW_PERSONA_README = """# 01. 페르소나설정

면접 보조 비서 프로그램은 이 폴더를 직접 읽지 않습니다 — 우리 조직만의 채용 기준과
면접 방향을 정리해 팀이 참고하는 자리입니다. 예를 들어 이런 파일을 만들어볼 수
있습니다.

- 페르소나.md — 이 면접에서 어떤 인재상을 찾는지
- 채용기준.md — 직무별 채용 기준
- 시스템-프롬프트.md — 면접 판정·질문 생성 방식에 대해 팀이 합의한 메모
- 테스트케이스.md — 기능 점검용 테스트 시나리오

비워둬도 프로그램은 정상 동작합니다 (실제 동작에는 참고자료/ 폴더의 파일들이
쓰입니다).
"""

INTERVIEW_JOB_INFO_TEMPLATE = """# 지원한 직무에 대한 정보(담당 업무, 필요 역량 등)를 적어두면 맞춤 질문·실시간
# 판정에 반영됩니다. 비워두면 일반 사무·행정직 기준으로 동작합니다.
"""

INTERVIEW_QUESTION_TEMPLATE = """# 모든 지원자에게 공통으로 묻고 싶은 질문을 적어두면 [질문] 탭에 표시됩니다.
# 형식 예시 (앞의 #을 지우고 실제 질문으로 바꿔서 쓰세요):
#
# [기본 소양]
# Q. 우리 조직에 지원한 이유가 궁금합니다.
#   - 메모: 답변에서 확인하고 싶은 포인트
"""

INTERVIEW_VOCAB_TEMPLATE = """# 면접에서 자주 나오는 고유명사(회사명·사업명·직무명 등)를 한 줄에 하나씩
# 적어두면 받아쓰기 정확도가 올라갑니다. 너무 많이 넣으면 오히려 인식이 나빠지니
# (실측상 20개 안쪽 권장) 자주 쓰는 것부터 위에 적으세요.
#
# 예시 (앞의 #을 지우고 실제 용어로 바꿔서 쓰세요):
# (주)우리회사
# 우리 프로젝트명
"""


def setup_meeting():
    if has_folder("회의관리"):
        return
    base = ROOT / "2. 회의관리"
    write_if_missing(base / "01. 페르소나설정" / "README.md", MEETING_PERSONA_README)
    write_if_missing(base / "참고자료" / "받아쓰기_용어.txt", MEETING_VOCAB_TEMPLATE)
    write_if_missing(base / "참고자료" / "회의록_형식.txt", MEETING_FORMAT_TEMPLATE)
    print(f"[초기설정] 회의 데이터 폴더를 새로 만들었습니다: {base}")


def setup_interview():
    if has_folder("면접"):
        return
    base = ROOT / "1. 면접관리"
    write_if_missing(base / "01. 페르소나설정" / "README.md", INTERVIEW_PERSONA_README)
    for stage in ("01. 이력서 검토 대상", "02. 면접 대상", "03. 면접 종료"):
        (base / "02. 면접자 정보" / stage).mkdir(parents=True, exist_ok=True)
    write_if_missing(base / "참고자료" / "직무정보.txt", INTERVIEW_JOB_INFO_TEMPLATE)
    write_if_missing(base / "참고자료" / "면접질문.txt", INTERVIEW_QUESTION_TEMPLATE)
    write_if_missing(base / "참고자료" / "받아쓰기_용어.txt", INTERVIEW_VOCAB_TEMPLATE)
    print(f"[초기설정] 면접 데이터 폴더를 새로 만들었습니다: {base}")


if __name__ == "__main__":
    try:
        setup_meeting()
        setup_interview()
    except OSError as e:
        # 폴더 생성 실패는 치명적이지 않음 — 두 서버.py 모두 실행 시점에 필요한
        # 폴더를 알아서 다시 만들 수 있다. 설치 자체를 막지 않는다.
        print(f"[초기설정] 건너뜀: {e}", file=sys.stderr)
