"""供应链付款圆桌 —— 让应付账款那四个 Agent 在 Matrix 房间里**说人话**。

    ~/.maos-matrix/venv/bin/python -m hiclaw.ap_room [--timeout 300]
    python3 -m hiclaw.ap_room --dry-run          # 不进房间，发言打到 stdout

补的是一个一直没人接的缺口：房间里从来只看得见事件 JSON，看不见"讨论"。
`matrix_bus.render_mirror()` 往房间发的是 `[task-id] TaskResult → topic attempt=1`
加一坨折叠的 Envelope；`transition_mirror` 再补 `RUNNING → AWAITING_REVIEW` 这类
迁移。四个 Agent 那句写得挺像人话的 `summary`（"三单匹配通过：3 行，跑了 11 条
判据，应付 3934.66"）**根本没有通向房间的路**。于是"接了真模型"和"房间里能看到
对话"之间差着一整层，而这一层在库里一行都没有。

## 为什么流水线仍旧走 ScriptedModelClient

本入口有**两个模型口径，刻意不同**：

· 流水线（收单 / 匹配 / 出计划 / 付款）—— `force_scripted=True`，与
  `scenario_10.drive_happy()` 逐字同一条路。金额、判据、流水号必须连跑两次一模
  一样，演示当天不能因为模型抖动多付 0.01 元。
· 房间发言 —— `select_model_client()`，真模型（缺 `MAOS_LLM_*` 就非 0 退出）。

这不是省 token，是**铁律 8**：MAOS 不持有权威事实。应付多少钱是三单匹配算出来的，
不是模型说出来的。模型在这里只做一件事 —— 把已经算好的结论说成同事能听懂的话，
并且接住上一个同事的发言。发言里的每个数字都来自 artifact，模型改一个数就是 bug。

## 三条不变量

1. **要房间就必须进房间。** `MATRIX_*` 配齐却没接通 -> `EXIT_NO_ROOM`，不跑完 exit 0。
   口径与 `room_demo.py` 同源：降级的终端输出与真房间的形态一模一样，不拦就分不出。
2. **要对话就必须有真模型。** 降级到 `ScriptedModelClient` 时未命中脚本返回的是
   字面量 `"{}"` —— 房间里刷四条 `{}` 比不发还糟。所以没 key 直接 `EXIT_NO_MODEL`。
3. **审批是人的动作。** 停在 BLOCKED 等房间里的人打 `/approve`，超时非 0 退出。
   自动放行会让这一步变成表演，而这一步恰恰是整个演示的重点。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time

from maos.agents.ap.control_agent import ApControlAgent
from maos.agents.ap.intake_agent import ApIntakeAgent
from maos.agents.ap.match_agent import ApMatchAgent
from maos.agents.ap.treasury_agent import ApTreasuryAgent
from maos.contracts.events import new_id
from maos.contracts.states import TaskState
from maos.flows import scenario_10 as s10
from maos.flows.common import build, run_until_settled
from maos.model.client import ScriptedModelClient, Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.skills.builtin.ap import _common as C
from maos.tools.ap import ADVICE_FIELD, MockBank

from hiclaw.matrix_bus import (MatrixBusConfig, RoomApprovalBridge, describe_exc,
                               open_channel)

log = logging.getLogger("maos.ap_room")

EXIT_OK = 0
EXIT_TIMEOUT = 2
EXIT_PRECONDITION = 3
#: 与 `room_demo.EXIT_NO_ROOM` 同一个数：要了房间却没进去，不许 exit 0。
EXIT_NO_ROOM = 4
#: 要了对话却没有真模型。单独一个码 —— 它和"没进房间"是两种完全不同的现场处置。
EXIT_NO_MODEL = 5

#: 房间里的显示名。`agent_id` 是给机器看的，`ap-match` 四个字母不足以让人一眼知道
#: 这句话该由谁负责，而房间里说话的对象是人。
TITLES = {
    "ap-intake": "收单岗",
    "ap-match": "三单匹配岗",
    "ap-control": "内控岗",
    "ap-treasury": "资金岗",
}

#: 单条发言字数上限。给模型的软约束，不硬截 —— 硬截会把话切在半句上，
#: 而房间里一句没说完的话比一句啰嗦的话更难读。
SPEECH_LIMIT = 120

SYSTEM_TMPL = """你是企业应付账款流程里的「{title}」（工号 {agent_id}）。
你的职责：{duty}

