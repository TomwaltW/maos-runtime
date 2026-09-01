"""文档守卫 —— md 的结构与引用必须站得住。

`scripts/gen_docs.py` 守的是**三份生成物**与代码逐字节一致；这个脚本守的是**其余
全部手写 md**：结构（围栏、表格、标题）与引用（链接、`file:line`）。两者不重叠。

守它的理由写在 `docs/BACKLOG.md ## task-Z3`（原文：「数字与主干 HEAD 绑定，
**没有任何机器守卫盯着它过期**」）。手写文档里的行号引用会随代码重构静默失效，
而失效那天没有任何东西变红 —— 2026-08-31 的实测是：冻结契约
`docs/parallel/contracts.md` 里 12 条可核行号引用中有 9 条已经指错，
其中 C-4「build() 返回六元组」的依据指向了一段 import。

## 八类判据

  A 代码围栏未闭合 / 围栏没写语言标注
  B 表格分隔行缺失、列数与表头不符
  C 标题层级跳跃、同级重名（锚点冲突）、一个文件多个 H1
  D 相对链接指向不存在的文件、锚点指不到本文任何标题
  E 反引号里的 `path[:line]`：路径不存在，或行号超出该文件实际行数
  F 空白卫生：CRLF、文件末尾无换行、行尾空格、正文里的 Tab

## 白名单按「被引用的路径」索引，不按 file:line

行号会随编辑上下漂，拿 `DECISIONS.md:1282` 当键，隔一次插入就失配，
于是白名单要么天天要改、要么被人加成通配。被引用的**路径**是稳定身份：
`maos/obs/cost.py` 这条豁免的理由（「决定不建」）不会因为账本多插一行而改变。

每条豁免都必须写理由。写不出理由的，就不是豁免，是没修。

## 不读受保护面的内容

`.contracts.lock` / `.claude/settings.json` / `scripts/guard_bash.py` 只判存在性，
不读字节 —— 与 `scripts/guard_bash.py` 的 PROT_PATHS 口径一致。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只判存在性、绝不读内容（与 guard_bash.py 的保护面口径一致）
NO_READ = frozenset({
    ".contracts.lock",
    ".claude/settings.json",
    "scripts/guard_bash.py",
})

#: 走 basename 反查时要跳过的目录 —— 它们要么不是本仓库的代码，要么是构建产物。
SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".worktrees", ".pytest_cache",
    "node_modules", "legacy-ts", ".claude",
})

# ---------------------------------------------------------------------------
# 白名单：**有意**引用不存在的路径。键是被引用的路径，值是理由。
# 加一条就要写清楚为什么，否则这份清单会退化成「把红的都塞进来」。
# ---------------------------------------------------------------------------
ALLOW_MISSING: dict[str, str] = {
    # —— 决定「不建」的文件。账本里那条记录的内容**就是**「它不该存在」 ——
    "maos/obs/cost.py":
        "docs/DECISIONS.md T29 明确不建，记账函数放进了 maos/core/store.py",
    "maos/tests/test_conftest_env_baseline.py":
        "docs/DECISIONS.md 明确不建（不在该轨白名单），改记进 BACKLOG ## task-T26",
    # —— 手册/账本里写的是**待建**文件，不是现状 ——
    "obs/otel.py": "docs/EXECUTION.md 是执行手册，写的是 Phase 6 待建的可观测后端",
    "maos/obs/otel.py": "同上，EXECUTION.md 的待建文件",
    "maos/tools/paths.py": "BACKLOG 提议的下沉落点，尚未建",
    "scripts/gen_room_transcript.py": "BACKLOG 提议收编的脚本，尚未建",
    # —— 反例：这个文件**存在**才是 bug ——
    "maos/agents/_sandbox_stub.py":
        "docs/parallel/contracts.md 的反例（禁止另起本地桩），它不存在才是对的",
    # —— 一次性探针，验完即删 ——
    "maos/skills/builtin/probe_autodiscovery_tmp.py": "自动发现探针，验完即删",
    "maos/skills/builtin/_private_probe.py": "自动发现探针，验完即删",
    "scratchpad/probe_mod.py": "scratchpad 里的探针，不入库",
    # —— 外部与虚构 ——
    "src/auth.py": "演示用虚构路径（讲补丁形态，不是本仓库文件）",
    "v2/nacos/config/remote/config_grpc_client_proxy.py":
        "nacos-sdk-python 的内部路径；该 SDK 是可选依赖，默认不装",
    "artifacts/run-result.json": "2026-08-16 旧设计稿里的产物名，早已改名",
    # —— 简写歧义，解析不到唯一目标 ——
    "refund/__init__.py":
        "仓库里有三个同名文件（agents/ domain/ skills.builtin/），按后缀反查不唯一",
    # —— 已在账本里挂号的**已知错误**，账本原文就在说它要改 ——
    "evidence/scenario-R2/trace.json":
        "docs/DECISIONS.md 那一条本身就在记录「这个路径指向不存在的目录、待改」",
}

#: 行号失效的豁免，按 **(含引用的文件, 被引用的路径)** 配对索引，值是理由。
#:
#: 刻意**不按整份文件**豁免：`docs/parallel/contracts.md` 是冻结契约面，也正是最该
#: 守的那一份。整份豁免会让「日后往它里面新加一条指错的行号引用」同样不被发现 ——
#: 那等于在唯一要紧的地方把守卫关掉。配对豁免只放行已知的那一个引用目标，
#: 该文件其余引用照常受查。
#:
#: 当前 8 条失效引用**全部**指向 `main.py`：场景实现迁进 `maos/flows/` 之后，
#: 它从约 160 行缩到 60 行，所有旧行号一次性作废。
ALLOW_STALE_LINES: dict[tuple[str, str], str] = {
    ("REVIEW.md", "maos/main.py"):
        "2026-08-26 基线 f104161 的只读审计快照，文件第 11 行已声明「状态快照已下线」。"
        "不能挪走或重写 —— docs/ops/ORCHESTRATION.md 有 9 处引用它，:4 把它列进"
        "事实源优先级链、:377 明写「不要再动 REVIEW.md」",
    ("REVIEW.md", "main.py"): "同上（同一份快照里的简写形式）",
    ("docs/parallel/contracts.md", "main.py"):
        "原文是「迁移前 main.py:118-122 即此形态，迁移时已消除」——"
        "自带限定，指的就是迁移前的状态，按定义不可核",
}

FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODESPAN = re.compile(r"`([^`\n]+)`")
PATHREF = re.compile(
    r"^((?:[\w.\-]+/)*[\w.\-]+\.(?:py|md|json|toml|sh|ya?ml|ini|cfg|lock|txt|Dockerfile))"
    r"(?::(\d+)(?:[-–](\d+))?)?$"
)
SEP_CELL = re.compile(r"^\s*:?-{2,}:?\s*$")

_linecache: dict[str, int | None] = {}
_basemap: dict[str, list[str]] | None = None


def basemap() -> dict[str, list[str]]:
    """basename -> 仓库内所有同名文件的相对路径。解析 `gate.py:812` 这类简写用。"""
    global _basemap
    if _basemap is None:
        found: dict[str, list[str]] = defaultdict(list)
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                found[f].append(os.path.relpath(os.path.join(base, f), ROOT))
        _basemap = found
    return _basemap


def resolve(fp: str) -> str | None:
    """把文档里写的引用解析成真实相对路径；解析不了返回 None。

    仓库里大量使用**省略前缀的简写**（`refund/payment_agent.py` 指
    `maos/agents/refund/payment_agent.py`），所以直接命中失败后再按「路径后缀且
    落在分段边界上」找一次；唯一命中才算解析成功，多个候选一律判「解析不了」，
    宁可漏报也不要指着错文件去核行号。
    """
    if os.path.exists(os.path.join(ROOT, fp)):
        return fp
    tail = "/" + fp
    hits = [p for p in basemap().get(fp.rsplit("/", 1)[-1], []) if p.endswith(tail)]
    return hits[0] if len(hits) == 1 else None


def line_count(rel: str) -> int | None:
    """文件行数；不存在返回 None；受保护面返回 -1（存在，但不读内容）。"""
    if rel not in _linecache:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            out = None
        elif rel in NO_READ:
            out = -1
        else:
            try:
                with open(path, "rb") as fh:
                    out = len(fh.read().splitlines())
            except OSError:
                out = -1
        _linecache[rel] = out
    return _linecache[rel]


def slug(text: str) -> str:
    """GitHub 风格锚点。中文按原样保留（GitHub 也是这么做的）。"""
    s = re.sub(r"[`*_~\[\]()]", "", text.strip().lower())
    return re.sub(r"[^\w一-鿿\- ]", "", s).replace(" ", "-")


def split_row(line: str) -> list[str]:
    """按未转义的 `|` 切表格行；反引号代码段内的 `|` 不算分隔符。"""
    cells: list[str] = []
    buf: list[str] = []
    i, in_code = 0, False
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i:i + 2])
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells


def check(path: str) -> list[tuple[str, int, str]]:
    """跑完一个文件的八类判据，返回 [(类别, 行号, 说明)]。"""
    rel = os.path.relpath(path, ROOT)
    raw = open(path, "rb").read()
    issues: list[tuple[str, int, str]] = []

    if b"\r\n" in raw:
        issues.append(("F-crlf", 0, "文件含 CRLF 换行"))
    if raw and not raw.endswith(b"\n"):
        issues.append(("F-eof", 0, "文件末尾没有换行符"))

    lines = raw.decode("utf-8", errors="replace").split("\n")

    # -- A 围栏；顺带算出每一行在不在围栏内，后面各类都要用 -----------------
    in_fence, fence_char, fence_len, opened_at = False, "", 0, 0
    code_line = [False] * (len(lines) + 2)
    for n, ln in enumerate(lines, 1):
        m = FENCE.match(ln)
        if m:
            marker, info = m.group(2), m.group(3).strip()
            if not in_fence:
                in_fence, fence_char, fence_len, opened_at = True, marker[0], len(marker), n
                if not info:
                    issues.append(("A-lang", n, "代码围栏没写语言标注"))
            elif marker[0] == fence_char and len(marker) >= fence_len and not info:
                in_fence = False
        code_line[n] = in_fence
    if in_fence:
        issues.append(("A-open", opened_at, "代码围栏从这里开始，直到文件结束都没闭合"))

    # -- B 表格 -----------------------------------------------------------
    i = 0
    while i < len(lines):
        n = i + 1
        if code_line[n] or not lines[i].strip().startswith("|"):
            i += 1
            continue
        header = split_row(lines[i])
        if i + 1 >= len(lines):
            break
        sep = split_row(lines[i + 1])
        if not (sep and all(SEP_CELL.match(c) for c in sep)):
            issues.append(("B-sep", n, f"表格第一行有 {len(header)} 列，但下一行不是合法分隔行"))
            i += 1
            continue
        if len(sep) != len(header):
            issues.append(("B-sepcols", n + 1, f"分隔行 {len(sep)} 列，表头 {len(header)} 列"))
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|") and not code_line[j + 1]:
            row = split_row(lines[j])
            if len(row) != len(header):
                issues.append(("B-cols", j + 1, f"该行 {len(row)} 列，表头 {len(header)} 列"))
            j += 1
        i = j

    # -- C 标题 -----------------------------------------------------------
    seen: dict[str, int] = {}
    prev = h1 = 0
    for n, ln in enumerate(lines, 1):
        if code_line[n]:
            continue
        m = HEADING.match(ln)
        if not m:
            continue
        lvl, title = len(m.group(1)), m.group(2)
        if lvl == 1:
            h1 += 1
            if h1 == 2:
                issues.append(("C-h1", n, "文件里出现第 2 个 H1"))
        if prev and lvl > prev + 1:
            issues.append(("C-skip", n, f"标题层级从 H{prev} 直接跳到 H{lvl}"))
        prev = lvl
        s = slug(title)
        if s in seen:
            issues.append(("C-dup", n, f"标题锚点与第 {seen[s]} 行重复：#{s}"))
        else:
            seen[s] = n

    # -- D 链接 -----------------------------------------------------------
    for n, ln in enumerate(lines, 1):
        if code_line[n]:
            continue
        for _text, target in LINK.findall(ln):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                if slug(target[1:]) not in seen:
                    issues.append(("D-anchor", n, f"锚点链接指不到本文任何标题：{target}"))
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            if not os.path.exists(os.path.normpath(
                    os.path.join(os.path.dirname(path), target))):
                issues.append(("D-link", n, f"链接指向不存在的文件：{target}"))

    # -- E 反引号里的 path[:line] -----------------------------------------
    for n, ln in enumerate(lines, 1):
        if code_line[n]:
            continue
        for span in CODESPAN.findall(ln):
            m = PATHREF.match(span.strip())
            if not m:
                continue
            fp, first, last = m.group(1), m.group(2), m.group(3)
            has_dir = "/" in fp
            if not has_dir and not first:
                continue                     # 裸文件名且无行号 —— 是简写，不是路径断言
            real = resolve(fp)
            if real is None:
                if has_dir and fp not in ALLOW_MISSING:
                    issues.append(("E-missing", n, f"引用的路径不存在：{fp}"))
                continue                     # 裸名反查不唯一：判不了，不报
            cnt = line_count(real)
            if cnt is None or cnt == -1 or (rel, fp) in ALLOW_STALE_LINES:
                continue
            for v in (first, last):
                if v and int(v) > cnt:
                    issues.append(("E-line", n,
                                   f"{fp}:{v} 超出实际行数（{real} 共 {cnt} 行）"))
                    break

    # -- F 空白 -----------------------------------------------------------
    trail = [n for n, ln in enumerate(lines, 1) if ln.strip() and ln != ln.rstrip()]
    if trail:
        issues.append(("F-trail", trail[0], f"{len(trail)} 行有行尾空格（首例在此）"))
    # 反引号代码段里的 Tab 不算：`git check-ignore -v` 之类的**真实输出样例**本就
    # 含 Tab，换成空格等于把样例改假。只查正文。
    tab = [n for n, ln in enumerate(lines, 1)
           if "\t" in CODESPAN.sub("", ln) and not code_line[n]]
    if tab:
        issues.append(("F-tab", tab[0], f"{len(tab)} 行含 Tab（首例在此）"))
    return issues


def default_targets() -> list[str]:
    """缺省检查面：`docs/**/*.md` + 仓库根的 `*.md`。

    根目录用通配而不是写死清单：新加一份根级 md 自动纳入，不需要有人记得来改这里。
    """
    out = [os.path.join(ROOT, f) for f in os.listdir(ROOT) if f.endswith(".md")]
    for base, dirs, files in os.walk(os.path.join(ROOT, "docs")):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        out += [os.path.join(base, f) for f in files if f.endswith(".md")]
    return sorted(set(out))


def run(targets: list[str] | None = None) -> list[tuple[str, str, int, str]]:
    """返回 [(相对路径, 类别, 行号, 说明)]，已扣掉白名单。给测试直接调。"""
    found: list[tuple[str, str, int, str]] = []
    for p in targets or default_targets():
        rel = os.path.relpath(p, ROOT)
        for kind, n, msg in check(p):
            found.append((rel, kind, n, msg))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MAOS 文档结构与引用守卫")
    ap.add_argument("paths", nargs="*", help="要查的 md 或目录；缺省 docs/ + 仓库根 *.md")
    ap.add_argument("--strict", action="store_true", help="有问题就 exit 1")
    args = ap.parse_args(argv)

    targets: list[str] = []
    for a in args.paths:
        if os.path.isdir(a):
            for base, dirs, files in os.walk(a):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                targets += [os.path.join(base, f) for f in files if f.endswith(".md")]
        else:
            targets.append(a)

    issues = run(sorted(set(targets)) or None)
    per_file: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for rel, kind, n, msg in issues:
        per_file[rel].append((kind, n, msg))
    for rel in sorted(per_file):
        print(f"\n### {rel}")
        for kind, n, msg in sorted(per_file[rel], key=lambda x: (x[1], x[0])):
            print(f"  [{kind}] {rel}:{n}  {msg}")

    tally: dict[str, int] = defaultdict(int)
    for _rel, kind, _n, _msg in issues:
        tally[kind] += 1
    print("\n" + "=" * 60)
    if not issues:
        print("文档守卫：全部通过")
    else:
        print(f"共 {len(issues)} 条")
        for k, v in sorted(tally.items(), key=lambda x: -x[1]):
            print(f"  {k:12s} {v}")
    return 1 if (issues and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
