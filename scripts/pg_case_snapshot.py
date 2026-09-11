#!/usr/bin/env python3
"""从 PostgreSQL 里捞出一案的业务对象实况 —— PG 束里「对象真在那些表里」的那一份证据。

    MAOS_PG_DSN=... python3 scripts/pg_case_snapshot.py \
        --tenant tnt-mfg-a --case RC-2026-0904-001 --plan plan_xxx
    python3 scripts/pg_case_snapshot.py --compare evidence/case-real-01/happy \
        evidence/case-real-01-pg/happy

## 这份文件回答的是哪句话

`evidence/case-real-01-pg/happy/` 那一束跑完，`run.log` 里每一步都绿，但读的人凭什么
相信**业务对象真的躺在 PolarDB 的表里**、而不是照旧落在 `maos.db`？那一束里的
`maos.db` 是控制面（`plan` / `task` / `artifact` / `event_log`），业务表在**另一个库**，
拿它当证据恰恰证不了这件事。

所以本脚本从 `MAOS_PG_DSN` 指的那个库里直接读三样东西，写成 `pg-tables.json`：

1. **行数**：退款域各表在本案 `tenant_id`（+ `case_id`，有这列的表）下有多少行；
2. **抽样**：十二类业务对象各抽一行（带版本号），足以对着 SQLite 束的
   `business-objects.json` 逐条核；
3. **引用解析**：本案 `business_ref` 的每一条走
   `objects.resolve_business_ref` 现解一次 —— 映射只有那一份，本脚本不另抄
   （抄一份的后果见那个函数的 docstring）。

## 铁律 6：DSN 里有口令

`MAOS_PG_DSN` 的名字**不匹配** `make_evidence.secret_values()` 的命名规则
（`api_key|secret|token|password|...`），所以它不会被自动纳入出口脱敏与哨兵反查 ——
这是本文件必须自己盯住的一条。两道都做：

· 本脚本产出的 `source` 段只留 `host` / `port` / `database` / `user`，**口令不进结构**；
· `pg_secrets()` 把整串 DSN 与口令段拆成两条哨兵交给调用方，塞进 `secrets` 字典之后
  既有的 `redact()` 出口替换与 `scan_for_secrets()` 字节反查就自动覆盖它们。

## 同构比对（`--compare`）

PG 束与 SQLite 束应当**语义同构**：同样 8/8 Skill、同样的事件链形状、同样的
`case_outcome` 四判据、同样的 `public_status`。`--compare` 把两束的这些字段逐条比，
分成「逐字节相同」与「必然不同」两栏输出。

**必然不同的那一栏不是豁免**，它是判据的一部分：`plan_id` / `trace_id` / 各种
`*_at` 时间戳 / `invocation_id` / `request_id` 每跑一次都新生成，两束字节相同反而
说明有一束不是真跑出来的。所以这里既不去抹平它们，也不把它们当噪声忽略 —— 列清楚，
让读的人自己看见「哪些必须一样、哪些必须不一样」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maos.domain import _dbport                                # noqa: E402
from maos.domain.refund import objects as refund_objects       # noqa: E402

#: 退款域落在 PG 上的全部表。`schema.sql` 的 16 张 + 三个 T 系列片段加的三张。
#: **写死一份清单是有意的**：少一张的症状不是报错，是行数段里悄悄缺一行，
#: 而读的人没法从「没列出来」分辨「这张表没建」还是「本脚本忘了问」。
#: 表建没建由本脚本现查 `information_schema`，查不到的照样列出来、标 `exists: false`。
DOMAIN_TABLES: tuple[str, ...] = (
    "refund_schema_version", "tenant", "channel", "order_snapshot",
    "product_snapshot", "policy_rule", "refund_case", "customer_evidence",
    "approval_record", "finance_entry", "refund_request", "payment_observation",
    "notification", "compensation_record", "business_ref", "intake_annotation",
    "case_outcome", "complaint", "failure_hint_index",
)

#: 抽样的对象类型 -> (表, 主键列)。**直接引用 objects 里那一份**，不在这里另抄。
REF_TARGETS = refund_objects._REF_TARGETS

#: **必然不同**的字段名片段：每跑一次都现生成，两束字节相同反而是异常
#: （说明有一束不是真跑出来的）。命中其一即归入 `differ_by_design` 栏，
#: 并由 `suspicious_identical` 盯着它们有没有反常地相同。
VOLATILE_KEYS = ("plan_id", "trace_id", "request_id", "invocation_id",
                 "_at", "basis", "wall_ms")

#: **不作判据**的字段名片段：内容摘要。相同与否都不携带「后端换了没有」的信息，
#: 所以既不判正也不判负，单列一栏。
#:
#: 这一栏是实跑逼出来的，不是先验分类：8 个 skill 里 6 个的 `input_digest` 两束
#: 逐字节相同（入参一样，摘要当然一样），另外两个不同 —— `finance.settle` 与
#: `payment.observe` 的入参里带着上一步的 `request_id`，而那个每跑都新。把整类
#: 塞进「必须相同」栏会让后两个恒判负，塞进「必然不同」栏会让前六个恒报可疑，
#: 两种都是拿一个与后端无关的量去当后端的判据。
DIGEST_KEYS = ("digest", "hash")


# --------------------------------------------------------------- DSN 与脱敏
def dsn_parts(dsn: str) -> dict:
    """把 DSN 拆成可以写进证据的那几段。**口令不在返回值里。**"""
    split = urlsplit(dsn)
    return {
        "host": split.hostname or "",
        "port": split.port or 5432,
        "database": (split.path or "/").lstrip("/"),
        "user": unquote(split.username or ""),
    }


def dsn_password(dsn: str) -> str:
    """DSN 里的口令段。只给哨兵用，**不许写进任何产物**。"""
    return unquote(urlsplit(dsn).password or "")


def pg_secrets(dsn: str) -> dict[str, str]:
    """交给 `make_evidence.redact()` / `scan_for_secrets()` 的两条哨兵。

    整串与口令段都给：只给整串的话，日志里出现的那种 `user:pass@host` 片段
    （psycopg 的报错常这么带）替换不到；只给口令段的话，整串里被 URL 编码过的
    那份替换不到。
    """
    out: dict[str, str] = {}
    if dsn:
        out["MAOS_PG_DSN"] = dsn
    password = dsn_password(dsn)
    if password:
        out["MAOS_PG_DSN_PASSWORD"] = password
    return out


# ------------------------------------------------------------------- PG 连接
def pg_store():
    """一条**业务域打了 postgres 标记**的 store。

    标记是装配级的（`_dbport.STORE_BACKEND_ATTR`），所以本脚本读 PG 不需要、也不该
    去动进程级的 `MAOS_DOMAIN_BACKEND` —— 动它会把同进程里别的一次性内存库一起
    拨到 PG，那正是 T126 要买掉的那个坑。

    底下那个 `SqliteStore(":memory:")` 只是个壳：postgres 分支根本不碰它的连接。
    """
    from maos.core.store import SqliteStore

    store = SqliteStore(":memory:")
    store.init_schema()
    _dbport.mark_store_backend(store, _dbport.POSTGRES)
    return store


def _columns_of(store, table: str) -> set[str]:
    rows = refund_objects.query(
        store,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = ?",
        (table,))
    return {str(r["column_name"]) for r in rows}


# ----------------------------------------------------------------- 三段内容
def row_counts(store, tenant_id: str, case_id: str) -> list[dict]:
    """各表在本案下的行数。

    收窄口径按表**现查列**决定，不写死：有 `case_id` 列的收到本案，只有 `tenant_id`
    的收到本租户，两者都没有的（`refund_schema_version`）报全表。写死的话，哪一轨
    往某张表加了 `case_id` 列，这里就会一直按租户报 —— 数字看着对，含义已经变了。
    """
    out: list[dict] = []
    for table in DOMAIN_TABLES:
        cols = _columns_of(store, table)
        if not cols:
            out.append({"table": table, "exists": False, "scope": None, "rows": None})
            continue
        if "case_id" in cols:
            scope, sql, params = ("tenant+case",
                                  f"SELECT count(*) AS n FROM {table}"
                                  " WHERE tenant_id=? AND case_id=?",
                                  (tenant_id, case_id))
        elif "tenant_id" in cols:
            scope, sql, params = ("tenant",
                                  f"SELECT count(*) AS n FROM {table} WHERE tenant_id=?",
                                  (tenant_id,))
        else:
            scope, sql, params = ("all", f"SELECT count(*) AS n FROM {table}", ())
        rows = refund_objects.query(store, sql, params)
        out.append({"table": table, "exists": True, "scope": scope,
                    "rows": int(rows[0]["n"]) if rows else 0})
    return out


def sample_objects(store, tenant_id: str, case_id: str) -> list[dict]:
    """十二类业务对象各抽一行（带版本号）。

    抽的是**本案能定位到的那一行**：有 `case_id` 列的按本案收窄，没有的（订单 /
    商品 / 规则快照那几张）按租户收窄再取第一行 —— 它们本来就是跨案共享的快照。
    抽不到就如实写 `row: null` 并给 `why`，不假装有数据（铁律 3）。
    """
    out: list[dict] = []
    for object_type, (table, key) in sorted(REF_TARGETS.items()):
        cols = _columns_of(store, table)
        if not cols:
            out.append({"object_type": object_type, "table": table, "row": None,
                        "why": "表不存在"})
            continue
        where, params = ("tenant_id=?", [tenant_id])
        if "case_id" in cols:
            where, params = ("tenant_id=? AND case_id=?", [tenant_id, case_id])
        order = " ORDER BY version DESC" if "version" in cols else ""
        rows = refund_objects.query(
            store, f"SELECT * FROM {table} WHERE {where}{order} LIMIT 1", tuple(params))
        if not rows:
            out.append({"object_type": object_type, "table": table, "row": None,
                        "why": f"{table} 在 {where} 下没有行"})
            continue
        row = rows[0]
        out.append({
            "object_type": object_type, "table": table,
            "key_column": key, "key_value": row.get(key),
            "version": row.get("version"),
            "row": {k: _plain(v) for k, v in row.items()},
        })
    return out


def business_refs(store, tenant_id: str, case_id: str, plan_id: str | None) -> dict:
    """本案的 `business_ref` 逐条 + 现解一次。

    解析走 `objects.resolve_business_ref` —— `object_type -> 表/主键` 的映射在那里
    有唯一一份，本脚本不另立（口径同 `make_evidence.collect_business_objects`）。

    **这张表没有 `case_id` 列**，它按 `(plan_id, task_id)` 组织 —— 引用记的是
    「哪个 Task 引用了哪个业务对象」，而 Task 挂在 Plan 上。所以本案的收窄走
    `plan_id`；没给 plan_id 时退回按租户列全部，并在 note 里说清这一点，免得
    读的人把别的案子的引用算到本案头上。
    """
    where, params = "tenant_id=?", [tenant_id]
    if plan_id:
        where += " AND plan_id=?"
        params.append(plan_id)
    rows = refund_objects.query(
        store,
        f"SELECT * FROM business_ref WHERE {where}"
        " ORDER BY plan_id, task_id, object_type, object_id", tuple(params))
    out: list[dict] = []
    for raw in rows:
        ref = {k: _plain(v) for k, v in raw.items()}
        try:
            target = refund_objects.resolve_business_ref(store, raw)
        except Exception as exc:                               # noqa: BLE001
            ref["resolved"] = False
            ref["resolve_error"] = f"{type(exc).__name__}: {exc}"
            out.append(ref)
            continue
        ref["resolved"] = target is not None
        ref["object"] = {k: _plain(v) for k, v in (target or {}).items()} or None
        out.append(ref)
    return {
        "refs": out,
        "resolved": sum(1 for o in out if o.get("resolved")),
        "dangling": sum(1 for o in out if not o.get("resolved")),
        "object_types": sorted({str(o.get("object_type")) for o in out}),
        "scope": f"tenant_id={tenant_id}" + (f" AND plan_id={plan_id}" if plan_id
                                             else "（未给 plan_id，列的是本租户全部）"),
        "note": ("resolve 走 maos.domain.refund.objects.resolve_business_ref，"
                 "本脚本不另立映射；这些行是从 PG 读的，不是从 maos.db。"
                 "business_ref 没有 case_id 列，本案的收窄走 plan_id。"),
    }


def _plain(value):
    """JSON 放得下的形状。psycopg 会把 numeric / timestamptz 还成 Python 对象。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --------------------------------------------------------------------- 快照
