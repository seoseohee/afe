#!/usr/bin/env python3
import sys
import os
import shutil

# __pycache__ 강제 삭제 — 교체된 .py가 구버전 .pyc로 로드되는 문제 방지
# getattr 기반 dispatch로 바꿨지만 혹시 모를 캐시 문제 원천 차단
_pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecc_core")
_cache = os.path.join(_pkg, "__pycache__")
if os.path.isdir(_cache):
    shutil.rmtree(_cache, ignore_errors=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ecc_core.cli import main

if __name__ == "__main__":
    main()
