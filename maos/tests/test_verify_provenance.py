"""`verify.py` 第 9 项 provenance —— 证据必须是**当前代码**在**干净工作区**上跑的（T105）。

前八项校验的是一束证据**内部自洽**：哈希对得上、引用不悬空。它们绿得很诚实，只是
绿的不是人以为的那件事 —— 一束一年前的证据可以八项全绿。本轨开工时仓库里正是这个
形态：`verify.py` 报 8/8 PASS，而 `evidence/scenario-*` 出自 `006b1d8-dirty`，落后
HEAD 18 个 commit；`evidence/room/` 出自 `f4ffa76-dirty`；`evidence/domains/` 出自
`4be02ec-dirty`。三束三个 sha，且**全部带 `-dirty`**。

`-dirty` 比落后更糟：评委按那个 sha `checkout` 也复现不出来，生成当时工作区有未提交
的改动，那份代码在 git 历史里根本不存在。而「可重放的证据链」是这个仓库在工程落地与
安全审计这一维上最硬的卖点，铁律 3 写着「证据必须真实」—— 首行是有的，只是它指向一个
复现不出来的地方。

这个文件把那件事变成会红的东西。**两个方向都钉**：

- 会红（`test_dirty_*` / `test_stale_*` / `test_malformed_*` / `test_one_stale_bundle_*`）
- 会绿（`test_clean_head_*`）—— 只会红不会绿的守卫等于没写，它会被人当成坏了而绕过

**造数据一律在 `tmp_path` 里现建一个真 git 仓库**，不碰工作区的 `evidence/`：那是交付
物，而且 T105 的派单明令不许动。用真 git 而不是 mock `git rev-parse`，是因为「落后几个
commit」这句话本身就是判据的一部分 —— mock 掉它，报错文案里那个数字就没人验过。
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

HEADER = "# generated at 2026-09-06T00:00:00+00:00 from {sha}"


def _load_verify() -> types.ModuleType:
    """`scripts/` 不是包，只能按路径加载（idiom 同 test_trace_evidence / test_verify_warn）。

    每个用例拿一份**独立的模块对象**：用例要 monkeypatch `verify.ROOT` 把它指到
    `tmp_path`，共用一份会互相串。
    """
    spec = importlib.util.spec_from_file_location("_t105_verify", ROOT / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t105_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _commit(repo: pathlib.Path, message: str) -> str:
    """在 `repo` 里做一个 commit，返回它的完整 sha。"""
    (repo / "file.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t105", "-c", "user.email=t105@example.com",
         "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_bundle(directory: pathlib.Path, sha: str, *, header: str | None = None) -> None:
    """写一束证据的锚点 `INDEX.json` —— 首行出处 + 一个能读的 json 正文。"""
    directory.mkdir(parents=True, exist_ok=True)
    line = HEADER.format(sha=sha) if header is None else header
    body = json.dumps({"git_sha": sha, "produced": []}, ensure_ascii=False)
    (directory / "INDEX.json").write_text(f"{line}\n{body}\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """一个真 git 仓库 + 把 `verify.ROOT` 指过去，于是 `ROOT/evidence` 就是本仓库交付束。

    返回 `(verify 模块, 仓库路径, [sha1, sha2, sha3])`；`sha3` 是 HEAD。
    三个 commit 是为了让「落后 2 个 commit」这句话有东西可数。
    """
    _git(tmp_path, "init", "-q", "-b", "main")
    shas = [_commit(tmp_path, f"c{i}") for i in range(1, 4)]
    verify = _load_verify()
    monkeypatch.setattr(verify, "ROOT", str(tmp_path))
    return verify, tmp_path, shas


def _run(verify) -> object:
    """跑第 9 项。传空 cases —— 本项只读证据文本，不需要库（正是它能在缺 db 时也判的原因）。"""
    return verify.check_provenance([])


# -- 1. 干净 + sha == HEAD -> PASS -----------------------------------------
def test_clean_head_passes(repo):
    """守卫必须**会绿**。只会红的守卫会被当成坏了，然后被人绕过 —— 那比没有更糟。"""
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[-1])
    chk = _run(verify)
    assert chk.status == verify.PASS, chk.notes
    assert (chk.passed, chk.total) == (1, 1), "分母为 0 的 PASS 是空转，与真跑了长得一样"


def test_clean_head_passes_for_every_bundle(repo):
    """三束都新 -> 三束都进分子。逐束报的另一面：全绿时也得看得出查了几束。"""
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[-1])
    _write_bundle(root / "evidence" / "domains", shas[-1])
    _write_bundle(root / "evidence" / "room", shas[-1])
    chk = _run(verify)
    assert chk.status == verify.PASS and (chk.passed, chk.total) == (3, 3), chk.notes


# -- 2. -dirty -> FAIL ------------------------------------------------------
def test_dirty_sha_fails_even_when_it_is_head(repo):
    """`-dirty` 判负与新旧无关：**就算 sha 正是 HEAD** 也不行。

    这是本项与「落后几个 commit」完全独立的一条。脏工作区跑出来的证据，按那个 sha
    checkout 得到的代码与产它的代码不是一份 —— 复现不出来的证据不是证据。
    """
    verify, root, shas = repo
    _write_bundle(root / "evidence", f"{shas[-1]}-dirty")
    chk = _run(verify)
    assert chk.status == verify.FAIL
    assert (chk.passed, chk.total) == (0, 1)
    assert any("make_evidence" in n for n in chk.notes), "报错必须给出下一步动作"
    assert any("-dirty" in n and "复现不出来" in n for n in chk.notes), chk.notes


def test_dirty_sha_fails_when_stale_too(repo):
    """开工时仓库的真实形态：既旧又脏。判负理由印 dirty —— 它是两者里更重的那个。"""
    verify, root, shas = repo
    _write_bundle(root / "evidence", f"{shas[0]}-dirty")
    chk = _run(verify)
    assert chk.status == verify.FAIL
    assert any("make_evidence" in n for n in chk.notes)


# -- 3. sha != HEAD -> FAIL，且写明差几个 commit -----------------------------
def test_stale_sha_fails_and_names_the_distance(repo):
    """落后必须**说出落后几个**。「与 HEAD 不符」是句废话，人还得自己去数。"""
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[0])
    chk = _run(verify)
    assert chk.status == verify.FAIL
    note = "\n".join(chk.notes)
    assert "落后 HEAD 2 个 commit" in note, note
    assert shas[0][:7] in note and shas[-1][:7] in note, "两头的 sha 都要印出来"
    assert "make_evidence" in note, "报错必须给出下一步动作"


def test_sha_absent_from_history_is_called_out(repo):
    """自称出自一个本仓库里不存在的 commit —— 比落后更严重，不许含糊成「落后 N 个」。"""
    verify, root, _ = repo
    _write_bundle(root / "evidence", "0" * 40)
    chk = _run(verify)
    assert chk.status == verify.FAIL
    assert any("不在本仓库历史里" in n for n in chk.notes), chk.notes


def test_commit_distance_reads_both_directions(repo):
    """`commit_distance` 自己的正负例：领先也要说得出来（整合期回滚过就会出现）。"""
    verify, root, shas = repo
    assert verify.commit_distance(shas[0], shas[-1]) == "落后 HEAD 2 个 commit"
    assert verify.commit_distance(shas[-1], shas[0]) == "领先 HEAD 2 个 commit"
    assert verify.commit_distance(shas[-1], shas[-1]) == "与 HEAD 同一个 commit"


# -- 4. 首行格式不对 -> FAIL，且不抛未捕获异常 -------------------------------
@pytest.mark.parametrize("header", [
    "{}",                                        # 直接就是正文，没有出处行
    "# generated at 2026-09-06T00:00:00+00:00",  # 有前半句，没 sha
    "# 本文件由人手写",                            # 像注释但不是出处注释
    "",                                          # 空首行
])
def test_malformed_header_fails_without_raising(repo, header):
    """拿不到出处 = 连自称都没有。判负，且**不许把异常抛到调用方** ——

    第 9 项炸了会让前八项的结论一起印不出来，那是拿一个更大的洞换一个守卫。
    """
    verify, root, _ = repo
    _write_bundle(root / "evidence", "irrelevant", header=header)
    chk = _run(verify)  # 不抛就是判据的一部分
    assert chk.status == verify.FAIL
    assert any("首行不是出处注释" in n for n in chk.notes), chk.notes


# -- 5. 三束里只有一束旧 -> 仍然 FAIL，且点名是哪一束 ------------------------
def test_one_stale_bundle_among_three_still_fails_and_is_named(repo):
    """「一束新两束旧」正是开工时的形态 —— 只看第一束就下结论会把它放过去。

    这一条同时钉两件事：**分子不许因为另外两束是新的就凑够**，以及报错要**点名**
    是哪一束旧了。不点名的话，人拿到一句「证据过期」还得自己去三个目录里翻。
    """
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[-1])            # 新
    _write_bundle(root / "evidence" / "domains", shas[-1])  # 新
    _write_bundle(root / "evidence" / "room", shas[0])      # 旧
    chk = _run(verify)
    assert chk.status == verify.FAIL
    assert (chk.passed, chk.total) == (2, 3), "过了的两束照旧进分子，旧的那束单独判负"
    assert len(chk.notes) == 1, chk.notes
    assert chk.notes[0].startswith("room "), f"没点名是哪一束: {chk.notes[0]}"
    assert "matrix-room-runbook" in chk.notes[0], (
        "room 束没有生成器，指引不许印 make_evidence.py —— 照做的人会发现那条命令不产它")


def test_room_bundle_without_index_is_checked_file_by_file(repo):
    """`evidence/room/` 没有 `INDEX.json`（不由 make_evidence.py 产）—— 退回逐文件查。

    少查一层可以，**整束不查不行**：现状恰恰是它最旧。没有出处首行的文件（截图、
    纯文本导出）不在判据内 —— `.png` 里塞不进注释行，对它判负只会逼人往二进制里写假头。
    """
    verify, root, shas = repo
    room = root / "evidence" / "room"
    room.mkdir(parents=True)
    (room / "README.md").write_text(HEADER.format(sha=shas[0]) + "\ndoc\n", encoding="utf-8")
    (room / "transcript.md").write_text(HEADER.format(sha=shas[-1]) + "\nlog\n", encoding="utf-8")
    (room / "01-card.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (room / "raw.txt").write_text("没有出处行的纯文本导出\n", encoding="utf-8")

    anchors = verify.provenance_anchors(str(root / "evidence"))
    assert [pathlib.Path(p).name for _, p in anchors] == ["README.md", "transcript.md"]
    chk = _run(verify)
    assert chk.status == verify.FAIL and (chk.passed, chk.total) == (1, 2), chk.notes


def test_scenario_dirs_are_not_anchors(repo):
    """`scenario-*` 不做锚点 —— `scenario-R5` 的首行**恒带** `-dirty`。

    它在场景 1-7 已经把 `evidence/` 改脏之后才自算 sha（见 `_SHA_DIRTY_SUFFIX` 的注释，
    submission-checklist.md §A-2 认下的口径）。逐文件查 dirty 会让这一项在任何情况下
    都红 —— 那样守卫只会红不会绿，等于没写。束内各文件与 INDEX.json 是否一致，由
    `load_evidence_json(expect_sha=...)` 管，两层分工不重叠。
    """
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[-1])
    r5 = root / "evidence" / "scenario-R5"
    r5.mkdir()
    (r5 / "result.json").write_text(
        HEADER.format(sha=f"{shas[-1]}-dirty") + "\n{}\n", encoding="utf-8")

    anchors = verify.provenance_anchors(str(root / "evidence"))
    assert [b for b, _ in anchors] == ["evidence/"], anchors
    chk = _run(verify)
    assert chk.status == verify.PASS, chk.notes


# -- SKIP：出处守的是交付物，不是临时目录 ------------------------------------
def test_non_delivery_bundle_skips(repo):
    """`--evidence` 指到别处 -> SKIP，不进分子。

    出处守的是**交付物**。测试与临时目录里现产的证据束（`tmp_path`、`sha="abc"`）的
    出处 sha 本来就没有意义，而且「现产」必然发生在脏工作区 —— 把它们也判进来，等于
    宣布「开发期间不许跑 pytest」，仓库里十几个跑 `verify.py` 的用例会集体转红。
    """
    verify, root, shas = repo
    elsewhere = root / "somewhere-else"
    _write_bundle(elsewhere, shas[-1])
    case = verify.Case(name="scenario-1", directory=str(elsewhere), db_path="", conn=None,
                       tables=set(), trace={}, result={}, evidence_root=str(elsewhere))
    chk = verify.check_provenance([case])
    assert chk.status == verify.SKIP
    assert (chk.passed, chk.total) == (0, 0)
    assert "不是仓库交付束" in chk.skip_reason


def test_missing_evidence_dir_skips_with_a_hint(repo):
    """`evidence/` 整个不存在 -> 报错 + 指引，不算 FAIL（姿态同 `missing_db_hint`）。"""
    verify, root, _ = repo
    chk = _run(verify)
    assert chk.status == verify.SKIP
    assert "make_evidence" in chk.skip_reason, "没有下一步动作的提示等于没提示"


def test_evidence_dir_without_any_header_skips(repo):
    """有目录但一个带出处首行的文件都没有 -> SKIP，不是 0/0 PASS（空转也算没跑）。"""
    verify, root, _ = repo
    (root / "evidence").mkdir()
    chk = _run(verify)
    assert chk.status == verify.SKIP and (chk.passed, chk.total) == (0, 0)
    assert "make_evidence" in chk.skip_reason


def test_outside_git_skips_instead_of_failing(repo, monkeypatch):
    """拿不到 HEAD（评委解压 tar 包跑）-> SKIP。

    判负会是**冤枉**的：证据可能完全没问题，只是这里没有 git 可比。SKIP 不进分子，
    屏幕上看得出这一项没跑，而 `[PASS] provenance` 会是彻头彻尾的谎报。
    """
    verify, root, shas = repo
    _write_bundle(root / "evidence", shas[-1])
    monkeypatch.setattr(verify, "git_head", lambda *a, **k: None)
    chk = _run(verify)
    assert chk.status == verify.SKIP
    assert "git" in chk.skip_reason.lower()


# -- 接线：它真的在 CHECKS 里 ------------------------------------------------
def test_provenance_is_wired_into_checks():
    """判据写了却没接进 `CHECKS`，屏幕上照旧一屏 PASS —— 这是最省事的削弱方式。"""
    verify = _load_verify()
    assert verify.check_provenance in verify.CHECKS
    assert len(verify.CHECKS) == 9, "第 9 项加进来了，前八项一个都不许少"
