"""房间入口的诉求类型判定（T106）—— 词表外的词在群里怎么说。

上一轮只给**命令行**接了模型：`scripts/run_requests.py` 里词表认不出就调
`refund.reason_classify`。房间那条一个字都没动，而房间才是演示主场 —— 老板在
Matrix 群里拖一张写着「漏发了两个」的表，得到的是一行「看不懂的诉求类型」。

这个文件钉四件事：

  1. **判据只有一处。** 那三个函数从 `scripts/run_requests.py` 搬进
     `maos/ingress/classify.py`（包不该 import scripts/），搬完两条入口调的是
     同一个 `classify_reason`。两套判据的症状是「CSV 里写『漏发了两个』能跑、
     群里发同一张表不认」，且两边都不报错。
  2. **判不准 ≠ 填错了。** 词表外的词进「待人工」，不进 `problems` ——
     让人回去改一张没填错的表，是把人指向一个不存在的问题。
  3. **模型判出来的每一格都留痕。** 房间这条链路上 `store` 是持久库，标注写进去
     查得回来（命令行那条是 `:memory:`，落了跑完就没）。转人工一律走
     `annotation.needs_human()`，不许本地比阈值。
  4. **Excel 拖进群给一句能行动的话。** 仍然拒收（`.xlsx` 是 zip 容器，魔数与
     任意 zip 相同，收下就等于白名单破掉），但把「认不出」换成「另存为 CSV」。
"""

from __future__ import annotations

import pathlib

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import annotation, objects
from maos.ingress import attachments, classify, router as router_mod, sheet
from maos.ingress.attachments import (
    AttachmentBuffer, AttachmentStore, AttachmentTypeRejected,
)
from maos.ingress.contracts import CHANNEL_FEISHU, Attachment, InboundMessage
from maos.ingress.router import IngressRouter
from maos.tests.test_ingress_router import Runs

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

HEADER = "订单号,诉求类型,申报金额,申请日期,说明\n"

#: 老板真会写的一句 —— 词表里没有，模型才判得出来（口径同 `scripts/run_requests.py`
#: 那条自检：「漏发了两个」是发错货，不是质量问题）。
OFF_LEXICON = "漏发了两个"

#: 模型那次调用的 actor 锚点。真跑时由 invoker 生成 uuid4().hex 并由 skill 原样回显。
FAKE_INVOCATION = "iv-t106-fake-0001"


def _ledger() -> dict:
    from maos.flows.custom_case import load
    from maos.ingress.router import DEFAULT_LEDGER
    return load(DEFAULT_LEDGER, require_case=False)


def _classifier(reason_code: str = "wrong_item", confidence: float = 0.93,
                *, invocation_id: str = FAKE_INVOCATION, why: str = "原文说少发了货"):
    """假的 skill 调用面。签名与 `classify.make_classifier()` 返回的那个逐字相同。

    用假件而不是真模型：这一层要钉的是「判出来之后怎么走」，不是模型判得准不准
    （那是 `test_reason_classify.py` 的事）。真模型还要 key，跑不成 CI。
    """
    seen: list[tuple[str, dict]] = []

    def call(name: str, payload: dict) -> dict | None:
        seen.append((name, payload))
        return {"reason_code": reason_code, "confidence": confidence, "why": why,
                "source": "model", "raw_text": payload.get("text", ""),
                "invocation_id": invocation_id}

    call.seen = seen                                    # type: ignore[attr-defined]
    return call


def _dead_classifier(call, payload=None):               # noqa: ANN001, ARG001
    """模型调用没产出结果时那个形态（`make_classifier` 的 call 恒返 None）。"""
    return None


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


def _router(tmp_path, adapter, *, store=None):
    return IngressRouter({adapter.name: adapter},
                         store=store if store is not None else _store(),
                         runner=Runs(),
                         approvers=lambda: frozenset(("ou_boss",)),
                         attachment_store=AttachmentStore(tmp_path),
                         attachment_buffer=AttachmentBuffer())


def _store() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def _inbound(key: str, filename: str, *, msg_id: str = "m1") -> InboundMessage:
    return InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_alice", text="", msg_id=msg_id,
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key=key, kind="file",
                                filename=filename, mime="text/csv"),))


