"""文档守卫的把守测试 —— `scripts/check_docs.py` 必须真的在查，而且必须绿。

## 它守的缺口

`docs/BACKLOG.md ## task-Z3` 的原话：文档里的数字「与主干 HEAD 绑定，**没有任何
机器守卫盯着它过期**」，并列了两条修法，②是「加一条测试断言文档里的引用必须真实
存在」。这个模块就是那个②。

2026-08-31 的实测说明了为什么值得：冻结契约 `docs/parallel/contracts.md` 里 12 条
可核的行号引用中 **9 条已经指错**，其中 C-4「build() 返回六元组」的依据
（`maos/flows/common.py:44-60`）指向的是一段 import —— 而真正的 return 在 `:105`。
这类失效不会让任何东西变红，只会让下一个照着行号去核契约的人扑空。

与 `test_generated_docs.py` 不重叠：那一份守的是 `gen_docs.py` 的**三份生成物**与
代码逐字节一致；这一份守的是**其余全部手写 md** 的结构与引用。

## 为什么必须有自检（`test_guard_catches_*`）

一个恒绿的守卫比没有守卫更坏 —— 它会让人以为这一维已经被盯住了。
判据写错、正则失配、`run()` 因为异常吞掉返回空列表，症状都是「测试全绿」。
所以这里造几份**已知有病**的文档喂给它，逐类断言它报得出来。
口径同 `scripts/guard_bash.py` 的开工自检：**被拦才算守卫挂上了**。

## 白名单必须自己也受查

`ALLOW_MISSING` / `ALLOW_STALE_LINES` 是「有意引用不存在的东西」的豁免清单。
清单会腐烂成两种形态，两种都要挡：

- **没理由的豁免** —— 值为空串，等于「把红的塞进来让它变绿」；
- **过期的豁免** —— 被豁免的文件后来真建出来了（比如 Phase 6 建了 `maos/obs/otel.py`），
  这条豁免此后永远不会被用到，却继续遮住那个路径上未来的真问题。

所以 `test_allowlists_*` 反过来查这份清单本身：每条都要有理由，且**每条都必须
当前真的在起作用**。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_script(name: str) -> types.ModuleType:
    """``scripts/`` 不是包，只能按路径加载（idiom 同 test_repro_path.py）。"""
    key = f"_docsguard_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


check_docs = _load_script("check_docs")


# ---------------------------------------------------------------------------
# 1. 主判据：仓库当前的 md 必须全过
# ---------------------------------------------------------------------------
def test_docs_structure_and_references_are_sound():
    """`docs/**/*.md` + 仓库根 `*.md` 一条问题都不许有（白名单已扣除）。

    红了不要往白名单里加 —— 先读报出来的那一条：它多半是真的。
    白名单只放「有意引用不存在的东西」，且必须写得出理由。
    """
    issues = check_docs.run()
    assert not issues, "文档守卫报出 %d 条：\n%s" % (
        len(issues),
        "\n".join(f"  [{k}] {rel}:{n}  {msg}" for rel, k, n, msg in issues[:40]),
    )


# ---------------------------------------------------------------------------
# 2. 自检：造已知有病的文档，逐类断言守卫报得出来
# ---------------------------------------------------------------------------
#: (用例名, 文档内容, 期望报出的类别)
BROKEN_SAMPLES = [
    ("围栏未闭合", "# T\n\n```python\nx = 1\n", "A-open"),
    ("围栏缺语言", "# T\n\n```\nx = 1\n```\n", "A-lang"),
    ("表格列数不符", "# T\n\n| a | b |\n|---|---|\n| 1 | 2 | 3 |\n", "B-cols"),
    ("表格缺分隔行", "# T\n\n| a | b |\n| 1 | 2 |\n", "B-sep"),
    ("标题层级跳跃", "# T\n\n### 跳了一级\n", "C-skip"),
    ("两个 H1", "# T\n\n# 又一个\n", "C-h1"),
    ("锚点重名", "# T\n\n## 验收\n\n## 验收\n", "C-dup"),
    ("链接指向不存在的文件", "# T\n\n见 [那份文档](./nope-not-here.md)。\n", "D-link"),
    ("锚点指不到标题", "# T\n\n见 [下一节](#根本没有这一节)。\n", "D-anchor"),
    ("引用的路径不存在", "# T\n\n见 `maos/core/nope_not_here.py`。\n", "E-missing"),
    ("行号超出文件长度", "# T\n\n见 `maos/main.py:999999`。\n", "E-line"),
    ("文件末尾无换行", "# T\n\n正文", "F-eof"),
    ("行尾空格", "# T\n\n正文有空格   \n", "F-trail"),
    ("正文里有 Tab", "# T\n\n正\t文\n", "F-tab"),
]


@pytest.mark.parametrize("name,body,expect",
                         BROKEN_SAMPLES,
                         ids=[s[0] for s in BROKEN_SAMPLES])
def test_guard_catches_broken_doc(tmp_path, name, body, expect):
    """守卫必须报得出这一类病。报不出来 = 判据失效，而症状会是「文档全绿」。"""
    doc = tmp_path / "broken.md"
    doc.write_text(body, encoding="utf-8")
    kinds = {k for k, _n, _m in check_docs.check(str(doc))}
    assert expect in kinds, f"{name}：守卫没报出 {expect}，只报了 {sorted(kinds) or '空'}"


def test_guard_passes_a_clean_doc(tmp_path):
    """反向锚点：一份干净文档必须零问题。

    没有这一条，上面那些用例可以靠「对什么都报错」全部通过 ——
    那样的守卫同样是坏的，只是坏在另一头。
    """
    doc = tmp_path / "clean.md"
    doc.write_text(
        "# 标题\n\n## 一节\n\n正文，引用 `maos/main.py:1` 与 `maos/core/store.py`。\n\n"
        "```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    assert check_docs.check(str(doc)) == []


# ---------------------------------------------------------------------------
# 3. 白名单自己也受查
# ---------------------------------------------------------------------------
def test_allowlist_entries_all_carry_a_reason():
    """每条豁免都要写清楚为什么。写不出理由的不是豁免，是没修。"""
    blank = [k for k, v in check_docs.ALLOW_MISSING.items() if not (v or "").strip()]
    blank += [k for k, v in check_docs.ALLOW_STALE_LINES.items() if not (v or "").strip()]
    assert not blank, f"这些豁免没写理由：{blank}"


def test_allow_missing_has_no_dead_entries():
    """被豁免的路径要是真建出来了，这条豁免就该删掉。

    留着不报错，但它会**继续遮住那个路径上未来的真问题** —— 比如 Phase 6 建了
    `maos/obs/otel.py` 之后，谁再往文档里写一个拼错的 `maos/obs/otel.pyc`
    仍然不会红。豁免清单只许装当下真的需要豁免的东西。
    """
    alive = [p for p in check_docs.ALLOW_MISSING if check_docs.resolve(p) is not None]
    assert not alive, (
        f"这些路径已经存在，对应的 ALLOW_MISSING 豁免该删了：{alive}")


#: 原始清单的拷贝。下面那条测试要 monkeypatch 掉模块属性再重跑，所以得先留一份 ——
#: 拷贝发生在 import 时，早于任何 monkeypatch。
_ORIGINAL_STALE = dict(check_docs.ALLOW_STALE_LINES)


def test_allow_stale_lines_has_no_dead_entries(monkeypatch):
    """同理：某条行号豁免要是已经不再被触发，就该删掉。

    做法是把两份白名单临时清空重跑一遍，看每条豁免是不是真的对应着一条会报出来的
    问题。清空后仍不出现的，说明那条豁免已经没有守着任何东西。
    """
    monkeypatch.setattr(check_docs, "ALLOW_STALE_LINES", {})
    monkeypatch.setattr(check_docs, "ALLOW_MISSING", {})
    raw = check_docs.run()

    stale_pairs = set()
    for rel, kind, _n, msg in raw:
        if kind == "E-line":
            stale_pairs.add((rel, msg.split(":", 1)[0]))

    # 这里读的是**原始**清单：monkeypatch 只换了模块属性，import 时拿到的引用没变。
    declared = set(_ORIGINAL_STALE)
    dead = declared - stale_pairs
    assert not dead, f"这些行号豁免已经不再被触发，该删了：{sorted(dead)}"


# ---------------------------------------------------------------------------
# 4. 受保护面：守卫不许读它们的内容
# ---------------------------------------------------------------------------
def test_guard_never_reads_protected_files():
    """`.contracts.lock` 等只判存在性，不读字节 —— 与 guard_bash.py 的口径一致。

    守卫自己去读受保护面，等于给「谁都能顺手读一下」开了个先例，
    而那正是 PROT_PATHS 要挡的事。
    """
    for rel in check_docs.NO_READ:
        if (ROOT / rel).exists():
            assert check_docs.line_count(rel) == -1, (
                f"{rel} 属受保护面，line_count 应返回 -1（只判存在性），"
                f"实际返回了真实行数 —— 说明守卫读了它的内容")
