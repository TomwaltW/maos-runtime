"""诉求类型的判定 —— 词表先判，认不出才问模型，模型也没有就挑去人工。

## 为什么住在包里，而不是 `scripts/run_requests.py`

这三个函数（:func:`make_classifier` / :func:`classify_reason` / :func:`_verdict`）
原本长在 `scripts/run_requests.py` 里，因为命令行那条入口是第一个用上它们的。
但房间那条入口（`maos.ingress.sheet`）也要用同一套判据，而**包不该 import
`scripts/`** —— `router._load_run_requests` 那条 importlib 加载是为了复用一个
已经跑绿的既有脚本，不是可以顺手扩大的依赖方向。同一个理由让 T101/T102 把词表
与表头别名收进了 skill：判据只许有一处，两处会各自长大，而症状是「命令行认、
房间不认」，两边都不报错。

所以搬到这里，`scripts/run_requests.py` 反过来从这里 import。搬的那一步**只挪
位置**：函数体逐字照旧，回归判据是 `scripts/run_requests.py` 的收口行与
「挑出 …… 诉求类型判不准，等人工」那一行（`maos/tests/test_request_sheet.py`）。

## 判不准 ≠ 填错了

这是本模块唯一的取向，两条入口都照它办：

  · **填错了**（诉求类型一格没填、日期看不懂、金额是负数）-> 让人回去改表。
  · **判不准**（写了「漏发了两个」，词表没这个词、模型也没敢定）-> 表没填错，
    是机器认不出。让人回去改一张没填错的表，是把人指向一个不存在的问题。

后者的出口是「挑出来等人工确认，不进入处置」—— 不是报错，也**不许**硬着头皮
按 `unknown` 往下跑：`unknown` 套不上任何一条政策，硬跑会走基线裁定直接批准，
而「一个判不出诉求类型的单子被自动批款」是这条链路上最坏的失败。
"""

from __future__ import annotations

import logging

from maos.domain.refund.annotation import needs_human
from maos.skills.builtin.refund.reason_classify import LEXICON, UNKNOWN

#: 老板会写的说法 -> 系统里的诉求类型。**权威定义在 skill 里**，这里只取过来用：
#: 两份词表并存的症状是它们会各自长大，然后同一个词在命令行与房间里判成两个 code。
REASONS: dict[str, str] = LEXICON


class RequestSheetError(ValueError):
    """申请表里有填不对的地方。消息直接给人看。

    与 :func:`classify_reason` 一起搬过来的：它是那个函数抛的类型，留在 scripts/
    的话，包里的调用方要 import scripts 才接得住 —— 正是这次搬迁要消掉的方向。
    `scripts/run_requests.py` 改成从这里 import 同一个类，`rr.RequestSheetError`
    仍然是它，两条入口 except 的是同一个东西。
    """


def make_classifier():
    """词表/别名认不出时的模型兜底。**拿不到真模型就返回 None，这不是故障。**

    只在三个环境变量齐备、`select_model_client()` 真给出 `GatewayModelClient`
    时才接线。理由是 `ScriptedModelClient.complete()` 恒返 `"{}"`，喂给 skill 只会
    抛 ValueError —— 而本脚本对外的承诺是「无 key、零出网」，不能因为接了模型就
    在没配 key 的机器上开始报错。没接上的后果是词表外的词落 `unknown` 挑去人工，
    其余行照常跑完，这正是「判不准就别猜」该有的结果。

    `store` 传 None：本脚本每单一个 `:memory:` 库，成本账落进去跑完就没了。
    `record_model_usage` 见 None 直接跳过（`core/store.py:572`），不抛。
    """
    from maos.agents.refund.intake_agent import RefundIntakeAgent
    from maos.model.client import GatewayModelClient, select_model_client
    from maos.skills.invoker import SkillInvoker

    model = select_model_client()
    if not isinstance(model, GatewayModelClient):
        return None
    identity = RefundIntakeAgent.identity
    invoker = SkillInvoker(identity, None)
    extras = {"model": model, "tier": identity.model_tier}

    def call(name: str, payload: dict) -> dict | None:
        """调一次 skill。**任何失败都返回 None**，由调用方落回 fallback。

        模型挂了、超时了、输出不合契约（invoker 已按 failure_policy 重试过一次），
        都不该让一张表读不下去 —— 那一列的结果是「这单要人看」，本来就是安全出口。
        """
        try:
            res = invoker.invoke(name, payload, extras=dict(extras))
        except Exception as exc:                       # noqa: BLE001
            # logger 名留作 `run_requests` 不动：搬迁这一步只挪位置，改名就不是
            # 逐字搬了，而现有的日志过滤规则认的是这个名字。
            logging.getLogger("run_requests").warning("%s 调用失败：%s", name, exc)
            return None
        if res.status != "ok" or not isinstance(res.output, dict):
            logging.getLogger("run_requests").warning("%s 未产出结果：%s", name, res.error)
            return None
        return res.output

    return call


def classify_reason(raw: str, classifier=None) -> dict:  # noqa: ANN001
    """判诉求类型。返回 ``{reason, source, confidence, why, invocation_id, needs_human}``。

    三段，顺序不能反：词表命中直接用（**一次模型都不调**）；认不出才问模型；
    没模型就落 `unknown`。

    **判不出来不抛**：那是「这一单要人看」，不是「这张表填错了」，两者的处置
    完全相反 —— 前者其余行照跑，后者才该让人回去改表。空的诉求类型仍然抛，
    它确实是填错了（一格都没填，模型也无从判起）。
    """
    text = raw.strip()
    if not text:
        raise RequestSheetError("诉求类型不能空 —— 不知道为什么退，就套不上任何一条政策")

    if text in REASONS:
        return _verdict(REASONS[text], 1.0, "词表直接命中", "lexicon")
    if text in set(REASONS.values()):
        return _verdict(text, 1.0, "原文就是一个合法 code", "lexicon")

    if classifier is None:
        return _verdict(UNKNOWN, 0.0, "词表未命中，且没有可用的模型（未配 key）", "fallback")

    out = classifier("refund.reason_classify", {"text": text})
    if out is None:
        return _verdict(UNKNOWN, 0.0, "词表未命中，模型调用没有产出结果", "fallback")
    return _verdict(str(out.get("reason_code") or UNKNOWN),
                    float(out.get("confidence") or 0.0),
                    str(out.get("why") or ""), str(out.get("source") or "model"),
                    invocation_id=str(out.get("invocation_id") or ""))


def _verdict(code: str, confidence: float, why: str, source: str,
             *, invocation_id: str = "") -> dict:
    """一条诉求类型判据。`needs_human` 走 `annotation.needs_human` —— **整仓唯一定义**，
    这里不自己比阈值，否则命令行与房间会各有一套「算不算低置信度」。

    `invocation_id` 是模型那次调用的 actor 锚点，由 skill 原样回显在 output 里
    （`refund.reason_classify` 的 output_schema 有这一项）。房间那条入口要把这一格
    的判定落进 `intake_annotation`，而那张表的主键带着它 —— 编一个合成 id 能过校验，
    却让审计链指向一条查不到的记录（见 `annotation.py::_require_invocation_id`）。
    词表命中与 fallback 压根没有那次调用，这里就是空串，落库的调用方按它决定落不落
    （见 `maos/ingress/router.py::_record_reason_annotations`）。**多这一个键不改变
    任何既有判读**：命令行那条只取 reason / source / confidence / why / needs_human。
    """
    return {"reason": code, "source": source, "confidence": confidence, "why": why,
            "invocation_id": invocation_id,
            "needs_human": needs_human(confidence, source, code)}
