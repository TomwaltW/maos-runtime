"""受保护路径判定 —— 全仓唯一的一处。

skills 层（`code_repo_patch` 的 `_reject_protected_paths`）与 tools 层
（`sandbox` 的 `_check_path`）都从这里取。判定只留一处不是洁癖：抄第二份，
两处一定会漂，而漂的那次没人会发现 —— 直到有人靠改测试让测试通过。

**为什么住在 tools 层而不是 skills 层**：tools 在 skills 下面。判定原先定义在
`maos/skills/builtin/code_repo_patch.py`，tools 要用就得反向 import 上层，
模块级 import 因此成环 —— `maos.tools.sandbox` → `skills.builtin.code_repo_patch`
→ 触发 `builtin/__init__` 的 `discover()` → import `test_verify` → 回到还没定义完的
`maos.tools.sandbox`，在 `PYTEST_RUN_PORT` 上抛 `ImportError: cannot import name ...
from partially initialized module`。当时靠「延迟到函数里 import」绕过去了，能用，
但依赖方向是反的。下沉到这里之后两边都是向下 import，环从根上没有了。

**本模块不许 import `maos/skills/**` 里的任何东西**，那正是它存在的全部意义；
也不许 import `maos.tools` 里除标准库之外的任何兄弟模块 —— 它是这一层的叶子。
`maos/tests/test_protected_paths_single_source.py` 把这两件事都钉住了。

`_path_segments` 带下划线，但它是**跨层公共件**：`sandbox` 与 `code_repo_patch`
都直接引它。名字保持原样不改成公开的 `path_segments` —— 重命名不产生任何价值，
却会让两个调用点连同已有测试的断言一起动，把结构性搬家混进行为面。
"""

from __future__ import annotations

import posixpath

# 受保护目录名：路径按 / 分段后任一段命中即安全事件。"tests" 挡的是「改测试让测试通过」。
#
# 存的是**目录名**，不是路径前缀 —— 这是本清单唯一容易写错的地方。上一版存前缀
# ("/infra", "/.github", "tests/", "/secrets") 配 startswith / 子串判定，结果是
# 声明拦的四项里只有 tests/ 真生效：仓库相对路径 "infra/main.tf" 不带前导斜杠，
# startswith("/infra") 恒 False，"/infra" 也不是它的子串。同时子串判定又把
# infrastructure、contests 这类正常目录误伤成安全事件。
# 分段相等把漏拦和误伤一起消掉，代价就是这里必须写裸目录名，不带任何斜杠。
PROTECTED_SEGMENTS = frozenset({"infra", ".github", "secrets", "tests"})

# git 的 quote_c_style 只用这几个字母转义，其余不可打印/高位字节一律走三位八进制。
# 抄的是 git 源码 quote.c 的 cq_lookup 表，多一个少一个都会让解码与 git 分叉。
_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n",
              "r": "\r", "t": "\t", "v": "\v", '"': '"', "\\": "\\"}


def unquote_c_style(path: str) -> str:
    """把 git 的 C-quoted 路径解回真实路径；不是 C-quoted 的原样返回。

    git 对含特殊字节的路径写成 ``"a/\\164ests/conftest.py"`` —— 双引号包裹 + 反斜杠
    转义，其中 ``\\164`` 是 ``t`` 的八进制。``git apply`` 会把它解码成 ``tests/…``
    再落盘，而 ``_path_segments`` 从前直接吃原串：``\\164ests`` 里的反斜杠被当成
    路径分隔符，段变成 ``164ests``，与 ``tests`` 不相等，三条校验一起失效。

    **只在首尾都是双引号时才解码**。合法路径里也可能带引号，「凡带引号一律拒绝」
    是把漏拦换成误伤 —— 正常补丁从此打不进去，不是修好了。

    八进制转义编的是**字节**（UTF-8 逐字节），所以先解成 bytes 再按 UTF-8 解码；
    解不出的字节走 surrogateescape 保留，不让一个畸形字节把整条路径吞掉。

    遇到无法识别的转义序列时保留反斜杠原样，不抛。这里是安全判定的上游，
    抛异常等于把「路径可疑」变成「整次产出崩掉」，而崩掉的那次没人会去看它想写哪。
    """
    if len(path) < 2 or not path.startswith('"') or not path.endswith('"'):
        return path

    body = path[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8", errors="surrogateescape"))
            i += 1
            continue
        if i + 1 >= len(body):                      # 末尾孤立反斜杠，原样留着
            out.extend(b"\\")
            break
        nxt = body[i + 1]
        triple = body[i + 1:i + 4]
        if nxt in _C_ESCAPES:
            out.extend(_C_ESCAPES[nxt].encode("utf-8"))
            i += 2
            continue
        # git 只写恰好三位、且落在单字节内的八进制（最大 \377）。位数不足、
        # 混进 8/9、或 \400 以上都不是 git 的产物，按「不是转义」处理。
        # 这里用不得 str.isdigit()：它对 '²' 这类 Unicode 数字也返回 True。
        if len(triple) == 3 and all(c in "01234567" for c in triple):
            value = int(triple, 8)
            if value <= 0xFF:
                out.append(value)
                i += 4
                continue
        out.extend(b"\\")                           # 认不出的转义：反斜杠原样保留
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


def _path_segments(path: str) -> list[str]:
    """把补丁路径规范化成小写分段，供分段相等匹配。

    归一**五**件事，每一件不做就是一个绕过口：
      * C-quoted 解引号：``"a/\\164ests/conftest.py"`` 是 git 自己的路径写法，
        它会解码成 ``tests/…`` 再落盘。不先解码，下面那条「反斜杠 → 斜杠」
        反而帮倒忙 —— 转义反斜杠被吃成分隔符，段成了 ``164ests``；
      * 反斜杠 → 斜杠：``.github\\workflows\\ci.yml`` 否则整条是一个段，判不出来；
      * 折叠 ``.`` / ``..`` / 重复斜杠：``./infra/x`` 与 ``maos/../infra/x``
        必须和 ``infra/x`` 判成同一个；
      * 剥前导斜杠：声明里写的就是 ``/infra``，模型照抄一遍不该反而放行；
      * casefold：本机 APFS 默认大小写不敏感，``Secrets/prod.env`` 与
        ``secrets/prod.env`` 在磁盘上是同一个文件，判定却会放行前者。

    解码必须排在最前：它产出的才是 git 眼里的真实路径，后面四件都得对着那一条做。

    normpath 消不掉开头的 ``..``（``../infra/x`` 原样返回），所以残留的
    ``..`` 段在这里一并滤掉 —— 留着它只会让越界路径躲开分段匹配。
    """
    decoded = unquote_c_style(path)
    collapsed = posixpath.normpath(decoded.replace("\\", "/"))
    return [seg.casefold() for seg in collapsed.split("/") if seg not in ("", ".", "..")]
