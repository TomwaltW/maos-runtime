"""T185 · 单号抽取与规范化（review/p15-cs-contracts.md §1 C6–C8、§2 T185）。

1. **C6**：单号与中文 / 商品名紧贴时照样抽出（边界按「字母数字串 ↔ 非字母数字」判，不靠空格）；
   型号（「RTX4090显卡」「iPhone15」）照旧不当单号。
2. **C7**：``identity.normalize_display_no`` 去掉 ``#`` / ``No.`` / ``NO:`` / ``№`` / ``单号：`` /
   ``订单号`` / ``order #`` 一类前缀（大小写、全半角）；抽取、绑定写入、核验三处同一个函数 ——
   p13 夹具与开发集里的绑定照旧命中，``records.load_bindings_file`` 读入的旧种子照旧命中。
3. **C8**：尾号、门牌 / 楼层 / 房号、日期、金额、数量、电话不当单号；尾号只是提示
   （``extract_order_tail``），不进 order_no 槽位、不查单。
4. 端到端（真前台 + 夹具端口，形状同 p13 开发集）：带前缀的单号查得到、尾号追问而不查单。

自测只用开发集与本文件按类别自写的说法（盲：不读任何留出集）。
"""

from __future__ import annotations

import json

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import evaluate, records
from maos.domain.cs import identity as I
from maos.domain.cs import lang
from maos.domain.cs import understand as U
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.ports import BINDING_TEST, SLOT_ORDER_NO, SLOT_KEYS, Binding
from maos.domain.cs.types import CHANNEL_WECHAT_KF

TENANT_T185 = "tnt-t185"
USER_T185 = "wm_t185"
CLOCK_T185 = "2026-09-25T08:00:00+00:00"


def _slots_t185(text: str) -> dict:
    return U.extract_slots(text, lang=lang.detect_lang(text))


def _store_t185() -> SqliteStore:
    s = SqliteStore()
    s.init_schema()
    return s


def _bind_t185(store, display_no: str, *, query_key: str = "q-1", user: str = USER_T185) -> None:
    records.upsert_binding(store, Binding(
        tenant_id=TENANT_T185, channel=CHANNEL_WECHAT_KF, external_userid=user,
        display_no=display_no, system_name="demo-orders", query_key=query_key,
        source=BINDING_TEST, bound_at=CLOCK_T185))


def _resolve_t185(store, display_no: str, *, user: str = USER_T185):
    return I.BindingVerifier().resolve(store, tenant_id=TENANT_T185, channel=CHANNEL_WECHAT_KF,
                                       external_userid=user, display_no=display_no)


# ---------------------------------------------------------------------------
# 1. C6：紧贴中文 / 商品名
# ---------------------------------------------------------------------------
C6_POSITIVES_T185 = [
    ("XX1234 耳机坏了", "XX1234"),
    ("XX1234耳机坏了", "XX1234"),
    ("KD5521 手表屏幕裂了", "KD5521"),
    ("QW88120充电器不能用", "QW88120"),
    ("单号AB3321前天拍的", "AB3321"),
    ("订单号AB3321昨天下的", "AB3321"),
    ("单号8812093前天拍的", "8812093"),
    ("订单号12345还没发货", "12345"),
    ("我的单AB12345一直没动", "AB12345"),
    ("耳机AB3321坏了要退", "AB3321"),
    ("蓝牙耳机XY20931坏了", "XY20931"),
    ("买的鞋子ZX7788码数不对", "ZX7788"),
    ("这单H8008怎么还不发", "H8008"),
    ("查下G7007到哪了", "G7007"),
    ("单号是ab3321帮我看看", "AB3321"),
    ("订单88120937还没到", "88120937"),
    ("上周拍的MN4455，还没发", "MN4455"),
    ("KD5521 平板屏幕碎了能换吗", "KD5521"),
    # 只靠「隔着空白」一条认的（没有坏了 / 碎了）
    ("XX1234 耳机还没发货", "XX1234"),
    ("麻烦看下 KD5521 手表到哪了", "KD5521"),
]


