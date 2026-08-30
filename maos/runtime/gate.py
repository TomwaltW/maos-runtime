"""Reviewer Gate —— 质量门禁。

七道闸按顺序跑，任何一道不过就出 rework，findings 必须结构化
（Coding Agent 要能直接消费，不能是一段自然语言吐槽）。

刻意做成规则驱动而不是模型驱动：Gate 的判定必须可复现、可解释、可审计。
需要模型参与的语义审查，走 Reviewer Agent，挂在 Gate 之后、审批之前，不放在这里。

Phase 2 起判据换了地基（见 ``_gate_acceptance``）：**代码类任务的验收证据不再是
Agent 自述的 self_check，而是一份跑出来的 test_report**。这一条就是对
「所有 Agent 都回复完成 ≠ 业务成功」的正面回答 —— 一个把 build/lint 全写成
pass 的补丁集，在没有测试报告时照样过不了闸。

Phase 3 加第六道闸 ``_gate_finance``（判据见跨轨冻结契约 F-1）。它顺带是「运行时
领域无关」这句话的试金石：退款域漏掉财务复核要能在这里被拦下，而 Gate 本身
**不许 import** ``maos.domain.refund``（铁律 9 推论）—— 判据只落在 ``task["inputs"]``
与 artifact 的 ``content`` 这两个数据形状上，不落在业务模块上。做不到这一点，
「换域只换 Skill/ToolPort/业务对象」当场作废。

Phase 7 给第六道闸补上 **plan 级判据**（见 ``_gate_finance_plan``）。补之前，上一段那句
「漏掉财务复核要能在这里被拦下」是句空话：F-1 按 ``biz_type + amount_claimed`` **逐任务**
触发，而漏排财务复核意味着**没有任何任务带着申报金额**，闸连可判的对象都没有
（BACKLOG ``## task-W3`` 第 3 条）。plan 级判据补的就是这个缺口。

它顺带引入第三个新概念：**作用域**（``scope``）。任务级 finding 说「这一轮产出不合格」，
交给返工；plan 级 finding 说「计划本身少排了一步」—— 返工任何单个任务都补不出那一步，
多返几轮只是把重试次数烧完，所以它交给人（跨轨冻结契约 D-1）。

Phase 4 加第七道闸 ``_gate_gateway``（手册 R2）。它是「网关错误码 -> replan 换渠道」
这条触发线的**输入端**：闸认网关回执、按官方码表判四象限处置，产出
``{"gate": "gateway", "disposition": ...}``；判定由 ``ControlPlane._should_replan``
消费。判据只算一次，就在这里。它同样只认数据形状（``content["receipt"]``），
不 import 退款域。

这道闸引入了一个新概念：**只记录、不挡闸的 finding**（``SEVERITY_INFO``）。
网关回执 ``outcome=unknown`` 时既不能判通过、也不能判不合格 —— 网关自己都说不清，
Gate 替它下结论就是铁律 8 的正面违例。于是这条观察进 findings 供控制面否决重规划，
但不把 verdict 拉成 rework。
"""

from __future__ import annotations

import logging
import os

from maos.artifacts import (
    KIND_COMPENSATION,
    KIND_PATCH_SET,
    KIND_TEST_REPORT,
    resolve_patch_ref,
    validate_artifact,
)
from maos.config import get_config_source
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import Risk, TaskState
from maos.core.control_plane import (
    GATEWAY_GATE,
    GW_HUMAN_TERMINAL,
    GW_QUERY_FIRST,
    GW_QUERY_OR_HUMAN,
    GW_REPLAN_CHANNEL,
    ControlPlane,
)
from maos.core.eventbus import EventBus
from maos.core.store import Store
from maos.tools.gateway_codes import OUTCOME_FAILED, OUTCOME_SUCCESS, lookup
from maos.tools.sandbox import sandbox_git_apply

log = logging.getLogger("maos.gate")

# 判「这是不是代码类任务」的唯一依据：本轮产出里有没有这两种 artifact。
# 用产物类型而不是 task["role"] 判：role 是派单人写的自述，产物是事实；
# 一个自称 "docs" 的任务只要吐出了补丁集，就要按代码类收严，不能靠改 role 绕过。
CODE_ARTIFACT_KINDS = frozenset({KIND_PATCH_SET, KIND_TEST_REPORT})

# 用例状态里算「没通过」的两种。error 与 failed 都不许静默放过：
# 前者是用例根本没跑起来，后者是跑起来但断言没过，两者都不是「通过」。
FAILING_CASE_STATUSES = frozenset({"failed", "error"})

# 靶场自带的隔离探针（``scenarios/fixture-repo/tests/test_isolation_probe.py``）。
# 它们验的是沙箱那几个 docker 参数与 env 白名单，**不由模型生成、也不归模型修**。
# junit 的 classname 是模块路径点号形式，所以前缀长这样。
ISOLATION_PROBE_PREFIX = "tests.test_isolation_probe::"

# 探针挂掉时那一条 finding 的 id。用固定字面量而不是探针用例名：Coding Agent
# 拿 findings 逐条修，把「沙箱断网没生效」当成待修用例喂给它，它只会去改一个
# 它读不懂的文件。这里要传的信息是「环境坏了，别改代码」，不是「这条用例红了」。
ISOLATION_FINDING_ID = "<sandbox-isolation>"

# -- 第六道闸的冻结口径（F-1）------------------------------------------------
# 写闸的一轨与产数的一轨照同一份，谁都不许另立口径：一边按业务表查、一边按
# artifact content 判，两轨各自都绿，合并后闸恒 blocker 或恒 pass，而症状要到
# 跑退款场景才暴露。
FINANCE_BIZ_TYPE = "refund"
FINANCE_THRESHOLD_ENV = "MAOS_FINANCE_THRESHOLD"
DEFAULT_FINANCE_THRESHOLD = 5000.0

