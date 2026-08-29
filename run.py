"""薄入口 —— python run.py 顺跑场景 1-7 端到端，等价于 python -m maos.main。"""

from __future__ import annotations

import sys

from maos.main import main

if __name__ == "__main__":
    sys.exit(main())
