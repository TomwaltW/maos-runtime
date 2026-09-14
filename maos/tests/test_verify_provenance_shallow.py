"""`verify.py` 第 9 项 provenance —— 放宽判据的护栏，与浅克隆下的「判不了」（T153）。

本轨开工时的现象：全新 `git clone --depth 1` 当前主干，按 README §4 的 ①② 跑下来
得到 `provenance 4/16`、`RESULT: 9/10`、`verify exit=1`；同一个 commit 用**完整**
clone 跑则是 `16/16`、`10/10`、`exit 0`。差别只在 `--depth`。

根因是一条自指链：证据入库这个动作本身会让 HEAD 前进一格，于是证据恒自称上一个
sha。`touched_outside_evidence()` 因此把判据从「sha == HEAD」放宽成「sha 之后没动过
`evidence/` 以外的东西」—— 但那一问要读**祖先 commit 的 tree**，而 `--depth 1` 一个
字节都没带过来。`git diff` 报错后按保守判负，报出的那句「该 sha 不在本仓库历史里，
谁也 checkout 不出来」在浅克隆里**是假话**：sha 就在历史里，只是这个克隆没带。

这个文件钉两件事：

1. **放宽没有把判据放空**（`test_code_changed_*` / `test_sidebranch_*`）。放宽的是
   「同一条历史上的纯证据 commit」，不是「任何 tree 像的地方」—— 中间动过 `maos/`
   要判负，旁支上的 commit 即使 tree 只差 `evidence/` 也要判负。
   开工时这两条路径**一条测试都没有**：`test_verify_provenance.py` 里那几条之所以
   还能判负，只因为它的 fixture 提交的是 `file.txt`（本来就在 `evidence/` 之外）。
2. **判不了就说判不了**（`test_shallow_*`）。浅克隆下既不判负（那是把好证据说成
   伪造）也不放行（那是拿「我查不了」冒充「我查过了」），按 SKIP 计、不进分子、
   逐束点名 —— 口径同 `verify.py` 文件头的「SKIP 的纪律」。

**造数据一律在 `tmp_path` 里现建真 git 仓库**，不碰工作区的 `evidence/`（那是交付物）。
浅克隆也是真 `git clone --depth 1`，不是 mock：这一项判的就是 git 对象在不在本地，
mock 掉它等于把被测的东西换成了自己的假设。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

HEADER = "# generated at 2026-09-14T00:00:00+00:00 from {sha}"


def _load_verify() -> types.ModuleType:
    """`scripts/` 不是包，只能按路径加载（idiom 同 test_verify_provenance）。

    每个用例拿一份**独立的模块对象**：用例要 monkeypatch `verify.ROOT`，共用会互相串。
    """
    spec = importlib.util.spec_from_file_location("_t153_verify", ROOT / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t153_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _init(repo: pathlib.Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t153")
    _git(repo, "config", "user.email", "t153@example.com")


def _commit(repo: pathlib.Path, files: dict[str, str], message: str) -> str:
    """按 `{相对路径: 内容}` 落盘并提交，返回完整 sha。"""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _bundle_text(sha: str, *, dirty: bool = False) -> str:
    """一束证据的锚点 `INDEX.json` 正文 —— 首行出处 + 一段读得动的 json。"""
    raw = f"{sha}-dirty" if dirty else sha
    body = json.dumps({"git_sha": raw, "produced": []}, ensure_ascii=False)
    return f"{HEADER.format(sha=raw)}\n{body}\n"


def _write_bundle(directory: pathlib.Path, sha: str, *, dirty: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "INDEX.json").write_text(_bundle_text(sha, dirty=dirty), encoding="utf-8")


def _at(verify: types.ModuleType, monkeypatch, root: pathlib.Path) -> object:
    """把 `verify.ROOT` 指到 `root` 并跑第 9 项。

    传空 cases —— 本项只读证据文本，不碰库（正是它能在缺 db 时也判的原因）。
    """
    monkeypatch.setattr(verify, "ROOT", str(root))
    return verify.check_provenance([])


@pytest.fixture
def verify():
    return _load_verify()


# ---------------------------------------------------------------------------
# 1. 放宽本身：只动 evidence/ 才放行
# ---------------------------------------------------------------------------
def test_evidence_only_commit_is_still_current_code(verify, tmp_path, monkeypatch):
    """证据自称上一个 sha，而那一格**只动了 evidence/** -> 放行。

    这是放宽判据存在的全部理由，也是开工时唯一没有测试覆盖的那条路径。
    它必须**会绿**：只会红的守卫会被当成坏了然后被绕过，那比没有更糟。
    """
    repo = tmp_path / "repo"
    _init(repo)
    base = _commit(repo, {"maos/core.py": "v1\n"}, "c1: 代码")
    _commit(repo, {"evidence/INDEX.json": _bundle_text(base)}, "c2: 只提交证据")

    chk = _at(verify, monkeypatch, repo)

    assert chk.status == verify.PASS, chk.notes
    assert (chk.passed, chk.total) == (1, 1), "分母为 0 的 PASS 是空转"
    assert any("只动过 evidence/" in n for n in chk.notes), (
        "放行了就得说清楚凭什么放行 —— 这一行是评委唯一能看见的理由")


def test_code_changed_since_the_bundle_fails(verify, tmp_path, monkeypatch):
    """同样落后一格，但那一格**动了 `maos/`** -> 判负。

    护栏的正题：放宽的是「只挪了证据」，不是「落后就放行」。这一条红不了的话，
    整个第 9 项就成了摆设 —— 任何过期证据都能混过去。
    """
    repo = tmp_path / "repo"
    _init(repo)
    base = _commit(repo, {"maos/core.py": "v1\n"}, "c1: 代码")
    _commit(repo, {"evidence/INDEX.json": _bundle_text(base),
                   "maos/core.py": "v2  # 证据跑完之后改的\n"}, "c2: 证据 + 改代码")

    chk = _at(verify, monkeypatch, repo)

    assert chk.status == verify.FAIL, chk.notes
    note = "\n".join(chk.notes)
    assert base[:7] in note, "判负要指出是哪个 sha 对不上"
    assert "make_evidence" in note, "报错必须给出下一步动作"


def test_code_change_anywhere_in_the_range_fails(verify, tmp_path, monkeypatch):
    """代码改动夹在**中间**那一格 -> 照样判负。

    判据比的是 `base..HEAD` 两点，不是「HEAD 这一格干不干净」。少了这一条，
    「改代码 -> 再补一个纯证据 commit」就能把守卫洗白。
    """
    repo = tmp_path / "repo"
    _init(repo)
    base = _commit(repo, {"maos/core.py": "v1\n"}, "c1: 代码")
    _commit(repo, {"maos/core.py": "v2\n"}, "c2: 改代码")
    _commit(repo, {"evidence/INDEX.json": _bundle_text(base)}, "c3: 只提交证据")

    chk = _at(verify, monkeypatch, repo)

    assert chk.status == verify.FAIL, chk.notes


def test_sidebranch_commit_is_not_an_ancestor_and_fails(verify, tmp_path, monkeypatch):
    """出处落在**旁支**上 -> 判负，哪怕它与 HEAD 的 tree 只差 `evidence/`。

    `git diff A..B` 比的是两棵 tree，不问 A 在不在 B 的历史上。少了 `merge-base
    --is-ancestor` 这一问，一个从来没进过主干的 commit 也能放行 —— 而评委按那个
    sha checkout 出来的代码，根本不是他手上这份。
    """
    repo = tmp_path / "repo"
    _init(repo)
    base = _commit(repo, {"maos/core.py": "v1\n"}, "c1: 代码")

    # 旁支：从 c1 分出去，只动 evidence/ —— 于是它与主干 HEAD 的 tree 差异也只在
    # evidence/ 下，「只动过 evidence/」那一问会放行它，只有祖先关系拦得住。
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, {"evidence/side.txt": "旁支上的证据\n"}, "s1: 旁支")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"evidence/INDEX.json": _bundle_text(side)}, "c2: 只提交证据")

    assert verify.touched_outside_evidence(side, _git(repo, "rev-parse", "HEAD"), str(repo)), (
        "旁支 commit 必须被当成「动过 evidence/ 以外的东西」拦下")

    chk = _at(verify, monkeypatch, repo)
    assert chk.status == verify.FAIL, chk.notes


# ---------------------------------------------------------------------------
# 2. 浅克隆：判不了，不是判负
# ---------------------------------------------------------------------------
def _shallow_clone(src: pathlib.Path, dst: pathlib.Path, depth: int = 1) -> pathlib.Path:
    subprocess.run(["git", "clone", "--quiet", "--depth", str(depth), "--no-hardlinks",
                    "--single-branch", "--branch", "main", f"file://{src}", str(dst)],
                   check=True, capture_output=True, text=True)
    return dst


@pytest.fixture
def shallow(tmp_path):
    """一个「代码 + 纯证据 commit」的源仓库，和它的 `--depth 1` 克隆。

    返回 `(源仓库, 浅克隆, 证据自称的 sha, 克隆的 HEAD)`。这正是交付包的形态：
    证据自称上一个 sha，而那一格历史没被 clone 带过来。
    """
    src = tmp_path / "src"
    _init(src)
    base = _commit(src, {"maos/core.py": "v1\n"}, "c1: 代码")
    head = _commit(src, {"evidence/INDEX.json": _bundle_text(base)}, "c2: 只提交证据")
    dst = _shallow_clone(src, tmp_path / "shallow")
    return src, dst, base, head


def test_shallow_clone_skips_instead_of_failing(verify, shallow, monkeypatch):
    """浅克隆里出处**核不了** -> 整项 SKIP，不进分子，且逐束点名。

    判负会把一份好证据说成伪造（那一格代码明明没变，只是历史没带过来）；
    放行则是拿「我查不了」冒充「我查过了」。两条都不走。
    """
    _src, clone, base, _head = shallow

    chk = _at(verify, monkeypatch, clone)

    assert chk.status == verify.SKIP, f"{chk.status} / {chk.notes}"
    assert "浅克隆" in chk.skip_reason or "--depth" in chk.skip_reason, chk.skip_reason
    assert "unshallow" in chk.skip_reason, "判不了就得说怎么才判得了"
    assert any(base[:7] in n for n in chk.notes), (
        f"跳过得含糊也是谎报 —— 哪一束没查成必须点名: {chk.notes}")


def test_shallow_clone_does_not_claim_the_sha_is_missing(verify, shallow, monkeypatch):
    """**不许说假话**：浅克隆里那句「该 sha 不在本仓库历史里」是错的。

    sha 就在历史里，只是这个克隆没带。核验器说错话比说不知道更糟 ——
    照着那句去追查的人会认定证据被伪造。
    """
    _src, clone, base, head = shallow

    chk = _at(verify, monkeypatch, clone)

    assert not any("不在本仓库历史里" in n for n in chk.notes), chk.notes
    assert "浅克隆" in verify.commit_distance(base, head, str(clone))


def test_shallow_clone_with_head_sha_still_passes(verify, shallow, monkeypatch):
    """浅克隆里证据自称的就是 HEAD -> 照旧 PASS，新逻辑不许在这里崩。

    这是 T150 客户包（`--orphan`，只有 1 个 commit）的形态：证据与代码同在一个
    commit 里，压根不需要祖先信息。放宽判据不能把这条本来就能判的路径带坏。
    """
    _src, clone, _base, _head = shallow
    clone_head = _git(clone, "rev-parse", "HEAD")
    _write_bundle(clone / "evidence", clone_head)

    chk = _at(verify, monkeypatch, clone)

    assert chk.status == verify.PASS, chk.notes
    assert (chk.passed, chk.total) == (1, 1)


def test_a_real_failure_is_not_hidden_behind_skip(verify, shallow, monkeypatch):
    """浅克隆里**另有一束真判负** -> 整项仍是 FAIL，不许降级成 SKIP。

    判负是确定结论，SKIP 会把它盖掉，屏幕上从「有证据对不上」变成「这项没跑」——
    那是拿判不了当挡箭牌。`-dirty` 不依赖任何历史就能判，正好用来钉这条。
    """
    _src, clone, _base, _head = shallow
    clone_head = _git(clone, "rev-parse", "HEAD")
    _write_bundle(clone / "evidence" / "domains", clone_head, dirty=True)

    chk = _at(verify, monkeypatch, clone)

    assert chk.status == verify.FAIL, f"{chk.status} / {chk.notes}"
    assert any("-dirty" in n for n in chk.notes), chk.notes


def test_deeper_clone_judges_it_for_real(verify, tmp_path, monkeypatch):
    """深度够了就**真的判**，不是 SKIP —— 这是 `make_release.sh` 打包深度的判据。

    交付包要的不是「核验器承认自己判不了」，是出处真的被核过。深度只要覆盖到
    证据自称的那一格，判据一个字都不用放宽。
    """
    src = tmp_path / "src"
    _init(src)
    base = _commit(src, {"maos/core.py": "v1\n"}, "c1: 代码")
    _write_bundle(src / "evidence", base)
    _commit(src, {"evidence/INDEX.json": _bundle_text(base)}, "c2: 只提交证据")
    clone = _shallow_clone(src, tmp_path / "deep", depth=2)

    chk = _at(verify, monkeypatch, clone)

    assert chk.status == verify.PASS, chk.notes
    assert (chk.passed, chk.total) == (1, 1)


# ---------------------------------------------------------------------------
# 3. 两个新原语自己的正负例
# ---------------------------------------------------------------------------
def test_commit_present_tells_local_objects_apart(verify, shallow):
    """`commit_present`：完整仓库里拿得到，浅克隆里拿不到 —— 这就是两句话的分水岭。"""
    src, clone, base, _head = shallow
    assert verify.commit_present(base, str(src))
    assert not verify.commit_present(base, str(clone))
    assert not verify.commit_present("0" * 40, str(src)), "不存在的 sha 到哪都拿不到"


def test_is_shallow_repo_reads_the_real_flag(verify, shallow):
    """`is_shallow_repo`：问 git 自己，不靠猜 `.git/shallow` 在不在。"""
    src, clone, _base, _head = shallow
    assert verify.is_shallow_repo(str(clone))
    assert not verify.is_shallow_repo(str(src))


def test_non_git_dir_is_not_reported_as_shallow(verify, tmp_path):
    """不在 git 仓库里 -> 不当成浅克隆（「不认识就从严」，同 `index_model_mode`）。

    当成浅克隆的话，评委解压一个没有 `.git` 的 tar 包会拿到「判不了」而不是判负 ——
    而那种包里的证据出处**确实**无从核起，该走的是已有的「拿不到 git HEAD」那条 SKIP。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not verify.is_shallow_repo(str(plain))
    assert not verify.commit_present("0" * 40, str(plain))
