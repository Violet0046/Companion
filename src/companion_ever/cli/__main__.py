"""cli/__main__.py — 让 `python -m companion_ever.cli ...` 可用。

没有这个文件时，cli 是一个包，python -m 会直接报错：
    No module named companion_ever.cli.__main__; 'companion_ever.cli' is a package
"""
from __future__ import annotations

import sys

from companion_ever.cli import main

if __name__ == "__main__":
    sys.exit(main())
