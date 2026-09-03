"""申请表进群：认表按内容、逐行收错、合法行只读预检并挂待办、回帖措辞。

被测的是**行为**，不是文案：
  · 一行填错不许挡住其余行（run_requests 那条入口是 fail-fast 的，这里刻意不是）。
  · 认表按内容：改名成 .csv 的二进制到不了这里，照旧走白名单。
  · 表只做预检、挂待办：runner **一次都不许**被调 —— 一张表进群不等于一批付款。
"""

from __future__ import annotations

import pytest

from maos.core.store import SqliteStore
from maos.ingress import sheet
from maos.ingress.attachments import AttachmentBuffer, AttachmentStore
from maos.ingress.contracts import CHANNEL_FEISHU, Attachment, InboundMessage
from maos.ingress.router import IngressRouter
from maos.tests.test_ingress_router import Runs

HEADER = "订单号,诉求类型,申报金额,申请日期,说明\n"

#: 与 `var/attachments/demo-inbox/bad-requests.csv` 同一张表：每行一种错法。
BAD = (HEADER
       + "ORD-9999-9999,质量问题,500,2026-09-01,底账里没有这个订单\n"
       + "ORD-2026-0001,天上掉馅饼,6800,2026-09-01,诉求类型不认识\n"
       + "ORD-2026-0002,质量问题,-500,2026-09-01,负数金额\n"
       + "ORD-2026-0003,质量问题,24000,2026-13-45,非法日期\n"
       + "\n"
       + "ORD-2026-0001,质量问题,999999999,2026-09-01,金额远超订单实付\n"
       + "ORD-2026-0001,quality_defect,6800,2026-09-01,英文 code + 多余列,extra,extra2\n")

GOOD = (HEADER
        + "ORD-2026-0001,质量问题,6800,2026-07-10,\n"
        + "ORD-2026-0003,七天无理由,,2026-08-25,金额留空\n")


class _Adapter:
    name = CHANNEL_FEISHU
    configured = True

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.sent: list = []

    def fetch(self, att: Attachment) -> bytes:
        return self.blobs[att.file_key]

    def send(self, msg) -> None:
        self.sent.append(msg)


def _ledger() -> dict:
    from maos.flows.custom_case import load
    from maos.ingress.router import DEFAULT_LEDGER
    return load(DEFAULT_LEDGER, require_case=False)


def _router(tmp_path, adapter, *, runner=None, approvers=("ou_boss",)):
    store = SqliteStore(":memory:")
    store.init_schema()
    return IngressRouter({adapter.name: adapter}, store=store,
                         runner=Runs() if runner is None else runner,
                         approvers=lambda: frozenset(approvers),
                         attachment_store=AttachmentStore(tmp_path),
                         attachment_buffer=AttachmentBuffer())


def _inbound(key: str, filename: str, *, msg_id: str = "m1", text: str = "",
             sender: str = "ou_alice") -> InboundMessage:
    return InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender=sender, text=text, msg_id=msg_id,
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key=key, kind="file",
                                filename=filename, mime="text/csv"),))


# --------------------------------------------------------------------------
# 认表
# --------------------------------------------------------------------------
@pytest.mark.parametrize("data, expected", [
    (BAD.encode("utf-8"), True),
    (b"\xef\xbb\xbf" + GOOD.encode("utf-8"), True),          # Excel 的 BOM
    (GOOD.encode("gbk"), True),                                # 中文 Windows 的 Excel
    (" 申请日期 , 订单号 ,诉求类型\n2026-07-10,ORD-2026-0001,质量问题\n".encode(), True),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, False),              # 二进制
    (b"a,b,c\n1,2,3\n", False),                                # 是 CSV，但没订单号那列
    (b"", False),
    (b"\n\n", False),
])
def test_looks_like_sheet_judges_by_content(data, expected):
    assert sheet.looks_like_sheet(data) is expected


def test_renamed_binary_is_not_a_sheet_and_still_hits_the_whitelist(tmp_path):
    """改名成 .csv 的 ELF：不是表，走白名单被拒 —— 且拒得出声。"""
    adapter = _Adapter({"k": b"\x7fELF" + b"\x00" * 64})
    router = _router(tmp_path, adapter)
    reply = router.handle(_inbound("k", "申请表.csv"))
    assert "未收下 1 份" in reply and "不收这个类型" in reply
    assert "申请表" not in reply.split("未收下")[0]