#: F-1 的申报金额字段名。抽成常量而不是各处再抄一遍字面量：plan 级判据要按**同一个
#: 字段名**去扫，两处一分叉就是两套口径 —— 正是本节抬头警告的那种事故形状。
FINANCE_AMOUNT_FIELD = "amount_claimed"

# -- 第六道闸的 plan 级判据（BACKLOG ``## task-W3`` 第 3 条）------------------
#: finding 的作用域。**缺省不写即为任务级**；plan 级 finding 显式写这个值，
#: 由控制面路由到人（跨轨冻结契约 D-1 第 4 条）。闸只管产，不管路由。
SCOPE_PLAN = "plan"

#: 递归扫 inputs 找申报金额时的最深层数。设上限而不是无限下潜：inputs 是外部喂进来
#: 的 JSON，Gate 不能假设上游收敛过形状（同 ``_gate_finance`` 的「一律不许抛」）。
FINANCE_SCAN_MAX_DEPTH = 4


# -- 第七道闸的口径（手册 R2）------------------------------------------------
#: 网关回执在 artifact ``content`` 里的字段名。闸只认这个**数据形状**，不认产物
#: kind、更不 import 退款域（同 ``_gate_finance``，铁律 9 推论）：换域之后只要产物
#: 里还挂着一份形如 ``{"code": ...}`` 的网关回执，这道闸一行都不用改。
GATEWAY_RECEIPT_FIELD = "receipt"

#: 只记录、不挡闸的严重度。网关回执 ``outcome=unknown`` 的两格用它 ——
#: 「网关自己也说不清」不是本轮产出的缺陷，判成 rework 等于替网关下了它没下的结论
#: （铁律 8）。但这条观察必须进 findings：控制面靠它一票否决重规划，否则一轮里
#: 凑够两个别的 blocker 就会把一笔下落不明的退款重新派发出去。
SEVERITY_INFO = "info"

#: 四象限的人话说明。四条文案分开写而不是拼字符串：findings 会原样喂回返工提示词，
#: 「可以换渠道重发」和「重发可能造成第二笔」差一个字，处置就反了。
GATEWAY_MESSAGES: dict[str, str] = {
    GW_REPLAN_CHANNEL:
        "网关回执 {code}（{message}）：retriable={retriable} / outcome={outcome}"
        " —— 网关在入口就拒了，这一笔业务确定没执行，可以换渠道重发。官方处置：{remedy}",
    GW_QUERY_FIRST:
        "网关回执 {code}（{message}）：retriable={retriable} / outcome={outcome}"
        " —— 能再发一次，但那一笔的下落网关自己说不清；**直接重发可能造成第二笔**，"
        "必须先 gateway.query 问清楚。官方处置：{remedy}",
    GW_HUMAN_TERMINAL:
        "网关回执 {code}（{message}）：retriable={retriable} / outcome={outcome}"
        " —— 终态失败，原样重发没有意义，需转人工或改单。官方处置：{remedy}",
    GW_QUERY_OR_HUMAN:
        "网关回执 {code}（{message}）：retriable={retriable} / outcome={outcome}"
        " —— 既不能原样重发、那一笔的下落也不明，是最危险的一档；必须 gateway.query"
        " 或转人工。官方处置：{remedy}",
}

#: 四象限的严重度，**与 disposition 同一张表**。
#:
#: 改造前 severity 另算一遍 ``"blocker" if outcome == failed else info`` —— 只看
#: ``outcome`` 一维，而 disposition 看 ``retriable × outcome`` 两维。两套判据必然
#: 在某一格分叉，分叉点就落在 ``GW_QUERY_OR_HUMAN``：**未知**错误码走下面的
#: ``except KeyError`` 分支给 blocker，**已知**的 ``retriable=False + outcome=unknown``
#: 码（现表里只有 ``ACQ.DISCORDANT_REPEAT_REQUEST``）走正常分支给 info。同一个
#: disposition 两种严重度，而 info 不挡闸 —— 于是它单独出现时 ``_review`` 判 pass，
#: 走不到 rework 分支，也就走不到第三出口。四象限里官方称「最危险的一档」，却是
#: 唯一一个**已知码比未知码更容易被放行**的组合（docs/BACKLOG.md 的 ## task-D1
#: 第 4 条）。收成一张表，这类分叉在结构上就不可能再出现。
#:
#: ``GW_QUERY_OR_HUMAN`` 取 blocker 而不是 info：它与未知码同源（那一条已经是
#: blocker），且它是 ``GW_HUMAN_EXIT`` 的两格之一 —— 控制面已经认定这一格
#: 「机器返工修不好」，产出侧却判它「本轮产出没问题」，两侧对不上。
#: ``GW_QUERY_FIRST`` 仍是 info 且不许改：网关自己说不清，判它「本轮产出不合格」
#: 就是替网关下了它没下的结论（铁律 8），而它还有 ``gateway.query`` 这一招机器动作。
GATEWAY_SEVERITY: dict[str, str] = {
    GW_REPLAN_CHANNEL: "blocker",
    GW_QUERY_FIRST: SEVERITY_INFO,
    GW_HUMAN_TERMINAL: "blocker",
    GW_QUERY_OR_HUMAN: "blocker",
}