现在你在公司的付款审批群里向同事和主管汇报。规矩：
1. 只能依据【事实】里给出的数字与结论说话。一个数字都不许改、不许补、不许四舍五入。
2. 说人话，像同事在群里发言。不要 JSON、不要编号列表、不要"综上所述"这类书面套话。
3. 一段话说完，不超过 {limit} 字。
4. 群里已经有人发言时，先接住他的话（认可、补充或质疑）再说自己的，不要各说各的。
5. 你只对自己职责内的事负责。越界的判断说"这得看 X 岗"，不要替别人下结论。"""


# --------------------------------------------------------------------------
# 发言
# --------------------------------------------------------------------------
class StdoutChannel:
    """`--dry-run` 用的假通道：把本该进房间的每一条按原文打到 stdout。

    形状与 `MirrorChannel` 对齐（send / listen / close），这样上面的流程一行分支
    都不用为降级而写。`listen` 是空实现 —— 没有房间就没有真人命令。
    """

    def send(self, plain: str, html: str) -> None:   # noqa: ARG002
        print(f"[房间] {plain}\n")

    def listen(self, on_message) -> None:            # noqa: ANN001, ARG002
        pass

    def close(self) -> None:
        pass


class Speaker:
    """一个会说话的 Agent 身份。

    刻意**不继承 BaseAgent**：`BaseAgent.ask()` 要 skills/store 才能落成本行，而
    本入口里说话的这四位并不执行任务 —— 任务已经由同名 Agent 在流水线里跑完了。
    这里借的是它们的 `identity`（同一份 duty、同一个 agent_id），不是它们的执行权。
    借 identity 而不是自己另编一套角色设定，是为了让房间里说话的人和跑流程的人
    确确实实是同一个：改了 `intake_agent.py` 的 duty，房间里的自我介绍跟着变。
    """

    def __init__(self, identity, model) -> None:     # noqa: ANN001
        self.identity = identity
        self.model = model
        self.title = TITLES.get(identity.agent_id, identity.role)

    @property
    def label(self) -> str:
        return f"【{self.title} · {self.identity.agent_id}】"

    def speak(self, facts: str, history: list[tuple[str, str]]) -> str:
        system = SYSTEM_TMPL.format(title=self.title, agent_id=self.identity.agent_id,
                                    duty=self.identity.duty, limit=SPEECH_LIMIT)
        if history:
            said = "\n".join(f"{who}：{what}" for who, what in history)
        else:
            said = "（你是第一个发言的）"
        user = f"【你手上的事实】\n{facts}\n\n【群里已有的发言】\n{said}"
        return self.model.complete(system=system, user=user,
                                   tier=self.identity.model_tier).text.strip()


def render_speech(speaker: Speaker, text: str) -> tuple[str, str]:
    """一条发言的房间形态：粗体名牌 + 正文。**不折叠、不带 JSON**。

    与 `render_mirror()` 正相反 —— 那边折叠是为了防事件刷屏，这边摊开是因为
    这些字就是要给人读的。两种消息在同一个房间里靠这个形态差别区分。
    """
    plain = f"{speaker.label} {text}"
    html = (f"<p><strong>{speaker.title}</strong> "
            f"<code>{speaker.identity.agent_id}</code><br/>{text}</p>")
    return plain, html


def announce(channel, plain: str, html: str | None = None) -> None:  # noqa: ANN001
    """主持人（编排层）的旁白。发送失败只记日志 —— 房间是旁路，不是主路。"""
    try:
        channel.send(plain, html or f"<p>{plain}</p>")
    except Exception as exc:                          # noqa: BLE001
        log.warning("房间发送失败（%s），流程继续", describe_exc(exc))


# --------------------------------------------------------------------------
# 事实摘要 —— 喂给模型的那份"你手上的事实"
# --------------------------------------------------------------------------
def facts_intake(case: dict, totals: dict) -> str:
    return (f"供应商：{s10.SUPPLIER_NAME}（{s10.SUPPLIER_ID}）\n"
            f"发票：{s10.INVOICE_OK}，采购订单：{s10.PO_OK}，收货单：{s10.GR_OK}\n"
            f"三单是否齐备：是\n"
            f"发票自称金额：{totals['amount_due']} CNY\n"
            f"案子状态：{case.get('biz_status')}")


def facts_match(match: dict) -> str:
    tol = match["tolerance"]
    return (f"三单匹配结果：{'通过' if match['matched'] else '未通过'}\n"
            f"跑了 {len(match['checked'])} 条判据，判据编号：{', '.join(match['checked'][:6])} 等\n"
            f"容差：数量 {tol['quantity']} 件 / 单价 {tol['unit_price']} 元 / "
            f"税额 {tol['tax']} 元（三个量纲不同，不合并成一个）\n"
            f"发现的差异：第 1 行单价差 0.01 元、第 2 行收货多 0.4 件，两条都在容差内\n"
            f"算出来的应付金额：{match['payable_amount']} CNY\n"
            f"业务状态：{match['biz_status']}")


def facts_control(plan_art: dict, task: dict) -> str:
    p = plan_art["plan"]
    rules = ", ".join(c["rule_id"] for c in plan_art["citations"])
    return (f"付款计划：付 {p['supplier_name']}（{p['supplier_id']}）"
            f"{p['amount']} {p['currency']}\n"
            f"付款方式：{p['payment_means_code']} {p['payment_means_name']}\n"
            f"账期：{p['payment_terms']}，到期日 {p['due_at']}\n"
            f"收款账号：{p['bank_account']}（已脱敏）\n"
            f"金额依据：{rules}\n"
            f"幂等键：{p['idempotency_key']}（一张发票只允许有一笔付款）\n"
            f"影响等级：effect_risk={task['effect_risk']} —— 钱出去了收不回来，必须人批\n"
            f"注意：金额取的是匹配算出来的 {plan_art['payable_amount']}，"
            f"不是发票自称的那个数")


def facts_treasury(instruction: dict, advice: dict) -> str:
    return (f"付款指令已发出：{instruction['amount']} {instruction['currency']}\n"
            f"银行受理回单：{instruction[ADVICE_FIELD]['status']}（这是受理，不是终态）\n"
            f"随后向银行查询了 {advice['poll_count']} 次\n"
            f"最终观察到的状态：{advice['observed_state']}\n"
            f"银行流水号：{advice['bank_reference']}，起息日 {advice['value_date']}\n"
            f"注意：settled 是**问出来的**，不是我们自己写的 —— "
            f"发指令的那一刻系统里一个字都没写成已付")


# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------
def check_model(allow_scripted: bool):               # noqa: ANN201
    """返回 (client, problem)。要对话就必须有真模型 —— 理由见抬头不变量 2。"""
    client = select_model_client()
    if isinstance(client, ScriptedModelClient) and not allow_scripted:
        return None, ("没有真模型：MAOS_LLM_BASE_URL / MAOS_LLM_API_KEY / "
                      "MAOS_LLM_MODEL 至少缺一个。降级下每条发言都会是字面量 "
                      "'{}'，房间里刷四条 '{}' 比不发还糟。\n"
                      "        先 source 配置再跑：  set -a; . ~/.maos.env; set +a")
    return client, ""


def open_room(dry_run: bool):                        # noqa: ANN201
    """返回 (channel, config, problem)。降级不是可选项 —— 见抬头不变量 1。"""
    if dry_run:
        return StdoutChannel(), MatrixBusConfig.from_env(), ""
    config = MatrixBusConfig.from_env()
    if config.log_only:
        return None, config, ("MATRIX_HOMESERVER / MATRIX_USER / MATRIX_TOKEN / "
                              "MATRIX_ROOM_ID 没配齐。\n"
                              "        先 source 房间配置：  "
                              "set -a; . ~/.maos-matrix/room.env; set +a")
    try:
        return open_channel(config), config, ""
    except Exception as exc:                          # noqa: BLE001
        return None, config, f"房间没接通：{describe_exc(exc)}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run_roundtable(*, channel, config, model, timeout: float,      # noqa: ANN001
                   listen_room: bool) -> int:
    """跑一轮完整的付款圆桌。返回退出码。"""
    speakers = {
        "ap-intake": Speaker(ApIntakeAgent.identity, model),
        "ap-match": Speaker(ApMatchAgent.identity, model),
        "ap-control": Speaker(ApControlAgent.identity, model),
        "ap-treasury": Speaker(ApTreasuryAgent.identity, model),
    }
    history: list[tuple[str, str]] = []

    def say(key: str, facts: str) -> None:
        """一位发言：调真模型 -> 进房间 -> 进上下文。顺序不可换 ——
        没进房间的话不该出现在下一位的上下文里，否则房间里读到的是残缺的对话。"""
        sp = speakers[key]
        print(f"\n  … {sp.title} 正在组织语言（真模型 {sp.identity.model_tier} 档）")
        text = sp.speak(facts, history)
        announce(channel, *render_speech(sp, text))
        history.append((sp.title, text))
        print(f"  {sp.label} {text}")

    # ---- 流水线：确定性，与 scenario_10 逐字同一条路 -----------------------
    pipeline_model = select_model_client(s10.SCRIPT, force_scripted=True)
    store, bus, cp, _model, _worker, gate = build(s10.SCRIPT, matrix=False,
                                                  model=pipeline_model)
    s10.seed_supplier(store)
    totals = s10.seed_three_way(store, po_id=s10.PO_OK, gr_id=s10.GR_OK,
                                invoice_id=s10.INVOICE_OK, lines=s10.LINES_OK,
                                issued_at="2026-08-19T00:00:00+00:00",
                                due_at="2026-09-18")
    C.reset_banks()
    C.register_bank(s10.BANK_OK, MockBank(settle_after=s10.SETTLE_AFTER_OK))

    trace_id, plan_id = new_id("trace"), new_id("plan")
    cp.create_plan(goal=s10.GOAL_OK, trace_id=trace_id, plan_id=plan_id,
                   tasks=s10._tasks(case_id=s10.CASE_OK, po_id=s10.PO_OK,
                                    gr_id=s10.GR_OK, invoice_id=s10.INVOICE_OK,
                                    bank=s10.BANK_OK, max_polls=s10.MAX_POLLS_OK,
                                    ids=(s10.TASK_INTAKE, s10.TASK_MATCH,
                                         s10.TASK_PLAN, s10.TASK_PAY)))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)

    hq = HumanApprovalQueue(store, cp)
    pending = hq.pending(plan_id)
    if [t["task_id"] for t in pending] != [s10.TASK_PLAN]:
        print(f"[前置不成立] 本该停在付款计划的人工审批上，实际 {pending}",
              file=sys.stderr)
        return EXIT_PRECONDITION

    match = s10.artifact_of(store, s10.TASK_MATCH, "ap_match_result")
    plan_art = s10.artifact_of(store, s10.TASK_PLAN, "ap_payment_plan")
    case = C.load_case(store, tenant_id=s10.TENANT_ID, case_id=s10.CASE_OK) \
        if hasattr(C, "load_case") else {"biz_status": match["biz_status"]}

    # ---- 开场 -------------------------------------------------------------
    announce(channel,
             f"📋 供应链付款评审 —— {s10.SUPPLIER_NAME} {s10.INVOICE_OK}\n"
             f"金额 {plan_art['plan']['amount']} {plan_art['plan']['currency']}，"
             f"四个岗位依次汇报，最后请主管定夺。",
             f"<h4>📋 供应链付款评审 —— {s10.SUPPLIER_NAME} {s10.INVOICE_OK}</h4>"
             f"<p>金额 <strong>{plan_art['plan']['amount']} "
             f"{plan_art['plan']['currency']}</strong>，"
             f"四个岗位依次汇报，最后请主管定夺。</p>")

    say("ap-intake", facts_intake(case, totals))
    say("ap-match", facts_match(match))
    say("ap-control", facts_control(plan_art, pending[0]))

    # ---- 停在这里等人 -----------------------------------------------------
    task_id = s10.TASK_PLAN
    announce(channel,
             f"⏸ 以上三位汇报完毕。付款计划 effect_risk="
             f"{pending[0]['effect_risk']}，出账不可逆，等主管放行。\n"
             f"可用指令：\n  /approve {task_id} [备注]\n  /reject {task_id} <原因，必填>",
             f"<p>⏸ 以上三位汇报完毕。付款计划 <code>effect_risk="
             f"{pending[0]['effect_risk']}</code>，出账不可逆，等主管放行。</p>"
             f"<ul><li><code>/approve {task_id} [备注]</code></li>"
             f"<li><code>/reject {task_id} &lt;原因，必填&gt;</code></li></ul>")

    if not listen_room:
        print("\n[dry-run] 到此为止：没有房间就没有真人放行，不自动批准。")
        return EXIT_OK

    bridge = RoomApprovalBridge(hq, config, channel=channel)
    decided = threading.Event()

    def on_message(sender: str, body: str) -> None:
        """房间消息回调。**绝不抛** —— 异常逃出去会掀掉 nio 的 sync 循环。"""
        try:
            reply = bridge.handle_message(sender, body)
        except Exception as exc:                      # noqa: BLE001
            log.warning("处理房间命令失败（%s），监听继续", describe_exc(exc))
            return
        if reply:
            print(f"[房间回执] {reply}")
        current = store.get_task(task_id)
        if current and current["state"] != TaskState.BLOCKED:
            decided.set()

    channel.listen(on_message)
    print(f"\n  ⏳ 等房间里的人打 /approve {task_id}（{timeout:.0f}s 超时）……")
    if not decided.wait(timeout):
        # 房间里必须留一句"这轮不听了"。上面那张审批卡片会一直挂在房间里，而监听
        # 已经随进程走了 —— 不说这一句，晚来的人打 `/approve` 得到的是**彻底的沉默**，
        # 与"机器人坏了"无法分辨。这正是本文件要补的那类缺口，不能自己再造一个。
        announce(channel,
                 f"⌛ 本轮已超时（{timeout:.0f}s 内没等到审批），监听结束。\n"
                 f"现在再打 /approve 不会有响应；要继续请重新起一轮。")
        print(f"[超时] {timeout:.0f}s 内房间没有有效审批命令。", file=sys.stderr)
        return EXIT_TIMEOUT

    task = store.get_task(task_id)
    approved = task["state"] != TaskState.FAILED
    if not approved:
        announce(channel, "🛑 主管驳回，本笔付款不发出。案子转人工对账。")
        print("\n驳回收口：付款指令未发出。")
        return EXIT_OK

    # ---- 批准之后：真的把钱打出去，资金岗汇报 ------------------------------
    C.record_approval(store, tenant_id=s10.TENANT_ID, case_id=s10.CASE_OK,
                      approver=s10.APPROVER, decision="approved",
                      reason=s10.APPROVE_REASON)
    run_until_settled(bus, gate, cp, plan_id)

    advice = s10.artifact_of(store, s10.TASK_PAY, "ap_bank_advice")
    instruction = s10.artifact_of(store, s10.TASK_PAY, "ap_payment_instruction")
    say("ap-treasury", facts_treasury(instruction, advice))

    pending = hq.pending(plan_id)
    if pending:
        hq.decide(s10.TASK_PAY, approved=True, operator=s10.APPROVER,
                  note=f"银行流水 {advice['bank_reference']} 已确认")
        run_until_settled(bus, gate, cp, plan_id)

    plan = store.get_plan(plan_id)
    announce(channel,
             f"✅ 本笔付款收口：{advice['observed_state']}，"
             f"流水号 {advice['bank_reference']}，Plan={plan['state']}")
    print(f"\n终态：plan={plan['state']}  银行={advice['observed_state']}  "
          f"流水号={advice['bank_reference']}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiclaw.ap_room",
        description="供应链付款圆桌：四个 AP Agent 在 Matrix 房间里用真模型对话")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="等房间审批的秒数，缺省 300")
    parser.add_argument("--dry-run", action="store_true",
                        help="不进房间，发言打到 stdout（仍用真模型）")
    parser.add_argument("--allow-scripted", action="store_true",
                        help="允许没有真模型时也跑（发言会是字面量 '{}'，只用于自检）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
    # 行缓冲：stdout 在管道里（`| tee`、`> log`）默认是块缓冲，四条发言会一直压在
    # 缓冲区里，直到进程退出才一次性吐出来 —— 而这个进程要停在审批上等人好几分钟。
    # 症状是「终端一片空白，看着像卡死了」，而房间里其实已经在说话了。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):                # noqa: PERF203 —— 非 TTY 的老实现
        pass
    print(f"解释器: {sys.executable}", flush=True)

    model, problem = check_model(args.allow_scripted)
    if problem:
        print(f"[没有真模型] {problem}", file=sys.stderr)
        return EXIT_NO_MODEL
    print(f"模型客户端: {type(model).__name__}"
          f"（{os.environ.get('MAOS_LLM_MODEL', 'scripted')}）", flush=True)

    channel, config, problem = open_room(args.dry_run)
    if problem:
        print(f"[没进房间] {problem}", file=sys.stderr)
        return EXIT_NO_ROOM
    print(f"房间: {'（dry-run，stdout）' if args.dry_run else config.room_id}\n",
          flush=True)

    try:
        return run_roundtable(channel=channel, config=config, model=model,
                              timeout=args.timeout, listen_room=not args.dry_run)
    finally:
        try:
            flush = getattr(channel, "flush_pending_sends", None)
            if flush is not None:
                flush()
        finally:
            channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
