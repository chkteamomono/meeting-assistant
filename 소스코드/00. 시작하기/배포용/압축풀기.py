# coding: utf-8
"""설치.bat에서 쓰는 압축 해제 도우미. zip 파일을 지정한 폴더에 그대로 푼다."""
import sys
import zipfile
from pathlib import Path


def main():
    zip_path = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    print(f"압축 해제 완료: {dest}")


if __name__ == "__main__":
    main()
