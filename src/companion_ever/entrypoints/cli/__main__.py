"""cli/__main__.py — 让 `python -m companion_ever.entrypoints.cli ...` 可用。

没有这个文件时，cli 是一个包，python -m 会直接报错：
    No module named companion_ever.entrypoints.cli.__main__; 'companion_ever.entrypoints.cli' is a package
"""
from __future__ import annotations

import sys

from companion_ever.entrypoints.cli import main

if __name__ == "__main__":
    sys.exit(main())
