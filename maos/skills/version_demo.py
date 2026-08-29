"""Skill 多版本发布 / 回滚独立入口 —— 把「机制有、单测有」演成「链路上真用过」。

    python3 -m maos.skills.version_demo

零出网、不读任何 API key、不落盘（内存库）。演砸了非 0 退出。

## 为什么是独立入口，而不是给 run.py 加一个场景

`run.py` 与 `maos/flows/**` 不在本轨手里；更要紧的是**跨轨契约 1**：既有七个场景
的输出与 `evidence/` 的证据束必须逐字节不变。而 `policy.match` 的两个调用点
（`maos/agents/refund/policy_agent.py:40`、`finance_agent.py:48`）都不钉版本，
`registry.get()` 缺省又返回最高版本 —— 新版本一旦进正常 import 路径，那两处会静默
升版，落库那行 `SkillInvoked` 的 `detail.version` 从 `"1.0.0"` 变成 `"1.1.0"`，
`evidence/scenario-*/trace.json` 里那几十处版本号跟着变。

所以 v1.1.0 由**本入口按需 import**（`policy_v1_1.py` 的 docstring 记了同一件事）。
这不是权宜：它恰恰让第一件事演得出来 —— 开场时 `versions("policy.match")` 还是
`['1.0.0']`，import 之后才变成两版，「投放即注册」是当场发生的，不是启动前就发生的。

## 演的四件事（对应 `docs/skill-catalog.md` 的「版本 / 发布 / 回滚 / 质量评估」四节）

1. **发布** —— 投放一个模块就注册，两个 `__init__.py` 一个字没改（当场读文件断言）。
2. **取版** —— `get()` 缺省拿最高版本，按段数值序（`_semver_key`）。
3. **回滚** —— `get(name, "1.0.0")` 拿到的**就是**当年那一个类，跑同一份输入，
   结论与升版前一模一样。这是整段演示的题眼：旧版本从不被覆盖。
4. **质量评估** —— 两版各调一次，`event_log` 落 `SkillInvoked`，按 `skill + version`
   聚合出成功率与耗时，无需另建埋点。

## 靶场数据为什么自带一份

不 import `maos/flows/scenario_6.py`：那个文件不在本轨手里，从它身上取常量等于给
本入口接一条随时会被别人改动的隐性依赖（`hiclaw/room_demo.py` 同一处理）。
这里的订单**支付于 92 天前**，而 `AS-01` 声明了 30 天的申请时效窗口 ——
两版结论因此分叉：v1.0.0 不读这个字段，照样 approve；v1.1.0 判超窗，reject。
第二组数据（`AS-02`，不声明窗口）演的是另一半：**版本升级没有误伤存量口径**，
两版逐字节同结论 —— 既有七个场景的规则全属这一档。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.skills import registry
from maos.skills.invoker import SkillInvoker

SKILL = "policy.match"
V_OLD = "1.0.0"
V_NEW = "1.1.0"

#: 本演示的靶场。全部写死 —— 连跑两次结论必须一致，随机值会让「可复现」无从谈起。
TENANT_ID = "tnt-demo-ver"
CHANNEL_ID = "ch-demo"
SKU = "SKU-BRG-6204"
PLAN_ID = "plan-skill-version-demo"

#: 支付于 2026-05-01，申请于 2026-08-01 —— 相隔 92 天。
PAID_AT = "2026-05-01T10:00:00+00:00"
AS_OF = "2026-08-01T10:00:00+00:00"

#: 两条案子对照着演：一条撞上时效窗口（两版分叉），一条没有（两版同结论）。
CASE_WINDOWED = "case-ver-0001"      # 命中 AS-01，窗口 30 天，已超窗
CASE_LEGACY = "case-ver-0002"        # 命中 AS-02，不声明窗口 —— 存量口径

ORDER_WINDOWED = "ord-ver-0001"
ORDER_LEGACY = "ord-ver-0002"
ORDER_VERSION = 1
AMOUNT = 6800.00

POLICY_RULES = [
    # rule_no, version, title, body(机器可读参数), sku_scope
    ("AS-01", 1, "整机质量问题全额退款（自支付起 30 日内提出）",
     {"refund_ratio": 1.0, "deduct_fee": 0, "window_days": 30}, SKU),
    ("AS-02", 1, "质保期内维修不满意退款（存量口径，不设申请时效）",
     {"refund_ratio": 0.8, "deduct_fee": 50}, "SKU-LEGACY"),
]

EXIT_OK = 0
EXIT_FAIL = 1

#: 「投放即注册」这条断言要读的两个清单文件。两个都不许出现新模块的名字。
NEW_MODULE = "policy_v1_1"
INIT_FILES = (
    "maos/skills/builtin/__init__.py",
    "maos/skills/builtin/refund/__init__.py",
)
ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------
def head(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\n【{n}】{title}\n{'=' * 72}")


def line(label: str, value: object) -> None:
    print(f"  {label:<22}{value}")


class Checks:
    """演示自己的判据。每条当场打印通过与否，全过才 exit 0。"""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def ok(self, cond: bool, what: str) -> bool:
        print(f"  {'[OK]  ' if cond else '[FAIL]'} {what}")
        if not cond:
            self.failed.append(what)
        return cond


# --------------------------------------------------------------------------
# 靶场
# --------------------------------------------------------------------------
def seed(store) -> None:
    """预置外部系统快照与政策 —— 是**读到的那一版**，不是外部系统的当前值（铁律 8）。"""
    objects.ensure_schema(store)
    objects.execute(store, "INSERT OR REPLACE INTO tenant (tenant_id, name, region)"
                           " VALUES (?,?,?)", (TENANT_ID, "版本演示租户", "CN-EAST"))
    objects.execute(store, "INSERT OR REPLACE INTO channel (tenant_id, channel_id, kind, name)"
                           " VALUES (?,?,?,?)", (TENANT_ID, CHANNEL_ID, "marketplace", "演示店"))
    for sku in (SKU, "SKU-LEGACY"):
        objects.execute(
            store,
            "INSERT OR REPLACE INTO product_snapshot (tenant_id, sku, version, name, category,"
            " warranty_months, payload_json) VALUES (?,?,?,?,?,?,?)",
            (TENANT_ID, sku, 1, "深沟球轴承 6204", "bearing", 12, "{}"))
    for order_id, sku in ((ORDER_WINDOWED, SKU), (ORDER_LEGACY, "SKU-LEGACY")):
        objects.execute(
            store,
            "INSERT OR REPLACE INTO order_snapshot (tenant_id, order_id, version, sku,"
            " amount_paid, paid_at, channel_id, policy_version_at_order, payload_json, read_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (TENANT_ID, order_id, ORDER_VERSION, sku, AMOUNT, PAID_AT, CHANNEL_ID,
             1, "{}", PAID_AT))
    for rule_no, version, title, params, sku_scope in POLICY_RULES:
        objects.execute(
            store,
            "INSERT OR REPLACE INTO policy_rule (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, channel_scope, sku_scope) VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT_ID, rule_no, version, title,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             "2026-01-01T00:00:00+00:00", None, "*", sku_scope))
    for case_id, order_id, sku in ((CASE_WINDOWED, ORDER_WINDOWED, SKU),
                                   (CASE_LEGACY, ORDER_LEGACY, "SKU-LEGACY")):
        guard.create_case(
            store, tenant_id=TENANT_ID, case_id=case_id, channel_id=CHANNEL_ID,
            order_id=order_id, order_version=ORDER_VERSION, sku=sku,
            reason_code="quality_defect", amount_claimed=AMOUNT, plan_id=PLAN_ID,
            actor_skill="demo.seed", invocation_id="demo-seed-0001")


def payload_of(case_id: str) -> dict:
    """申请时点显式钉死 —— 不取建案时刻（那是 now()，会让演示随日子漂）。"""
    return {"tenant_id": TENANT_ID, "case_id": case_id, "as_of": AS_OF}


def call(invoker: SkillInvoker, case_id: str, version: str | None, task_id: str):
    return invoker.invoke(SKILL, payload_of(case_id), version=version,
                          extras={"plan_id": PLAN_ID, "task_id": task_id})


def verdict(res) -> str:
    if res.status != "ok" or not isinstance(res.output, dict):
        return f"<{res.status}: {res.error}>"
    return f"{res.output['decision']}　{res.output['reason']}"


# --------------------------------------------------------------------------
# 四件事
# --------------------------------------------------------------------------
def act_publish(ck: Checks) -> None:
    head(1, "发布 —— 投放一个模块就注册，两个 __init__.py 一个字没改")

    before = registry.versions(SKILL)
    line("import 之前", before)
    ck.ok(before == [V_OLD], f"开场只有一个版本 {[V_OLD]}")

    # 「投放」在这里发生：import 一个模块，@register_skill 在 import 期入表。
    from maos.skills.builtin.refund import policy_v1_1  # noqa: F401

    after = registry.versions(SKILL)
    line("import 之后", after)
    ck.ok(after == [V_OLD, V_NEW], f"投放后两版共存 {[V_OLD, V_NEW]}")

    for rel in INIT_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        ck.ok(NEW_MODULE not in src, f"{rel} 里没有 {NEW_MODULE} —— 清单一个字没改")


def act_select(ck: Checks) -> None:
    head(2, "取版 —— 缺省拿最高版本，按段数值序而非字符串序")

    default_cls = registry.get(SKILL)
    line("get(skill)", f"{default_cls.__name__} @ {default_cls.contract.version}")
    ck.ok(default_cls.contract.version == V_NEW, f"缺省取到最高版本 {V_NEW}")

    line("_semver_key('1.10.0')", registry._semver_key("1.10.0"))
    line("_semver_key('1.9.0')", registry._semver_key("1.9.0"))
    ck.ok(registry._semver_key("1.10.0") > registry._semver_key("1.9.0"),
          "1.10.0 > 1.9.0（字符串序会判反）")


def act_rollback(ck: Checks, invoker: SkillInvoker) -> dict:
    head(3, "回滚 —— 旧版本从不被覆盖，按版本取拿到的就是当年那一个")

    from maos.skills.builtin.refund.policy import PolicyMatchSkill

    old_cls = registry.get(SKILL, V_OLD)
    ck.ok(old_cls is PolicyMatchSkill, "get(skill, '1.0.0') 拿到的就是 v1.0.0 那个类本身")

    old_c, new_c = old_cls.contract, registry.get(SKILL, V_NEW).contract
    changed = [f for f in ("purpose", "input_schema", "output_schema", "preconditions",
                           "security_boundary", "reuse_note")
               if getattr(old_c, f) != getattr(new_c, f)]
    line("九要素里变了的", "、".join(changed))
    ck.ok("input_schema" in changed and "output_schema" in changed,
          "两版契约确有差异，不是只改了版本号")

    print("\n  ── 同一份输入（订单支付于 92 天前，AS-01 声明 30 天申请时效）──")
    new_res = call(invoker, CASE_WINDOWED, None, "t-new")
    old_res = call(invoker, CASE_WINDOWED, V_OLD, "t-old")
    line("默认版本 1.1.0", verdict(new_res))
    line("回滚到 1.0.0", verdict(old_res))
    ck.ok(new_res.output["decision"] == "reject", "v1.1.0 判超窗，不予受理")
    ck.ok(old_res.output["decision"] == "approve", "v1.0.0 不读 window_days，照旧受理")
    ck.ok(new_res.output != old_res.output, "两版对同一份输入结论确有差异")

    print("\n  ── 存量口径的输入（AS-02 不声明窗口）——升级不许误伤 ──")
    legacy_new = call(invoker, CASE_LEGACY, None, "t-legacy-new")
    legacy_old = call(invoker, CASE_LEGACY, V_OLD, "t-legacy-old")
    line("默认版本 1.1.0", verdict(legacy_new))
    line("回滚到 1.0.0", verdict(legacy_old))
    ck.ok(_business_equal(legacy_new.output, legacy_old.output),
          "存量输入下两版输出逐字段相同（跨轨契约 1 的依据）")

    return {"new": new_res, "old": old_res}


def _business_equal(a: dict, b: dict) -> bool:
    """比业务字段。invocation_id 每次调用都新生成，不属于结论的一部分。"""
    drop = {"invocation_id"}
    return {k: v for k, v in a.items() if k not in drop} == \
           {k: v for k, v in b.items() if k not in drop}


def act_quality(ck: Checks, store) -> None:
    head(4, "质量评估 —— 按 skill + version 聚合 event_log，无需另建埋点")

    rows = [r for r in store.list_event_log(PLAN_ID) if r["event_type"] == "SkillInvoked"]
    line("SkillInvoked 行数", len(rows))
    ck.ok(len(rows) >= 2, "两版调用各自落了审计行")

    want = {"skill", "version", "status", "duration_ms",
            "input_digest", "output_hash", "usage", "invocation_id"}
    ck.ok(all(want <= set(r["detail"]) for r in rows), f"每行 detail 字段齐：{sorted(want)}")

    sample = rows[0]["detail"]
    line("detail 抽样", f"skill={sample['skill']} version={sample['version']} "
                        f"status={sample['status']} duration_ms={sample['duration_ms']}")
    line("input_digest", sample["input_digest"][:16] + "…")
    line("output_hash", sample["output_hash"][:16] + "…")

    agg: dict[tuple, dict] = {}
    for r in rows:
        d = r["detail"]
        a = agg.setdefault((d["skill"], d["version"]), {"n": 0, "ok": 0, "ms": 0})
        a["n"] += 1
        a["ok"] += 1 if d["status"] == "ok" else 0
        a["ms"] += d["duration_ms"]

    print(f"\n  {'skill':<16}{'version':<10}{'次数':<7}{'成功':<7}{'成功率':<10}平均耗时")
    print(f"  {'-' * 62}")
    for (skill, version), a in sorted(agg.items()):
        print(f"  {skill:<16}{version:<10}{a['n']:<8}{a['ok']:<8}"
              f"{a['ok'] / a['n']:<11.1%}{a['ms'] / a['n']:.1f} ms")

    ck.ok({v for _, v in agg} == {V_OLD, V_NEW}, "聚合里两个版本分得开，不混成一行")


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python3 -m maos.skills.version_demo",
        description="Skill 多版本发布 / 取版 / 回滚 / 质量评估的独立演示（零出网，无需 key）",
    ).parse_args(argv)

    # 基线：builtin 装载完毕（`versions()` 刻意不带动态发现，registry.py:75）。
    # 此刻在册的 policy.match 只有 1.0.0 —— 第 1 件事就是从这个状态出发的。
    import maos.skills.builtin  # noqa: F401
    from maos.agents.refund.policy_agent import RefundPolicyAgent

    store = SqliteStore()
    store.init_schema()
    seed(store)
    invoker = SkillInvoker(RefundPolicyAgent.identity, store)

    ck = Checks()
    act_publish(ck)
    act_select(ck)
    act_rollback(ck, invoker)
    act_quality(ck, store)

    head(5, "小结")
    if ck.failed:
        for what in ck.failed:
            print(f"  [FAIL] {what}")
        print(f"\n演示未通过：{len(ck.failed)} 条判据不成立。")
        return EXIT_FAIL
    print("  发布 / 取版 / 回滚 / 质量评估 四件事全部当场演出，判据全绿。")
    print(f"  在册版本：{SKILL} -> {registry.versions(SKILL)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
