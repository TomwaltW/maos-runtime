"""复检在房间里看得出来 —— 冒烟两轮、预告措辞（T100）。

本轨只**消费** `locate_order` / `AttachmentBuffer.take` / `round_no` 三份跨轨契约，
自己一份都不实现。所以这里钉的是本轨自己做的两件事：

  1. 冒烟脚本的 `--recheck`：同一单先无证据过一轮、补上证据再过一轮，末尾并排
     给出两轮收口卡结论。🔴 **不带这个开关时 stdout 逐字节不变** —— 范式同
     `test_room_team_fixture.py` 的 `LEGACY_STDOUT_MD5`（一条给可读 diff，
     一条给指纹）；
  2. `_ChairTeam` 的预告措辞：`round_no >= 2` 那一轮读起来必须与首检不一样，而
     `round_no=1` 与**不传**逐字相同 —— T99 并入之前，房间里走的全是后者，
     那条路的观感一个字都不该变。

与 `test_room_team_fixture.py` 不同，这份**要** import `hiclaw`：预告措辞就长在
那个模块里。真房间那条路仍然一步不走 —— 假通道、假圆桌、零网络零模型零 env。

引擎认不认 `round_no` 用假引擎钉死两种形状，不靠真引擎当时的样子：真引擎今天
不认（基线）、T99 并入后认，拿它当判据的测试会在并轨那天以「测试红了」的形态
报一件其实正常的事。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hiclaw import room_ingress

ROOT = Path(__file__).resolve().parents[2]
CUSTOM = ROOT / "scenarios" / "custom"
LEDGER = CUSTOM / "ledger.json"
TEAM_SHEET = CUSTOM / "refund-requests-team.csv"
EVIDENCE = CUSTOM / "evidence"
SMOKE = ROOT / "scripts" / "room_team_smoke.py"

#: `python3 scripts/room_team_smoke.py`（不带任何参数）的 stdout 指纹，
#: 2026-09-11 在 38eaf88 + 底账加 ORD-2026-0007 那笔改动上实跑所得
#: （上一版 29ad0381… 是 2026-09-10 在 7bb29ab 上取的，再上一版 7ca73cc0…
#: 是 2026-09-05 在 c2f06cc 上取的）。
#:
#: 指纹这次是**该变的**，且变动面只有一处字样。底账 `order_snapshot` 末尾加了
#: ORD-2026-0007（房间里专演「网关失败 → 开工单 → /assign → /resolve」那一单），
#: 它与老三新单同属客户 `CUS-2026-0042`；而
#: `skills/builtin/refund/risk_screen.py::multi_order_same_account` 数的是
#: **底账里的订单快照**，不是申请表的行 —— 所以哪怕 0007 不进
#: `refund-requests-team.csv`，风险岗那句也一定跟着涨 1。改前改后逐行 diff 原文：
#:
#:     -  命中信号：…；同一客户名下有 3 笔订单快照，存在批量退款的可能
#:     +  命中信号：…；同一客户名下有 4 笔订单快照，存在批量退款的可能
#:     -  同账号关联订单：3 单，近 30 天退款申请 N 次  [事实卡]
#:     +  同账号关联订单：4 单，近 30 天退款申请 N 次  [事实卡]
#:
#: 除这两行（及末尾风险摘要里同一句的重复）之外**逐字节未变**：风险档位、评分、
#: 裁定、金额、五岗措辞全部原样。同日给 `product_snapshot` 补的
#: `SKU-VLV-118` 那一行对本指纹**零影响**（加行前后实测同为本值，均 15024 字节）——
#: 这份语料的五岗发言不读商品快照。
#:
#: 它红了先看 `test_recheck_without_evidence_says_so_and_changes_nothing_else`：
#: 那条给的是能读的 diff，这条只说「变了」。只有这一条红、那条绿，说明动的
#: 不是本轨（多半是圆桌引擎那侧改了发言措辞），去看 git log 再判断。
PLAIN_STDOUT_MD5 = "1c84e3bc1e61abdc1d00302f5f8df72b"

#: 演示语料里配得上证据的行数：0004 一行 + 0006 两行（`refund-requests-team.csv`）。
#: 0005 那两行故意一张图都不配 —— 它是 `need_more` 唯一的演出场地。
RECHECKED_ROWS = 3

SEATS = "（申请受理岗 → 规则审核岗）"
BOSS = "@boss:example.org"
FIRST_ROUND_NOTICE = f"五岗正在过这一单{SEATS}，请稍候"


def _load_script(name: str):
    """加载 `scripts/` 下的脚本。范式与 `test_room_team_fixture.py` 逐字一致。"""
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load_script("room_team_smoke")


@pytest.fixture()
def attachments(tmp_path, monkeypatch):
    """附件落 tmp，不落仓库的 `var/attachments/`。测试不该给工作区留下东西。"""
    monkeypatch.setattr(smoke, "ATTACHMENT_ROOT", tmp_path / "attachments")
    return tmp_path


def _recheck(out, **kw) -> int:
    return smoke.run(TEAM_SHEET, LEDGER, out=out, evidence_dir=EVIDENCE,
                     recheck=True, **kw)


# ---------------------------------------------------------------- 1 老路不动
def test_recheck_without_evidence_says_so_and_changes_nothing_else():
    """`--recheck` 一份证据都没配上时：多报一行人话，**其余逐行相同**。

    这条与下一条指纹是一对：它给可读的 diff，指纹给「变了没有」。
    静默按单轮跑是本脚本处处在躲的那种失败 —— 参数没生效与参数生效了
    在屏幕上长得一模一样，而前者是人能自己修的。
    """
    plain = io.StringIO()
    assert smoke.run(TEAM_SHEET, LEDGER, out=plain) == smoke.EXIT_OK

    lonely = io.StringIO()
    assert smoke.run(TEAM_SHEET, LEDGER, out=lonely, recheck=True) == smoke.EXIT_OK

    assert smoke.NO_RECHECK_TARGET in lonely.getvalue(), "没证据可复检，得说出来"
    stripped = [ln for ln in lonely.getvalue().splitlines()
                if ln != smoke.NO_RECHECK_TARGET]
    assert stripped == plain.getvalue().splitlines(), (
        "`--recheck` 在没有复检对象时改动了单轮那条路的输出")


def test_plain_smoke_stdout_is_byte_identical_to_the_baseline():
    """🔴 不带 `--recheck` 时 stdout **逐字节**与基线相同。判据是字节，不是解析后的对象。"""
    proc = subprocess.run([sys.executable, str(SMOKE)],
                          cwd=str(ROOT), capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert hashlib.md5(proc.stdout).hexdigest() == PLAIN_STDOUT_MD5, (
        "不带 --recheck 的输出变了；先看上一条断言的 diff 再判断是谁动了")


# ---------------------------------------------------------------- 2 两轮演得出
def test_recheck_plays_two_rounds_and_compares_the_two_verdicts(attachments):
    """配得上证据的那几单跑两轮，末尾并排给出两轮结论；配不上的照旧一轮。

    末行那句 `第 1 轮 need_more → 第 2 轮 approve` 是这个开关的**全部意义**：
    补一张图之后结论会不会改口。两个结论都来自 `decide()`，脚本自己不下判断。
    它随合议真值表变 —— 红了先跑一遍 `--recheck` 看新结论对不对，再改这里。
    """
    out = io.StringIO()
    assert _recheck(out) == smoke.EXIT_OK
    text = out.getvalue()

    assert text.count(smoke.TAG_ROUND_1) == RECHECKED_ROWS
    assert text.count(smoke.TAG_ROUND_2.format(n=1)) == RECHECKED_ROWS
    assert "Traceback" not in text

    assert "ORD-2026-0004：第 1 轮 need_more → 第 2 轮 approve" in text, (
        "证据齐那一单补图之后没翻成 approve —— 复检这件事就演不出来了")
    assert "ORD-2026-0006：第 1 轮 need_more → 第 2 轮 escalate" in text, (
        "大额那一单证据齐了仍该是升级审批：风险岗只提示不裁定")

    # 🔴 一条消息只触发一轮（四轨共同不变量）：配不上证据的单不许多演一遍。
    assert text.count("第 3 行 · ORD-2026-0005") == 1
    assert text.count("第 6 行 · ORD-2026-0005") == 1


def test_recheck_second_round_is_the_one_that_carries_the_evidence(attachments):
    """第 1 轮的随案证据是 **0 份**、第 2 轮才是 1 份 —— 两轮各自重新建 case。

    第 1 轮不是「把第 2 轮的证据藏起来」，它就是这一单刚进来时的样子。
    两轮共用一个 payload 的症状是第 1 轮也带着证据，而屏幕上照样打「第 1 轮」。
    """
    out = io.StringIO()
    assert _recheck(out) == smoke.EXIT_OK

    # 抬头行自己占一块（前后各一条 `=` 横线），发言正文在**下一块**。
    blocks = out.getvalue().split("=" * 78)
    head1 = next(i for i, b in enumerate(blocks) if smoke.TAG_ROUND_1 in b)
    head2 = next(i for i, b in enumerate(blocks)
                 if smoke.TAG_ROUND_2.format(n=1) in b)

    assert "随案证据：0 份" in blocks[head1 + 1], "第 1 轮就带着证据 = 两轮共用了 payload"
    assert "随案证据：1 份" in blocks[head2 + 1]


# ---------------------------------------------------------------- 3 退化路径
def test_recheck_degrades_instead_of_raising_when_roundtable_missing(monkeypatch):
    """圆桌引擎缺席时 `--recheck` 走的仍是既有那条退化：人话 + exit 3，不抛栈。"""
    monkeypatch.setattr(smoke, "load_team", lambda: None)
    out = io.StringIO()

    code = smoke.run(TEAM_SHEET, LEDGER, out=out, evidence_dir=EVIDENCE, recheck=True)

    assert code == smoke.EXIT_NO_ROUNDTABLE == 3
    assert smoke.NO_ROUNDTABLE in out.getvalue()
    assert "Traceback" not in out.getvalue()


def test_no_flip_line_when_the_verdict_engine_is_missing(attachments, monkeypatch):
    """收口卡算不出来时**整行不打**，不打「None → None」。

    「None → None」看着像「两轮都没结论」，实际是「合议引擎没装载」——
    两件事该做的反应完全不同（前者去查真值表，后者去查部署）。
    """
    monkeypatch.setattr(smoke, "load_decide", lambda: None)
    out = io.StringIO()

    assert _recheck(out) == smoke.EXIT_OK
    text = out.getvalue()

    assert text.count(smoke.TAG_ROUND_1) == RECHECKED_ROWS, "两轮照跑，只是没有卡"
    assert "→ 第 2 轮" not in text, "两轮都没结论时那一行整条不打"
    assert "None →" not in text and "→ None" not in text


# ---------------------------------------------------------------- 4 探参数
class _Report:
    """`StageReport` 的同形假件：排版与核对只读这几个字段。"""

    def __init__(self, agent_id: str, title: str, speech: str) -> None:
        self.agent_id, self.title, self.speech = agent_id, title, speech
        self.data: dict = {}
        self.spoken_by_model = False


class _OldEngine:
    """T99 并入**之前**的圆桌：`on_preflight` 是 keyword-only 具名参数，没有 `**kw`。

    形状照 `maos/roundtable/team.py` 基线那一版来 —— 多传一个键当场 `TypeError`。
    所以这份假件同时也是「不探就传会炸」的反面证明。
    """

    def __init__(self, model, voices, *, ledger_loader=None) -> None:  # noqa: ANN001
        self._voices = voices
        self.seen: list[dict] = []

    def _speak(self, evidence: list) -> list:
        reports = []
        for agent_id, title in (("refund-intake", "申请受理岗"),
                                ("refund-evidence", "证据核验岗")):
            speech = f"{title}：随案 {len(evidence or [])} 份"
            self._voices.voice(agent_id).say(speech)
            reports.append(_Report(agent_id, title, speech))
        return reports

    def on_preflight(self, *, payload, checked, ledger, evidence, requested_by):
        self.seen.append({"evidence": len(evidence or [])})
        return self._speak(evidence)


class _NewEngine(_OldEngine):
    """T99 并入**之后**的形状：两个带缺省值的新关键字参（跨轨契约 §3）。"""

    def on_preflight(self, *, payload, checked, ledger, evidence, requested_by,
                     round_no: int = 1, added_evidence: int = 0):
        self.seen.append({"evidence": len(evidence or []),
                          "round_no": round_no, "added_evidence": added_evidence})
        return self._speak(evidence)


def _install(monkeypatch, engine_cls) -> list:
    """把假引擎装成 `maos.roundtable.team` 那个模块的位置，返回建出来的实例清单。"""
    built: list = []

    class _Engine(engine_cls):
        def __init__(self, model, voices, *, ledger_loader=None) -> None:  # noqa: ANN001
            super().__init__(model, voices, ledger_loader=ledger_loader)
            built.append(self)

    monkeypatch.setattr(smoke, "load_team", lambda: types.SimpleNamespace(
        RefundRoundtable=_Engine,
        TITLES={"refund-intake": "申请受理岗", "refund-evidence": "证据核验岗"},
        TEAM_ORDER=("refund-intake", "refund-evidence")))
    return built


def test_recheck_does_not_pass_round_no_to_an_engine_that_lacks_it(attachments,
                                                                  monkeypatch):
    """🔴 老签名的引擎：**不传**那两个参、报一行说明、两轮照跑。

    探法与 `--pace` 那段同一个范式（`inspect.signature`）。不探就传的后果不是
    少一句话：`TypeError` 会炸在第一单上，`--recheck` 整条路当场不可用。
    """
    built = _install(monkeypatch, _OldEngine)
    out = io.StringIO()

    assert _recheck(out) == smoke.EXIT_OK
    text = out.getvalue()

    assert smoke.NO_ROUND_NO in text, "退化了就得说出来，静默降级看不出来"
    assert text.count(smoke.TAG_ROUND_1) == RECHECKED_ROWS, "轮次照打 —— 那是脚本的排版"
    assert "Traceback" not in text
    assert [c["evidence"] for c in built[0].seen[:2]] == [0, 1], "两轮：先 0 份、后 1 份"


def test_recheck_passes_both_new_kwargs_to_an_engine_that_accepts_them(attachments,
                                                                      monkeypatch):
    """新签名的引擎：`round_no` 与 `added_evidence` **一起**传，且份数与实际相符。

    只传 `round_no` 而漏了 `added_evidence`，证据核验岗按缺省 0 说「本轮新增 0 份」
    —— 屏幕上明明配了图，那是一句看着像事实的假话。
    """
    built = _install(monkeypatch, _NewEngine)
    out = io.StringIO()

    assert _recheck(out) == smoke.EXIT_OK
    assert smoke.NO_ROUND_NO not in out.getvalue(), "引擎认这个参数，不该报退化"

    seen = built[0].seen
    assert seen[0] == {"evidence": 0, "round_no": 1, "added_evidence": 0}
    assert seen[1] == {"evidence": 1, "round_no": 2, "added_evidence": 1}

    # 五行申请表、三行复检 = 八次调用。配不上证据的单只跑一轮，且**不传**新参
    # （引擎按缺省 1 待它）—— 单轮不是「第 1 轮」，它就是这一单的全部。
    assert [c["round_no"] for c in seen] == [1, 2, 1, 1, 2, 1, 2, 1]
    assert [c["added_evidence"] for c in seen] == [0, 1, 0, 0, 1, 0, 1, 0]


def test_accepts_round_no_says_no_when_the_signature_cannot_be_read():
    """签名读不到就当不支持 —— 口径同 `make_roundtable` 对 `pace` 的探法。"""

    class _Opaque:
        on_preflight = staticmethod(print)              # C 实现，签名读不出来

    assert smoke.accepts_round_no(_Opaque()) is False
    assert smoke.accepts_round_no(object()) is False, "连方法都没有也只是返回 False"


# ------------------------------------------------------------ 4.5 判单第 ① 级
# ------------------------------------------------ 4.6 批量路径上的空审批人（T138）
#: 政策没写审批人时，收口卡该点到的那个岗。**不写字面量**：口径的单一出处是角色
#: 目录，`test_roundtable_verdict.py::test_the_demotion_target_is_the_directory_default_approver_seat`
#: 钉的就是「降级落在 `roles.DEFAULT_APPROVER_SEAT` 上」这件事。
def _default_approver_seat() -> str:
    from maos.domain.refund import roles

    return roles.verdict_role_of(roles.DEFAULT_APPROVER_SEAT)


def _one_row_sheet(tmp_path, order_id: str):
    """从演示语料里抄出**一行**，落 tmp。

    `scenarios/custom/refund-requests-team.csv` 是禁动面（跨轨契约 §A.2：动它
    `PLAIN_STDOUT_MD5` 就变，而本波谁都不许刷那个值）。所以这里抄一行出来单跑，
    不给语料加第六行 —— 抄的是同一份表头与同一行原文，走的也是同一条批量路径，
    只是把分母缩到一单，跑完 ~0.2 秒。
    """
    src = TEAM_SHEET.read_text(encoding="utf-8").splitlines()
    row = next(ln for ln in src[1:] if ln.startswith(order_id))
    sheet = tmp_path / f"one-row-{order_id}.csv"
    sheet.write_text(f"{src[0]}\n{row}\n", encoding="utf-8")
    return sheet


def _play_one(tmp_path, monkeypatch, order_id: str, *, blank_approver: bool) -> str:
    """跑一单，返回 stdout。`blank_approver` 时把政策那一格抹空。

    抹的是 `maos.ingress.router.preflight` 的返回值而不是政策规则文件：
    要演的是「政策没写 `approver_role`」这一格，而 `preflight` 的产物正是圆桌
    唯一读得到它的地方（`verdict._approver` 读 `seats["refund-policy"]`）。
    改语料或改政策规则都会牵动别的判据，改这一格谁都不碰。

    **monkeypatch 打在 `router` 模块上**，不是打在 `smoke` 上：`run()` 里那句
    `from maos.ingress.router import preflight` 是函数内 import，每次调用重新取名字。
    """
    import maos.ingress.router as router

    if blank_approver:
        real = router.preflight
        monkeypatch.setattr(
            router, "preflight",
            lambda payload: {**real(payload), "approver_role": ""})

    out = io.StringIO()
    assert smoke.run(_one_row_sheet(tmp_path, order_id), LEDGER, out=out,
                     evidence_dir=EVIDENCE) == smoke.EXIT_OK
    return out.getvalue()


def test_the_bulk_path_never_invents_an_approver_when_the_policy_omits_one(
        attachments, monkeypatch, caplog):
    """🔴 **批量路径上**政策没写审批人时，房间那句话仍点得到人（T138 补的盲区）。

    T136 把这条病修在 `verdict._approver` 里，判据钉在
    `test_roundtable_verdict.py::test_an_empty_approver_role_is_demoted_to_the_default_seat`
    上 —— 那条够用，但它只走**单元**那条路。批量路径（`room_team_smoke.run`）上一条
    判据都没有，原因很具体：演示语料每一单的政策规则都写了 `approver_role`（实测五
    单全是 `supervisor`），所以改前改后 `PLAIN_STDOUT_MD5` 逐字节相同 —— **那条病
    在这条路径上从来没被任何判据碰到过**。真房间的真政策就可能缺这个字段
    （脱敏真实需求 9/14 到手，格式不一定齐），而那时撞上的正是这条路径。

    期望措辞逐字照 `test_an_empty_approver_role_is_demoted_to_the_default_seat`：
    降到缺省审批岗 + 一条 WARNING，**不是**原样留空（会渲染成「请  拍板」，两个
    空格，一句话没了主语），**也不是**猜一个别的岗。三件事各有一条断言。
    """
    seat = _default_approver_seat()
    text = _play_one(attachments, monkeypatch, "ORD-2026-0004", blank_approver=False)
    assert f"请 {seat} 拍板" in text, "对照组：语料原样跑时政策写了审批人，本就点得到人"

    with caplog.at_level(logging.WARNING, logger="maos.roundtable"):
        blank = _play_one(attachments, monkeypatch, "ORD-2026-0004",
                          blank_approver=True)

    # ① 政策缺了这一格这件事**如实说出来**，不被降级悄悄补平。
    assert "放行需要的审批角色：未指定" in blank, (
        "政策没写审批人时规则审核岗仍报了一个角色 —— 那是把降级的结果当成政策原文")
    # ② 收口卡照旧点得到人，且点的就是目录的缺省审批岗。
    assert f"请 {seat} 拍板" in blank, (
        f"空审批人没降到缺省审批岗 {seat!r}，房间里那句话点不到人")
    # ③ 不许渲染成「请  拍板」（两个空格）—— T136 修之前就是这个样子。
    assert "请  " not in blank, "又渲染成「请  拍板」了（两个空格），收口卡没了主语"
    # ④ 降级留痕：看得见的那一半在日志里。
    assert any("未指定审批人" in r.getMessage() for r in caplog.records), caplog.text


def test_the_bulk_path_still_escalates_from_the_default_seat(attachments, monkeypatch):
    """高风险那一单同样从缺省岗继续往上升 —— 降级不许变成一条比写错还宽松的路。

    口径与 `test_an_empty_approver_role_is_demoted_to_the_default_seat` 末尾那半条
    逐字相同（`escalated_empty.headline` 那句）。单独一条是因为两支落点不同：
    approve 那支停在缺省岗，escalate 那支要从缺省岗再往上走一级。合成一条的话，
    升档整个失效时只有一半断言会红，而读红的人会以为是降级坏了。
    """
    text = _play_one(attachments, monkeypatch, "ORD-2026-0006", blank_approver=True)

    assert "建议升级审批 · 风险 high · 请 finance_manager 复核" in text, (
        "空审批人 + 高风险该升到 finance_manager，卡在缺省岗说明升档被降级吃掉了")
    assert "请  " not in text


def test_order_of_keeps_the_prefix_rule_that_t97_imports():
    """🔴 判单四级的第 ① 级就是这个函数（跨轨契约 §1）—— T97 会 import 它。

    本轨**不改它的行为**，只把口径钉下来：长的订单号先匹配、前缀之后必须紧跟
    `-` 或 `.`。`ORD-1` 抢走 `ORD-10-x.png` 这种错**不报错** —— 图挂到另一单
    头上，而那一单的证据核验岗照样算得自洽，屏幕上看不出任何异样。
    """
    orders = ("ORD-1", "ORD-10", "ORD-2026-0004")

    assert smoke.order_of("ORD-2026-0004-rust.png", orders) == "ORD-2026-0004"
    assert smoke.order_of("ORD-2026-0004.png", orders) == "ORD-2026-0004"
    assert smoke.order_of("ORD-10-x.png", orders) == "ORD-10", "长的先匹配，不许张冠李戴"
    assert smoke.order_of("ORD-100-x.png", orders) == "", "ORD-1 不许吃掉 ORD-100 的图"
    assert smoke.order_of("ORD-2026-00041.png", orders) == "", "分隔符必须紧跟前缀"
    assert smoke.order_of("IMG_8821.jpg", orders) == "", "认不出就是认不出，不猜"


# ---------------------------------------------------------------- 5 预告措辞
class _Channel:
    """形状对齐 `_NioChannel` 的 `send`。范式同 `test_room_ingress.py::_Channel`。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, plain: str, html: str) -> None:
        self.sent.append((plain, html))