@pytest.mark.parametrize("text,want", C6_POSITIVES_T185)
def test_c6_order_no_glued_to_chinese_is_extracted_t185(text, want):
    assert _slots_t185(text).get(SLOT_ORDER_NO) == want


#: C6 的反例：型号（写成一个词的「型号 + 品类名词」、驼峰）、短串、没单号字眼的短纯数字。
C6_NEGATIVES_T185 = [
    "RTX4090显卡什么时候发",
    "我订单里的RTX4090显卡还没发",
    "我买的RTX4090显卡坏了",
    "请问GTX1080显卡还有货吗",
    "MX5000鼠标有现货吗",
    "我买的iPhone15坏了",
    "AirPods3耳机能便宜点吗",
    "华为Mate60手机什么时候到货",
    "耳机AB12坏了",
    "那单 12345 呢",
    "我买了个XY20931款的耳机",
    # 复核 L3-1：型号与品类名词隔着空白、在问政策 / 参数，或前面有「我买的 / 请问」一类引语 —— 仍是型号
    "我买的 GTX1660 显卡能七天无理由退吗",
    "我买的 RTX4090 显卡能七天无理由退吗",
    "GTX1660 显卡能七天无理由退吗",
    "我买的 RTX4090 显卡坏了要退货",
    "SM-G9980 手机能退吗",
    "WH1000XM5 耳机支持无线充电吗",
    "请问 MX5000 鼠标有保修吗",
    "XPS9320 笔记本多少钱",
    "KD55X9000 电视能退吗",
    "我的 A2894 充电器还在保修吗",
    "DJI3000 相机可以七天无理由吗",
    "想问下 XX1234 耳机坏了",
]


@pytest.mark.parametrize("text", C6_NEGATIVES_T185)
def test_c6_model_numbers_and_short_strings_stay_out_t185(text):
    assert SLOT_ORDER_NO not in _slots_t185(text)


# ---------------------------------------------------------------------------
# 2. C7：前缀
# ---------------------------------------------------------------------------
C7_POSITIVES_T185 = [
    ("order #88120937", "88120937"),
    ("order #88120937 where is it", "88120937"),
    ("is order #A1001 on its way", "A1001"),
    ("has order #A1001 shipped yet", "A1001"),
    ("Order#A1001 status please", "A1001"),
    ("Order No. 88120937", "88120937"),
    ("order no:88120937 still not here", "88120937"),
    ("order number: SO-2026-000123", "SO-2026-000123"),
    ("No.A1001 到哪了", "A1001"),
    ("NO:A1001 发了没", "A1001"),
    ("no.88120937", "88120937"),
    ("№ 88120937 发货了吗", "88120937"),
    ("№A1001", "A1001"),
    ("订单号#A1001", "A1001"),
    ("单号:#A1001", "A1001"),
    ("单号：A1001", "A1001"),
    ("订单号：＃Ａ１００１", "A1001"),
    ("＃８８１２０９３７ 到哪了", "88120937"),
    ("订单 #1001 能取消吗", "1001"),
    ("订单号: #8812093 前天拍的", "8812093"),
    ("#A1001 这单发了吗", "A1001"),
    # 复核 L2-1（修复轮 2）：光秃秃的 No. 自己就是单号字眼 —— 短数字、合法日期形态的 8 位数都靠它认
    ("No.8812093 到哪了", "8812093"),
    ("No.20260903 到哪了", "20260903"),
    ("no: 1203 发了没", "1203"),
    ("订单号 No.88120937", "88120937"),
    ("我的卡号是6222，订单 No.88120937 到哪了", "88120937"),
]


@pytest.mark.parametrize("text,want", C7_POSITIVES_T185)
def test_c7_prefixed_order_no_is_extracted_without_prefix_t185(text, want):
    assert _slots_t185(text).get(SLOT_ORDER_NO) == want


#: C7 的反例：前缀字眼后面不是单号 / 太短；「#」单独跟短数字不认。
C7_NEGATIVES_T185 = [
    "#1203",
    "No. I don't have the number",
    "no, thanks",
    "订单号是多少我忘了",
    "order #12",
    "单号：不记得了",
    "#12 楼",
    "No.1 款式有货吗",
    "请问 #666 是什么意思",
    "order now please",
]


