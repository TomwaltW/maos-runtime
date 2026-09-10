#!/usr/bin/env python3
"""批量语料生成器 —— 底账、两张申请表、一份分类评测集。

## 为什么要有这个目录

`scenarios/custom/` 那份底账只有 6 张订单、3 条政策，够演剧情，不够看**分布**：
金额全在一条阈值的同一侧、诉求类型各一两单、词表命中率恒等于 100%（因为那几行
本来就是照着词表写的）。想知道「换成一天几十单真进件会怎么样」，得有量。

这里造的东西**一个字都不碰 `scenarios/custom/`**：`run_requests.py` 有 `--ledger`，
所以另起一份底账就够了，老三单那条逐字节比对的判据（`test_room_team_fixture.py`）
完全不受影响。

## 产出四份

| 文件 | 喂给谁 | 判据 |
| :-- | :-- | :-- |
| `ledger-bulk.json` | `run_requests.py --ledger` | 装载器列集合严格，多一列少一列当场抛 |
| `refund-requests-bulk.csv` | `run_requests.py` | 诉求类型写标准词，**60 单应当全部跑完** |
| `refund-requests-raw.csv` | 同上 | 诉求类型写**客户原话**，考的是分类器 |
| `reason-eval.csv` | `eval_reason.py` | 每行自带期望 code，能算准确率 |

第二张表是这批数据里唯一「会失败」的那份，也是唯一有信息量的那份：没配 key 时
词表外的行全部挑去人工，配了 key 才看得出模型兜底判得准不准。**跑不满 60 单是
预期结果，不是 bug** —— 判据在 `maos/ingress/classify.py` 的「判不准 ≠ 填错了」。

## 确定性

订单号、金额、日期全部由 `random.Random(SEED)` 现算，同一颗种子跑两次逐字节一致。
客户原话不参与随机：它们逐条手写在 `lexicon_corpus.py` 里，随机只决定谁配哪一单。

    python3 scenarios/bulk/generate.py            # 写四份文件
    python3 scenarios/bulk/generate.py --check    # 只校验已落盘的四份还对不对
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from scenarios.bulk.lexicon_corpus import (  # noqa: E402
    AMBIGUOUS, GENUINELY_UNKNOWN, LEXICON_HITS, REAL_SAYINGS,
)

SEED = 20260910
TENANT = "tnt-demo"
UTC = timezone.utc

#: 有聊天截图的那 12 单。**这里是唯一事实源**：`gen_chat.py` import 它并断言自己的
#: `CONVERSATIONS` 覆盖的订单号与之逐个相等 —— 改一处漏一处的症状是图配不上单，
#: 而 `--evidence` 对配不上的文件只报一行就跳过，不报错，演示时全场都判 missing。
CHAT_ORDERS = (
    "ORD-2026-1000", "ORD-2026-1003", "ORD-2026-1007", "ORD-2026-1011",
    "ORD-2026-1016", "ORD-2026-1022", "ORD-2026-1029", "ORD-2026-1034",
    "ORD-2026-1041", "ORD-2026-1047", "ORD-2026-1053", "ORD-2026-1058",
)

#: 「今天」。写死而不是取 `datetime.now()` —— 取当下会让同一份语料明天再跑就换一批
#: 日期，而申请表里「第 55 天申请」这类剧情是靠日期差演出来的。
TODAY = datetime(2026, 9, 10, tzinfo=UTC)

#: 财务复核阈值，`maos/runtime/gate.py::DEFAULT_FINANCE_THRESHOLD`。这里只是拿它
#: **摆金额分布**：让六成单子落在线上、四成落在线下，结果表的「转人工」列才有两种值。
FINANCE_THRESHOLD = 5000.0

# --------------------------------------------------------------------- 商品
#: `(sku, 名称, 类目, 质保月数, 单价区间)`。质保月数分 6/12/24/36 四档 ——
#: 质量问题的裁定要看在不在保，一档到底就演不出「出保」那一半。
PRODUCTS = [
    ("SKU-BRG-6204", "深沟球轴承 6204", "bearing", 12, (28, 96)),
    ("SKU-BRG-6308", "深沟球轴承 6308", "bearing", 12, (85, 240)),
    ("SKU-FLG-DN80", "碳钢平焊法兰 DN80", "flange", 24, (62, 130)),
    ("SKU-FLG-DN150", "不锈钢对焊法兰 DN150", "flange", 24, (210, 460)),
    ("SKU-GSK-DN80", "缠绕垫片 DN80", "gasket", 6, (5, 14)),
    ("SKU-ORING-45", "氟橡胶 O 型圈 45mm", "seal", 6, (2, 9)),
    ("SKU-SEAL-90", "骨架油封 90mm", "seal", 12, (95, 180)),
    ("SKU-CPL-ML95", "梅花弹性联轴器 ML95", "coupling", 12, (320, 780)),
    ("SKU-RDC-NMRV63", "蜗轮减速机 NMRV63", "reducer", 24, (860, 1680)),
    ("SKU-MTR-Y2-90L", "三相异步电机 Y2-90L", "motor", 24, (620, 1450)),
    ("SKU-SNS-PT100", "铂电阻温度传感器 PT100", "sensor", 12, (86, 260)),
    ("SKU-CYL-SC63", "标准气缸 SC63x200", "cylinder", 36, (180, 420)),
]

# --------------------------------------------------------------------- 客户
#: 多客户是为了让**重复退款风险**有分布：全挂在一个客户名下，风险面每一单都判 high。
CUSTOMERS = [
    ("CUS-2026-0042", "杭州锐驰机电有限公司"),
    ("CUS-2026-0057", "宁波恒信传动设备有限公司"),
    ("CUS-2026-0061", "苏州华工自动化科技有限公司"),
    ("CUS-2026-0073", "台州百晟液压件厂"),
    ("CUS-2026-0088", "无锡中远流体设备有限公司"),
    ("CUS-2026-0094", "常州精锐工业装备有限公司"),
]

CHANNELS = [
    ("ch-online", "marketplace", "官方自营旗舰店"),
    ("ch-tmall", "marketplace", "天猫企业购旗舰店"),
    ("ch-dealer", "dealer", "华东区一级代理"),
]

CARRIERS = ["顺丰速运", "德邦物流", "中通快运", "京东物流", "跨越速运"]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ----------------------------------------------------------------- 政策规则
def policy_rules() -> list[dict]:
    """五条规则、两个版本。

    AS-001 从 30 天收到 15 天（v2，2026-06-01 生效）是**故意加的**：订单锁的是
    下单当时那一版，所以 2026-06 之后下单的单子按 15 天判、之前的仍按 30 天判。
    这是「政策版本锁在订单上」这句话唯一能被看见的地方 —— 两版参数一样的话，
    改没改版本跑出来一模一样，等于没演。
    """
    def body(**kw) -> str:
        return json.dumps(kw, ensure_ascii=False, sort_keys=True)

    common = {"effective_to": None, "channel_scope": "*", "sku_scope": "*"}
    return [
        {"tenant_id": TENANT, "rule_no": "AS-001", "version": 1,
         "title": "无理由退货期（30 天）",
         "body": body(applies_when={"reason_code": ["no_reason_return"]},
                      deduct_fee="0", no_reason_days=30, refund_ratio="1",
                      rule_kind="no_reason_return"),
         "effective_from": "2025-01-01T00:00:00+00:00", **common},
        {"tenant_id": TENANT, "rule_no": "AS-001", "version": 2,
         "title": "无理由退货期收窄至 15 天并扣 2% 手续费",
         "body": body(applies_when={"reason_code": ["no_reason_return"]},
                      deduct_fee="0", no_reason_days=15, refund_ratio="0.98",
                      rule_kind="no_reason_return"),
         "effective_from": "2026-06-01T00:00:00+00:00", **common},
        {"tenant_id": TENANT, "rule_no": "AS-002", "version": 1,
         "title": "质保期内质量问题全额退",
         "body": body(applies_when={"reason_code": ["quality_defect"]},
                      deduct_fee="0", refund_ratio="1",
                      rule_kind="quality_within_warranty",
                      warranty_basis="product_snapshot.warranty_months"),
         "effective_from": "2025-01-01T00:00:00+00:00", **common},
        {"tenant_id": TENANT, "rule_no": "AS-002", "version": 2,
         "title": "质保期内质量问题全额退（含上门检测费）",
         "body": body(applies_when={"reason_code": ["quality_defect"]},
                      deduct_fee="0", refund_ratio="1",
                      rule_kind="quality_within_warranty",
                      warranty_basis="product_snapshot.warranty_months"),
         "effective_from": "2026-06-01T00:00:00+00:00", **common},
        {"tenant_id": TENANT, "rule_no": "AS-003", "version": 1,
         "title": "发错货全额退并免手续费",
         "body": body(applies_when={"reason_code": ["wrong_item"]},
                      deduct_fee="0", refund_ratio="1", rule_kind="wrong_item"),
         "effective_from": "2025-01-01T00:00:00+00:00", **common},
    ]


# --------------------------------------------------------------------- 订单
ORDER_COUNT = 60


def build_orders(rng: random.Random) -> list[dict]:
    """60 张订单。列集合与 `scenarios/custom/ledger.json` 逐字相同（十列，严格）。

    金额按 `FINANCE_THRESHOLD` 两侧摆：四成落在 5000 以下（闸过就走），
    六成在 5000 以上（过了闸还要人放行）。扩展信息一律进 `payload_json` 字符串，
    **不加兄弟列** —— 那十列是严格列集合，多一个键装载时当场抛。
    """
    orders: list[dict] = []
    for i in range(ORDER_COUNT):
        sku, _name, _cat, _warranty, (lo, hi) = PRODUCTS[i % len(PRODUCTS)]
        cus_id, _cus_name = CUSTOMERS[i % len(CUSTOMERS)]
        ch_id, _kind, _ch_name = CHANNELS[i % len(CHANNELS)]

        # 下单时刻铺在 2025-10 ~ 2026-09：跨过 AS-001 v2 的 2026-06-01 生效点，
        # 两侧各有单子，才看得出「锁在订单上的政策版本」到底锁住了什么。
        days_ago = rng.randint(3, 330)
        paid_at = TODAY - timedelta(days=days_ago, hours=rng.randint(0, 23))

        qty = rng.choice([1, 2, 4, 5, 8, 10, 12, 20, 24, 40, 50, 60, 100, 120, 200])
        unit = round(rng.uniform(lo, hi), 2)
        amount = round(qty * unit, 2)
        # 四成压到阈值以下、六成抬到阈值以上，两侧都要有量。
        if i % 5 in (0, 1) and amount >= FINANCE_THRESHOLD:
            amount = round(rng.uniform(320, FINANCE_THRESHOLD - 60), 2)
        elif i % 5 not in (0, 1) and amount < FINANCE_THRESHOLD:
            amount = round(rng.uniform(FINANCE_THRESHOLD + 120, 156000), 2)

        policy_version = 2 if paid_at >= datetime(2026, 6, 1, tzinfo=UTC) else 1

        payload: dict = {"customer_id": cus_id, "qty": qty, "unit_price": unit}
        # 七成的单子有物流签收记录；没有签收记录的那三成是「还在路上就要退」的样子。
        if rng.random() < 0.7:
            signed = paid_at + timedelta(days=rng.randint(2, 9))
            payload["logistics"] = {
                "carrier": rng.choice(CARRIERS),
                "signed_at": _iso(signed),
                "tracking_no": f"SF-{rng.randint(1000, 9999)}-"
                               f"{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
            }
        # 三成有质检报告，其中一部分判 pass —— 与「质量问题」对不上，
        # 证据核验岗才有 missing / 冲突可判（`scenarios/custom/evidence/README.md`）。
        if rng.random() < 0.3:
            issued = paid_at + timedelta(days=rng.randint(5, 20))
            payload["qc_report"] = {
                "report_no": f"QC-{issued:%Y-%m%d}-{rng.randint(1, 99):03d}",
                "result": rng.choice(["defect", "defect", "pass"]),
                "issued_at": _iso(issued),
            }

        orders.append({
            "tenant_id": TENANT,
            "order_id": f"ORD-2026-{1000 + i}",
            "version": 1,
            "sku": sku,
            "amount_paid": amount,
            "paid_at": _iso(paid_at),
            "channel_id": ch_id,
            "policy_version_at_order": policy_version,
            "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "read_at": _iso(TODAY),
        })
    return orders


def build_history(rng: random.Random, orders: list[dict]) -> list[dict]:
    """历史退款记录。**只挂在四个客户身上**，另外两个干净。

    风险面按 `customer_id` 聚合历史，全员都有历史等于风险维度恒为 high，
    收口卡永远给「升级审批」，那一列就不再有信息。
    """
    rows: list[dict] = []
    dirty = [c[0] for c in CUSTOMERS[:4]]
    for i, order in enumerate(orders):
        payload = json.loads(order["payload_json"])
        cus = payload["customer_id"]
        if cus not in dirty or rng.random() > 0.35:
            continue
        status = rng.choice(["settled", "settled", "pending", "rejected"])
        decided = datetime.fromisoformat(order["paid_at"]) - timedelta(
            days=rng.randint(20, 200))
        rows.append({
            "tenant_id": TENANT,
            "case_id": f"RC-{order['order_id']}-H{len(rows) + 1}",
            "order_id": order["order_id"],
            "customer_id": cus,
            "amount": 0.0 if status == "rejected" else round(
                order["amount_paid"] * rng.uniform(0.2, 0.9), 2),
            "status": status,
            "decided_at": _iso(decided),
        })
    return rows


def build_ledger(rng: random.Random) -> dict:
    orders = build_orders(rng)
    return {
        "_note": "批量演示底账 —— 由 scenarios/bulk/generate.py 生成，勿手改。"
                 "改数据请改生成器再重跑，手改的那一行下次重跑就没了。",
        "_provenance": {
            "generated_by": "scenarios/bulk/generate.py",
            "seed": SEED,
            "today": _iso(TODAY),
            "orders": len(orders),
        },
        "_rule_no_scope": f"本文件的 AS-00x 编号只在租户 {TENANT} 内有意义 ——"
                          "跨语料引用规则号前先看租户。",
        "gateway": {"settle_after": 2, "fail_with": None},
        # 登记表里的每一段都必须是 **list**，哪怕只有一行：`_checked_rows` 见到 dict
        # 会报「语料里没有 'tenant' 这一段」—— 报的是段名，不是类型，照字面找会找错方向。
        "tenant": [{"tenant_id": TENANT, "name": "示例精密制造", "region": "cn-hangzhou"}],
        "channel": [{"tenant_id": TENANT, "channel_id": cid, "kind": kind, "name": name}
                    for cid, kind, name in CHANNELS],
        "product_snapshot": [
            {"tenant_id": TENANT, "sku": sku, "version": 1, "name": name,
             "category": cat, "warranty_months": warranty,
             "payload_json": json.dumps({"unit_price_range": list(rng_range)},
                                        ensure_ascii=False)}
            for sku, name, cat, warranty, rng_range in PRODUCTS
        ],
        "order_snapshot": orders,
        "policy_rule": policy_rules(),
        # 以下两个是**未知顶层键**：`seed_case` 静默忽略，圆桌的风险面自己读。
        "customer": [{"tenant_id": TENANT, "customer_id": cid, "name": name}
                     for cid, name in CUSTOMERS],
        "refund_history": build_history(rng, orders),
    }


# ----------------------------------------------------------------- 申请表
REASON_LABEL = {
    "quality_defect": "质量问题",
    "no_reason_return": "七天无理由",
    "wrong_item": "发错货",
}


def build_sheets(rng: random.Random, ledger: dict) -> tuple[list[dict], list[dict]]:
    """两张申请表，**同一批单子、同一批原话**，只有「诉求类型」那一格写法不同。

    这是本目录的对照实验：标准词那张全跑完，原话那张跑不满 —— 差出来的行数
    就是词表当前的盲区。两张表不共用一行代码之外的任何东西，行序一致，可以逐行对着看。
    """
    orders = ledger["order_snapshot"]
    by_code: dict[str, list[tuple[str, str, str]]] = {}
    for text, code, why in REAL_SAYINGS:
        by_code.setdefault(code, []).append((text, code, why))
    for pool in by_code.values():
        rng.shuffle(pool)

    # 诉求类型分布：质量 25 / 无理由 20 / 发错货 10 / 判不准 5。
    plan = (["quality_defect"] * 25 + ["no_reason_return"] * 20
            + ["wrong_item"] * 10 + ["__unknown__"] * 5)
    rng.shuffle(plan)

    unknown_pool = list(GENUINELY_UNKNOWN)
    rng.shuffle(unknown_pool)
    cursor = {k: 0 for k in by_code}
    std_rows: list[dict] = []
    raw_rows: list[dict] = []

    for order, code in zip(orders, plan):
        paid_at = datetime.fromisoformat(order["paid_at"])
        if code == "__unknown__":
            saying, why = unknown_pool[len(std_rows) % len(unknown_pool)]
            std_code, label = "quality_defect", "质量问题"
        else:
            pool = by_code[code]
            saying, _code, why = pool[cursor[code] % len(pool)]
            cursor[code] += 1
            std_code, label = code, REASON_LABEL[code]

        # 申请日期：无理由那批**故意一半超窗**（v1 是 30 天、v2 收到 15 天），
        # 驳回那条路要有单子走，否则整张表跑出来全是 approve，看不出政策在起作用。
        if std_code == "no_reason_return":
            gap = rng.choice([3, 6, 9, 12, 18, 22, 26, 34, 41, 55, 68])
        else:
            gap = rng.randint(4, 140)
        requested = min(paid_at + timedelta(days=gap), TODAY)

        # 三成留空（按订单实付），一成报高于实付（核算按实付封顶）。
        roll = rng.random()
        if roll < 0.3:
            declared = ""
        elif roll < 0.4:
            declared = f"{round(order['amount_paid'] * rng.uniform(1.05, 1.4), 2)}"
        else:
            declared = f"{order['amount_paid']}"

        base = {"订单号": order["order_id"], "申报金额": declared,
                "申请日期": f"{requested:%Y-%m-%d}"}
        std_rows.append({**base, "诉求类型": label, "说明": f"客户原话：{saying}"})
        raw_rows.append({**base, "诉求类型": saying, "说明": why})

    return std_rows, raw_rows


# --------------------------------------------------------------- 分类评测集
def build_eval_rows() -> list[dict]:
    """评测集。`分组` 那一列决定这一行**怎么算分**，三组判据不一样。

    * `lexicon`   —— 期望必中且 `source` 必须是 lexicon。走到模型即失败。
    * `real`      —— 词表必然判不出，考模型兜底。没配 key 时全落 unknown（预期退化）。
    * `unknown`   —— 期望就是 unknown，判成任何 code 都是错。
    * `ambiguous` —— 两个意图，期望写的是政策上该优先的那个。
    """
    rows: list[dict] = []
    for text, code in LEXICON_HITS:
        rows.append({"说法": text, "期望诉求类型": code, "分组": "lexicon",
                     "这条考什么": "词表逐字命中，一次模型都不该调"})
    for text, code, why in REAL_SAYINGS:
        rows.append({"说法": text, "期望诉求类型": code, "分组": "real",
                     "这条考什么": why})
    for text, why in GENUINELY_UNKNOWN:
        rows.append({"说法": text, "期望诉求类型": "unknown", "分组": "unknown",
                     "这条考什么": why})
    for text, code, why in AMBIGUOUS:
        rows.append({"说法": text, "期望诉求类型": code, "分组": "ambiguous",
                     "这条考什么": why})
    return rows


# ------------------------------------------------------------------- 落盘
SHEET_COLUMNS = ["订单号", "诉求类型", "申报金额", "申请日期", "说明"]
EVAL_COLUMNS = ["说法", "期望诉求类型", "分组", "这条考什么"]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    """带 BOM 写 —— Excel 双击打开不乱码，与 `run_requests.py --csv` 同一口径。"""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def generate(out_dir: Path) -> dict[str, int]:
    rng = random.Random(SEED)
    ledger = build_ledger(rng)
    std_rows, raw_rows = build_sheets(rng, ledger)
    eval_rows = build_eval_rows()

    # 圆桌那张只留有聊天截图的 12 单：五岗逐个说话，60 单跑起来又慢又没人看得完，
    # 而配不上证据的单子在演示里全判 missing，正好把要演的那半边盖掉。
    chat_rows = [r for r in std_rows if r["订单号"] in set(CHAT_ORDERS)]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ledger-bulk.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_csv(out_dir / "refund-requests-bulk.csv", SHEET_COLUMNS, std_rows)
    write_csv(out_dir / "refund-requests-raw.csv", SHEET_COLUMNS, raw_rows)
    write_csv(out_dir / "refund-requests-chat.csv", SHEET_COLUMNS, chat_rows)
    write_csv(out_dir / "reason-eval.csv", EVAL_COLUMNS, eval_rows)

    missing = sorted(set(CHAT_ORDERS) - {r["订单号"] for r in chat_rows})
    if missing:
        raise SystemExit(
            f"CHAT_ORDERS 里这几单不在申请表里：{missing}。"
            " 聊天图会配不上单，而 --evidence 对配不上的文件只报一行就跳过。")

    return {
        "订单": len(ledger["order_snapshot"]),
        "商品": len(ledger["product_snapshot"]),
        "政策": len(ledger["policy_rule"]),
        "退款历史": len(ledger["refund_history"]),
        "申请单（标准词）": len(std_rows),
        "申请单（客户原话）": len(raw_rows),
        "申请单（配聊天图）": len(chat_rows),
        "评测集": len(eval_rows),
    }


def check(out_dir: Path) -> int:
    """重跑一遍，与已落盘的逐字节比 —— 同一颗种子跑两次不一致就是生成器有隐藏状态。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp))
        bad = []
        for name in ("ledger-bulk.json", "refund-requests-bulk.csv",
                     "refund-requests-raw.csv", "refund-requests-chat.csv",
                     "reason-eval.csv"):
            want = (Path(tmp) / name).read_bytes()
            have_path = out_dir / name
            if not have_path.exists():
                bad.append(f"{name}：还没落盘")
            elif have_path.read_bytes() != want:
                bad.append(f"{name}：与重跑结果不一致（被手改过，或生成器改了没重跑）")
    for line in bad:
        print("  ✗", line)
    if not bad:
        print("五份文件与重跑结果逐字节一致。")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(HERE), help="落盘目录，缺省本目录")
    parser.add_argument("--check", action="store_true",
                        help="不写文件，只校验已落盘的四份与重跑结果一致")
    args = parser.parse_args()
    out_dir = Path(args.out).resolve()

    if args.check:
        return check(out_dir)

    counts = generate(out_dir)
    print(f"落盘到 {out_dir}")
    for key, value in counts.items():
        print(f"  {key:16s} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