def snapshot(*, tenant_id: str, case_id: str, plan_id: str | None = None,
             dsn: str | None = None) -> dict:
    """`pg-tables.json` 的全部内容。"""
    dsn = os.environ.get("MAOS_PG_DSN", "") if dsn is None else dsn
    if not dsn:
        raise RuntimeError(
            "MAOS_PG_DSN 未配置：PG 快照没有库可读。DSN 只从环境变量读（铁律 6）。")
    store = pg_store()
    counts = row_counts(store, tenant_id, case_id)
    return {
        "source": {
            "backend": "postgres",
            **dsn_parts(dsn),
            "read_from": "MAOS_PG_DSN",
            "note": ("下面每一行都是从 MAOS_PG_DSN 指的那个 PostgreSQL 库里读出来的，"
                     "不是从束里的 maos.db（那是控制面：plan / task / artifact / "
                     "event_log）。DSN 的口令段不在本文件的任何字段里，也不在日志里"
                     "（铁律 6）。"),
        },
        "case": {"tenant_id": tenant_id, "case_id": case_id, "plan_id": plan_id},
        "row_counts": counts,
        "tables_present": sum(1 for c in counts if c["exists"]),
        "tables_expected": len(DOMAIN_TABLES),
        "rows_for_case": sum(c["rows"] or 0 for c in counts if c["scope"] == "tenant+case"),
        "objects": sample_objects(store, tenant_id, case_id),
        "business_refs": business_refs(store, tenant_id, case_id, plan_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------- 同构比对
def _read_bundle_json(directory: str, name: str):
    """读回证据束里的一份 json（跳过首行出处注释）。"""
    from scripts.make_evidence import load_evidence_json

    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        return None
    return load_evidence_json(path)


def _is_volatile(key: str) -> bool:
    return any(frag in key for frag in VOLATILE_KEYS)


def _is_digest(key: str) -> bool:
    return any(frag in key for frag in DIGEST_KEYS)


def _compare_mapping(left, right, prefix: str, same: list, differ: list, bad: list,
                     skipped: list) -> None:
    """逐键比，dict 与 list 都往下钻。`VOLATILE_KEYS` 命中的归 differ 栏，其余不等即判负。

    **list 必须下钻**，不能整体比：`outcome.payment_observations` 是一串观察行，里面
    `request_id` / `observed_at` / `actor_invocation_id` 每跑一次都是新的，而
    `observed_state` / `gateway_code` 两跑必须一字不差。整体比会把这一串判成「不同」，
    于是「同一条案子在两个后端上观察到的是同一个网关终态」这句话就核不出来了 ——
    真正该核的那两个字段被埋在噪声里。
    """
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            bad.append({"field": f"{prefix}[len]", "sqlite": len(left), "pg": len(right)})
            return
        same.append(f"{prefix}[len]")
        for i, (lv, rv) in enumerate(zip(left, right)):
            _compare_mapping(lv, rv, f"{prefix}[{i}]", same, differ, bad, skipped)
        return
    if not isinstance(left, dict) or not isinstance(right, dict):
        if left != right:
            bad.append({"field": prefix, "sqlite": _plain(left), "pg": _plain(right)})
        else:
            same.append(prefix)
        return
    for key in sorted(set(left) | set(right)):
        field = f"{prefix}.{key}" if prefix else key
        lv, rv = left.get(key), right.get(key)
        if _is_digest(key):
            skipped.append({"field": field, "identical": lv == rv})
            continue
        if _is_volatile(key):
            differ.append({"field": field, "sqlite": _plain(lv), "pg": _plain(rv),
                           "both_present": lv is not None and rv is not None,
                           "differs": lv != rv})
            continue
        if isinstance(lv, (dict, list)) or isinstance(rv, (dict, list)):
            _compare_mapping(lv, rv, field, same, differ, bad, skipped)
            continue
        if lv == rv:
            same.append(field)
        else:
            bad.append({"field": field, "sqlite": _plain(lv), "pg": _plain(rv)})


def compare(sqlite_dir: str, pg_dir: str) -> dict:
    """两束的语义同构比对。返回 `isomorphism.json` 的内容。"""
    same: list[str] = []
    differ: list[dict] = []
    bad: list[dict] = []
    skipped: list[dict] = []

    s_out, p_out = (_read_bundle_json(d, "outcome.json") for d in (sqlite_dir, pg_dir))
    if s_out is None or p_out is None:
        raise RuntimeError(f"两束都要有 outcome.json：{sqlite_dir} / {pg_dir}")
    # note 是给人看的说明文字，两边逐字相同但不是判据 —— 比它只会让 same 栏变长。
    _compare_mapping({k: v for k, v in s_out.items() if k != "note"},
                     {k: v for k, v in p_out.items() if k != "note"},
                     "outcome", same, differ, bad, skipped)

    # 8 个 Skill **逐条**比，不只比 present/total 那两个数：8/8 对上而清单里换了一个
    # skill，两个数照样是 8/8 —— 那正是「同样 8/8 Skill」这句话想排除的情况。
    s_sk, p_sk = (_read_bundle_json(d, "skills.json") for d in (sqlite_dir, pg_dir))
    if s_sk and p_sk:
        for key in ("present", "total", "expect_compensation",
                    "contract_skills", "compensation_skills"):
            if key in s_sk or key in p_sk:
                _compare_mapping({key: s_sk.get(key)}, {key: p_sk.get(key)},
                                 "skills", same, differ, bad, skipped)

    s_ch, p_ch = (_read_bundle_json(d, "event-chain.json") for d in (sqlite_dir, pg_dir))
    if s_ch and p_ch:
        _compare_mapping({"counts": s_ch.get("counts"),
                          "missing_required": s_ch.get("missing_required")},
                         {"counts": p_ch.get("counts"),
                          "missing_required": p_ch.get("missing_required")},
                         "event_chain", same, differ, bad, skipped)

    # 「必然不同」的那一栏里，两边都有值却**字节相同**的，本身就是一条异常：
    # plan_id / 时间戳每跑一次都该是新的，一样说明有一束不是真跑出来的。
    suspicious = [d for d in differ if d["both_present"] and not d["differs"]]
    return {
        "sqlite_bundle": os.path.relpath(sqlite_dir, ROOT),
        "pg_bundle": os.path.relpath(pg_dir, ROOT),
        "verdict": "isomorphic" if not bad else "divergent",
        "identical_fields": sorted(same),
        "identical_count": len(same),
        "differ_by_design": differ,
        "mismatches": bad,
        "suspicious_identical": suspicious,
        "not_compared": skipped,
        "note": ("三栏各有各的含义。identical_fields：两束**必须**逐字节相同 —— 8 个 "
                 "Skill 的名字/present/status/version/task_id、事件链各类型计数、"
                 "case_outcome 四判据、biz_status、public_status、观察行的 "
                 "observed_state 与 gateway_code。differ_by_design：每跑一次都现生成的"
                 "（plan_id / trace_id / request_id / invocation_id / *_at），**字节相同"
                 "反而是异常**，那种情况列进 suspicious_identical。not_compared："
                 "input_digest / output_hash 这类内容摘要，相同与否都不携带「后端换了"
                 "没有」的信息，所以不判正也不判负（实测：8 个 skill 里 6 个的 "
                 "input_digest 两束相同、2 个不同，两种都正常）。"
                 "**两边的行为没有为了对齐字节而改过任何一处。**"),
    }


# ----------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pg_case_snapshot",
        description="从 PG 捞一案的业务对象实况；或比对 PG 束与 SQLite 束是否语义同构")
    parser.add_argument("--tenant", help="租户 id")
    parser.add_argument("--case", help="案号")
    parser.add_argument("--plan", default=None, help="本次的 plan_id（收窄 business_ref）")
    parser.add_argument("--out", default=None, help="写到这个文件，缺省打到 stdout")
    parser.add_argument("--compare", nargs=2, metavar=("SQLITE_DIR", "PG_DIR"),
                        help="比对两束是否语义同构")
    args = parser.parse_args(argv)

    if args.compare:
        doc = compare(*args.compare)
    else:
        if not args.tenant or not args.case:
            parser.error("要么给 --compare，要么给 --tenant 与 --case")
        doc = snapshot(tenant_id=args.tenant, case_id=args.case, plan_id=args.plan)

    body = json.dumps(doc, ensure_ascii=False, indent=2)
    # 独立跑时也要过一遍口令反查：DSN 的名字不匹配 make_evidence 的命名规则，
    # 那两道自动机制在这条路径上够不着（束内的那份由 make_case_bundle 走 write_json）。
    password = dsn_password(os.environ.get("MAOS_PG_DSN", ""))
    if password and password in body:
        raise RuntimeError("产物里出现了 DSN 口令，已中止（铁律 6）")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(f"# generated at {datetime.now(timezone.utc).isoformat()}"
                     f" from {os.environ.get('MAOS_EVIDENCE_SHA', 'unknown')}\n")
            fh.write(body + "\n")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