@pytest.mark.parametrize("text", C7_NEGATIVES_T185)
def test_c7_prefix_words_alone_are_not_order_numbers_t185(text):
    assert SLOT_ORDER_NO not in _slots_t185(text)


@pytest.mark.parametrize("raw,want", [
    ("#A1001", "A1001"), ("# A1001", "A1001"), ("＃Ａ１００１", "A1001"),
    ("No.A1001", "A1001"), ("no. A1001", "A1001"), ("NO:A1001", "A1001"), ("No：A1001", "A1001"),
    ("№A1001", "A1001"), ("№.88120937", "88120937"),
    ("单号：A1001", "A1001"), ("单号:A1001", "A1001"), ("订单号A1001", "A1001"),
    ("订单号：#A1001", "A1001"), ("订单编号 88120937", "88120937"), ("订单 A1001", "A1001"),
    ("order #88120937", "88120937"), ("ORDER NO. 88120937", "88120937"),
    ("Order Number: A1001", "A1001"), ("order id:A1001", "A1001"), ("order A1001", "A1001"),
    ("　＃Ａ１００１　", "A1001"),
])
def test_c7_normalize_strips_prefixes_t185(raw, want):
    assert I.normalize_display_no(raw) == want


@pytest.mark.parametrize("raw", [
    "A1001", "NO1001", "ORDER123", "Nob1234", "a1001", "A 1001", "SO-2026-000123", "88120937",
    "①001",
])
def test_c7_normalize_leaves_bare_numbers_alone_t185(raw):
    """前缀只认「前缀词 + 分隔符」：字母贴着数字的不动；大小写、中间空白、NFKC 照 T171 口径不动。"""
    assert I.normalize_display_no(raw) == raw


@pytest.mark.parametrize("raw", ["#", "单号：", "No.", "  ＃ ", "订单号"])
def test_c7_normalize_prefix_only_is_empty_t185(raw):
    assert I.normalize_display_no(raw) == ""


def test_c7_prefix_only_binding_is_refused_t185():
    with pytest.raises(ValueError):
        _bind_t185(_store_t185(), "单号：#")


@pytest.mark.parametrize("stored,typed", [
    ("A1001", "#A1001"), ("A1001", "单号：A1001"), ("A1001", "No.A1001"), ("88120937", "order #88120937"),
    ("#A1001", "A1001"), ("订单号：A1001", "A1001"), ("Ａ１００１", "＃A1001"),
])
def test_c7_write_and_verify_share_one_normalization_t185(stored, typed):
    """绑定写入与核验同一个函数：哪一侧带前缀都对得上。"""
    s = _store_t185()
    _bind_t185(s, stored)
    got = _resolve_t185(s, typed)
    assert got is not None
    assert got.display_no == I.normalize_display_no(stored) == I.normalize_display_no(typed)
    assert got.display_no in ("A1001", "88120937")


def test_c7_prefix_does_not_widen_to_other_numbers_t185():
    s = _store_t185()
    _bind_t185(s, "A1001")
    for typed in ("#A1002", "No.A10011", "单号：B1001", "a1001", "A-1001", "#", "单号："):
        assert _resolve_t185(s, typed) is None, typed
    assert _resolve_t185(s, "#A1001", user="wm_other_t185") is None


# ---------------------------------------------------------------------------
# 3. 旧绑定照旧命中：p13 夹具、开发集正文、load_bindings_file 的旧种子
# ---------------------------------------------------------------------------
def _p13_doc_t185() -> dict:
    return json.loads(evaluate.P13_EVAL_PATH.read_text(encoding="utf-8"))


