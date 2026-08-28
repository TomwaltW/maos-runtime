"""靶场根 conftest —— 只做一件事：把靶场根放进 sys.path，让 tests/ 能 import auth。

这个文件同时是「conftest.py 任意层级禁改」那条规则的**守护对象**：
它在 pytest collection 阶段先于一切用例执行，能改它就等于能绕开 tests/ 禁改
（往这里塞一句 monkeypatch 就能让任何用例变绿，而 tests/ 一个字都没动）。
所以 sandbox_git_apply 对任意层级的 conftest.py 一律拒绝新增与修改。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
