"""自动晋升 —— 只有证据完整且外部结果明确的案例进默认知识层。

评委第三条的后半段是这个模块存在的理由：

    只有**证据完整且外部结果明确**的案例进入默认知识层，失败实例则用于提示
    **哪类渠道、支付返回或政策组合**需要额外步骤。

规则本身早就写好了（`guardrails.classify_case`），缺的是**谁来调它**：在此之前
它只在 R5 对照实验里被手动调过一次（`experiment.promote_history_case`，那个
docstring 自己就写着「自动晋升调度器不在本轮范围内，已记 BACKLOG」）。于是
「自动晋升」是一句 PPT 上的话，库里没有对应的执行点。本模块把它接在 Plan 的终态上。

## 两条产出，各管一半

· **正例** -> `kb_doc(history_case, success)`：`business_success && evidence_complete`
  才写。两个条件缺一不可 —— 前者答「这单业务成了没有」，后者答「这单的证据全不全」。
  只看前者会把一条查不到订单快照的成功案例当成范本复制给下一单；只看后者会把
  一条材料齐全但钱压根没到账的案子当成正例。
· **失败** -> `kb_doc(failure_hint, failed)` **加** `failure_hint_index` 一行聚合。

聚合表与单条实例不是一份数据的两处副本（见 schema 片段里的注释）：Planner 要的是
「这类**组合**普遍缺哪一步」，而不是「上一单是怎么栽的」。键就是评委原话里的三个词
—— 渠道 × 支付返回码 × 政策规则号（契约 §G）。

## extra_steps 不是编出来的

`failure_hint_index.extra_steps` 有两个来源，都是库里/表里现成的事实：

1. 网关码表 `maos/tools/gateway_codes.py` 里那个码的 **remedy 原文**（官方文档照抄）。
2. 这一单真开出来的人工工单 `compensation_record` 里的 `todo` 清单。

不从模型来、也不由本模块现编 —— 「哪类组合需要额外步骤」如果是编的，Planner 照着
它排出来的任务就是编的，而那正是知识层最该防的一件事（铁律 3 在知识层的样子）。

## 语料导入也走同一份口径

`classify_corpus_row()` 是给**外部导入的历史语料**用的分流口径，与
`classify_case()` 同一个判据面：前者管「外部导入的历史该进哪一类」，后者管
「本库跑出来的案子该进哪一类」，两边都在问「外部结果明不明确」。
装载器（`domain/refund/fixtures.seed_history_kb`）当前把这条规则**自己抄了一遍**
（`history_case if outcome == success else failure_hint`），抄的那份现在与这里一致，
但两份口径迟早分叉 —— 那正是 T118 那 8 条标错 kind 的失败案例混进正例的机理。
接线（把装载器改成调本函数）留在整合期：`fixtures.py` 是 T115 的白名单面，
本轨不越界改它，已记 BACKLOG。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

from maos import kb
from maos.kb import guardrails

log = logging.getLogger("maos.kb.promotion")


def _refund():
    """退款域的三个模块，**用到才拖**。返回 `(guard, objects, outcome_mod)`。

    `maos/kb/**` 是领域无关的检索内核（铁律 9）：模块级 `from maos.domain.refund
    import ...` 会让 `import maos.kb.promotion` 顺带把整个退款域拉进来，于是
    「换个业务域不必改内核」这句话一个 `grep` 就能证伪。口径与
    `plan_advice._ticket_role()`、`experiment.py` 那一串局部 import 同一条
    （整合期 p10-e 定的，原文在 `docs/DECISIONS.md`）：

        **取值可以局部 import + 兜底，断言不行。**

    本模块取的全是值（读域的表、算域的四判据），没有一处拿域的存在与否当断言。
    域不在时的落点也分两档，**不是一律吞掉**：
    · `promote_plan` / `list_failure_hints` 经 `_has_table()` 退化成空列表 ——
      它们挂在**通用**的 Plan 终态钩子上，软件域那几个场景照样会走到。
    · `promote_case` 让 ImportError 原样上抛 —— 域不在还直接点名要晋升某个退款
      case，那是调用方的错，吞掉只会让知识层静默地少一条。
    """
    from maos.domain.refund import guard, objects, outcome as outcome_mod
    return guard, objects, outcome_mod


def _objects():
    """只要 `objects`（退款域的通用 SQL 访问器）那一个时的简写。见 `_refund()`。"""
    return _refund()[1]


#: `event_log.event_type` 字符串（契约 §F）。同 `CaseOutcomeComputed`，
#: 走 `append_event_log` 的字符串类型，不碰 `maos/contracts/events.py`（铁律 1）。
EVENT_CASE_PROMOTED = "CasePromoted"

#: 退款域的业务类型标记，与 `maos/skills/builtin/refund/_common.py::BIZ_TYPE` 同值。
#: 这里写字面量而不 import 那个模块：`promotion` 挂在通用的 Plan 终态钩子上，
#: import 一个 skill 包会把整棵 skill 注册树拉进来（`fixtures._experiment()` 同一口径）。
BIZ_TYPE = "refund"

#: 观察不到网关码时 `failure_hint_index.gateway_code` 的占位。
#: **不是空串**：空串在主键里与「真的返回了空码」分不开，而这两件事差得很远 ——
#: 轮询到顶问不出终态（本占位）与网关明确回了一个空码，处置方式相反。
GATEWAY_CODE_UNOBSERVED = "unobserved"

#: 命中不到政策规则号时的占位。`*` 与政策表里的 `channel_scope='*'` 同一个通配约定。
RULE_NO_ANY = "*"


# ------------------------------------------------------------------ 语料导入口径
def classify_corpus_row(row: Mapping[str, Any]) -> tuple[str, str]:
    """外部导入的一条历史语料该进哪一类。返回 `(kind, outcome)`。

    判据与 `classify_case` 同一面：**外部结果明不明确**。语料行没有观察表可查，
    它自报的 `outcome` 就是它能提供的全部外部结果，所以：

    · `outcome == 'success'` -> `(history_case, success)`
    · 其余（含缺失、拼错、写成别的词）-> `(failure_hint, failed)`

    缺失与拼错**一律落到失败侧**，不抛：语料是外部来的，一条写坏的行不该让整批
    导入失败；而把它当正例放进默认知识层，等于让一条来路不明的记录去指导下一次规划。
    向失败侧倒是这里唯一安全的失败方向。
    """
    if str(row.get("outcome") or "") == kb.OUTCOME_SUCCESS:
        return kb.KIND_HISTORY_CASE, kb.OUTCOME_SUCCESS
    return kb.KIND_FAILURE_HINT, kb.OUTCOME_FAILED


# ------------------------------------------------------------------ 失败聚合表
def bump_failure_hint(store: Any, *, tenant_id: str, channel_id: str,
                      gateway_code: str, rule_no: str,
                      extra_steps: Sequence[str]) -> dict:
    """给「渠道 × 返回码 × 规则号」这个组合记上一次，并合并它需要的额外步骤。

    `count` 累加、`extra_steps` 取并集（保序去重）：同一个组合栽第二次时，
    第二次带来的新步骤要留下，已有的不该被覆盖掉 —— 覆盖的症状是
    「越聚合知道得越少」，而那与这张表的目的正好相反。
    """
    _, objects, outcome_mod = _refund()
    outcome_mod.ensure_outcome_schema(store)
    rows = objects.query(
        store,
        "SELECT * FROM failure_hint_index WHERE tenant_id=? AND channel_id=?"
        " AND gateway_code=? AND rule_no=?",
        (tenant_id, channel_id, gateway_code, rule_no))
    existing = rows[0] if rows else None

    merged = list(_loads(existing["extra_steps"], []) if existing else [])
    for step in extra_steps:
        text = str(step).strip()
        if text and text not in merged:
            merged.append(text)

    count = int(existing["count"]) + 1 if existing else 1
    objects.execute(
        store,
        "INSERT INTO failure_hint_index (tenant_id, channel_id, gateway_code, rule_no,"
        " extra_steps, count, updated_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT (tenant_id, channel_id, gateway_code, rule_no) DO UPDATE SET"
        " extra_steps=excluded.extra_steps, count=excluded.count,"
        " updated_at=excluded.updated_at",
        (tenant_id, channel_id, gateway_code, rule_no,
         json.dumps(merged, ensure_ascii=False), count, objects._now()),
    )
    return {"tenant_id": tenant_id, "channel_id": channel_id,
            "gateway_code": gateway_code, "rule_no": rule_no,
            "extra_steps": merged, "count": count}


def list_failure_hints(store: Any, *, tenant_id: str | None = None) -> list[dict]:
    """读聚合表。T119 在 Wave B 消费这个函数。表不在就返回空，不抛。"""
    if not _has_table(store, "failure_hint_index"):
        return []                                  # 退款域缺席也落这一支，见 `_has_table`
    objects = _objects()
    sql = "SELECT * FROM failure_hint_index"
    params: tuple = ()
    if tenant_id:
        sql += " WHERE tenant_id=?"
        params = (tenant_id,)
    sql += " ORDER BY count DESC, channel_id, gateway_code, rule_no"
    rows = objects.query(store, sql, params)
    for row in rows:
        row["extra_steps"] = _loads(row["extra_steps"], [])
    return rows


# ------------------------------------------------------------------ 晋升主链
def promote_plan(store: Any, *, plan_id: str) -> list[dict]:
    """把这个 Plan 上的每个退款 case 各晋升一次。返回逐 case 的结果。

    退款域没落地就返回空 —— 本函数挂在**通用**的 Plan 终态钩子上，软件域那几个
    场景一样会走到这里，不能因为它们没有 `refund_case` 表就抛。**两种「没落地」
    都算**：表不在（域在、库里还没建表），以及 `maos.domain.refund` 整个装不出来
    （换业务域的部署）。两条都由 `_has_table()` 收，见那里的注释与 `_refund()`。
    """
    if not _has_table(store, "refund_case"):
        return []                                  # 退款域缺席也落这一支，见 `_has_table`
    objects = _objects()
    cases = objects.query(
        store, "SELECT * FROM refund_case WHERE plan_id=? ORDER BY created_at", (plan_id,))
    results = []
    for case in cases:
        results.append(promote_case(store, tenant_id=case["tenant_id"],
                                    case_id=case["case_id"], plan_id=plan_id))
    return results


def promote_case(store: Any, *, tenant_id: str, case_id: str, plan_id: str) -> dict:
    """一个 case：算四判据 -> 分类 -> 落 kb_doc（+ 失败则记聚合）-> 落事件。

    返回 `{"case_id", "outcome", "verdict", "doc_id", "hint"}`，`verdict` 为 None
    表示**不进知识层**（还没收口、观察不全）—— 那不是失败，是「没结论」。

    退款域装不出来时 `_refund()` 的 ImportError **原样上抛**（与 `promote_plan`
    的软降级相反）：点名要晋升某个退款 case 却没有退款域，是调用方的错。
    """
    guard, objects, outcome_mod = _refund()
    row = outcome_mod.record_case_outcome(
        store, tenant_id=tenant_id, case_id=case_id, plan_id=plan_id)
    case = guard.get_case(store, tenant_id, case_id)
    observations = objects.query(
        store, "SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?"
               " ORDER BY observed_at", (tenant_id, case_id))
    notifications = objects.query(
        store, "SELECT * FROM notification WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id))

    verdict = guardrails.classify_case(
        observations=observations, notifications=notifications,
        case_row=case, outcome=row)

    result: dict[str, Any] = {"tenant_id": tenant_id, "case_id": case_id,
                              "outcome": row, "verdict": verdict,
                              "doc_id": None, "hint": None}
    if verdict is None:
        log.info("[%s] 不进知识层：到账=%s 证据完整=%s —— 没结论的案例不是知识",
                 case_id, row.get("arrival"), bool(row.get("evidence_complete")))
        return result

    doc_kind, doc_outcome = verdict
    doc = _write_doc(store, case=case or {}, plan_id=plan_id, kind=doc_kind,
                     doc_outcome=doc_outcome, observations=observations, row=row)
    result["doc_id"] = doc["doc_id"]

    if doc_kind == kb.KIND_FAILURE_HINT:
        result["hint"] = bump_failure_hint(
            store, tenant_id=tenant_id,
            channel_id=str((case or {}).get("channel_id") or ""),
            gateway_code=_gateway_code_of(store, plan_id, observations),
            rule_no=_rule_no_of(store, tenant_id, case_id),
            extra_steps=_extra_steps_of(store, plan_id, tenant_id, case_id, observations))

    store.append_event_log({
        "plan_id": plan_id,
        "event_type": EVENT_CASE_PROMOTED,
        "reason": f"晋升为 {doc_kind}/{doc_outcome}（业务成功={bool(row.get('business_success'))}，"
                  f"证据完整={bool(row.get('evidence_complete'))}）",
        "detail": {"tenant_id": tenant_id, "case_id": case_id,
                   "kind": doc_kind, "outcome": doc_outcome, "doc_id": doc["doc_id"],
                   "business_success": bool(row.get("business_success")),
                   "evidence_complete": bool(row.get("evidence_complete")),
                   "arrival": row.get("arrival"),
                   "failure_hint": result["hint"]},
    })
    return result


# ------------------------------------------------------------------ 文档装配
def _write_doc(store: Any, *, case: Mapping[str, Any], plan_id: str, kind: str,
               doc_outcome: str, observations: Sequence[Mapping[str, Any]],
               row: Mapping[str, Any]) -> dict:
    """把这一趟的真实 DAG 压成一条 kb_doc。

    body 走 `guardrails.case_to_doc_body`，与 R5 手动晋升那条路**同一个函数**：
    事实字段在那里就被剔掉了（护栏 2 的写入侧落点），不是等检索回来再拦。
    """
    from maos.kb.retriever import embed        # 局部 import：嵌入实现不该被本模块的
                                               # 导入图绑死（同 fixtures._experiment()）
    tenant_id = str(case.get("tenant_id") or "")
    case_id = str(case.get("case_id") or "")
    tasks = store.list_tasks(plan_id)
    note = _note_of(kind, row, observations)
    doc_id = f"auto-{kind}-{case_id}"

    title = (f"{case.get('reason_code') or '退款'}：{_headline(kind, row)}")
    doc = {
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "biz_type": BIZ_TYPE,
        "channel_id": case.get("channel_id"),
        "region": _region_of(store, tenant_id),
        "sku": case.get("sku"),
        "policy_version": _policy_version_of(store, case),
        "workflow_version": 1,
        "rule_no": _rule_no_of(store, tenant_id, case_id),
        "gateway_code": (_gateway_code_of(store, plan_id, observations)
                         if kind == kb.KIND_FAILURE_HINT else None),
        "kind": kind,
        "outcome": doc_outcome,
        "source_case_id": case_id,
        "title": title,
        "body": guardrails.case_to_doc_body(tasks, note=note),
    }
    doc["embedding"] = embed(f"{title} {case.get('sku') or ''} {doc['rule_no']}")
    return kb.upsert_doc(store, doc)


def _headline(kind: str, row: Mapping[str, Any]) -> str:
    """标题里那半句话。**四判据是什么就写什么，不许往上凑**。

    正例里也分两档：客户确认了的和没确认的。把后者也写成「经客户确认」是这一整轨
    要拆掉的那种表述 —— 标题是检索结果里最先被读到的一行，它自称的事实必须和
    `case_outcome` 那一行对得上，否则知识层自己就成了第二份说法。
    """
    if kind == kb.KIND_HISTORY_CASE:
        return ("到账并经客户确认的完整路径"
                if row.get("customer_confirmation") == "confirmed"
                else "到账的完整路径（客户尚未确认）")
    return f"未收口（到账={row.get('arrival')}，人工纠错={row.get('manual_correction')}）"


def _note_of(kind: str, row: Mapping[str, Any],
             observations: Sequence[Mapping[str, Any]]) -> str:
    """写进 body 的那句说明。四判据原样带上，**包括 arrival_basis**。

    带 basis 是刻意的：这条知识将来被检索到时，读它的人（或 Planner）要能顺着
    basis 回到那一行观察去核。一条说「这单到账了」却指不回观察行的知识，
    与它想要取代的那种自述没有区别。
    """
    head = ("到账并经客户确认" if kind == kb.KIND_HISTORY_CASE
            else "未走到「到账 + 客户确认」，只作提示不作规划正例")
    return (f"{head}。四判据：到账={row.get('arrival')}"
            f"（依据 {row.get('arrival_basis') or '无观察行'}）、"
            f"客户确认={row.get('customer_confirmation')}、"
            f"人工纠错={row.get('manual_correction')}、"
            f"投诉={row.get('complaint')}；"
            f"证据完整={bool(row.get('evidence_complete'))}，"
            f"观察行 {len(observations)} 条。")


# ------------------------------------------------------------------ 维度取值
def _region_of(store: Any, tenant_id: str) -> str | None:
    objects = _objects()
    rows = objects.query(store, "SELECT region FROM tenant WHERE tenant_id=?", (tenant_id,))
    return rows[0]["region"] if rows else None


def _policy_version_of(store: Any, case: Mapping[str, Any]) -> int | None:
    """本单锁定的政策版本 —— 取**下单当时**那一版，不是 policy_rule 的最新版。

    口径唯一一份在 `objects.pinned_policy_version`，这里直接调它。取不到就 None：
    知识文档少一维会少被召回，而抄一个「当前最新版」进去会让它在错误的场景被召回。
    """
    objects = _objects()
    try:
        return objects.pinned_policy_version(
            store, tenant_id=str(case.get("tenant_id") or ""),
            order_id=str(case.get("order_id") or ""),
            order_version=int(case.get("order_version") or 0))
    except (LookupError, ValueError, TypeError):
        return None


def _rule_no_of(store: Any, tenant_id: str, case_id: str) -> str:
    """本单实际命中的政策规则号。取财务核算落下的 `rule_refs` 第一条。

    从 `finance_entry` 取而不是重跑一次 `policy_rules_at_order`：核算那一步落库的
    才是**这一单当时真的按哪条规则算的**，重跑一次得到的是「现在按哪条算」，
    在政策改过版之后两者会不一样，而这条知识说的是当时那一单。
    """
    objects = _objects()
    rows = objects.query(
        store, "SELECT rule_refs FROM finance_entry WHERE tenant_id=? AND case_id=?",
        (tenant_id, case_id))
    refs = _loads(rows[0]["rule_refs"], []) if rows else []
    return str(refs[0]) if refs else RULE_NO_ANY


def _gateway_code_of(store: Any, plan_id: str,
                     observations: Sequence[Mapping[str, Any]]) -> str:
    """这一单的支付返回码。观察行优先，其次闸判出来的 finding，再没有就是未观察到。

    两个来源缺一不可：观察行只在**问出终态**时才有（场景 7 轮询到顶那条路径下
    这张表是空的），而闸的 finding 在网关一返回错误码时就产出了。只认前者的话，
    最典型的一类失败（网关返回可重试错误、最终问不出下落）在聚合表里恒为
    `unobserved`，「哪类支付返回需要额外步骤」这句话当场落空。
    """
    for obs in reversed(list(observations)):
        code = str(obs.get("gateway_code") or "")
        if code:
            return code
    for finding in _gateway_findings(store, plan_id):
        code = str(finding.get("code") or "")
        if code:
            return code
    return GATEWAY_CODE_UNOBSERVED


def _gateway_findings(store: Any, plan_id: str) -> list[dict]:
    """第七道闸在这个 Plan 上产出的网关 finding，按任务顺序。"""
    out: list[dict] = []
    for task in store.list_tasks(plan_id):
        for finding in task.get("findings") or []:
            if isinstance(finding, dict) and finding.get("gate") == "gateway":
                out.append(finding)
    return out


def _extra_steps_of(store: Any, plan_id: str, tenant_id: str, case_id: str,
                    observations: Sequence[Mapping[str, Any]]) -> list[str]:
    """这类组合需要哪些额外步骤。**两个来源都是现成事实，本函数不编第三种。**

    1. 官方码表的 `remedy` 原文（`gateway_codes.lookup`）—— 支付宝文档怎么说就怎么记。
    2. 这一单真开出来的人工工单里的 `todo`（`compensation_record.detail_json`）。

    一条都取不到时返回空列表，聚合行照记（`count` 仍然有意义：这个组合栽过几次
    本身就是信息）。宁可空着也不填一句「请人工核查」—— 那种话对 Planner 没有信息量，
    却会让这张表看起来是满的。
    """
    from maos.tools.gateway_codes import lookup

    objects = _objects()
    steps: list[str] = []
    for finding in _gateway_findings(store, plan_id):
        remedy = str(finding.get("remedy") or "")
        if not remedy:
            code = str(finding.get("code") or "")
            try:
                remedy = lookup(code).remedy if code else ""
            except KeyError:
                remedy = ""
        if remedy:
            steps.append(f"网关 {finding.get('code')}：{remedy}")

    for obs in observations:
        code = str(obs.get("gateway_code") or "")
        if not code:
            continue
        try:
            entry = lookup(code)
        except KeyError:
            continue
        if entry.outcome != "success":
            steps.append(f"网关 {code}：{entry.remedy}")

    for comp in objects.query(
            store, "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=?"
                   " ORDER BY executed_at", (tenant_id, case_id)):
        detail = _loads(comp["detail_json"], {})
        for todo in (detail.get("todo") or []) if isinstance(detail, dict) else []:
            steps.append(str(todo))

    return list(dict.fromkeys(s for s in steps if s.strip()))


# ------------------------------------------------------------------ 小工具
def _has_table(store: Any, name: str) -> bool:
    """这张域表在不在。**「退款域整个装不出来」也从这里回 False**。

    `_objects()` 的局部 import 特意放在 try **里面**：换业务域的部署上
    `maos.domain.refund` 压根不存在，那时 ImportError 与「表还没建」是同一件事
    —— 本函数的两个调用方（`promote_plan` / `list_failure_hints`）都挂在通用的
    Plan 终态钩子上，两种情况都该退化成空列表，而不是把一条跑完的 Plan 掀翻。
    """
    try:
        rows = _objects().query(
            store, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    except Exception:                                  # noqa: BLE001 —— 探针不该炸
        return False
    return bool(rows)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