def test_p13_fixture_bindings_still_resolve_t185():
    """p13 开发集里每一条夹具绑定：写进库（新规范化）后，用夹具原样的 display_no 与带前缀的写法都命中。"""
    doc = _p13_doc_t185()
    pairs = [(c["id"], b) for c in doc["cases"] for b in c.get("fixtures", {}).get("bindings", [])]
    assert len(pairs) >= 20
    for case_id, b in pairs:
        s = _store_t185()
        _bind_t185(s, b["display_no"], query_key=b["query_key"])
        for typed in (b["display_no"], "#" + b["display_no"], "单号：" + b["display_no"]):
            got = _resolve_t185(s, typed)
            assert got is not None and got.query_key == b["query_key"], (case_id, typed)
        assert I.normalize_display_no(b["display_no"]) == b["display_no"], case_id


def test_p13_dev_turns_extract_the_fixture_display_no_t185():
    """开发集里正文含夹具单号的每一轮：抽出来的值就是夹具的 display_no（夹具端口按原样精确比对）。"""
    doc = _p13_doc_t185()
    hits = 0
    for c in doc["cases"]:
        nos = {b["display_no"] for b in c.get("fixtures", {}).get("bindings", [])}
        for text in c["turns"]:
            present = [n for n in nos if n.lower() in text.lower()]
            if not present:
                continue
            hits += 1
            assert U.extract_order_no(text) in present, c["id"]
    assert hits >= 25


def test_old_seed_file_bindings_still_resolve_t185(tmp_path):
    """p13 口径写的旧种子（无前缀、全角、带空白）经 load_bindings_file 读入后，照旧命中；带前缀问也命中。"""
    seed = tmp_path / "bindings_t185.json"
    seed.write_text(json.dumps({"_note": "旧种子", "bindings": [
        {"tenant_id": TENANT_T185, "channel": CHANNEL_WECHAT_KF, "external_userid": USER_T185,
         "display_no": "A1001", "system_name": "demo-orders", "query_key": "gid-1001"},
        {"tenant_id": TENANT_T185, "channel": CHANNEL_WECHAT_KF, "external_userid": USER_T185,
         "display_no": "Ｂ２００２", "system_name": "demo-orders", "query_key": "gid-2002"},
        {"tenant_id": TENANT_T185, "channel": CHANNEL_WECHAT_KF, "external_userid": USER_T185,
         "display_no": "  2026092400123 ", "system_name": "demo-orders", "query_key": "gid-3"},
        {"tenant_id": TENANT_T185, "channel": CHANNEL_WECHAT_KF, "external_userid": USER_T185,
         "display_no": "SO-2026-000123", "system_name": "demo-orders", "query_key": "gid-4"},
    ]}, ensure_ascii=False), encoding="utf-8")
    s = _store_t185()
    assert records.load_bindings_file(s, seed) == 4
    for typed, key in (("A1001", "gid-1001"), ("B2002", "gid-2002"), ("2026092400123", "gid-3"),
                       ("SO-2026-000123", "gid-4"), ("#A1001", "gid-1001"),
                       ("order #2026092400123", "gid-3"), ("单号：SO-2026-000123", "gid-4")):
        got = _resolve_t185(s, typed)
        assert got is not None and got.query_key == key, typed
    # 抽取 → 核验同一个口径：客户带前缀说的那句，抽出来的值直接能核验
    for text, key in (("order #A1001 到哪了", "gid-1001"), ("单号：＃Ｂ２００２发了吗", "gid-2002")):
        got = _resolve_t185(s, U.extract_order_no(text))
        assert got is not None and got.query_key == key, text