# --------------------------------------------------------------------------
# 逐行收错
# --------------------------------------------------------------------------
def test_every_bad_row_is_reported_and_good_rows_still_run():
    parsed = sheet.parse(BAD.encode("utf-8"), "bad-requests.csv", _ledger())

    assert [r.line for r in parsed.invalid] == [2, 3, 4, 5]
    assert [r.line for r in parsed.valid] == [7, 8]
    assert parsed.skipped_blank == 1                       # 第 6 行是空行
    by_line = {r.line: r for r in parsed.rows}
    assert "底账里没有订单 ORD-9999-9999" in by_line[2].problems[0]
    assert "看不懂的诉求类型" in by_line[3].problems[0]
    assert "负数" in by_line[4].problems[0]
    assert "看不懂的日期" in by_line[5].problems[0]
    # 不阻断的提醒：超实付会封顶、多余列被忽略、同一订单出现多次
    assert any("超过订单实付 6800" in w for w in by_line[7].warnings)
    assert any("多出 2 列" in w for w in by_line[8].warnings)
    assert all(any("出现多次" in w for w in by_line[n].warnings) for n in (7, 8))


def test_amount_is_shown_as_money_not_scientific_notation():
    parsed = sheet.parse(BAD.encode("utf-8"), "x.csv", _ledger())
    row = {r.line: r for r in parsed.rows}[7]
    assert "999999999" in row.warnings[0] and "e+" not in row.warnings[0]


def test_one_row_collects_all_its_problems_at_once():
    """一行里三处错要一次说完 —— 人是改完整张表再发，不是改一处发一次。"""
    data = (HEADER + "ORD-9999-9999,天上掉馅饼,-1,2026-13-45,\n").encode()
    row, = sheet.parse(data, "x.csv", _ledger()).rows
    assert len(row.problems) == 4
    assert row.req is None


def test_blank_amount_and_blank_date_are_fine():
    parsed = sheet.parse((HEADER + "ORD-2026-0001,质量问题,,,\n").encode(), "x.csv", _ledger())
    row, = parsed.rows
    assert row.ok and row.req["amount"] is None and row.req["requested_at"]


def test_requested_before_paid_is_a_warning_not_a_block():
    parsed = sheet.parse((HEADER + "ORD-2026-0001,质量问题,,2020-01-01,\n").encode(),
                         "x.csv", _ledger())
    row, = parsed.rows
    assert row.ok
    assert any("早于该订单付款日 2026-07-01" in w for w in row.warnings)


def test_refund_command_refuses_negative_amount_like_the_sheet_does(tmp_path):
    """群里一行命令与表里一行是同一道闸：负数在入口就拦，不给核算去抹成 0 元「已到账」。"""
    runs = Runs()
    adapter = _Adapter({})
    router = _router(tmp_path, adapter, runner=runs)
    out = router.handle(InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1",
                                       sender="ou_alice", msg_id="n1",
                                       text="/refund ORD-2026-0002 质量问题 -500"))
    assert "不能是负数或 0" in out and "RC-ORD-2026-0002" not in router._tickets
    assert runs == []


def test_zero_amount_is_refused_with_the_fix():
    parsed = sheet.parse((HEADER + "ORD-2026-0001,质量问题,0,,\n").encode(), "x.csv", _ledger())
    row, = parsed.rows
    assert not row.ok and "留空" in row.problems[0]


def test_gbk_is_decoded_and_the_reply_says_so(tmp_path):
    adapter = _Adapter({"k": GOOD.encode("gbk")})
    router = _router(tmp_path, adapter)
    reply = router.handle(_inbound("k", "gbk.csv"))
    assert "共 2 行，可预检 2 行" in reply
    assert "按 gbk 解码" in reply


def test_undecodable_bytes_get_a_plain_answer(tmp_path):
    """表头像表、正文解不出：说清试过哪些编码，不给 traceback。"""
    data = HEADER.encode("utf-8") + b"ORD-2026-0001,\xff\xfe\xfd\xfc,6800,2026-07-10,\n"
    adapter = _Adapter({"k": data})
    router = _router(tmp_path, adapter)
    reply = router.handle(_inbound("k", "怪.csv"))
    assert "解不出文字" in reply and "utf-8-sig" in reply and "gbk" in reply


def test_too_many_rows_are_said_not_silently_dropped():
    body = "".join(f"ORD-2026-0001,质量问题,,2026-07-10,{i}\n" for i in range(sheet.MAX_ROWS + 3))
    parsed = sheet.parse((HEADER + body).encode(), "big.csv", _ledger())
    assert len(parsed.rows) == sheet.MAX_ROWS and parsed.truncated == 3
    text = sheet.render(parsed, {}, {}, decision_cn={})
    assert f"只看前 {sheet.MAX_ROWS} 行" in text and "3 行未看" in text


