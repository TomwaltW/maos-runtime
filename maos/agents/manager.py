"""Manager Agent —— 把用户请求转成 Plan DAG。

注意它的 Identity：write_scope 只有 plan / task，且 allowed_tools 是空的。
Manager 不直接产出任何业务产物，也不碰 git/ci —— 它只做规划和汇报。
这个边界在 MVP 阶段就要守住，否则后面很容易滑成"Manager 什么都干"。
"""

from __future__ import annotations

import json
import logging

from maos import kb
from maos.agents.base import _ATTRIBUTION, AgentIdentity, BaseAgent, TaskContext, AgentOutput
from maos.contracts.events import new_id
from maos.kb import guardrails, retriever
from maos.model.client import Tier

log = logging.getLogger("maos.agents")

SYSTEM = """你是 Manager Agent，负责把用户请求拆成可执行、可验证的任务计划。
只输出 JSON，格式：
{"tasks":[{"role":"coding","title":"...","inputs":{...},"acceptance":["..."],
"depends_on":[],"risk_level":"L|M|H"}]}
每个任务的 acceptance 必须是可机器判定的，不要写"代码质量好"这种。"""

SKILL_KB = "kb.retrieve"

#: 规划期检索的查询维度，从 `context` 里原样取（阶段一硬过滤 + 两个精确通道）。
_KB_QUERY_FIELDS = (*retriever.PREFILTER_FIELDS, "rule_no", "gateway_code")


class ManagerAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="manager",
        role="manager",
        duty="把用户请求转化为可执行、可验证的 Plan DAG，并在执行中维持计划有效性",
        allowed_skills=frozenset({"req.normalize", "kb.retrieve"}),
        allowed_tools=frozenset(),                 # 刻意为空：不给任何业务工具权限
        write_scope=frozenset({"plan", "task"}),
        max_risk="L",
        model_tier=Tier.STRONG,
    )

    def plan(self, goal: str, *, context: dict | None = None) -> list[dict]:
        """规划前先检索历史知识，命中的结果作为「建议任务」并进 DAG。

        `context` 是**可选**的结构化检索上下文（tenant_id / biz_type / channel_id /
        sku / rule_no / …，外加只用来定归属、进不了检索查询的 plan_id / trace_id）。
        不传就退化成纯规划，prompt 与 1.0 逐字节一致，规划这一次的用量照旧落空
        trace_id 并由 `obs/trace.py::unattributed_usage` 逐条点名 —— 归属是**如实
        记录**，缺了就说缺了，不拿缺省值糊过去。

        检索到的东西**只能增加任务**，且不许替代订单事实、不许跳过人工审批：
        三条护栏在 `kb/guardrails.py` 里写成断言，违反抛 GuardrailViolation。
        """
        ctx = context or {}
        docs = self._kb_prefetch(goal, ctx)
        raw = self._ask_attributed(SYSTEM, self._user_message(goal, docs), ctx)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 规划失败要有确定性兜底，不能让整条链路挂在模型输出上
            return [{
                "task_id": new_id("task"), "role": "coding", "title": goal,
                "inputs": {"goal": goal}, "acceptance": ["产出补丁集且本地自检通过"],
                "depends_on": [], "risk_level": "L",
            }]
        tasks = data.get("tasks", [])
        tasks = self._merge_kb_suggestions(tasks, docs)
        for t in tasks:
            t.setdefault("task_id", new_id("task"))
            t.setdefault("depends_on", [])
            t.setdefault("risk_level", "L")
        return tasks

    def _ask_attributed(self, system: str, user: str, context: dict) -> str:
        """带归属调模型 —— 规划期这一次 `ask()` 唯一能拿到 Run id 的地方。

        `BaseAgent.ask()` 的归属取自 `_ATTRIBUTION`，而那个 ContextVar 只由
        `__init_subclass__` 包在子类的 `run(ctx)` 上（`agents/base.py`）。`plan()`
        不是 `run()`：它跑在 `create_plan` **之前**，压根没有 `TaskContext` 可包，
        所以规划这一次调用的用量恒落空 trace_id。调用方**早就**先生成好 id 放进
        `context` 了（`flows/scenario_6.py`、`flows/contrast.py` 都这么传，两处的
        用量却仍归属不上）—— 缺的一直只是把它接到 `_ATTRIBUTION` 上这一步。

        `ask()` 与 `plan()` 的签名都一个字节不动：归属走 ContextVar，不走参数。
        这是 `docs/DECISIONS.md ## task-T29` 定下的口径，本方法只是把同一条线
        从 `run()` 延到 `plan()`。

        `context` 里没有 id 就**不动**当前绑定：既让不传 context 的调用方逐字节
        等价，也不会拿空串把外层已绑好的归属抹掉。宁可落空串由
        `unattributed_usage` 点名，也不编一个 trace_id —— 口径同
        `core/store.py::record_model_usage` 里那段「不许编 trace_id」。
        """
        ids = {k: str(context.get(k) or "") for k in ("trace_id", "plan_id")}
        if not any(ids.values()):
            return self.ask(system, user)
        token = _ATTRIBUTION.set({**ids, "task_id": None})
        try:
            return self.ask(system, user)
        finally:
            _ATTRIBUTION.reset(token)

    # -- 检索前置（Phase 5）------------------------------------------------
    def _kb_prefetch(self, goal: str, context: dict) -> list[dict]:
        """规划前检索。没 store / 没 tenant_id / KB 关掉 -> 空清单，不抛。

        走 `kb.retrieve`（白名单已含），由它落 SkillInvoked 与 KbRetrieved 两条事件。
        这次检索发生在 `create_plan` **之前**：调用方若在规划前先生成好 plan_id
        并放进 `context`（`ControlPlane.create_plan` 收得下预生成的 id），两条事件
        就挂在它们真正属于的那棵树上；不给就仍落空串，由 trace 列进 stray_events
        单独点名，不假装它们属于某棵树。

        `plan_id` / `trace_id` 不是检索维度 —— `_KB_QUERY_FIELDS` 不收它们，
        它们只用来给事件定归属，进不了检索查询。
        """
        store = getattr(self.skills, "store", None)
        if store is None or not context.get("tenant_id") or not kb.kb_enabled():
            return []

        payload = {f: context[f] for f in _KB_QUERY_FIELDS if context.get(f) not in (None, "")}
        payload["keyword"] = context.get("keyword") or goal
        try:
            res = self.skills.invoke(SKILL_KB, payload, extras={
                "plan_id": str(context.get("plan_id") or ""),
                "trace_id": str(context.get("trace_id") or ""),
                "tier": self.identity.model_tier,
            })
        except Exception as exc:                       # noqa: BLE001 —— 检索不阻塞规划
            log.warning("规划前检索失败（%s），按无知识继续规划", exc)
            return []
        if res.status != "ok" or not isinstance(res.output, dict):
            return []

        # skill 只回命中摘要（不含 body）。建议任务的步骤清单在 body 里，
        # 按 doc_id 回表取 —— prompt 里要塞什么由调用方决定，不由 skill 替它决定。
        docs = []
        for hit in res.output.get("docs") or []:
            row = kb.get_doc(store, context["tenant_id"], hit["doc_id"])
            docs.append({**hit, "body": (row or {}).get("body")})
        return docs

    def _merge_kb_suggestions(self, tasks: list[dict], docs: list[dict]) -> list[dict]:
        """把命中知识翻译成建议任务并合并。护栏不过就**抛**，不静默丢弃。

        丢弃式兜底在这里是错的：护栏拦下的是「知识替代了事实或授权」，
        静默丢掉它，下一次同样的知识还会被同样地用上，而没人知道拦过。
        """
        if not docs or not tasks:
            return tasks
        merged, added = guardrails.apply_suggestions(tasks, docs)
        if added:
            log.info("规划前检索补上 %d 个任务：%s", len(added),
                     [guardrails.task_key(t) for t in added])
        return merged

    @staticmethod
    def _user_message(goal: str, docs: list[dict]) -> str:
        """无命中时逐字节等于 1.0 的 prompt —— 「用户请求」这个前缀是
        ScriptedModelClient 的分派关键字，动了它，走 ManagerAgent 规划的
        场景（1 / 2 / 5 / 6 / 7）全部改判。"""
        base = f"用户请求：{goal}"
        if not docs:
            return base
        lines = [f"- [{d.get('kind')}] {d.get('title')}（相关度 {d.get('score')}）"
                 for d in docs]
        return (f"{base}\n\n历史知识（建议任务 / 建议审批人 / 已知异常分支，"
                f"仅供参考，不得替代当前订单事实与人工授权）：\n" + "\n".join(lines))

    def run(self, ctx: TaskContext) -> AgentOutput:  # Manager 不作为普通 Worker 被调度
        raise NotImplementedError("Manager 由 Control Plane 直接驱动，不走任务队列")
