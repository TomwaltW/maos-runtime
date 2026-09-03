#!/usr/bin/env python3
"""五岗圆桌冒烟 —— 没起 Matrix、没配 key 的机器上，一条命令看见五个岗位依次说话。

    python3 scripts/room_team_smoke.py
    python3 scripts/room_team_smoke.py scenarios/custom/refund-requests-team.csv --json

## 它替代不了什么

它**不是**真房间。真房间要五个独立 Matrix 账号、要 Synapse、要 boss 在 Element 里
敲命令，那条路在 `docs/matrix-room-runbook.md` §10。本脚本只回答一个问题：
**圆桌这套东西装没装上、五岗的事实算得对不对**。所以它零网络、零模型、零环境变量：

  · 模型一律 `select_model_client(..., force_scripted=True)` —— 这台机器上
    `~/.maos.env` source 过之后，不带 `force_scripted` 的构造会真打 DeepSeek；
  · 发声面用本文件自带的假件，**不 import `hiclaw`**（那是 T84 的面，且 import 它
    会把 `nio` 一起拖进来，冒烟脚本不该有这个依赖）；
  · 一个环境变量都不读，因此也没有任何 token 会出现在输出里。

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
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
DEFAULT_SHEET = ROOT / "scenarios" / "custom" / "refund-requests-team.csv"

EXIT_OK = 0
EXIT_DATA = 2
EXIT_NO_ROUNDTABLE = 3

#: 占 `requested_by` 的位。不是真 mxid —— 本脚本不连房间，也不该看起来像连了。
REQUESTED_BY = "@smoke:local"

NO_ROUNDTABLE = "圆桌引擎未装载（maos.roundtable 不存在），T87 并入后重跑"


# ------------------------------------------------------------------ 发声假件
class _RecordingVoice:
    """一个岗位的嘴的假件，顶 `hiclaw.room_voices.Voice`（跨轨契约 §1.3）的位。

    字段与真件同名同义，只是 `say` 落到列表里而不是落到房间里。
    """

    def __init__(self, agent_id: str, title: str, said: list) -> None:
        self.agent_id = agent_id
        self.title = title
        self.user_id = REQUESTED_BY
        self.own_identity = False
        self._said = said

    def say(self, text: str) -> None:
        self._said.append((self.agent_id, self.title, text))


class _LocalVoices:
    """`VoiceSet` 的假件：任何 agent_id 都返回一个 Voice，**永不抛**（同真件语义）。"""

    def __init__(self, titles: dict | None = None) -> None:
        self._titles = dict(titles or {})
        self.said: list[tuple[str, str, str]] = []

    def voice(self, agent_id: str) -> _RecordingVoice:
        return _RecordingVoice(agent_id, self._titles.get(agent_id, agent_id), self.said)

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


def _load_run_requests():
    """复用 `scripts/run_requests.py` 的 `read_sheet` / `build_case`。

    不另抄一份：抄了就会出现「CSV 里写『坏了』能跑、冒烟里写『坏了』不认」这种
    两套口径，且两边都不报错。口径同 `maos/ingress/router.py::_load_run_requests`。
    """
    import run_requests

    return run_requests


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


def check_said(reports: list, said: list) -> list:
    """核对每段发言真的经 Voice 发出去过。返回没发出去的岗位名，空 = 全发出去了。

    圆桌是旁路：`Voice.say` 抛异常只 log.warning、不影响下一岗（跨轨契约 §1.3）。
    也就是说「返回值里有这段话」不等于「房间里看得见这段话」—— 两者要分开核。
    """
    spoken = {(agent_id, text) for agent_id, _title, text in said}
    return [r.title for r in reports if (r.agent_id, r.speech) not in spoken]


# ------------------------------------------------------------------ 主流程
def run(sheet, ledger_path, *, as_json: bool = False, out=None) -> int:
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

    voices = _LocalVoices(getattr(team, "TITLES", None))
    model = select_model_client(None, force_scripted=True)
    roundtable = team.RefundRoundtable(model, voices, ledger_loader=lambda: ledger)
    team_order = tuple(getattr(team, "TEAM_ORDER", ()))

    if not as_json:
        print(f"底账 {Path(ledger_path).name} ｜ 申请表 {Path(sheet).name} ｜ "
              f"{len(requests)} 单", file=out)
        print(voices.describe(), file=out)

    dumped: list = []
    try:
        for req in requests:
            payload = rr.build_case(ledger, req)
            checked = preflight(payload)
            before = len(voices.said)
            reports = roundtable.on_preflight(
                payload=payload, checked=checked, ledger=ledger,
                evidence=[], requested_by=REQUESTED_BY)
            if as_json:
                dumped.append({
                    "line": req["line"], "order_id": req["order_id"],
                    "reports": [{"agent_id": r.agent_id, "title": r.title, "data": r.data}
                                for r in order_reports(reports, team_order)],
                })
                continue
            print(f"\n{'=' * 78}\n第 {req['line']} 行 · {req['order_id']}"
                  f"（{req['reason_raw'] or req['reason']}）："
                  f"预检裁定 {checked['decision']}\n{'=' * 78}", file=out)
            _print_reports(reports, team_order, out)
            missing = check_said(reports, voices.said[before:])
            if missing:
                print(f"\n  ⚠ 这几岗的发言没经 Voice 发出：{'、'.join(missing)}", file=out)
    except (rr.RequestSheetError, CaseFileError) as exc:
        print(f"读不到数据：{exc}", file=sys.stderr)
        return EXIT_DATA

    if as_json:
        print(json.dumps(dumped, ensure_ascii=False, indent=2), file=out)
    return EXIT_OK


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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-5s %(name)-12s %(message)s")
    return run(args.sheet, args.ledger, as_json=args.as_json)


if __name__ == "__main__":
    sys.exit(main())