def test_parse_stops_walking_rows_after_the_cap():
    """到上限就停：余下几十万行不再一条条建 dict（评审实测 59 MB 表 545 MB RSS）。"""
    import maos.ingress.sheet as sheet_mod

    calls = {"n": 0}
    real = sheet_mod._parse_row

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    body = "".join("ORD-2026-0001,质量问题,,2026-07-10,\n" for _ in range(sheet.MAX_ROWS * 4))
    orig = sheet_mod._parse_row
    sheet_mod._parse_row = counting
    try:
        parsed = sheet.parse((HEADER + body).encode(), "big.csv", _ledger())
    finally:
        sheet_mod._parse_row = orig
    assert calls["n"] == sheet.MAX_ROWS
    assert parsed.truncated == sheet.MAX_ROWS * 3


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "Infinity", "1e400", "NaN"])
def test_non_finite_amounts_are_not_numbers(raw):
    """float() 认 nan/inf；nan 跟什么比都 False，会穿过每道闸再在核算里炸掉。"""
    parsed = sheet.parse((HEADER + f"ORD-2026-0001,质量问题,{raw},2026-07-10,\n").encode(),
                         "x.csv", _ledger())
    row, = parsed.rows
    assert not row.ok and "不是数字" in row.problems[0]


def test_refund_command_refuses_non_finite_amount(tmp_path):
    runs = Runs()
    router = _router(tmp_path, _Adapter({}), runner=runs)
    out = router.handle(InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1",
                                       sender="ou_alice", msg_id="n2",
                                       text="/refund ORD-2026-0001 质量问题 nan"))
    assert "不是数字" in out and router._tickets == {} and runs == []


def test_cr_only_line_endings_are_a_sheet():
    """Excel for Mac 的「CSV (Macintosh)」只用 \\r 换行。"""
    data = (HEADER + "ORD-2026-0001,质量问题,6800,2026-07-10,\n").replace("\n", "\r").encode()
    assert sheet.looks_like_sheet(data) is True
    parsed = sheet.parse(data, "mac.csv", _ledger())
    assert [r.line for r in parsed.rows] == [2] and parsed.rows[0].ok


def test_multiline_cell_keeps_excel_row_numbers():
    """「说明」里 Alt+Enter 的换行是一条记录跨几个物理行；行号按记录数，对得上 Excel。"""
    data = (HEADER
            + 'ORD-2026-0001,质量问题,6800,2026-07-10,"第一行\n第二行\n第三行"\n'
            + "ORD-2026-0002,质量问题,-5,2026-07-10,\n").encode()
    parsed = sheet.parse(data, "x.csv", _ledger())
    assert [r.line for r in parsed.rows] == [2, 3]
    assert parsed.skipped_blank == 0
    assert not parsed.rows[1].ok and "负数" in parsed.rows[1].problems[0]


def test_echoed_fields_are_clipped():
    """字段是人填的、会原样刷回群里 —— 一个粘了聊天记录的单元格不该把回帖撑爆。"""
    long_reason = "天上掉馅饼" * 200
    data = (HEADER + f"ORD-2026-0001,{long_reason},6800,2026-07-10,\n").encode()
    row, = sheet.parse(data, "x.csv", _ledger()).rows
    assert len(row.reason_raw) == sheet.FIELD_MAX and row.reason_raw.endswith("…")
    assert len(row.problems[0]) < 400


def test_field_over_csv_limit_is_a_parse_error_not_fetch_failure(tmp_path):
    import csv as _csv

    huge = "x" * (_csv.field_size_limit() + 10)
    data = (HEADER + f'ORD-2026-0001,质量问题,6800,2026-07-10,"{huge}"\n').encode()
    adapter = _Adapter({"k": data})
    router = _router(tmp_path, adapter)
    reply = router.handle(_inbound("k", "big-cell.csv"))
    assert "表解析失败" in reply and "取件失败" not in reply


def test_oversized_attachment_is_refused_before_fetch(tmp_path):
    """平台自报的 size 超限：一次都不出网。"""
    fetched: list = []

    class Counting(_Adapter):
        def fetch(self, att):
            fetched.append(att.file_key)
            return super().fetch(att)

    adapter = Counting({"k": GOOD.encode()})
    router = _router(tmp_path, adapter)
    msg = InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_alice", text="", msg_id="big",
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key="k", kind="file",
                                filename="巨.csv", size=router.attachments.max_bytes + 1),))
    reply = router.handle(msg)
    assert fetched == [] and "超过上限" in reply and "未收下 1 份" in reply


def test_oversized_sheet_bytes_are_refused_after_fetch(tmp_path):
    """自报值不可信：拿到字节再校一次，申请表分支不许绕过 put() 那道闸。"""
    from maos.ingress.attachments import AttachmentStore

    big = (HEADER + "ORD-2026-0001,质量问题,6800,2026-07-10,\n" * 40).encode()
    adapter = _Adapter({"k": big})
    store = SqliteStore(":memory:")
    store.init_schema()
    router = IngressRouter({adapter.name: adapter}, store=store, runner=Runs(),
                           attachment_store=AttachmentStore(tmp_path, max_bytes=len(big) - 1),
                           attachment_buffer=AttachmentBuffer())
    reply = router.handle(_inbound("k", "big.csv"))
    assert "超过上限" in reply and "申请表" not in reply.split("未收下")[0]
    assert router._tickets == {}


