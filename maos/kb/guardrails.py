"""知识层护栏 —— 检索结果能做什么、不能做什么，写成断言，违反抛异常。

评委原话是这一整个文件的来源：

    历史流程只能帮助规划，不能替代当前订单事实和人工授权。

拆成三条可执行的断言（各自一条负例测试）：

1. **只增不删** —— 检索结果可以往 DAG 里补任务，不能删掉任何一条既有任务。
   历史案例里没出现过某一步，不等于这一步不必要；它可能只是当时没记全。
2. **不替代事实** —— 建议任务不许携带订单事实字段。历史案例里的金额、政策版本、
   订单版本是**那一单**的事实，抄到当前 case 上就是把别人的事实当成自己的。
   当前订单事实只能从 `order_snapshot` 读，一条都不许从知识层来。
3. **不跳审批** —— 建议不能移除高风险标记、不能把 effect_risk 降下来、
   不能带任何「免审批」标记。检索到「上次这类单直接放行了」也不行：
   上次放行是人做的决定，不是这次可以省掉人的理由。

## 晋升规则也在这里

「什么样的案例够格进正例知识层」与「检索结果不许做什么」是同一个问题的两面：
前者管入口，后者管出口。放同一个文件，改的时候两边一起看得见。

规则按评委原话：只有**证据完整且外部结果明确**的案例才进默认知识层 ——
有 `payment_observation`、`observed_state='settled'`、且有客户 ack。
差一条都不进正例层；失败实例进 `failure_hint`，只用于提示「哪类渠道 / 支付返回 /
政策组合需要额外步骤」，**不作为规划正例**。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from maos import kb
from maos.contracts.events import new_id

log = logging.getLogger("maos.kb")

#: 当前订单事实的字段。建议任务的 inputs 里出现任何一个即判违规（护栏 2）。
#: 这些值的唯一权威来源是 `order_snapshot`，MAOS 只持有「执行前读到的那一份」。
ORDER_FACT_FIELDS = frozenset({
    "order_id", "order_version", "amount_paid", "paid_at",
    "policy_version_at_order", "biz_status", "observed_state",
})

#: 「免审批」标记的各种写法。护栏 3 一律拒绝。
APPROVAL_SKIP_KEYS = frozenset({
    "skip_approval", "auto_approve", "bypass_approval", "no_approval",
})

RISK_ORDER = {"L": 0, "M": 1, "H": 2}

#: 建议任务里允许出现的键。多出来的键一律丢弃（不抛）——
#: 知识层给的是「该做哪一步」，不是一份可以直接执行的任务定义。
SUGGESTION_KEYS = ("role", "title", "inputs", "acceptance", "depends_on",
                   "risk_level", "effect_risk")


class GuardrailViolation(RuntimeError):
    """检索结果越界。**不要 catch 成告警** —— 这是「知识替代了事实或授权」，
    与越权调用同级，必须中止并留痕。"""


# ------------------------------------------------------------------ 任务同一性
def task_key(task: dict) -> tuple[str, str]:
    """任务的同一性：`(role, step)`。

    只按 role 判会把「受理」和「通知」当成同一步（退款域里两者同 role）；
    只按 title 判会被一次措辞改动骗过去。`inputs["step"]` 缺省回落 role。
    """
    inputs = task.get("inputs") or {}
    step = inputs.get("step") if isinstance(inputs, dict) else None
    role = str(task.get("role") or "")
    return role, str(step or role)


# ------------------------------------------------------------------ 三条护栏
def assert_only_adds(baseline: list[dict], proposed: list[dict]) -> None:
    """护栏 1：只能增加任务，不能删除必要任务。"""
    before = {task_key(t) for t in baseline}
    after = {task_key(t) for t in proposed}
    missing = sorted(before - after)
    if missing:
        raise GuardrailViolation(
            f"检索结果删掉了既有任务 {missing} —— 历史流程只能帮助规划，"
            f"不能替代当前计划：知识层只许增加任务，不许删除")


def assert_no_fact_override(suggestions: list[dict]) -> None:
    """护栏 2：建议任务不得携带当前订单的事实字段。"""
    for task in suggestions:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        bad = sorted(ORDER_FACT_FIELDS & set(inputs))
        if bad:
            raise GuardrailViolation(
                f"建议任务 {task.get('title') or task.get('role')!r} 的 inputs 里带了"
                f"订单事实字段 {bad} —— 当前订单事实只能从 order_snapshot 读，"
                f"历史案例里的那一份是**别的单**的事实，不是这一单的")


def assert_no_dependency_removed(baseline: list[dict], proposed: list[dict]) -> None:
    """护栏 1 的另一半：依赖边只许加，不许删。

    补一步「财务核算」的同时把「付款依赖核算」这条边接上，是知识层**该**做的事；
    而把某条既有依赖边摘掉，等于让一个本来有前置的步骤提前跑 —— 那是删约束，
    与删任务同性质，同样禁止。
    """
    after = {task_key(t): t for t in proposed}
    for old in baseline:
        new = after.get(task_key(old))
        if new is None:
            continue
        lost = set(old.get("depends_on") or []) - set(new.get("depends_on") or [])
        if lost:
            raise GuardrailViolation(
                f"任务 {task_key(old)} 的依赖 {sorted(lost)} 被摘掉了 —— "
                f"知识层只许给 DAG 加约束，不许放松约束")


def assert_no_approval_skip(baseline: list[dict], proposed: list[dict]) -> None:
    """护栏 3：不得跳过或降低任何人工审批。"""
    for task in proposed:
        inputs = task.get("inputs") or {}
        if isinstance(inputs, dict):
            bad = sorted(APPROVAL_SKIP_KEYS & set(inputs))
            if bad:
                raise GuardrailViolation(
                    f"建议任务 {task.get('title') or task.get('role')!r} 带了免审批标记 "
                    f"{bad} —— 检索到「上次这类单直接放行了」不是这次可以省掉人的理由")

    after = {task_key(t): t for t in proposed}
    for old in baseline:
        new = after.get(task_key(old))
        if new is None:
            continue                       # 缺任务由护栏 1 判，这里不重复报
        for field in ("effect_risk", "risk_level"):
            was = RISK_ORDER.get(str(old.get(field) or "L").upper(), 0)
            now = RISK_ORDER.get(str(new.get(field) or "L").upper(), 0)
            if now < was:
                raise GuardrailViolation(
                    f"任务 {task_key(old)} 的 {field} 被从 {old.get(field)} 降到 "
                    f"{new.get(field)} —— 风险等级决定它要不要停在人工审批，"
                    f"知识层不许动它")


def check_all(baseline: list[dict], proposed: list[dict],
              suggestions: list[dict]) -> None:
    """三条护栏一次跑完。任何一条不过都抛，不返回「大体上还行」。"""
    assert_only_adds(baseline, proposed)
    assert_no_dependency_removed(baseline, proposed)
    assert_no_fact_override(suggestions)
    assert_no_approval_skip(baseline, proposed)


# ------------------------------------------------------------------ 建议合并
def collect_steps(docs: list[dict]) -> dict[tuple[str, str], dict]:
    """把命中文档里的步骤按 `task_key` 收成一张图。同一步以先命中的那份为准。

    分数高的文档排在前面，所以「先命中」= 相关度更高的那条知识说了算。
    """
    graph: dict[tuple[str, str], dict] = {}
    for doc in docs:
        for step in _steps_of(doc):
            if not step.get("role"):
                continue
            key = task_key(step)
            if key not in graph:
                graph[key] = {**step, "_doc": doc}
    return graph


def suggested_tasks_from_docs(docs: list[dict], baseline: list[dict]) -> list[dict]:
    """把命中的知识文档翻译成「当前 case 的建议任务」。

    文档 body 里存的是历史 DAG 的步骤清单（晋升时从真实 Plan 抽出来的，见
    `case_to_doc_body`）。翻译时**只取步骤本身**，参数从 baseline 借 ——
    历史案例告诉我们「该做哪一步」，做这一步用的数据必须是当前 case 的。
    这正是护栏 2 在实现上的样子：事实不从知识层来。
    """
    have = {task_key(t) for t in baseline}
    shared = _shared_inputs(baseline)
    out: list[dict] = []
    for key, step in collect_steps(docs).items():
        if key in have:
            continue                       # 当前计划里已经有这一步了
        task = {k: step[k] for k in SUGGESTION_KEYS if k in step}
        inputs = dict(task.get("inputs") or {})
        # 历史 inputs 里的事实字段一律丢掉，再用当前 case 的共享参数补齐。
        inputs = {k: v for k, v in inputs.items() if k not in ORDER_FACT_FIELDS}
        inputs.update(shared)
        task["inputs"] = inputs
        task["depends_on"] = []            # 真正的前置在 _rewire 里按步骤映射
        doc = step.get("_doc") or {}
        task["_kb_source"] = {"doc_id": doc.get("doc_id"), "score": doc.get("score"),
                              "title": doc.get("title")}
        task["_depends_keys"] = [tuple(k) for k in (step.get("depends_on_keys") or [])]
        out.append(task)
    return out


#: 往 inputs 深处找共享参数时的最深层数。设上限而不是无限下潜：inputs 是外部喂进来
#: 的 JSON，知识层不能假设上游收敛过形状（同 `runtime.gate.FINANCE_SCAN_MAX_DEPTH`
#: 的理由，数值也照它取 —— 两处扫的是同一片 inputs 树，深度分叉会出现「闸看得见的
#: 金额，规划期却取不到」，而那正是下面这段要修的症状本身）。
SHARED_SCAN_MAX_DEPTH = 4


def _nested_hits(node: Any, keys: frozenset[str], depth: int = 0):
    """在一份 inputs 里按**字段名**深搜这几个键，逐个 yield `(键, 值)`。

    **按字段名下潜，不按路径**（与 `runtime.gate._claimed_amounts` 同一把尺）。
    写死 `case_seed.amount_claimed` 这种嵌套路径，换个域、换个种子键名，这段当场
    变成死代码而且没有任何症状 —— 按字段名扫的话，顶层与嵌套只是同一个字段的两个
    位置，不是两套口径。

    命中的键**不再往下潜**：`amount_claimed` 的值本身是个 dict 时，那是「金额解析
    不出」这一档（闸按 `_over_finance_threshold` 收严成触发），不是「里面还藏着一个
    金额」。潜下去等于把一份自证不了的脏数据洗成一个干净数字，与闸的口径正好相反。
    """
    if depth > SHARED_SCAN_MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                yield key, value
            else:
                yield from _nested_hits(value, keys, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _nested_hits(value, keys, depth + 1)


def _shared_inputs(baseline: list[dict]) -> dict:
    """从既有任务里取当前 case 的共享参数（租户 / case / 业务域 / 申报金额）。

    `amount_claimed` 取自当前计划已有的任务，不是从历史文档抄的 —— 它是
    第六道财务复核闸的触发量，抄错一位数就是把闸绕过去。

    **顶层取不到就往载荷里找**（BACKLOG `## task-D2` 第 3 条）。只扫顶层时，上面那句
    话在最要紧的那个场景里是假的：漏排财务核算的计划里，当前 case 的申报金额只剩
    受理那一步的 `case_seed` 里那一份（这个形状**不许为了迁就判据去改**，见
    `kb.experiment._tasks`）。取不到之后并不是「建议任务没有金额」——
    `suggested_tasks_from_docs` 先抄历史 step 的 inputs、再用这里的结果覆盖，而
    `amount_claimed` 不在 `ORDER_FACT_FIELDS` 里（申报金额是客户诉求，不是订单事实），
    于是历史那一单的钱数原样留在了建议任务上，第六道闸按**别人的金额**判这一单。
    R5 两段用同一个 `AMOUNT` 常量，两个数碰巧相等，症状被完全掩盖。

    **两轮，顶层优先**。顶层是任务自己声明的参数，比任何载荷内部的同名键更权威；
    先扫完全部任务的顶层，行为与只扫顶层的旧版逐字节相同，第二轮只补第一轮一个都
    没取到的那些键。这条顺序也是误取的主要闸门：多源信号里混进别的 case 的同名键
    时，只要有任何一个任务在顶层声明过它，深搜就压根不会被叫到。
    """
    keys = ("tenant_id", "case_id", "biz_type", "channel_id", "amount_claimed")
    shared: dict[str, Any] = {}
    for task in baseline:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for key in keys:
            if key in inputs and key not in shared:
                shared[key] = inputs[key]

    missing = frozenset(k for k in keys if k not in shared)
    if not missing:
        return shared
    for task in baseline:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        for key, value in _nested_hits(inputs, missing):
            if key not in shared:
                shared[key] = value
    return shared


def _steps_of(doc: dict) -> list[dict]:
    """从文档 body 里读步骤清单。读不出来就当没有，不抛 —— 检索不阻塞。"""
    body = doc.get("body")
    if body is None and isinstance(doc.get("doc"), dict):
        body = doc["doc"].get("body")
    try:
        parsed = json.loads(body) if isinstance(body, str) else (body or {})
    except (TypeError, ValueError):
        return []
    steps = parsed.get("steps") if isinstance(parsed, dict) else None
    return [s for s in (steps or []) if isinstance(s, dict)]


def apply_suggestions(baseline: list[dict], docs: list[dict]) -> tuple[list[dict], list[dict]]:
    """把知识建议并进 DAG，护栏全过才返回。返回 `(新计划, 实际补上的任务)`。

    正例才参与规划：`failure_hint` 只用来提示「哪类组合需要额外步骤」，
    它本身不是可照做的流程（晋升规则的另一半在 `classify_case`）。

    补进来的任务会被**接进依赖图**：历史 DAG 里谁依赖这一步，当前 DAG 里的同一步
    就补上这条边。只补一步而不接边等于没补 —— 新任务与它的后继并行跑，
    后继照样在前置还没产出时就执行，症状与压根没补一模一样。
    """
    positives = [d for d in docs if (d.get("kind") or _kind_of(d)) in kb.POSITIVE_KINDS]
    graph = collect_steps(positives)
    suggestions = suggested_tasks_from_docs(positives, baseline)
    if not suggestions:
        return list(baseline), []
    proposed = _rewire(baseline, suggestions, graph)
    check_all(baseline, proposed, suggestions)
    return proposed, suggestions


def _rewire(baseline: list[dict], suggestions: list[dict],
            graph: dict[tuple[str, str], dict]) -> list[dict]:
    """把新任务插进 DAG 并接好依赖边。**只加边不删边**（护栏 1 的另一半）。

    只补一步而不接边等于没补：新任务与它的后继并行跑，后继照样在前置还没产出时
    就执行，症状与压根没补一模一样 —— 而两版 DAG 的任务清单看上去确实不同了，
    这是最容易骗过自己的一种「修好了」。

    新任务插在「第一个依赖它的既有任务」之前。位序不影响调度（派发看 depends_on），
    但影响人读 DAG 的观感，而这份 DAG 是要给评委看的。
    """
    merged = [dict(t) for t in baseline]
    # 先给所有任务定 id：依赖边是按 task_id 存的，边要在这一步接好，
    # 就不能等到调用方事后 setdefault —— 那时新任务的 id 还是 None，边接了个空。
    for task in merged:
        task.setdefault("task_id", new_id("task"))
    for task in suggestions:
        task.setdefault("task_id", new_id("task"))
    by_key = {task_key(t): t for t in merged}

    for new in suggestions:
        new_key = task_key(new)
        new["depends_on"] = [by_key[k]["task_id"] for k in new.pop("_depends_keys", [])
                             if k in by_key and by_key[k].get("task_id")]
        by_key[new_key] = new

    for new in suggestions:
        new_key = task_key(new)
        insert_at = len(merged)
        for idx, old in enumerate(merged):
            # 历史里谁依赖这一步，当前 DAG 里的同一步就补上这条边。
            # 当前 DAG 的 depends_on 存的是 task_id，跨不了两份计划，
            # 所以前置关系一律按**步骤**（task_key）算，再映射回本次的 task_id。
            hist = graph.get(task_key(old)) or {}
            if new_key not in {tuple(k) for k in (hist.get("depends_on_keys") or [])}:
                continue
            edge = new.get("task_id")
            if edge and edge not in (old.get("depends_on") or []):
                old["depends_on"] = list(old.get("depends_on") or []) + [edge]
            insert_at = min(insert_at, idx)
        merged.insert(insert_at, new)
    return merged


def _kind_of(doc: dict) -> str | None:
    inner = doc.get("doc")
    return inner.get("kind") if isinstance(inner, dict) else None


# ------------------------------------------------------------------ 晋升规则
def classify_case(*, observations: list[dict], notifications: list[dict],
                  case_row: dict | None) -> tuple[str, str] | None:
    """一个已收口的 case 该进哪一类知识层。不够格返回 None。

    · 证据完整且外部结果明确 -> `(history_case, success)`：有 payment_observation、
      终态观察是 settled、且有客户 ack。**三条缺一不可** —— 少了 ack 就不是
      「外部结果明确」，只是「我们这边做完了」。
    · 明确失败 / 已补偿 -> `(failure_hint, failed)`：只用于提示哪类组合需要
      额外步骤，不作为规划正例。
    · 其余（还没收口、观察不全、settled 却拿不出观察）-> None：不进知识层。
      **没结论的案例不是知识**，放进去就是拿半截事实去指导下一次规划；
      而 settled 却没有 settled 观察本身是权威事实边界被绕过的迹象，
      那种案例更不该被当成正例复制给下一单。
    """
    status = (case_row or {}).get("biz_status")
    settled_obs = [o for o in (observations or []) if o.get("observed_state") == "settled"]
    acked = [n for n in (notifications or []) if n.get("ack_at")]

    if status == "settled" and settled_obs and acked:
        return kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS
    if status in ("rejected", "compensated"):
        return kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED
    return None


def case_to_doc_body(tasks: list[dict], *, note: str = "") -> str:
    """把一条真实 Plan 的 DAG 压成文档 body（JSON）。

    存的是**步骤**，不是那一单的数据：事实字段在这里就被剔掉，
    而不是等到检索回来再靠护栏 2 拦 —— 脏数据不进库，比进库后拦得住更可靠。

    依赖也存成**步骤**（`depends_on_keys`）而不是 task_id：id 只在那一份计划里有意义，
    存进知识库就是一串指不到任何东西的字符串，下一次规划照着它接边只会接空。
    """
    id_to_key = {t["task_id"]: list(task_key(t)) for t in tasks if t.get("task_id")}
    steps = []
    for task in tasks:
        inputs = task.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        steps.append({
            "role": task.get("role"),
            "title": task.get("title"),
            "inputs": {k: v for k, v in inputs.items() if k not in ORDER_FACT_FIELDS},
            "acceptance": task.get("acceptance") or [],
            "risk_level": task.get("risk_level") or "L",
            "effect_risk": task.get("effect_risk") or "L",
            "depends_on_keys": [id_to_key[d] for d in (task.get("depends_on") or [])
                                if d in id_to_key],
        })
    return json.dumps({"steps": steps, "note": note}, ensure_ascii=False, sort_keys=True)