# ---------------------------------------------------------------------------
# 4. C8：不是单号的数字
# ---------------------------------------------------------------------------
C8_NEGATIVES_T185 = [
    # 尾号
    "尾数 4417", "尾号4417的那单发了没", "后四位是4417", "订单尾号 4417 还没到", "手机尾号 8812",
    "卡尾号 6688 的退款到了吗", "尾数为 12345678", "the one ending in 4417", "last four digits 4417",
    "最后四位 0937 那单",
    # 门牌 / 楼层 / 房号
    "我住1203写成1302了", "门牌号 1203 写错了", "12楼1203室", "B1203室没人收", "房间号 88120937 写错",
    "3栋2单元501", "地址是幸福路 12345678 号", "改到 1203 房",
    # 日期
    "28 号申请的退款", "28号申请的", "9 月 3 日下的单", "20250903 下的单", "2026-09-03 买的",
    "20260903那天买的",
    # 金额
    "退了 199 元还没到", "¥12345678 什么时候退", "扣了 12345678 块", "金额 88120937 不对",
    # 数量
    "买了 3 件", "一共 12345678 件", "买了 10000 个",
    # 电话
    "电话 13812345678", "打 4008123123 没人接", "手机号 138-1234-5678",
    # 复核 L2-2：紧挨强单号字眼的 4–7 位短数字，后面跟单位的仍不是单号（金额 / 房号 / 数量）
    "订单 1500 元退了吗", "订单2000块能退吗", "订单 1203 室没人收", "单号 3000 件", "订单 5000 块钱退哪了",
    # 复核 L2-4：「#」+ 字母数字串不挨单号字眼 —— 色号、话题标签
    "颜色 #FF5733 那款坏了", "话题 #AB12345 好火", "要 #CC0033 这个色",
    # 复核 L2-1（修复轮 2）：「别的号」字眼 + 光秃秃的 No.（No. 是那个号的标签，不是单号字眼）
    "card no. 88120937", "account no. 88120937", "phone no. 88120937", "id no. 88120937",
    "passport no. 88120937", "Card No.88120937", "卡号 No.88120937", "银行卡 No.62220212",
    "会员号 No.12345678 能积分吗", "QQ No:12345678", "身份证号 No.12345678", "会员卡 No.A1001",
    "门牌 No.1203", "房间 No.1203 的快递", "手机号 NO:12345678",
]


@pytest.mark.parametrize("text", C8_NEGATIVES_T185)
def test_c8_non_order_numbers_are_not_extracted_t185(text):
    assert SLOT_ORDER_NO not in _slots_t185(text)


#: C8 的反例：旁边有尾号 / 日期 / 金额字眼，但真单号照样抽出。
C8_COUNTER_T185 = [
    ("单号 A1001，手机尾号 8812", "A1001"),
    ("28 号拍的 88120937 还没发", "88120937"),
    ("订单号 20250903 到哪了", "20250903"),
    ("20250903 这单到哪了", "20250903"),
    ("退了 199 元，单号 A1001", "A1001"),
    ("买了 3 件，订单 88120937", "88120937"),
    ("12345678到哪了", "12345678"),
    ("2026092400123 这单 28 号拍的", "2026092400123"),
    ("9 月 3 日下的 A1001 还没发", "A1001"),
    ("门牌写错了，单号 88120937", "88120937"),
    # 复核 L2-1：后面的普通词以单位字开头（包裹 / 平台 / 天猫 / 米家 / 点了 / 条码 / 月底 / 周末 / 台灯 / 号订单）
    ("订单号 88120937 包裹没到", "88120937"),
    ("88120937号订单还没发货", "88120937"),
    ("订单12345678米家扫地机坏了", "12345678"),
    ("订单号88120937平台显示已签收", "88120937"),
    ("单号 88120937 天猫显示签收了", "88120937"),
    ("订单号 88120937 点了退款没反应", "88120937"),
    ("订单号88120937条码扫不出", "88120937"),
    ("订单号 88120937 月底能到吗", "88120937"),
    ("单号88120937周末能到吗", "88120937"),
    ("订单号: 88120937 台灯坏了", "88120937"),
    ("单号 8812093 包裹没到", "8812093"),
    # 复核 L2-3：紧挨单号字眼的字母串后面是「楼下 / 房东」
    ("订单号A1001楼下签收的", "A1001"),
    ("单号 AB3321 房东代收了", "AB3321"),
    # 紧挨单号字眼时不做单位 / 房号判定：后面的词以单位字开头、又不在排除表里的也照认
    ("单号 88120937 盒子破了", "88120937"),
    ("订单号88120937瓶子漏了", "88120937"),
    ("订单号 88120937 个人信息填错了", "88120937"),
    ("订单 A1001 室友代收了", "A1001"),
    ("单号 AB3321 单元门口放着", "AB3321"),
]


