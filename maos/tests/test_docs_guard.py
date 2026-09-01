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

## 判决必须与 checkout 无关（`test_guard_scope_*` / `test_git_ignored_*`）

2026-09-01 的实测：同一个 commit、同一份脚本，在主仓跑报 46 条、在 worktree 里跑报
74 条。28 条差额全部来自 `review/`（编排面，走 `.git/info/exclude`，只存在于主仓的
文件系统里）。**一个在不同 checkout 里给不同判决的守卫不是守卫** —— 它报出来的每一条
都要先问一句「你是在哪儿跑的」，等于没有判决。

于是射程按 git 划：在册面取 `git ls-files --cached --others --exclude-standard`，
被 git 忽略的路径整个不在射程内。下面几条钉的就是这条口径，且**必须在主仓和任一
worktree 里都绿**。

## 提示类不进主判据（`SEVERITY`）

主判据只钉**阻断类**（断链、指不到的锚点、不存在的引用、过期行号、坏掉的结构）。
排版偏好（围栏语言标注、标题重名、第二个 H1）归提示类，照报照计数，但不让主断言
变红 —— 两档绑在一起时它从来没绿过，而一条永远红的断言会把真正的断链一起淹掉。
分档表本身也受查：脚本里能报出来的每个类别都必须在 `SEVERITY` 里显式登记。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil
import subprocess
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
    """`docs/**/*.md` + 仓库根 `*.md` 一条**阻断类**问题都不许有（白名单已扣除）。

    红了不要往白名单里加 —— 先读报出来的那一条：它多半是真的。
    白名单只放「有意引用不存在的东西」，且必须写得出理由。

    提示类（排版偏好）不进这条断言，见模块头。它们的欠账记在
    `docs/BACKLOG.md ## task-T52`，由持有各文档的轨去清。
    """
    issues = check_docs.blocking(check_docs.run())
    assert not issues, "文档守卫报出 %d 条阻断类：\n%s" % (
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
    ("外部仓引用写坏了", "# T\n\n见 `cumora:docs/COORDINATION`。\n", "E-extref"),
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
        "外部仓写 `cumora:docs/COORDINATION.md:31-33`，散文简写写 `scenario_5/6.py`，\n"
        "cumora 自己的事件命名空间写 `cumora:nudge:<convoId>`，三者都不该报。\n\n"
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


# ---------------------------------------------------------------------------
# 5. 判决与 checkout 无关
# ---------------------------------------------------------------------------
requires_git = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(),
    reason="射程口径由 git 回答；没有 git 时守卫退回文件系统口径，本节判据不适用",
)


@requires_git
def test_guard_scope_is_exactly_what_git_can_see():
    """在册面必须**逐字**等于 `git ls-files --cached --others --exclude-standard`。

    这是「判决与 checkout 无关」的地基：只要射程里混进一个 git 管不到的文件，
    同一份文档在主仓和在 worktree 里就会得到不同判决 —— 而两边的差异
    （别人的在制品、编排面草稿、构建残渣）跟文档本身对不对毫无关系。
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    expected = {p for p in out.split("\0") if p}
    assert check_docs.universe() == expected


#: 被 git 忽略的采样路径。挑这两条是因为它们**在两种 checkout 里的磁盘状态不同**，
#: 而正确的判决必须一样：
#:   - `docs/superpowers/plans/**` 由**被跟踪的** `.gitignore` 挡掉（新克隆也一样），
#:     主仓里可能真有这个文件、worktree 里一定没有；
#:   - `.worktrees/**` 同样由 `.gitignore` 挡掉，只在主仓的文件系统里存在。
#: 两条都不许因为「磁盘上有」就被守卫看见。
IGNORED_SAMPLES = [
    "docs/superpowers/plans/parallel-build-plan.md",
    ".worktrees/probe/README.md",
]


@requires_git
@pytest.mark.parametrize("rel", IGNORED_SAMPLES)
def test_git_ignored_paths_are_out_of_scope(rel):
    """被 git 忽略 = 按设计进不了版本库 = 不归守卫管，且**与磁盘上有没有无关**。"""
    assert check_docs.out_of_scope(rel), f"{rel} 应判为不在射程内"
    assert check_docs.resolve(rel) is None, (
        f"{rel} 被 git 忽略，却仍被解析成了在册路径 —— 说明射程漏到文件系统上去了")


@requires_git
def test_a_doc_citing_an_ignored_path_is_not_flagged(tmp_path):
    """端到端：引用一条被忽略的路径，守卫一条都不该报。

    这正是 2026-09-01 那 28 条差额的形状 —— 编排面文件（`review/**`、gitignored 的
    操作剧本）只在主仓的文件系统里有，在任何 worktree 里都没有。
    """
    doc = tmp_path / "cites-ignored.md"
    doc.write_text(
        "# T\n\n任务定义的原始出处是 "
        "`docs/superpowers/plans/parallel-build-plan.md`（gitignored 操作剧本）。\n",
        encoding="utf-8",
    )
    assert check_docs.check(str(doc)) == []


# ---------------------------------------------------------------------------
# 6. 分档表自己也受查
# ---------------------------------------------------------------------------
#: 脚本里真正能报出来的类别，从源码里扒 —— 比手抄一份清单可靠：
#: 新加一类判据却忘了分档时，这里会立刻发现。
EMITTED_KINDS = set(re.findall(
    r'issues\.append\(\(\s*"([A-Za-z]-[\w\-]+)"',
    (ROOT / "scripts" / "check_docs.py").read_text(encoding="utf-8"),
))


def test_every_kind_the_guard_can_emit_is_classified():
    """能报出来的每个类别都必须在 `SEVERITY` 里显式登记。

    不登记也不会静默变松（缺省是阻断），但会**静默地变严** —— 新判据一上来就把
    主判据顶红，而没人记得它是哪一档。分档是要有人拍板的，不是默认出来的。
    """
    assert EMITTED_KINDS, "没从脚本里扒到任何类别 —— 正则跟脚本写法漂了"
    unclassified = sorted(EMITTED_KINDS - set(check_docs.SEVERITY))
    assert not unclassified, f"这些类别没在 SEVERITY 里分档：{unclassified}"


def test_severity_defaults_to_blocking():
    """没登记的类别按阻断处理（fail-closed）。

    反过来（缺省提示）意味着「忘了登记」= 「悄悄不管了」，那是守卫最坏的坏法。
    """
    assert check_docs.severity("Z-brand-new-kind") == "blocking"


def test_advisory_kinds_never_reach_the_main_verdict():
    """`blocking()` 必须真的把提示类滤掉，且不动阻断类的顺序与内容。"""
    sample = [
        ("a.md", "E-missing", 1, "x"),
        ("a.md", "A-lang", 2, "y"),
        ("b.md", "D-link", 3, "z"),
    ]
    assert check_docs.blocking(sample) == [sample[0], sample[2]]