def _finance_threshold() -> float:
    """每次判定现读一次，不在 import 时固化 —— 否则改阈值得重启进程。

    T28 起「现读」这个动作走 `maos.config` 的配置面：缺省源就是
    `os.environ.get`，取值逐字节不变；`MAOS_CONFIG_SOURCE=nacos` 时同一句改从
    Nacos 取，于是「不重启就能改阈值」从本进程扩到了整个部署面。

    读不出数就回落默认值并告警，不抛：Gate 的异常会掀掉整个 plan（见
    ``_dry_run_reverse`` 的同款理由）。回落方向是**收严**（默认 5000 通常低于
    误配的那个大数），宁可多拦一次，也不因为配置写错而漏掉财务复核。
    """
    raw = get_config_source().get(FINANCE_THRESHOLD_ENV, "")
    if raw is None or not str(raw).strip():
        return DEFAULT_FINANCE_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r 解析不出数值，回落默认阈值 %s",
                    FINANCE_THRESHOLD_ENV, raw, DEFAULT_FINANCE_THRESHOLD)
        return DEFAULT_FINANCE_THRESHOLD


def _over_finance_threshold(raw, threshold: float) -> bool:
    """按 F-1 的字面口径判「这个申报金额要不要走财务复核」。

    三档压成一个 bool，三档都不许改：

      · 缺失 / ``None`` -> ``float(None or 0)`` = 0 -> **不触发**（F-1 字面口径）；
      · 解析不出数（``float("六千")`` 抛）-> **触发**。吞掉当 0 算的话，一笔字段脏掉
        的高额退款就悄悄绕过了财务复核 —— 与把 tool_error 读成「0 条失败」同类假绿；
      · 恰好等于阈值 -> 不触发（``>`` 不是 ``>=``，与 F-1 一字不差）。

    抽成模块函数是因为任务级与 plan 级两条判据必须用**同一把尺**：两边各写一遍
    ``float(... or 0)``，哪天有人只改一边，症状是「闸对同一笔钱一处触发一处不触发」。
    """
    try:
        amount = float(raw or 0)
    except (TypeError, ValueError):
        return True                        # 解析不出 = 自证不了它在阈值之下
    return amount > threshold


