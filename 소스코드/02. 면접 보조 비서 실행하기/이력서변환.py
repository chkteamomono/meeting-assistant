"""이력서 PDF → 텍스트 변환기 (스캔 이미지 PDF 전용) — 수동 실행용

평소에는 「2_실행하기.bat」으로 프로그램을 켜면 서버가 알아서 변환하므로
이 도구를 따로 돌릴 필요가 없습니다.

이럴 때만 쓰세요:
  · 프로그램을 켜지 않고 미리 변환해 두고 싶을 때
  · 변환이 실패한 파일을 다시 시도할 때 (.txt를 지우고 실행)

변환 규칙은 서버와 같은 코드를 씁니다(서버.py).
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import 서버 as S


def main():
    print("=" * 54)
    print("📄 이력서 PDF → 텍스트 변환기 (스캔본 전용)")
    print("=" * 54)

    if not S.HAS_FITZ:
        print("[오류] PyMuPDF가 없습니다. '1_설치하기.bat'을 다시 실행해 주세요.")
        input("\nEnter 누르면 닫힘...")
        return
    if not S.CLAUDE_CLI:
        print("[오류] Claude Code CLI를 찾지 못했습니다. `claude --version` 확인 후 다시 실행하세요.")
        input("\nEnter 누르면 닫힘...")
        return

    print(f"대상 폴더: {S.LOG_DIR}")
    print(f"설정: {S.RENDER_DPI}dpi / 모델 {S.OCR_MODEL}\n")

    targets = S.find_scan_targets()
    if not targets:
        print("변환할 스캔 이력서가 없습니다.")
        print("(글자가 들어 있는 PDF와 이미 변환한 파일은 건너뜁니다)")
        input("\nEnter 누르면 닫힘...")
        return

    print(f"스캔 이력서 {len(targets)}개 발견:")
    for t in targets:
        print(f"  · {t.parent.name} / {t.name}")
    print()

    S.auto_convert()

    st = S.convert_state
    print("=" * 54)
    line = f"완료 — 변환 {len(st['converted'])}개"
    if st["failed"]:
        line += f", 실패 {len(st['failed'])}개"
    print(line)
    if st["converted"]:
        print("변환된 .txt는 면접 시작 시 이력서로 함께 읽힙니다.")
        print("⚠️ 기계 변환이라 오탈자가 있을 수 있으니, 중요한 내용은 원본 PDF와 대조하세요.")
    if st["failed"]:
        print("실패한 파일:")
        for f in st["failed"]:
            print(f"  · {f}")
    print("=" * 54)
    input("\nEnter 누르면 닫힘...")


if __name__ == "__main__":
    main()
