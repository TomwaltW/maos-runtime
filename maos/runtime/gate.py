"""Reviewer Gate —— 质量门禁。

五道闸按顺序跑，任何一道不过就出 rework，findings 必须结构化
（Coding Agent 要能直接消费，不能是一段自然语言吐槽）。

刻意做成规则驱动而不是模型驱动：Gate 的判定必须可复现、可解释、可审计。
需要模型参与的语义审查，走 Reviewer Agent，挂在 Gate 之后、审批之前，不放在这里。

Phase 2 起判据换了地基（见 ``_gate_acceptance``）：**代码类任务的验收证据不再是
Agent 自述的 self_check，而是一份跑出来的 test_report**。这一条就是对
「所有 Agent 都回复完成 ≠ 业务成功」的正面回答 —— 一个把 build/lint 全写成
pass 的补丁集，在没有测试报告时照样过不了闸。
"""

from __future__ import annotations

import logging

from maos.artifacts import (
    KIND_COMPENSATION,
    KIND_PATCH_SET,
    KIND_TEST_REPORT,
    resolve_patch_ref,
    validate_artifact,
)
from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import Risk, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import Store
from maos.tools.sandbox import sandbox_git_apply

log = logging.getLogger("maos.gate")

# 判「这是不是代码类任务」的唯一依据：本轮产出里有没有这两种 artifact。
# 用产物类型而不是 task["role"] 判：role 是派单人写的自述，产物是事实；
# 一个自称 "docs" 的任务只要吐出了补丁集，就要按代码类收严，不能靠改 role 绕过。
CODE_ARTIFACT_KINDS = frozenset({KIND_PATCH_SET, KIND_TEST_REPORT})

# 用例状态里算「没通过」的两种。error 与 failed 都不许静默放过：
# 前者是用例根本没跑起来，后者是跑起来但断言没过，两者都不是「通过」。
FAILING_CASE_STATUSES = frozenset({"failed", "error"})


class ReviewerGate:
    """轮询 AWAITING_REVIEW 的任务，跑五道闸，发 ReviewVerdict。"""

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
        ):
            fs = check(task, artifacts)
            results[name] = "fail" if fs else "pass"
            findings.extend(fs)

        verdict = "pass" if not findings else "rework"
        log.info("[%s] Gate %s -> %s", task["task_id"], results, verdict)

        self.bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
            plan_id=task["plan_id"], task_id=task["task_id"], attempt=task["attempt"],
            trace_id=task["trace_id"], verdict=verdict, findings=findings,
            gate_results=results,
        ))

    # -- 五道闸 -----------------------------------------------------------
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
            Coding Agent 能直接消费），不合成一句自然语言吐槽。

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
        for case in failing:
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


class HumanApprovalQueue:
    """人工审批队列。高风险任务 Gate 过了也停在 BLOCKED，等这里放行。"""

    def __init__(self, store: Store, cp: ControlPlane) -> None:
        self.store = store
        self.cp = cp

    def pending(self, plan_id: str) -> list[dict]:
        return [t for t in self.store.list_tasks(plan_id)
                if t["state"] == TaskState.BLOCKED and t["effect_risk"] == Risk.HIGH]

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        self.cp.human_decision(task_id, approved, operator, note)