def _claimed_amounts(node, depth: int = 0):
    """挖出一份 ``inputs`` 里所有 ``amount_claimed`` 的取值，含嵌套的那些。

    **按字段名下潜，不按路径**。写死 ``case_seed.amount_claimed`` 这种嵌套路径，
    换个域、换个 seed 键名，这条判据当场变成死代码而且没有症状；按字段名扫的话，
    判据跟着 F-1 的词汇走 —— 顶层与嵌套只是同一个字段的两个位置，不是两套口径。
    换域时要动的仍然只有 ``FINANCE_AMOUNT_FIELD`` 这一个常量，与任务级判据同一个。

    命中的键**不再往下潜**：``amount_claimed`` 的值本身是个 dict 时，那是「金额解析
    不出」这一档（交给 ``_over_finance_threshold`` 收严），不是「里面还藏着一个金额」。
    """
    if depth > FINANCE_SCAN_MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key == FINANCE_AMOUNT_FIELD:
                yield value
            else:
                yield from _claimed_amounts(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _claimed_amounts(value, depth + 1)


class ReviewerGate:
    """轮询 AWAITING_REVIEW 的任务，跑七道闸，发 ReviewVerdict。"""

    def __init__(self, store: Store, bus: EventBus, cp: ControlPlane) -> None:
        self.store = store
        self.bus = bus
        self.cp = cp

    def review_pending(self, plan_id: str) -> int:
        n = 0
        for task in self.store.list_tasks(plan_id):
            if task["state"] != TaskState.AWAITING_REVIEW:
                continue
            self._review(task)
            n += 1
        return n

    def _review(self, task: dict) -> None:
        artifacts = [a for a in self.store.list_artifacts(task["task_id"])
                     if a["version"] == task["attempt"]]
        findings: list[dict] = []
        results: dict[str, str] = {}

        for name, check in (
            ("schema", self._gate_schema),
            ("acceptance", self._gate_acceptance),
            ("security", self._gate_security),
            ("evidence", self._gate_evidence),
            ("compensation", self._gate_compensation),
            ("finance", self._gate_finance),
            (GATEWAY_GATE, self._gate_gateway),
        ):
            fs = check(task, artifacts)
            # 挡闸的只算非 info 的那些。info 是「记下来了，但这不是本轮产出的缺陷」，
            # 三态而不是两态：读 gate_results 的人要分得清「这道闸没话说」和
            # 「这道闸有话说但没拦」，压成 pass 就把后者藏起来了。
            blocking = [f for f in fs if f.get("severity") != SEVERITY_INFO]
            results[name] = "fail" if blocking else ("noted" if fs else "pass")
            findings.extend(fs)

        verdict = "rework" if any(f.get("severity") != SEVERITY_INFO
                                  for f in findings) else "pass"
        log.info("[%s] Gate %s -> %s", task["task_id"], results, verdict)

        self.bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
            plan_id=task["plan_id"], task_id=task["task_id"], attempt=task["attempt"],
            trace_id=task["trace_id"], verdict=verdict, findings=findings,
            gate_results=results,
        ))

    # -- 七道闸 -----------------------------------------------------------
    @staticmethod
    def _gate_schema(task, artifacts) -> list[dict]:
        if not artifacts:
            return [{"gate": "schema", "severity": "blocker", "path": None,
                     "message": "本轮没有产出任何 artifact"}]
        out = []
        for a in artifacts:
            if a["kind"] == KIND_PATCH_SET and "files" not in a["content"]:
                out.append({"gate": "schema", "severity": "blocker",
                            "path": None, "message": "patch_set 缺少 files 字段"})
        return out

    def _gate_acceptance(self, task, artifacts) -> list[dict]:
        """验收闸：代码类看测试报告，非代码类看 self_check。判据说死，不留歧义。

        **代码类任务**（本轮产出含 patch_set / test_report）：
          · 读同 attempt 的 test_report artifact；
          · 没有报告 = **blocker，无降级** —— 不回落 self_check。这一条是本闸的
            题眼：一个 self_check 全 pass 的补丁集，没有报告照样过不了。回落等于
            把「Agent 自称完成」重新放回验收依据里，那正是这次要拆掉的东西；
          · 报告带 tool_error = 工具根本没跑成，同样是**没有证据**，判 blocker。
            tool_error 与 failed 必须分开判：把「没跑成」当成「0 条失败」放行，
            是这条链路上最容易造出的假绿；
          · 有 failed / error 用例 = **major**，逐条转成结构化 finding（带 id 与 msg，
            Coding Agent 能直接消费），不合成一句自然语言吐槽；
          · 唯一的例外是**靶场自带的隔离探针**（id 前缀 ``ISOLATION_PROBE_PREFIX``）：
            它们验的是沙箱本身，挂了是环境失效，判 **blocker** 照样挡闸，但压成
            一条不带用例名的 finding —— 逐条喂回去只会让模型去改它读不懂的探针。

        **非代码类任务**（requirement / architecture / review_note 等）：继续用
        self_check，口径与改造前一字不变 —— 「非 pass 即 finding」：
          · self_check 缺失 = 没自检过，不是自检过了，必须判 finding；
          · self_check 不是 dict（None / 字符串）一律按「缺失」处理，**不抛异常** ——
            Gate 是独立判定面，不能假设上游已经把形状收敛好（skill 侧用的是
            setdefault，键在则原样保留；而 validate_artifact 在生产入库路径上
            当前零调用方，见 BACKLOG fix-2）；且 review_pending() 在
            flows/common.py 的驱动循环里是裸调用，异常逃出去会把整个 plan 掀掉，
            连退化成一次 rework 都做不到。
        """
        if any(a["kind"] in CODE_ARTIFACT_KINDS for a in artifacts):
            return self._acceptance_by_test_report(task, artifacts)
        return self._acceptance_by_self_check(artifacts)

    # -- 验收闸的两条分支 ---------------------------------------------------
    def _acceptance_by_test_report(self, task, artifacts) -> list[dict]:
        report = self._resolve_test_report(task, artifacts)
        if report is None:
            return [{"gate": "acceptance", "severity": "blocker", "path": None,
                     "message": f"代码类任务缺少 attempt={task['attempt']} 的 test_report，"
                                f"不接受 self_check 代替 —— 没有跑出来的证据就不算通过"}]

        tool_error = report.get("tool_error")
        if tool_error:
            return [{"gate": "acceptance", "severity": "blocker", "path": None,
                     "message": f"测试工具没跑成（tool_error={tool_error}），"
                                f"本轮没有有效测试证据；这与「0 条失败」不是一回事"}]

        out: list[dict] = []
        failing = [c for c in report.get("cases") or []
                   if isinstance(c, dict) and c.get("status") in FAILING_CASE_STATUSES]

        # 隔离探针与业务用例分开走：探针挂了是**沙箱环境失效**，比一条用例挂严重
        # 得多，所以照样 blocker 挡闸；但它不进逐条 findings —— 那些 findings 会
        # 原样喂回 Coding Agent 的返工提示词，让模型去改靶场自带的探针，既修不好
        # 也把注意力从真正的失败上引开。
        probes = [c for c in failing
                  if str(c.get("id") or "").startswith(ISOLATION_PROBE_PREFIX)]
        if probes:
            # 探针用例名只进日志，不进 finding：审计要看得见，模型不该看见。
            log.error("[%s] 沙箱隔离探针未通过: %s", task["task_id"],
                      [c.get("id") for c in probes])
            out.append({
                "gate": "acceptance", "severity": "blocker", "path": None,
                "id": ISOLATION_FINDING_ID, "msg": f"{len(probes)} 条隔离探针未通过",
                "message": f"沙箱隔离探针有 {len(probes)} 条未通过（断网 / 宿主密钥 / "
                           f"宿主 HOME 三类之一失效）—— 这是沙箱环境故障，不是补丁缺陷。"
                           f"本轮测试结果不可信，不放行；请修沙箱，不要改代码或用例",
            })

        for case in failing:
            if case in probes:
                continue
            case_id = str(case.get("id") or "<未命名用例>")
            msg = str(case.get("msg") or "")
            out.append({
                "gate": "acceptance", "severity": "major", "path": case.get("path"),
                "id": case_id, "msg": msg,
                "message": f"测试用例 {case_id} 未通过：{msg}",
            })

        # 声明的失败数与列出来的用例对不上，说明报告本身不完整 ——
        # 只按 cases 判会让「failed=5 但 cases 为空」的报告静默过闸。
        declared = report.get("failed")
        declared = declared if isinstance(declared, int) else 0
        if declared > len(failing):
            out.append({
                "gate": "acceptance", "severity": "major", "path": None,
                "id": "<report-inconsistent>",
                "msg": f"declared={declared} listed={len(failing)}",
                "message": f"测试报告声明 {declared} 条失败，cases 里只列出 {len(failing)} 条，"
                           f"证据不完整，无法逐条返工",
            })
        return out

    @staticmethod
    def _acceptance_by_self_check(artifacts) -> list[dict]:
        out = []
        for a in artifacts:
            check = a["content"].get("self_check")
            if not isinstance(check, dict):
                check = {}
            for k in ("build", "lint"):
                if check.get(k) != "pass":
                    out.append({"gate": "acceptance", "severity": "major", "path": None,
                                "message": f"本地自检 {k} 未通过，需修复后重新提交"})
        return out

    def _resolve_test_report(self, task, artifacts) -> dict | None:
        """取同 attempt 的测试报告：先看本任务自己的，再认领验证方挂过来的。

        第二条路是给「验证与产出分属两个任务」留的口：Testing Agent 的报告里带
        ``target_task_id`` / ``target_attempt``，指明它验的是谁的哪一次 attempt，
        Gate 据此把这份报告认领到被验任务的验收闸上。没有这条，报告和补丁分居
        两个 task_id，Gate 永远看不到彼此。
        """
        for a in artifacts:
            if a["kind"] == KIND_TEST_REPORT and isinstance(a["content"], dict):
                return a["content"]

        for other in self.store.list_tasks(task["plan_id"]):
            if other["task_id"] == task["task_id"]:
                continue
            for a in self.store.list_artifacts(other["task_id"]):
                content = a.get("content")
                if (a.get("kind") == KIND_TEST_REPORT
                        and a.get("version") == task["attempt"]
                        and isinstance(content, dict)
                        and content.get("target_task_id") == task["task_id"]
                        and content.get("target_attempt") == task["attempt"]):
                    return content
        return None

    @staticmethod
    def _gate_security(task, artifacts) -> list[dict]:
        out = []
        for a in artifacts:
            for f in a["content"].get("files", []):
                diff = f.get("diff", "")
                if any(k in diff for k in ("AKIA", "-----BEGIN", "password=", "api_key=")):
                    out.append({"gate": "security", "severity": "blocker",
                                "path": f["path"], "message": "补丁中疑似出现明文凭证"})
        return out

    @staticmethod
    def _gate_evidence(task, artifacts) -> list[dict]:
        out = []
        for a in artifacts:
            if not a["content"].get("summary"):
                out.append({"gate": "evidence", "severity": "minor", "path": None,
                            "message": "缺少变更说明，无法形成审计证据"})
        return out

    def _gate_compensation(self, task, artifacts) -> list[dict]:
        """补偿干跑闸：高风险任务的补偿方案，必须**当场干跑一遍**证明它真能执行。

        为什么要有这道闸：effect_risk=H 的任务人一批准就立即落地，补偿是唯一的
        退路。而补偿不可执行这件事，不干跑就永远发现不了 —— 症状是补偿「成功」地
        什么都没还原、日志一片正常，直到现场才发现文件根本没回滚。

        三条硬规矩：
          · patch_ref 解析**只走** ``maos/artifacts.py::resolve_patch_ref``（A-7），
            不在这里自写一份解析；
          · **缺 patch_ref 硬失败**，绝不写 ``.get("patch_ref", {})`` 兜底 ——
            那会让补偿静默不执行；
          · 沙箱只用 ``maos/tools/sandbox.py`` 的冻结签名，不另起本地桩。
            Task-B 合并前它抛 NotImplementedError，那就是「干跑不过」，判 blocker，
            不是「没这道闸」。

        只在**存在补偿产物**时才跑：补偿产物由 Task-D 产出，本轨不替它判定
        「高风险任务却没有补偿方案」——那条缺口已记 BACKLOG，留 D 轨接线时定。
        """
        if task.get("effect_risk") != Risk.HIGH:
            return []
        comps = [a for a in artifacts if a["kind"] == KIND_COMPENSATION]
        if not comps:
            return []

        out: list[dict] = []
        for a in comps:
            content = a["content"]
            if not isinstance(content, dict) or "patch_ref" not in content:
                out.append({"gate": "compensation", "severity": "blocker", "path": None,
                            "message": "补偿产物缺 patch_ref —— 不兜底成空引用，"
                                       "否则补偿会静默不执行"})
                continue

            errs = validate_artifact(KIND_COMPENSATION, content)
            if errs:
                out.append({"gate": "compensation", "severity": "blocker", "path": None,
                            "message": "补偿产物形状不合契约: " + "; ".join(errs)})
                continue

            patch_art = resolve_patch_ref(self.store, content["patch_ref"])
            if patch_art is None:
                ref = content["patch_ref"]
                out.append({"gate": "compensation", "severity": "blocker", "path": None,
                            "message": f"patch_ref 解析不到原补丁集"
                                       f"（task_id={ref.get('task_id')} "
                                       f"attempt={ref.get('attempt')}）"})
                continue

            out.extend(self._dry_run_reverse(task, patch_art))
        return out

    @staticmethod
    def _dry_run_reverse(task, patch_art) -> list[dict]:
        """git apply -R --check。不落盘，只回答「这份补丁现在还反得回去吗」。"""
        workdir = str((task.get("inputs") or {}).get("workdir") or "")
        try:
            res = sandbox_git_apply(patch_art["content"], workdir,
                                    reverse=True, check_only=True)
        except NotImplementedError as exc:
            return [{"gate": "compensation", "severity": "blocker", "path": None,
                     "message": f"补偿干跑不可执行（沙箱未就位: {exc}）—— "
                                f"高风险任务不放行未经验证的补偿方案"}]
        except Exception as exc:                      # noqa: BLE001
            # Gate 绝不把异常抛回驱动循环：review_pending() 在 flows/common.py
            # 是裸调用，异常逃出即整个 plan 崩，连退化成一次 rework 都做不到。
            return [{"gate": "compensation", "severity": "blocker", "path": None,
                     "message": f"补偿干跑异常（{type(exc).__name__}: {exc}）"}]

        if isinstance(res, dict) and res.get("ok"):
            return []
        err = (res or {}).get("error") or {} if isinstance(res, dict) else {}
        return [{
            "gate": "compensation", "severity": "blocker",
            "path": err.get("path"),
            "hunk": err.get("hunk"),
            "message": f"补偿干跑不过（stage={err.get('stage')}）: "
                       f"{err.get('message') or '沙箱未给出结构化错误'}",
        }]

    def _gate_finance(self, task, artifacts) -> list[dict]:
        """财务复核闸。两条判据，一条守「金额进了闸还交不出凭据」，一条守「金额压根没进闸」。

        · **任务级**（``_gate_finance_task``，F-1 冻结判据）：这个任务报了超阈金额，
          本轮产出里就必须有财务核算的凭据；
        · **plan 级**（``_gate_finance_plan``，BACKLOG ``## task-W3`` 第 3 条）：这个 Plan
          里报了超阈金额，就必须有任务把它带进任务级判据的触发面 —— 一个任务都没有，
          意味着计划里漏排了财务复核，闸连可判的对象都没有。

        **两条互斥**，所以拼起来至多命中一条：plan 级只在「没有任何任务的顶层
        ``amount_claimed`` 超阈」时开口，而任务级只对「顶层 ``amount_claimed`` 超阈」的
        任务开口。写成相加而不是 if/else，是为了让这条互斥性由代码形状本身托住 ——
        哪天判据松动、两条同时命中，findings 里会如实出现两条，而不是被 else 吃掉一条。

        三条硬规矩（两条判据同守）：
          · **不许 import ``maos.domain.refund``**（铁律 9 推论）。闸只读
            ``task["inputs"]``、artifact 的 ``content``、以及 ``store.list_tasks``
            这三种数据形状，判据不落在业务模块上；换域时要动的只有本文件顶上那几个
            常量。手册正文里「Gate 会查 finance_entry 表」那句与本条冲突，按事实源
            优先级取 F-1（详见 DECISIONS ``## task-R0``）；
          · **金额解析不出数 = 触发，不是放过**（见 ``_over_finance_threshold``）；
          · **空 dict 不算凭据**（见 ``_gate_finance_task``）。

        这道闸是「RAG 有无」对照实验的判定面：没检索到历史案例 → 计划里漏排财务复核
        → 在这里被拦下。**Phase 3 到 Phase 7 之间这句话是假的**，实测里一次都没走过：
        那时只有任务级判据，而漏排意味着没有任何任务带着申报金额，闸根本没被叫到，
        症状要等到下一步 ``payment.execute`` 查不到 ``finance_entry`` 才暴露
        （BACKLOG ``## task-W3`` 第 3 条记的就是这个）。补上 plan 级判据之后它才成立。
        """
        # 相加不是 if/else —— 理由见上面「两条互斥」那段。
        return (self._gate_finance_plan(task)
                + self._gate_finance_task(task, artifacts))

    @staticmethod
    def _gate_finance_task(task, artifacts) -> list[dict]:
        """任务级判据（F-1 冻结原文，一字未动）：报了超阈金额就必须交得出凭据。

        · **触发**：``inputs["biz_type"] == "refund"`` 且
          ``float(inputs["amount_claimed"] or 0)`` 大于阈值（``MAOS_FINANCE_THRESHOLD``，
          默认 5000）。金额缺失 / 为 None 按 0 算 —— 这是 F-1 的字面口径，不改。
        · **判据**：同 attempt 的 artifacts 里，任一份 ``content["finance_entry"]``
          是**非空 dict** 即 pass；否则 blocker。

        **空 dict 不算凭据**：``finance_entry = {}`` 是「跑过了但什么都没算出来」，
        放行它等于把判据降级成「键在不在」。

        这一段判的只有「金额已经在闸的视野里」的那些任务。「金额压根没进视野」
        是另一条判据的事，见 ``_gate_finance_plan``。
        """
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict) or inputs.get("biz_type") != FINANCE_BIZ_TYPE:
            return []

        threshold = _finance_threshold()
        raw_amount = inputs.get(FINANCE_AMOUNT_FIELD)
        try:
            amount = float(raw_amount or 0)
        except (TypeError, ValueError):
            amount = None                      # 解析不出 = 自证不了它在阈值之下
        if amount is not None and amount <= threshold:
            return []

        for a in artifacts:
            content = a.get("content")
            if not isinstance(content, dict):
                continue
            entry = content.get("finance_entry")
            if isinstance(entry, dict) and entry:
                return []

        why = (f"退款金额 {amount} 超过财务复核阈值 {threshold}" if amount is not None
               else f"退款金额 {FINANCE_AMOUNT_FIELD}={raw_amount!r} 解析不出数值，"
                    f"自证不了它在财务复核阈值 {threshold} 之下")
        return [{
            "gate": "finance", "severity": "blocker", "path": None,
            "message": f"{why}，而本轮产出里没有任何一份 artifact 带非空 finance_entry"
                       f" —— 缺少财务核算凭据，不放行",
        }]

    def _gate_finance_plan(self, task) -> list[dict]:
        """plan 级判据：这个 Plan 报了超阈金额，却没有任何任务把它带进闸的视野。

        **判据落在哪个数据形状上**（BACKLOG ``## task-W3`` 第 3 条问的就是这个）：
        两侧都只读 ``store.list_tasks(plan_id)`` 拿到的 ``task["inputs"]``，
        一个字段名（``FINANCE_AMOUNT_FIELD``），两个位置 ——

          · **报没报**：这个 Plan 的任一退款任务的 inputs 树里，任意深度出现过一个
            超阈的 ``amount_claimed``（见 ``_claimed_amounts``）。漏排财务复核之后，
            仍然在场的申报金额只剩下受理那一步的案件种子里那份；
          · **进没进闸**：任一退款任务的**顶层** ``inputs["amount_claimed"]`` 超阈 ——
            那正是 F-1 任务级判据的触发面。

        报了、却一个都没进闸 → plan 级 blocker。反过来只要有一个任务进了闸，这里就闭嘴，
        剩下的交给任务级判据 —— 那才是「带了金额却交不出凭据」该管的事。

        **不判「有没有 finance_entry」**，虽然 BACKLOG 的原话是那么写的。凭据是**跑出来**
        的，判它就等于判「到此刻为止跑出来没有」：正常计划在受理那一步过闸时，财务那一步
        还没轮到，凭据当然还不在，这条判据会对一个完全健康的计划报 blocker。判「计划里有
        没有排这一步」则是计划的静态属性，与跑到哪儿无关 —— 这是下面顺序无关性的前提。

        **与 review 顺序无关**，这是这条判据能挂在逐任务的闸上的立身之本
        （类 docstring 第 5 行：判定必须可复现、可解释、可审计）：

          1. **判据本身与顺序无关**。它是当前任务集的纯函数，只读 ``inputs``，不读任何
             随执行推进而变的东西（任务状态、attempt、artifacts 一概不读）。同一个任务集，
             无论先评审哪个任务、评审到第几个，两侧的扫描结果都一样。
             需要说清的是**任务集本身不是永远不变的**：重规划会覆写 inputs、新增任务
             （``ControlPlane._apply_replan``）。但那是一次显式的计划变更事件，不是评审顺序
             ——变更之后判据照新任务集重算，且这正是要的行为：一次把财务复核改掉的重规划，
             应当在这里被重新拦下。
          2. **命中 N 次不会自相矛盾**。上一条已经保证 N 次判定的**结论**相同；这里再让
             **文案**也相同：报错取 ``min(..., key=task_id)`` 而不是「当前正在评审的任务」，
             所以 N 条 finding 逐字节一致，不会出现「在 A 上说漏了、在 B 上说别的」。
             至于同一条结论要不要重复 N 次 —— 那是控制面的事：它见到第一条
             ``scope="plan"`` 的 blocker 就该转人工，不会走到第二条（跨轨冻结契约 D-1）。
        """
        plan_id = task.get("plan_id")
        if not plan_id:
            return []
        try:
            siblings = self.store.list_tasks(plan_id)
        except Exception as exc:               # noqa: BLE001 —— 闸的异常会掀掉整个 plan
            # 读不到任务集就闭嘴，不抛也不猜。抛会让 review_pending() 裸调处崩掉整个
            # plan（同 _dry_run_reverse）；猜「大概是漏排了」会把存储抖动报成计划缺陷。
            log.warning("[%s] plan 级财务判据读不到任务集（%s: %s），本轮跳过",
                        plan_id, type(exc).__name__, exc)
            return []

        threshold = _finance_threshold()
        declared: list[tuple[str, object]] = []
        in_view = False
        for sibling in siblings:
            inputs = sibling.get("inputs")
            if not isinstance(inputs, dict) or inputs.get("biz_type") != FINANCE_BIZ_TYPE:
                continue
            if _over_finance_threshold(inputs.get(FINANCE_AMOUNT_FIELD), threshold):
                in_view = True
            for raw in _claimed_amounts(inputs):
                if _over_finance_threshold(raw, threshold):
                    declared.append((str(sibling.get("task_id")), raw))

        if in_view or not declared:
            return []

        where, raw = min(declared, key=lambda item: item[0])
        return [{
            "gate": "finance", "scope": SCOPE_PLAN, "severity": "blocker", "path": None,
            "message": f"这个 Plan 报了 {FINANCE_AMOUNT_FIELD}={raw!r}（在任务 {where} 的 "
                       f"inputs 里），超过财务复核阈值 {threshold}，却没有任何一个任务把它"
                       f"带到 inputs[\"{FINANCE_AMOUNT_FIELD}\"] 上 —— 第六道闸对这笔钱没有"
                       f"可判的对象，计划里漏排了财务复核这一步。这是计划本身的缺陷，"
                       f"返工任何单个任务都补不出这一步，需要重规划或转人工。",
        }]

    # -- 第七道闸：网关回执 ------------------------------------------------
    @staticmethod
    def _gate_gateway(task, artifacts) -> list[dict]:
        """认网关回执，按官方码表判四象限处置 —— replan 第三条触发线的输入端。

        · **触发**：本轮任一 artifact 的 ``content["receipt"]`` 是带 ``code`` 的 dict。
          只认数据形状，不认产物 kind、不 import 退款域（同 ``_gate_finance``）。
        · **判据**：一律查 ``maos/tools/gateway_codes.py`` 的已核对官方码表，
          **不看 message 文案、不按语感自判**。码表里 ``retriable`` 与 ``outcome``
          是两个正交维度 —— 前者答「能不能再发一次」，后者答「这一笔到底执行了
          没有」，四象限各对应一种处置（见 control_plane 的 GW_* 常量）。
        · **产出**：``{"gate": "gateway", "disposition": ...}``，由
          ``ControlPlane._should_replan`` 消费；判据只在这里算一次。

        严重度分两档，分界线正是 ``outcome``：
          · ``outcome=failed`` -> **blocker**。业务确定没执行，本轮产出确实不合格。
          · ``outcome=unknown`` -> **info**，不挡闸。网关自己都说不清，判它「不合格」
            就是替网关下结论（铁律 8）；这一格的正确动作是 query 或转人工，那条路
            由 ``effect_risk=H`` 的人工审批走，不该退化成一次机器返工 —— 场景 7
            走的就是这条，闸在那里必须放行。
          · **未知码**（不在已核对官方表里）-> blocker，且归到最危险的那一档。
            ``gateway_codes.lookup`` 的规矩是未知码抛而不兜底，这里照办：兜底成
            「可重试」正是那张表要防的事。
        """
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for a in artifacts:
            content = a.get("content")
            if not isinstance(content, dict):
                continue
            receipt = content.get(GATEWAY_RECEIPT_FIELD)
            if not isinstance(receipt, dict):
                continue
            code = receipt.get("code")
            if not isinstance(code, str) or not code:
                continue
            # 同一笔请求的受理回执与观察回执常常是同一个码（付款任务两份产物各带
            # 一份），只出一条：findings 会原样喂回返工提示词，重复条目只是噪声。
            key = (str(receipt.get("request_id") or ""), code)
            if key in seen:
                continue
            seen.add(key)
            finding = ReviewerGate._gateway_finding(code)
            if finding is not None:
                out.append(finding)
        return out

    @staticmethod
    def _gateway_finding(code: str) -> dict | None:
        """一个码 -> 一条 finding。成功码返回 None（没什么要说的）。"""
        try:
            entry = lookup(code)
        except KeyError as exc:
            log.error("网关回执带未知错误码 %r，按未知外部状态处置", code)
            return {
                "gate": GATEWAY_GATE,
                "severity": GATEWAY_SEVERITY[GW_QUERY_OR_HUMAN],
                "path": None,
                "id": code, "disposition": GW_QUERY_OR_HUMAN,
                "code": code, "retriable": None, "outcome": None,
                "msg": str(exc),
                "message": f"网关回执带未知错误码 {code!r}：不在已核对官方文档的清单内，"
                           f"既判不了它可重试、也判不了它终态失败 —— 按未知外部状态处置，"
                           f"先 gateway.query 或转人工，不许兜底重发",
            }

        if entry.outcome == OUTCOME_SUCCESS:
            return None

        if entry.retriable:
            disposition = (GW_REPLAN_CHANNEL if entry.outcome == OUTCOME_FAILED
                           else GW_QUERY_FIRST)
        else:
            disposition = (GW_HUMAN_TERMINAL if entry.outcome == OUTCOME_FAILED
                           else GW_QUERY_OR_HUMAN)

        return {
            "gate": GATEWAY_GATE,
            # severity 与 disposition 同源：同一格只有一种严重度（见 GATEWAY_SEVERITY）。
            "severity": GATEWAY_SEVERITY[disposition],
            "path": None,
            "id": entry.code, "disposition": disposition, "code": entry.code,
            "retriable": entry.retriable, "outcome": entry.outcome,
            "remedy": entry.remedy, "source": entry.source,
            "msg": entry.message,
            "message": GATEWAY_MESSAGES[disposition].format(
                code=entry.code, message=entry.message, retriable=entry.retriable,
                outcome=entry.outcome, remedy=entry.remedy),
        }