def test_sheet_says_when_it_replaces_someone_elses_ticket(tmp_path):
    """同一 case_id 的待办被表换掉了：换的是别人的、或原来挂着证据，必须在回帖里说。"""
    from maos.tests.test_ingress_attachments import PNG_1PX

    adapter = _Adapter({"png": PNG_1PX, "csv": GOOD.encode("utf-8")})
    router = _router(tmp_path, adapter)
    router.handle(InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_boss", text="", msg_id="p1",
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key="png", filename="a.png"),)))
    router.handle(InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_boss",
                                 text="/refund ORD-2026-0001 质量问题", msg_id="r1"))
    assert len(router._tickets["RC-ORD-2026-0001"].evidence) == 1

    reply = router.handle(_inbound("csv", "good.csv", msg_id="s1", sender="ou_mallory"))
    assert "替换了 ou_boss 之前提交的待办" in reply
    assert "1 份证据不再随案" in reply
    assert router._tickets["RC-ORD-2026-0001"].requested_by == "ou_mallory"


# --------------------------------------------------------------------------
# router：预检、待办、放行、不动钱
# --------------------------------------------------------------------------
def test_sheet_preflights_valid_rows_without_moving_money(tmp_path):
    runs = Runs()
    adapter = _Adapter({"k": BAD.encode("utf-8")})
    router = _router(tmp_path, adapter, runner=runs)

    reply = router.handle(_inbound("k", "bad-requests.csv"))

    assert runs == [], "一张表进群不许直接跑处置"
    assert "共 6 行，可预检 2 行，有问题 4 行" in reply
    assert "未动任何资金" in reply
    assert "/approve RC-ORD-2026-0001" in reply
    assert "RC-ORD-2026-0001" in router._tickets          # 待办挂上了
    assert router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1") == []   # 表不是证据
    assert adapter.sent and adapter.sent[-1].text == reply


def test_ticket_from_sheet_can_be_released_by_approver(tmp_path):
    runs = Runs()
    adapter = _Adapter({"k": GOOD.encode("utf-8")})
    router = _router(tmp_path, adapter, runner=runs, approvers=("ou_boss",))
    router.handle(_inbound("k", "good.csv"))

    out = router.handle(InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1",
                                       sender="ou_boss", text="/approve RC-ORD-2026-0001",
                                       msg_id="m2"))
    assert "已放行 RC-ORD-2026-0001" in out
    assert len(runs) == 1 and runs[0]["case"]["order_id"] == "ORD-2026-0001"


def test_sheet_rows_do_not_claim_buffered_photos(tmp_path):
    """十行申请配三张图，没法知道图是谁的 —— 表不认领暂存证据。"""
    from maos.tests.test_ingress_attachments import PNG_1PX

    adapter = _Adapter({"png": PNG_1PX, "csv": GOOD.encode("utf-8")})
    router = _router(tmp_path, adapter)
    router.handle(InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_alice", text="", msg_id="p1",
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key="png", filename="a.png"),)))
    router.handle(_inbound("csv", "good.csv", msg_id="s1"))

    assert len(router.pending_evidence.peek(CHANNEL_FEISHU, "oc_1")) == 1
    assert "customer_evidence" not in router._tickets["RC-ORD-2026-0001"].payload


def test_preflight_failure_on_one_row_does_not_sink_the_sheet(tmp_path, monkeypatch):
    import maos.ingress.router as router_mod

    real = router_mod.preflight
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if payload["case"]["order_id"] == "ORD-2026-0001":
            raise RuntimeError("政策视图炸了")
        return real(payload)

    monkeypatch.setattr(router_mod, "preflight", flaky)
    adapter = _Adapter({"k": GOOD.encode("utf-8")})
    router = _router(tmp_path, adapter)
    reply = router.handle(_inbound("k", "good.csv"))

    assert calls["n"] == 2
    assert "第 2 行 ORD-2026-0001：预检失败 —— RuntimeError: 政策视图炸了" in reply
    assert "第 3 行 ORD-2026-0003（七天无理由）" in reply
    assert "RC-ORD-2026-0003" in router._tickets and "RC-ORD-2026-0001" not in router._tickets


def test_sheet_summary_is_remembered_per_chat(tmp_path):
    adapter = _Adapter({"k": BAD.encode("utf-8")})
    router = _router(tmp_path, adapter)
    router.handle(_inbound("k", "bad-requests.csv"))
    note = router._last_sheet[(CHANNEL_FEISHU, "oc_1")]
    assert "bad-requests.csv" in note and "4 行填错" in note and "2 行预检完成" in note
