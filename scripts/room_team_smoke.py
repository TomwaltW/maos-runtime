#!/usr/bin/env python3
"""五岗圆桌冒烟 —— 没起 Matrix、没配 key 的机器上，一条命令看见五个岗位依次说话。

    python3 scripts/room_team_smoke.py
    python3 scripts/room_team_smoke.py scenarios/custom/refund-requests-team.csv --json
    python3 scripts/room_team_smoke.py --evidence scenarios/custom/evidence --pace 400

## 它替代不了什么

它**不是**真房间。真房间要五个独立 Matrix 账号、要 Synapse、要 boss 在 Element 里
敲命令，那条路在 `docs/matrix-room-runbook.md` §10。本脚本只回答一个问题：
**圆桌这套东西装没装上、五岗的事实算得对不对**。所以它零网络、零模型、零环境变量：

  · 模型一律 `select_model_client(..., force_scripted=True)` —— 这台机器上
    `~/.maos.env` source 过之后，不带 `force_scripted` 的构造会真打 DeepSeek；
  · 发声面用本文件自带的假件，**不 import `hiclaw`**（那是 T84 的面，且 import 它
    会把 `nio` 一起拖进来，冒烟脚本不该有这个依赖）；
  · 一个环境变量都不读，因此也没有任何 token 会出现在输出里。
    `--evidence` 落盘时也一样：`AttachmentStore` 的两个参数都显式传，
    不让它去读 `MAOS_ATTACHMENT_DIR` / `MAOS_ATTACHMENT_MAX_BYTES`。

## `--evidence <目录>`：把「证据齐」那半条剧情演出来

不给这个参数时每一单的随案证据都是空的，证据核验岗恒判 `missing` ——
`docs/BACKLOG.md ## task-T89` 记的就是这件事：**证据齐的剧情在 CSV 那条路上
只演得出一半**，因为申请表只有四列，没有地方放附件。

`--evidence` 补的就是这一段：**目录里的文件按文件名前缀配给订单**。

    scenarios/custom/evidence/ORD-2026-0004-rust.png    -> 配给 ORD-2026-0004
    scenarios/custom/evidence/ORD-2026-0006-damage.png  -> 配给 ORD-2026-0006

前缀取申请表里出现过的订单号，**按长度从长到短匹配**（`ORD-1` 与 `ORD-10` 并存时
不会张冠李戴），前缀之后必须紧跟 `-` 或 `.`。认不出订单号的文件跳过并报一行 ——
静默丢掉会让人以为图配上了、实际没配上，而两种情况的屏幕输出一模一样。

走的是**与真房间逐字相同的那条路**：`AttachmentStore.put()` 内容寻址落盘 ->
`StoredAttachment.as_evidence()` 翻成 `customer_evidence` 行 -> 塞进 payload。
所以白名单校验（只收图与 PDF）、digest、去重全部免费获得，也不会出现
「冒烟里配得上、真房间里配不上」这种两套口径。目录不存在或为空只报一行、照常跑完。

## 退出码

  0  五岗全跑通
  2  底账 / 申请表读不到、或表里的订单不在底账（数据问题，人能自己修）
  3  圆桌引擎未装载（`maos.roundtable` 还没并进来）—— 这是**预期的退化路径**，不是失败
  1  留给未捕获异常，本文件不主动返回它

## 为什么发声假件不自己打印

`spoken_by_model` 只在 `StageReport` 上，`Voice` 拿不到 —— 让 Voice 打印就标不出
`[事实卡]`。所以假件只记账，排版交给 `_print_reports`；记下来的那份账另有用处：
`check_said` 用它核对「每一段发言真的经 Voice 发出去过」，而不是只在返回值里存在。
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
DEFAULT_SHEET = ROOT / "scenarios" / "custom" / "refund-requests-team.csv"
DEFAULT_EVIDENCE = ROOT / "scenarios" / "custom" / "evidence"

#: 附件字节的落点。与真房间同一个库（`attachments.DEFAULT_ROOT`），走 .gitignore。
#: 显式写在这里而不是让 `AttachmentStore` 自己取缺省，是因为它的缺省会先读
#: `MAOS_ATTACHMENT_DIR` —— 那就破了本脚本抬头「一个环境变量都不读」那一条。
ATTACHMENT_ROOT = ROOT / "var" / "attachments"
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024

EXIT_OK = 0
EXIT_DATA = 2
EXIT_NO_ROUNDTABLE = 3

#: 占 `requested_by` 的位。不是真 mxid —— 本脚本不连房间，也不该看起来像连了。
REQUESTED_BY = "@smoke:local"

#: 随案证据的来源标记。真房间里这一栏是 `matrix:@boss:maos.local`，
#: 冒烟里写明它来自本地目录 —— 出参里看得出证据是怎么进来的，才对得上账。
EVIDENCE_CHANNEL = "smoke"

NO_ROUNDTABLE = "圆桌引擎未装载（maos.roundtable 不存在），T87 并入后重跑"
NO_VERDICT = "合议引擎未装载（maos.roundtable.verdict 不存在），T90 并入后重跑"

#: 证据文件名与订单号之间的分隔符。`ORD-2026-0004-rust.png` 与
#: `ORD-2026-0004.png` 两种写法都认，`ORD-2026-00041.png` 则**不**算 0004 的证据。
_EVIDENCE_SEPS = ("-", ".")


# ------------------------------------------------------------------ 发声假件
class _RecordingVoice:
    """一个岗位的嘴的假件，顶 `hiclaw.room_voices.Voice`（跨轨契约 §1.3）的位。

    字段与真件同名同义，只是 `say` 落到列表里而不是落到房间里。
    """

    def __init__(self, agent_id: str, title: str, said: list,
                 *, pace_ms: int = 0) -> None:
        self.agent_id = agent_id
        self.title = title
        self.user_id = REQUESTED_BY
        self.own_identity = False
        self._said = said
        self._pace_ms = pace_ms

    def say(self, text: str) -> None:
        self._said.append((self.agent_id, self.title, text))
        # 停顿放在**记账之后**：`--pace` 是给录屏与真房间演示看的观感，
        # 不该影响「这句话有没有发出去」这个判据（`check_said` 读的就是那份账）。
        if self._pace_ms > 0:
            time.sleep(self._pace_ms / 1000.0)


class _LocalVoices:
    """`VoiceSet` 的假件：任何 agent_id 都返回一个 Voice，**永不抛**（同真件语义）。"""

    def __init__(self, titles: dict | None = None, *, pace_ms: int = 0) -> None:
        self._titles = dict(titles or {})
        self.said: list[tuple[str, str, str]] = []
        #: `--pace` 的**退化实现**。引擎带 `pace` 参数（跨轨契约 §3）时由调用方
        #: 清零，节奏走那条正路；引擎还没有那个参数时才由这里的嘴自己停。
        #: 缺省 0 = 一次都不 sleep，`maos/tests` 与不带参数的冒烟一秒都不变慢。
        self.pace_ms = int(pace_ms or 0)

    def voice(self, agent_id: str) -> _RecordingVoice:
        return _RecordingVoice(agent_id, self._titles.get(agent_id, agent_id), self.said,
                               pace_ms=self.pace_ms)

    def bot_users(self) -> frozenset:
        return frozenset()

    def describe(self) -> str:
        return "冒烟模式：五岗全部走本地假件，一条消息都不进房间，一个 env 都不读"

    def close(self) -> None:
        return None


# ------------------------------------------------------------------ 惰性装载
def load_team():
    """惰性 import `maos.roundtable.team`（T87 的面）。没并进来就返回 None，不抛栈。

    冒烟脚本比圆桌引擎先落地，所以「引擎不在」是常态而不是异常：报一行人话、
    给一个专用退出码，比抛 ImportError 栈更能让人知道下一步该干什么。
    """
    try:
        from maos.roundtable import team
    except ImportError:
        return None
    return team if getattr(team, "RefundRoundtable", None) is not None else None


def load_decide():
    """惰性 import 合议引擎的 `decide()`（跨轨契约 §2，T90 的面）。没并进来返回 None。

    与 `load_team` 同一姿态、同一理由：收口卡比引擎先落地，「引擎不在」是常态。
    区别是这里**不给专用退出码** —— 五岗说完了就算跑通，主席没到场不算失败。
    """
    try:
        from maos.roundtable.verdict import decide
    except ImportError:
        return None
    return decide if callable(decide) else None


def _load_run_requests():
    """复用 `scripts/run_requests.py` 的 `read_sheet` / `build_case`。

    不另抄一份：抄了就会出现「CSV 里写『坏了』能跑、冒烟里写『坏了』不认」这种
    两套口径，且两边都不报错。口径同 `maos/ingress/router.py::_load_run_requests`。
    """
    import run_requests

    return run_requests


# ------------------------------------------------------------------ 随案证据
def order_of(filename: str, order_ids) -> str:
    """这个文件名配给哪一单。认不出返回 ``""``。

    **按长度从长到短匹配**：`ORD-1` 与 `ORD-10` 同时在表里时，短的那个会先匹配上
    `ORD-10-x.png`，于是 0 号单的图挂到 1 号单头上 —— 而这种错不报错。
    前缀之后必须紧跟 `-` 或 `.`，否则 `ORD-1` 会吃掉 `ORD-100` 的图。
    """
    name = Path(filename).name
    for order_id in sorted({str(o) for o in order_ids if o}, key=len, reverse=True):
        if name.startswith(order_id) and name[len(order_id):len(order_id) + 1] in _EVIDENCE_SEPS:
            return order_id
    return ""


def load_evidence(directory, order_ids, *, out) -> dict:
    """把目录里的文件按订单号分组落盘，返回 ``{order_id: [StoredAttachment]}``。

    走真房间那条路（`AttachmentStore.put`），因此白名单校验、内容寻址、同图去重
    全部与房间里拖一张图逐字一致。**目录不存在 / 为空 / 全被拒收都只报一行**，
    不抛也不改退出码：`--evidence` 是给演示加料的，不是主路。
    """
    from maos.ingress.attachments import AttachmentStore
    from maos.ingress.contracts import Attachment

    root = Path(directory)
    if not root.is_dir():
        print(f"随案证据目录不存在，按无证据继续：{root}", file=out)
        return {}
    files = sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        print(f"随案证据目录是空的，按无证据继续：{root}", file=out)
        return {}

    store = AttachmentStore(ATTACHMENT_ROOT, max_bytes=ATTACHMENT_MAX_BYTES)
    claimed: dict[str, list] = {}
    skipped: list[str] = []
    for path in files:
        order_id = order_of(path.name, order_ids)
        if not order_id:
            skipped.append(f"{path.name}（文件名前缀不是申请表里的任何一个订单号）")
            continue
        att = Attachment(channel=EVIDENCE_CHANNEL, file_key=path.name,
                         filename=path.name)
        try:
            stored = store.put(path.read_bytes(), att,
                               chat_id=str(root), sender=REQUESTED_BY)
        except (OSError, ValueError) as exc:
            # ValueError 涵盖 AttachmentTooLarge / AttachmentTypeRejected 两个子类：
            # 它们是**拒收**不是故障，报清楚哪一份被拒、为什么，然后接着配下一份。
            skipped.append(f"{path.name}（{type(exc).__name__}: {exc}）")
            continue
        claimed.setdefault(order_id, []).append(stored)

    paired = "；".join(f"{oid} 配 {len(items)} 份"
                       for oid, items in sorted(claimed.items())) or "一份都没配上"
    print(f"随案证据 {root.name}/：{paired}", file=out)
    for line in skipped:
        print(f"  ⚠ 跳过 {line}", file=out)
    return claimed


def attach_evidence(payload: dict, items: list) -> list:
    """把认领到的附件挂成 case 自带的 `customer_evidence`，返回挂上去的那几份。

    形状与 evidence_id 的取法**逐字照抄** `maos/ingress/router.py` 里那段
    （`item.as_evidence(f"ev-{i:02d}")`）—— 两边不同源的症状是同一张图在冒烟里
    算一份证据、在房间里算另一份，而两边各自都自洽。
    """
    if not items:
        return []
    payload["customer_evidence"] = [
        item.as_evidence(f"ev-{i:02d}") for i, item in enumerate(items, 1)]
    return list(items)


# ------------------------------------------------------------------ 排版
def order_reports(reports: list, team_order: tuple) -> list:
    """按 `TEAM_ORDER` 排一遍。不在名单里的排最后、保持原序 —— 不丢发言。"""
    rank = {agent_id: i for i, agent_id in enumerate(team_order)}
    return sorted(reports, key=lambda r: rank.get(r.agent_id, len(rank)))


def _print_reports(reports: list, team_order: tuple, out) -> None:
    """每岗一段：先岗位名，再发言正文；没经模型的在**末行行尾**标 `[事实卡]`。"""
    for report in order_reports(reports, team_order):
        lines = report.speech.splitlines() or [""]
        if not report.spoken_by_model:
            lines[-1] = f"{lines[-1]}  [事实卡]"
        print(f"\n  【{report.title}】", file=out)
        for line in lines:
            print(f"  {line}", file=out)


def _print_verdict(verdict, out) -> None:
    """收口卡：五岗说完之后，对 boss 说「所以这一单批还是不批」。

    形态照跨轨契约 §5.2 的房间卡片排 —— 首行 headline，其后依据 / 拦路条 /
    下一步。房间里那张由主通道发（不归五岗任何一岗），这里同理：它缩进在五岗之外。
    """
    print(f"\n  ┌─ 收口 · {getattr(verdict, 'recommend', '')} "
          f"（{getattr(verdict, 'case_id', '') or '案号未知'}）", file=out)
    print(f"  │ {getattr(verdict, 'headline', '')}", file=out)
    for reason in list(getattr(verdict, "reasons", None) or []):
        print(f"  │   · {reason}", file=out)
    for blocker in list(getattr(verdict, "blockers", None) or []):
        print(f"  │   ⚠ {blocker}", file=out)
    print(f"  └ 下一步：{getattr(verdict, 'next_command', '') or '无'}", file=out)


def verdict_of(decide, reports: list, case_id: str, *, out):
    """算一张收口卡。**算不出来只报一行、不带崩这一单**（圆桌是旁路，跨轨契约 R4）。"""
    if decide is None:
        return None
    try:
        return decide(reports, case_id=case_id)
    except Exception as exc:                            # noqa: BLE001
        print(f"\n  ⚠ 收口卡算不出来（{type(exc).__name__}: {exc}），五岗发言不受影响",
              file=out)
        return None


def verdict_json(verdict) -> dict:
    """收口卡的 JSON 形态。**刻意剔掉 `seats`** —— 它是五岗 data 的原样副本，
    而同一份 data 在 `reports[].data` 里已经有了一份，带上等于把输出翻倍。"""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(verdict):
        data = asdict(verdict)
    else:                                               # 不是 dataclass 也别炸
        data = {k: getattr(verdict, k) for k in
                ("case_id", "recommend", "headline", "reasons", "blockers",
                 "approver_role", "amount_preview", "next_command")
                if hasattr(verdict, k)}
    data.pop("seats", None)
    return data


def make_roundtable(team, model, voices, ledger, pace_ms: int, *, out):
    """建圆桌，并把 `--pace` 接到**引擎认的那个口**上（跨轨契约 §3）。

    引擎带 `pace` 参数就走那条正路，并把发声假件的停顿清零 —— 两处都停会让
    每一岗停两次。引擎还没有那个参数（本轨基线正是如此）时退回假件自己停，
    并报一行说明：`--pace` 在两种引擎下都有效，但走的不是同一条路，读输出的人
    有权知道是哪一条。
    """
    kwargs = {"ledger_loader": lambda: ledger}
    if pace_ms > 0:
        try:
            accepts_pace = "pace" in inspect.signature(team.RefundRoundtable).parameters
        except (TypeError, ValueError):                 # 签名读不到就当不支持
            accepts_pace = False
        if accepts_pace:
            kwargs["pace"] = lambda _i, _total: time.sleep(pace_ms / 1000.0)
            voices.pace_ms = 0
        else:
            print(f"引擎还没有 pace 入参，--pace {pace_ms} 毫秒由发声面代劳", file=out)
    return team.RefundRoundtable(model, voices, **kwargs)


def check_said(reports: list, said: list) -> list:
    """核对每段发言真的经 Voice 发出去过。返回没发出去的岗位名，空 = 全发出去了。

    圆桌是旁路：`Voice.say` 抛异常只 log.warning、不影响下一岗（跨轨契约 §1.3）。
    也就是说「返回值里有这段话」不等于「房间里看得见这段话」—— 两者要分开核。
    """
    spoken = {(agent_id, text) for agent_id, _title, text in said}
    return [r.title for r in reports if (r.agent_id, r.speech) not in spoken]


# ------------------------------------------------------------------ 主流程
def run(sheet, ledger_path, *, as_json: bool = False, out=None,
        evidence_dir=None, pace_ms: int = 0) -> int:
    out = out or sys.stdout
    from maos.flows.custom_case import CaseFileError, load
    from maos.model.client import select_model_client

    rr = _load_run_requests()

    # 数据先读：路径写错时该报「读不到数据」，而不是把它算成「引擎没装」。
    try:
        ledger = load(ledger_path, require_case=False)
        requests = rr.read_sheet(sheet)
    except (rr.RequestSheetError, CaseFileError) as exc:
        print(f"读不到数据：{exc}", file=sys.stderr)
        return EXIT_DATA

    team = load_team()
    if team is None:
        print(NO_ROUNDTABLE, file=out)
        return EXIT_NO_ROUNDTABLE

    from maos.ingress.router import preflight

    # 旁白（配了几份证据、pace 走哪条路、收口卡在不在）的去处。`--json` 时**必须**
    # 是 stderr：那个模式的 stdout 是给机器解析的，掺一行人话进去，`json.loads`
    # 当场炸在第 1 列，而报错完全指不到「是那行说明」。
    notes = sys.stderr if as_json else out

    voices = _LocalVoices(getattr(team, "TITLES", None), pace_ms=pace_ms)
    model = select_model_client(None, force_scripted=True)
    roundtable = make_roundtable(team, model, voices, ledger, pace_ms, out=notes)
    team_order = tuple(getattr(team, "TEAM_ORDER", ()))
    decide = load_decide()

    # 证据先配、再开跑：配到一半才发现目录写错，前几单已经按「没证据」演完了。
    evidence = (load_evidence(evidence_dir, {r["order_id"] for r in requests}, out=notes)
                if evidence_dir else {})

    if not as_json:
        print(f"底账 {Path(ledger_path).name} ｜ 申请表 {Path(sheet).name} ｜ "
              f"{len(requests)} 单", file=out)
        print(voices.describe(), file=out)
        if decide is None:
            print(NO_VERDICT, file=out)

    dumped: list = []
    try:
        for req in requests:
            payload = rr.build_case(ledger, req)
            attached = attach_evidence(payload, evidence.get(req["order_id"]) or [])
            checked = preflight(payload)
            before = len(voices.said)
            reports = roundtable.on_preflight(
                payload=payload, checked=checked, ledger=ledger,
                evidence=attached, requested_by=REQUESTED_BY)
            verdict = verdict_of(decide, reports, str(checked.get("case_id") or ""),
                                 out=notes)
            if as_json:
                row = {
                    "line": req["line"], "order_id": req["order_id"],
                    "reports": [{"agent_id": r.agent_id, "title": r.title, "data": r.data}
                                for r in order_reports(reports, team_order)],
                }
                # 取不到就**不加这个键**，而不是给 null：读 JSON 的人分得清
                # 「合议引擎没装」和「装了但这一单没结论」，后者是 bug。
                if verdict is not None:
                    row["verdict"] = verdict_json(verdict)
                dumped.append(row)
                continue
            print(f"\n{'=' * 78}\n第 {req['line']} 行 · {req['order_id']}"
                  f"（{req['reason_raw'] or req['reason']}）："
                  f"预检裁定 {checked['decision']}\n{'=' * 78}", file=out)
            _print_reports(reports, team_order, out)
            missing = check_said(reports, voices.said[before:])
            if missing:
                print(f"\n  ⚠ 这几岗的发言没经 Voice 发出：{'、'.join(missing)}", file=out)
            if verdict is not None:
                _print_verdict(verdict, out)
    except (rr.RequestSheetError, CaseFileError) as exc:
        print(f"读不到数据：{exc}", file=sys.stderr)
        return EXIT_DATA

    if as_json:
        print(json.dumps(dumped, ensure_ascii=False, indent=2), file=out)
    return EXIT_OK


# ------------------------------------------------------------------ 证据留痕
class _Tee:
    """同时往屏幕和文件写。屏幕上看得见、盘上留得下，两者逐字同一份。"""

    def __init__(self, *targets) -> None:
        self._targets = targets

    def write(self, text: str) -> int:
        for target in self._targets:
            target.write(text)
        return len(text)

    def flush(self) -> None:
        for target in self._targets:
            target.flush()


def header_line() -> str:
    """铁律 3 的证据首行。sha 由 `git rev-parse` 现取，**不接受手写** ——
    口径同 `scripts/make_evidence.py::header_line`，工作区不干净带 `-dirty` 后缀。
    """
    def _git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(ROOT), check=True,
                              capture_output=True, text=True).stdout.strip()

    sha = _git("rev-parse", "HEAD")
    if _git("status", "--porcelain", "--untracked-files=no"):
        sha = f"{sha}-dirty"
    return f"# generated at {datetime.now(timezone.utc).isoformat()} from {sha}"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="room_team_smoke",
        description="五岗圆桌冒烟（零网络、零模型、零 env；不需要 Matrix）")
    parser.add_argument("sheet", nargs="?", default=str(DEFAULT_SHEET),
                        help="演示申请表 CSV，缺省用 refund-requests-team.csv")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="底账 JSON，缺省用 scenarios/custom/ledger.json")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="打成 JSON（每行订单一组 {agent_id, title, data}），供机器比对")
    parser.add_argument("--evidence", metavar="目录", default=None,
                        help="把目录里的文件当随案证据喂进圆桌；按文件名前缀配单"
                             f"（ORD-xxxx-*.png 配给 ORD-xxxx）。演示语料在 "
                             f"{DEFAULT_EVIDENCE.relative_to(ROOT)}。"
                             "目录不存在或为空只报一行、照常跑完")
    parser.add_argument("--pace", metavar="毫秒", type=int, default=0,
                        help="五岗发言之间停一停，给真房间演示与录屏用；缺省 0 = 不停")
    parser.add_argument("--evidence-out", metavar="文件", default=None,
                        dest="evidence_out",
                        help="把这一轮输出原样落成证据文件，首行 "
                             "`# generated at <ISO8601> from <sha>` 由本脚本写入（铁律 3）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    if args.pace < 0:
        parser.error("--pace 不能是负数")

    if not args.evidence_out:
        return run(args.sheet, args.ledger, as_json=args.as_json,
                   evidence_dir=args.evidence, pace_ms=args.pace)

    target = Path(args.evidence_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        print(header_line(), file=handle)
        code = run(args.sheet, args.ledger, as_json=args.as_json,
                   out=_Tee(sys.stdout, handle),
                   evidence_dir=args.evidence, pace_ms=args.pace)
    print(f"\n证据已落盘：{target}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