# ==========================================================================
# 1. 搬迁：判据只有一处，行为逐字不变
# ==========================================================================
def test_the_script_reuses_the_package_and_not_the_other_way_round():
    """`scripts/run_requests.py` 从包里取那三个函数，**不是**包 import scripts/。

    方向反了的症状不是报错：`maos.ingress.sheet` 会开始通过 `_load_run_requests`
    那条 importlib 加载去够一个脚本，而那条加载存在的理由是复用一个既有脚本，
    不是可以顺手扩大的依赖方向。
    """
    from maos.ingress.router import _load_run_requests

    rr = _load_run_requests()
    assert rr.classify_reason is classify.classify_reason
    assert rr.make_classifier is classify.make_classifier
    assert rr.RequestSheetError is classify.RequestSheetError


def test_classify_py_does_not_reach_into_the_scripts_directory():
    """这个模块一条 import 都不许指向 scripts/ —— 搬迁就是为了消掉那个方向。

    判据走 AST 而不是搜字符串：docstring 里说得清「为什么不 import scripts」，
    按字符串搜会把那段解释本身判成违例。
    """
    import ast

    tree = ast.parse(pathlib.Path(classify.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert names and not any(n.startswith("scripts") for n in names), names
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "spec_from_file_location" not in calls


@pytest.mark.parametrize("raw, code, source", [
    ("质量问题", "quality_defect", "lexicon"),
    ("七天无理由", "no_reason_return", "lexicon"),
    ("quality_defect", "quality_defect", "lexicon"),    # 英文 code 直接写也认
])
def test_lexicon_hits_never_call_the_model(raw, code, source):
    """词表命中**一次模型都不调** —— 传了 classifier 也不许调。"""
    call = _classifier()
    verdict = classify.classify_reason(raw, call)
    assert verdict["reason"] == code and verdict["source"] == source
    assert verdict["needs_human"] is False
    assert call.seen == [], "词表命中还去问模型，等于每张表都在烧 token"


def test_an_empty_reason_is_still_a_filling_error_not_a_hold():
    """一格都没填是**填错了**，仍然抛 —— 模型也无从判起。"""
    with pytest.raises(classify.RequestSheetError, match="诉求类型不能空"):
        classify.classify_reason("   ", _classifier())


def test_the_verdict_carries_the_actor_anchor_from_the_model_call():
    """模型判出来的那一格带着 `invocation_id` —— 落标注要拿它当 actor 锚点。"""
    verdict = classify.classify_reason(OFF_LEXICON, _classifier())
    assert verdict["source"] == "model" and verdict["reason"] == "wrong_item"
    assert verdict["invocation_id"] == FAKE_INVOCATION
    assert verdict["needs_human"] is False              # 0.93 高于阈值


@pytest.mark.parametrize("call, source", [
    (None, "fallback"),
    (_dead_classifier, "fallback"),
])
def test_without_a_working_model_the_word_is_held_not_guessed(call, source):
    """没模型 / 模型没产出 -> 落 unknown 挑去人工。**不猜**，也不抛。"""
    verdict = classify.classify_reason(OFF_LEXICON, call)
    assert verdict["reason"] == "unknown" and verdict["source"] == source
    assert verdict["needs_human"] is True and verdict["invocation_id"] == ""


# ==========================================================================
# 2. 房间那条：判不准进「待人工」，不进 problems
# ==========================================================================
def _parse(reason: str, *, classifier=None, order: str = "ORD-2026-0001"):
    data = (HEADER + f"{order},{reason},6800,2026-07-10,\n").encode()
    return sheet.parse(data, "x.csv", _ledger(), classifier=classifier)


def test_parse_without_a_classifier_behaves_exactly_as_before():
    """不传 classifier 时词表那条路**逐字照旧** —— 现有那批房间测试全都不传它。"""
    parsed = _parse("质量问题")
    row, = parsed.rows
    assert row.ok and row.req["reason"] == "quality_defect"
    assert row.verdict["source"] == "lexicon" and not row.pending
    assert parsed.pending == [] and parsed.invalid == []


def test_an_off_lexicon_word_is_judged_by_the_model_and_runs_on():
    """词表外 + 能用的模型 -> 判出来、照常预检，判据记成 `model`。"""
    parsed = _parse(OFF_LEXICON, classifier=_classifier())
    row, = parsed.rows
    assert row.ok and row.req["reason"] == "wrong_item"
    assert row.verdict["source"] == "model" and row.verdict["confidence"] == 0.93
    assert parsed.pending == []


def test_an_off_lexicon_word_without_a_model_is_held_not_reported_as_an_error():
    """词表外 + 没模型 -> 进「待人工」，**不进 problems**。

    这是本轨的题眼：`problems` 的语义是「这一行填错了，人回去改了再拖一次」，
    而判不准是「填得没错，是机器认不出」。混成一件事的后果是让人回去改一张
    没填错的表 —— 一个不存在的问题。
    """
    parsed = _parse(OFF_LEXICON)
    row, = parsed.rows
    assert row.problems == [], "判不准不是填错了"
    assert row.pending and row.needs_human_intake
    assert parsed.pending == [row] and parsed.invalid == []


def test_a_held_row_never_reaches_preflight():
    """判不准的行**不建 req**，于是进不了 `valid`，也就不会被送去预检。

    硬送的后果是最坏的那种：`unknown` 套不上任何一条政策，会走基线裁定直接批准 ——
    一个判不出诉求类型的单子被自动批款。
    """
    parsed = _parse(OFF_LEXICON)
    row, = parsed.rows
    assert row.req is None and row not in parsed.valid


def test_a_low_confidence_model_verdict_is_held_too():
    """模型判出来了但没把握 -> 一样挑去人工。判据走 `annotation.needs_human`。"""
    parsed = _parse(OFF_LEXICON, classifier=_classifier(confidence=0.4))
    row, = parsed.rows
    assert row.pending and row.verdict["source"] == "model"
    assert row.req is None


def test_the_hold_threshold_lives_only_in_annotation(monkeypatch):
    """挪 `annotation.HUMAN_REVIEW_THRESHOLD`，房间的判定跟着挪。

    跟不动就说明这一层自己写了一份阈值比较 —— 那种分叉的症状不是报错，
    是受理岗说要人工、房间事实卡说不用，两边都不报错。
    """
    call = _classifier(confidence=0.8)
    assert not _parse(OFF_LEXICON, classifier=call).pending      # 0.8 > 0.75

    monkeypatch.setattr(annotation, "HUMAN_REVIEW_THRESHOLD", 0.9)
    held = _parse(OFF_LEXICON, classifier=_classifier(confidence=0.8)).pending
    assert [r.line for r in held] == [2], "阈值挪了，房间这一层没跟着挪"


def test_a_row_that_is_also_filled_in_wrong_is_reported_as_an_error():
    """既判不准、又填错了别的 -> 按填错报。那张表本来就要改，改完再发也许就判得出来。"""
    parsed = _parse(OFF_LEXICON, order="ORD-9999-9999")
    row, = parsed.rows
    assert row.needs_human_intake and not row.pending
    assert parsed.invalid == [row] and parsed.pending == []


# ==========================================================================
# 3. 回帖：判不准单列一类，措辞与命令行一致
# ==========================================================================
def test_render_lists_the_held_rows_as_their_own_category():
    """回帖里判不准单列一段，措辞与 `run_requests.py::summarize` 逐字同一句。

    **刻意不说「改好后再发一次」** —— 那句是给填错的行的。这些行没填错。
    """
    parsed = _parse(OFF_LEXICON)
    out = sheet.render(parsed, {}, {}, decision_cn={})
    assert "诉求类型判不准，已挑出等人工确认，未进入处置：" in out
    assert f"第 2 行 ORD-2026-0001（{OFF_LEXICON}）" in out
    assert "有问题的行（改好后整张表再发一次）" not in out
    assert "判不准 1 行" in out


def test_render_of_a_clean_sheet_is_word_for_word_unchanged():
    """没有判不准的表，抬头一个字都不许多 —— 「判不准 0 行」会让人先去确认它是 0。"""
    out = sheet.render(_parse("质量问题"), {}, {}, decision_cn={})
    assert "共 1 行，可预检 1 行，有问题 0 行\n" in out + "\n"
    assert "判不准" not in out


def test_summary_says_held_instead_of_filled_in_wrong():
    """一行摘要要**说成判不准**：混进「填错」里，回话器会跟着劝人去改一张没填错的表。"""
    note = sheet.summary(_parse(OFF_LEXICON), {}, {})
    assert "0 行填错" in note
    assert "1 行诉求类型判不准、已挑出等人工确认，未进入处置" in note


def test_a_sheet_of_only_held_rows_is_not_reported_as_empty():
    """整张表都判不准时不许说「表里一行申请都没有」—— 那句是给空表的。"""
    out = sheet.render(_parse(OFF_LEXICON), {}, {}, decision_cn={})
    assert "表里一行申请都没有" not in out


# ==========================================================================
# 4. router：标注落进持久库
# ==========================================================================
def _handle_sheet(tmp_path, monkeypatch, reason: str, *, classifier, store=None):
    monkeypatch.setattr(classify, "make_classifier", lambda: classifier)
    data = (HEADER + f"ORD-2026-0001,{reason},6800,2026-07-10,\n").encode()
    adapter = _Adapter({"k": data})
    router = _router(tmp_path, adapter, store=store)
    return router, router.handle(_inbound("k", "requests.csv"))


def test_the_room_path_records_what_the_model_judged(tmp_path, monkeypatch):
    """判完落一行 `intake_annotation`，**查库断言** —— 这条链路上 store 是持久的。

    命令行那条每单一个 `:memory:` 运行时，落了跑完就没，接线时刻意没落。
    有了这一行，「模型当时把这个词认成了什么、多有把握、凭哪一次调用」才答得出来。
    """
    monkeypatch.setenv("MAOS_LLM_MODEL", "deepseek-chat")
    store = _store()
    router, reply = _handle_sheet(tmp_path, monkeypatch, OFF_LEXICON,
                                  classifier=_classifier(), store=store)

    rows = objects.query(store, "SELECT * FROM intake_annotation")
    assert len(rows) == 1
    got = rows[0]
    assert got["tenant_id"] == "tnt-demo" and got["case_id"] == "RC-ORD-2026-0001"
    assert got["field"] == "reason_code" and got["value"] == "wrong_item"
    assert got["raw_text"] == OFF_LEXICON and got["source"] == "model"
    assert got["confidence"] == 0.93 and got["model"] == "deepseek-chat"
    assert got["invocation_id"] == FAKE_INVOCATION
    assert "RC-ORD-2026-0001" in router._tickets, "判出来了就该照常挂待办"
    assert "预检结果" in reply


def test_the_case_id_matches_the_one_build_case_uses(tmp_path, monkeypatch):
    """标注的 `case_id` 与处置那条用的是同一条口径（``RC-<订单号>``）。

    两处不一致的症状是「受理岗写的标注与房间读的案子对不上号」，而两边都不报错。
    """
    from maos.ingress.router import _load_run_requests

    store = _store()
    _handle_sheet(tmp_path, monkeypatch, OFF_LEXICON,
                  classifier=_classifier(), store=store)
    built = _load_run_requests().build_case(_ledger(), {
        "order_id": "ORD-2026-0001", "reason": "wrong_item",
        "amount": None, "requested_at": "2026-07-10T00:00:00+00:00"})

    row, = objects.query(store, "SELECT * FROM intake_annotation")
    assert row["case_id"] == built["case"]["case_id"]
    assert row["tenant_id"] == built["case"]["tenant_id"]


def test_lexicon_and_fallback_rows_leave_no_annotation(tmp_path, monkeypatch):
    """词表命中与 fallback **不落标注**，也不建表。

    `record()` 的 `invocation_id` 是 actor 锚点、也是主键的一部分，而这两种情形
    压根没有那次调用。编一个合成 id 能过校验，却让审计链指向一条查不到的记录 ——
    正好是这张表存在理由的反面。词表命中本来就确定性可复现，不留痕也答得出来。
    """
    for reason, classifier in ((

            "质量问题", _classifier()), (OFF_LEXICON, None)):
        store = _store()
        _handle_sheet(tmp_path, monkeypatch, reason, classifier=classifier, store=store)
        names = {r["name"] for r in objects.query(
            store, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "intake_annotation" not in names, f"{reason} 这一行不该建表落库"


def test_a_held_row_still_gets_a_reply_and_no_ticket(tmp_path, monkeypatch):
    """没配 key 时房间里那一行长什么样：说清判不准、不挂待办、不预检。"""
    router, reply = _handle_sheet(tmp_path, monkeypatch, OFF_LEXICON, classifier=None)
    assert "诉求类型判不准，已挑出等人工确认，未进入处置：" in reply
    assert f"第 2 行 ORD-2026-0001（{OFF_LEXICON}）" in reply
    assert router._tickets == {}, "判不准的单子不许挂待办 —— 挂了就能被 /approve 放行"
    assert "看不懂的诉求类型" not in reply


def test_a_broken_annotation_write_does_not_sink_the_reply(tmp_path, monkeypatch, caplog):
    """标注写不进去要出声，但群里那份预检结果一个字都不该因此少。"""
    def boom(*a, **kw):                                 # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("库炸了")

    monkeypatch.setattr(router_mod.annotation, "record", boom)
    with caplog.at_level("WARNING"):
        router, reply = _handle_sheet(tmp_path, monkeypatch, OFF_LEXICON,
                                      classifier=_classifier())
    assert "预检结果" in reply and "RC-ORD-2026-0001" in router._tickets
    assert any("标注没落进库" in r.getMessage() for r in caplog.records)


# ==========================================================================
# 5. Excel 拖进群：仍然拒收，但给一句能行动的话
# ==========================================================================
#: 一份最小的 zip 容器头。`.xlsx` / `.docx` / `.pptx` / `.jar` / 任意 zip 的头四个
#: 字节**完全相同** —— 这正是不许把它加进 `ALLOWED_MIME` 的理由。
XLSX_HEAD = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 40


@pytest.mark.parametrize("head", [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"])
def test_zip_containers_are_recognised_for_the_message_only(head):
    assert attachments.zip_hint(head + b"\x00" * 32)
    assert "另存为" in attachments.zip_hint(head)


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,                # 白名单里的，不该沾这条
    b"\x7fELF" + b"\x00" * 32,
    b"PK" + b"\x99\x99" + b"\x00" * 32,                 # PK 开头但不是 zip 的四字节
    b"",
])
def test_non_zip_bytes_get_no_hint(data):
    assert attachments.zip_hint(data) == ""


def test_zip_is_still_rejected_but_the_message_says_what_to_do(tmp_path):
    """判出 zip 仍然**拒收** —— 只是把「认不出」换成「认出来了，要你做件 10 秒的事」。"""
    store = AttachmentStore(tmp_path)
    att = Attachment(channel=CHANNEL_FEISHU, file_key="k", filename="退款申请.xlsx",
                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with pytest.raises(AttachmentTypeRejected) as exc:
        store.put(XLSX_HEAD, att)
    assert "另存为" in str(exc.value) and "CSV" in str(exc.value)
    assert "zip 容器" in str(exc.value)
    assert list(tmp_path.rglob("*")) == [], "拒收就是拒收，一个字节都不许落盘"


def test_zip_never_enters_the_whitelist():
    """白名单里不许出现 zip / xlsx —— `sniff_mime` 只按魔数判，收下 `PK` 就是收下任意 zip。"""
    assert not any("zip" in m or "sheet" in m or "officedocument" in m
                   for m in attachments.ALLOWED_MIME)
    assert attachments.sniff_mime(XLSX_HEAD) == "", "zip 不许成为一个可落盘的类型"


def test_dropping_an_xlsx_into_the_room_tells_the_boss_what_to_do(tmp_path):
    """群里那一句：说了不收，也说了下一步。"""
    adapter = _Adapter({"k": XLSX_HEAD})
    router = _router(tmp_path, adapter)
    reply = router.handle(InboundMessage(
        channel=CHANNEL_FEISHU, chat_id="oc_1", sender="ou_boss", text="", msg_id="x1",
        attachments=(Attachment(channel=CHANNEL_FEISHU, file_key="k", kind="file",
                                filename="退款申请.xlsx"),)))
    assert "未收下 1 份" in reply and "退款申请.xlsx" in reply
    assert "另存为" in reply and "CSV UTF-8" in reply
    assert "只收 application/pdf" not in reply, "对着一串 MIME 名猜下一步，是这条要修的观感"


# ==========================================================================
# 6. 命令行那条：搬完之后行为不变（第 1 节自检第 3、4 条的回归判据）
# ==========================================================================
def test_the_command_line_entry_still_holds_the_off_lexicon_row(tmp_path):
    """`run_requests.read_sheet` 搬完之后仍然把词表外的行挑出来等人工。"""
    from maos.ingress.router import _load_run_requests

    rr = _load_run_requests()
    csv_path = tmp_path / "one.csv"
    csv_path.write_text(HEADER + f"ORD-2026-0001,{OFF_LEXICON},,2026-07-10,\n",
                        encoding="utf-8")
    # include_pending：本条要检查的正是那一行的内容，而 read_sheet 缺省不返回它
    # （整合修复：调用方漏检查标记的代价是自动批款，所以默认不给，想要的显式说）。
    req, = rr.read_sheet(csv_path, include_pending=True)
    assert req["needs_human_intake"] is True and req["reason"] == "unknown"
    assert rr.reason_basis(req) == "待人工"
    assert "诉求类型判不准，已挑出等人工确认，未进入处置" in rr.summarize([rr.pending_row(req)])


def test_the_command_line_summary_line_is_unchanged_without_held_rows(tmp_path):
    """没有判不准的单子时收口行**逐字不变** —— 那是回归基线断言的那一行。"""
    from maos.ingress.router import _load_run_requests

    rr = _load_run_requests()
    rows = [{"decision": "approve", "amount_approved": 6800.0, "status": "settled",
             "human_exits": 1, "needs_human_intake": False},
            {"decision": "reject", "amount_approved": 0.0, "status": "submitted",
             "human_exits": 0, "needs_human_intake": False}]
    assert rr.summarize(rows) == (
        "共 2 单：批准 1、驳回 1；已到账 1 单合计 6800.00 元；期间 1 次停下来等人放行。")