class HumanApprovalQueue:
    """人工审批队列。停在 BLOCKED 等人的任务从这里捞，捞不到的就是没人知道。"""

    def __init__(self, store: Store, cp: ControlPlane) -> None:
        self.store = store
        self.cp = cp

    def pending(self, plan_id: str) -> list[dict]:
        """捞出所有在等人的 BLOCKED 任务。两类，缺一不可。

        · ``effect_risk=H`` —— 产物落地是不可逆动作，Gate 过了也要人放行（既有语义）。
        · 控制面在那一跳的 ``detail`` 里明说 ``await == "human_decision"`` 的
          —— 控制面判定「机器已经没有别的招了」，这一类与 ``effect_risk`` 无关。

        为什么第二类非加不可：控制面的第三出口（``HUMAN_EXIT_*``）判的是
        「机器返工修不好」，判据是 finding 的 disposition 与 scope，两者都不看
        ``effect_risk``。plan 级缺陷尤其可能落在一个 ``effect_risk=L`` 的任务上。
        只按 H 捞，这类任务会停在 BLOCKED 且**没有任何人捞得到** —— 静默挂起，
        比改造前那个明确的 FAILED 更糟。理由与取舍见 docs/DECISIONS.md 的
        ## task-D1 设计点 3。

        判据取自 event_log 而不是任务行上的某个字段：``detail`` 只落在迁移那一条
        事件上，而 event_log 是 Trace 与审计的唯一来源（control_plane 铁律 4）。
        为此在任务行上另开一个字段，就有了第二份事实。

        历史上等过人、后来 ``human_resume`` 回去、这次因**别的**原因再次 BLOCKED
        的任务也会被捞出来 —— 这是刻意的宽：BLOCKED 的三条出边
        （``human_resume`` / ``human_approve`` / ``human_reject``，states.py:40-42）
        全是人的动作，任何一个 BLOCKED 都在等人，多捞不会错，漏捞才会。
        """
        awaiting = {
            e["task_id"] for e in self.store.list_event_log(plan_id)
            if e.get("to_state") == TaskState.BLOCKED
            and isinstance(e.get("detail"), dict)
            and e["detail"].get("await") == "human_decision"
        }
        return [t for t in self.store.list_tasks(plan_id)
                if t["state"] == TaskState.BLOCKED
                and (t["effect_risk"] == Risk.HIGH or t["task_id"] in awaiting)]

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        self.cp.human_decision(task_id, approved, operator, note)
