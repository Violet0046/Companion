"""__main__.py — 让 `python -m companion_ever ...` 可用（等价于 `python -m companion_ever.cli`）。"""
from __future__ import annotations

import sys

from companion_ever.cli import main

if __name__ == "__main__":
    sys.exit(main())