class _SilentTeam:
    """圆桌假件：什么都不说，只把收到的 kw 记下来 —— 本节测的是预告，不是发言。"""

    def __init__(self) -> None:
        self.seen: dict = {}

    def on_preflight(self, **kw):
        self.seen = kw
        return []


def _notice(**kw) -> tuple[str, dict]:
    ch, team = _Channel(), _SilentTeam()
    chair = room_ingress._ChairTeam(team, ch, seats=SEATS)
    chair.on_preflight(payload={}, checked={}, ledger={}, evidence=[],
                       requested_by=BOSS, **kw)
    return ch.sent[0][0], team.seen


def test_first_round_notice_is_word_for_word_unchanged():
    """🔴 `round_no=1`、`None` 与**不传**三种写法逐字相同 —— T99 并入前全走这条。"""
    plain, _ = _notice()
    assert plain == FIRST_ROUND_NOTICE
    assert _notice(round_no=1)[0] == FIRST_ROUND_NOTICE
    assert _notice(round_no=None)[0] == FIRST_ROUND_NOTICE


def test_recheck_notice_reads_as_a_second_pass_over_the_same_case():
    """复检那一轮要读得出「第二遍、因为有新证据」，不是把首检那句再刷一遍。"""
    plain, _ = _notice(round_no=2)

    assert plain != FIRST_ROUND_NOTICE
    assert "收到新证据" in plain and "复检" in plain and "第 2 轮" in plain
    assert SEATS in plain, "岗位串照挂 —— 预告里那串名牌是房间的观感基线"
    assert _notice(round_no=3)[0].count("第 3 轮") == 1


def test_notice_never_raises_on_a_round_no_it_did_not_expect():
    """`"2"` 认、`"第二轮"` 按首检待 —— 都不许抛。

    抛了不是少一句话：异常一路抛出这个钩子、落进 router 的 except，
    五岗**整轮**哑掉，而房间里只剩一条指不到原因的 WARNING。
    """
    assert "第 2 轮" in _notice(round_no="2")[0]
    assert _notice(round_no="第二轮")[0] == FIRST_ROUND_NOTICE
    assert _notice(round_no=0)[0] == FIRST_ROUND_NOTICE
    assert _notice(round_no=[])[0] == FIRST_ROUND_NOTICE


def test_chair_still_forwards_round_no_verbatim_to_the_roundtable():
    """🔴 读它换措辞，**不**把它从转发里吃掉：`**kw` 那个形状归 router 那侧定。"""
    _, seen = _notice(round_no=2, added_evidence=3)

    assert seen["round_no"] == 2 and seen["added_evidence"] == 3
    assert set(seen) == {"payload", "checked", "ledger", "evidence", "requested_by",
                         "round_no", "added_evidence"}