@pytest.mark.parametrize("text,want", C8_COUNTER_T185)
def test_c8_real_order_numbers_next_to_other_numbers_t185(text, want):
    assert _slots_t185(text).get(SLOT_ORDER_NO) == want


@pytest.mark.parametrize("text,want", [
    ("尾数 4417", "4417"), ("尾号4417的那单发了没", "4417"), ("后四位是4417", "4417"),
    ("the one ending in 4417", "4417"), ("last four digits 4417", "4417"), ("尾号：881", "881"),
    ("单号 A1001", ""), ("28 号申请的", ""), ("1203 写成 1302", ""),
])
def test_c8_tail_is_only_a_hint_t185(text, want):
    """尾号提示单独取（extract_order_tail），不进槽位：槽位键仍是冻结的五个。"""
    assert U.extract_order_tail(text) == want
    slots = _slots_t185(text)
    assert set(slots) <= set(SLOT_KEYS)
    if want:
        assert SLOT_ORDER_NO not in slots


# ---------------------------------------------------------------------------
# 5. 端到端：真前台 + 夹具端口
# ---------------------------------------------------------------------------
def _factory_t185(ports) -> FrontDesk:
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    return FrontDesk(store, CsConfig(tenants={"wk_eval": "tnt-demo"}, handoff_target=None),
                     clock=lambda: CLOCK_T185, **ports)


def _case_t185(cid: str, display_no: str, turns: list[str], expect: list[dict]) -> dict:
    return {"id": cid, "synthetic": True, "tags": ["t185"], "open_kfid": "wk_eval",
            "fixtures": {"bindings": [{"display_no": display_no, "system_name": "demo-orders",
                                       "query_key": display_no}],
                         "orders": {display_no: {"outcome": "ok", "status": "shipped"}}},
            "turns": turns, "expect": expect}


E2E_CASES_T185 = [
    _case_t185("T185-01", "88120937", ["order #88120937 发货了吗"],
               [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "shipped"}]),
    _case_t185("T185-02", "A1001", ["单号：#A1001 到哪了"],
               [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "shipped"}]),
    _case_t185("T185-03", "AB3321", ["单号AB3321前天拍的，发货了吗"],
               [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "shipped"}]),
    _case_t185("T185-04", "8812093", ["帮我查一下物流，订单号8812093"],
               [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "shipped"}]),
    # 尾号只是提示：不查单，追问单号
    _case_t185("T185-05", "88124417", ["尾号4417的那单发货了吗"],
               [{"route": "clarify", "intent": "logistics", "ask": "order_no"}]),
    _case_t185("T185-06", "88120937", ["我 28 号拍的，物流到哪了"],
               [{"route": "clarify", "intent": "logistics", "ask": "order_no"}]),
    # 复核 L3-1：型号隔空白 + 问政策 —— 型号不进槽位，照旧走政策回答（不掉进 only_order_no 兜底）
    _case_t185("T185-07", "88120937", ["我买的 GTX1660 显卡能七天无理由退吗"],
               [{"route": "answer", "intent": "return_exchange"}]),
    _case_t185("T185-08", "88120937", ["GTX1660 显卡能七天无理由退吗"],
               [{"route": "answer", "intent": "return_exchange"}]),
    # 复核 L2-1：紧挨单号字眼、后面是「包裹」的长串照样查单
    _case_t185("T185-09", "88120937", ["订单号 88120937 包裹到哪了"],
               [{"route": "answer", "intent": "logistics", "lookup": "ok", "say": "shipped"}]),
]


def test_e2e_prefixed_and_glued_order_no_reach_lookup_t185(tmp_path):
    path = tmp_path / "t185_cases.json"
    path.write_text(json.dumps({"_note": "T185 自写", "_provenance": {"synthetic": True},
                                "_thresholds": {}, "cases": E2E_CASES_T185}, ensure_ascii=False),
                    encoding="utf-8")
    report = evaluate.run_eval_p13(_factory_t185, evaluate.load_cases(path))
    assert report.failures == (), "\n".join(
        f"{m.case_id}#{m.turn} {m.problems} 期望 {m.expected} 实得 {m.actual}" for m in report.failures)
