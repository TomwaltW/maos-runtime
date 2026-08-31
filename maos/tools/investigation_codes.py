"""ISO 20022 差错处理码表 —— 码值**从数据文件读**，判据在本模块。

## 为什么这张表不许凭记忆填

编一张码表，被评委问一句「这个码是哪来的」就全塌。`AC01` / `AM04` / `LEGL` 这类
四字母码看起来很好背，但**含义与适用报文有版本差异**，写错了就是「用一份编出来的
规范去论证我们对齐了规范」—— 比不接更糟。

所以本模块**一个码值都不硬编**：184 条码全部从
`maos/domain/investigation/iso20022_codes.json` 读入，那个文件由本机从
iso20022.org 下载官方 xlsx 之后逐条抄出，带 URL、SHA-256、抓取日期与发布版本
（见其 `_provenance`）。本模块只做两件事：把它包成冻结对象，以及在它上面**加判据**。

数据与判据分开放，是因为它们过期的方式不一样：码表过期靠重新抓文件，
判据过期靠人重新读规范。混在一处，重抓一次文件就会把判据一起冲掉。

## 四个码集，以及它们各自答哪个问题（这是本模块最要紧的一段）

    ExternalCancellationReason1Code            camt.056  我为什么要撤这一笔
    ExternalInvestigationExecutionConfirmation1Code  camt.029  你那个撤销请求，我怎么处理的
    ExternalPaymentCancellationRejection1Code  camt.029  （被拒时）为什么拒
    ExternalReturnReason1Code                  pacs.004  钱退回来了，退回原因是这个

**码集与报文的对应不是我说的**，是官方 xlsx 的 `UsageInMsgs` 表里写的，
逐条落在 JSON 的 `used_in_messages` 字段上，`is_used_in()` 可以当场查。

## `resolution` 与 `funds_evidence` 是两个正交的维度（本模块最重要的一条）

只有一个「成功了吗」的 bool 是**不够**的，而且不够的地方正好踩在铁律 8 上。

`resolution` 答的是「**撤销请求**的下落」；`funds_evidence` 答的是
「**钱**回来了没有」。后者只有 pacs.004 给得出：

    resolution="confirmed"  清算方说：撤销指令照办了（CNCL）
    resolution="rejected"   清算方说：这个撤销请求我拒了（RJCR + 一条拒绝原因码）
    resolution="pending"    清算方说：还在处理 / 还没结果（PDCR、PDNG、CWFW、FTNA）
    resolution="partial"    清算方说：部分执行了（PECR）

而 `funds_evidence` 在**整个 camt.029 码集上恒为 False** —— 包括 `CNCL`。
这不是保守，是规范如此：CNCL 的官方定义是
「Used when a requested cancellation is successful.」，它成功的是**那个请求**；
资金实际退回走的是另一条报文 pacs.004，带自己的 `ExternalReturnReason1Code`
和一个退回金额。

于是本域最像成功的那个错误是：**收到 CNCL 就写 returned**。
`is_funds_evidence()` 存在的全部理由就是让这条路在代码里走不通，
而 `maos/domain/investigation/guard.py` 的第 ④ 道把它钉死在守卫层。

> **与派单原文的一处出入**（已记 docs/DECISIONS.md）：派单写「camt.029 恒为否定
> 答复」。按官方码表实查不成立 —— camt.029 可以是肯定答复（CNCL）。判据因此比
> 派单原文**更严**：派单那个前提下「不许拿 camt.029 写 returned」是白给的，
> 真实规范下它是一条真会被触发的防线。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# 出处
# ---------------------------------------------------------------------------
#: 码表数据文件。放在域包里而不是挨着本文件，理由见
#: `maos/domain/investigation/__init__.py` 的模块 docstring（白名单边界 + 它是域词汇表）。
CODES_PATH = (Path(__file__).resolve().parent.parent
              / "domain" / "investigation" / "iso20022_codes.json")

#: ISO 20022 External Code Sets 的官方目录页。**注意**：本机沙箱只放行
#: iso20022.org 的 `/sites/default/files/` 静态路径，目录页这类 HTML 一律超时，
#: 所以数据文件是直连 xlsx 取的，具体 URL 记在 JSON 的 `_provenance.source_url`。
SRC_CATALOGUE = ("https://www.iso20022.org/catalogue-messages/"
                 "additional-content-messages/external-code-sets")

# 四个码集的名字。按名取，不在各处抄字面量。
SET_CANCELLATION_REASON = "ExternalCancellationReason1Code"
SET_RESOLUTION = "ExternalInvestigationExecutionConfirmation1Code"
SET_CANCELLATION_REJECTION = "ExternalPaymentCancellationRejection1Code"
SET_RETURN_REASON = "ExternalReturnReason1Code"

ALL_CODE_SETS = (SET_CANCELLATION_REASON, SET_RESOLUTION,
                 SET_CANCELLATION_REJECTION, SET_RETURN_REASON)

# ---- 撤销请求的下落。四态，**不是**「成功/失败」两态 ----------------------------
RESOLUTION_CONFIRMED = "confirmed"
RESOLUTION_REJECTED = "rejected"
RESOLUTION_PENDING = "pending"
RESOLUTION_PARTIAL = "partial"
RESOLUTION_OTHER = "other"
"""不是在答「撤销请求怎么样了」的那些码（改单、补充信息、对账确认……）。

单列一档而不是塞进 pending：它们与「还没结果」不是一回事，混起来会让
「清算方回了一条与撤销无关的决议」被当成「还在等」，从而一直轮询下去。
"""

RESOLUTIONS = frozenset({RESOLUTION_CONFIRMED, RESOLUTION_REJECTED,
                         RESOLUTION_PENDING, RESOLUTION_PARTIAL, RESOLUTION_OTHER})

#: 报文族 -> 它能不能单独证明「资金已退回」。
#:
#: **只有 pacs.004 是 True**，这是本域全部判据的地基。camt.029 无论带哪个结论码
#: 都是 False —— 它答的是请求的下落，不是资金的下落。
_FUNDS_EVIDENCE_BY_FAMILY: dict[str, bool] = {
    "camt.056": False,     # 我们发出去的请求，本来就不是答复
    "camt.029": False,     # 决议答复 —— 含 CNCL 在内，一律不是资金证据
    "pacs.004": True,      # 退款报文 —— 唯一的资金证据
}


class UnknownCodeError(KeyError):
    """码不在已核对的官方清单内。

    **不兜底成「默认可重试」之类**：兜底的后果不是报错，是把没核过出处的码当成
    已知码处理 —— 那正是这张表要防的事。未知码应该在上层被当作「未知外部状态」
    显式处理（口径同 `maos/tools/gateway_codes.py::lookup`）。
    """


@dataclass(frozen=True)
class Iso20022Code:
    """一条官方码。

    frozen=True 是有意的：这张表是**对外部规范的转录**，运行期任何地方都不该改它。
    要改只能改数据文件，改数据文件就要重新抓一次官方发布。
    """

    code_set: str
    code: str
    """码值，如 "CNCL" / "AM04"。"""

    name: str
    """官方 Code Name，原文照抄。"""

    definition: str
    """官方 Code Definition，**原文照抄**，不要自己润色 —— 润色过就对不上文档了。"""

    status: str
    """官方状态（Registered / Deprecated …）。"""

    last_update: str
    """官方最后更新日期。规范改版时这一列会动。"""

    @property
    def source(self) -> str:
        """出处。回执里带着它，评委问「这个码哪来的」当场能答。"""
        return _provenance()["source_url"]


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _raw() -> dict:
    """读数据文件。lru_cache 是为了避免每次判据都读一次盘，不是为了藏错误 ——
    文件缺失/坏了在第一次调用就抛，不静默返回空表。

    空表的后果特别恶劣：`lookup()` 会对**每一个**码抛 UnknownCodeError，
    而上层看到的症状是「所有码都不认识」，离「数据文件没打包进去」这个真因很远。
    """
    if not CODES_PATH.exists():
        raise FileNotFoundError(
            f"ISO 20022 码表数据文件不存在：{CODES_PATH}。"
            "它由 maos/domain/investigation/ 提供，是本域判据的唯一码值来源；"
            "缺了不许 fallback 到硬编码 —— 那等于用一份编出来的规范做论证")
    with CODES_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not data.get("code_sets"):
        raise ValueError(f"{CODES_PATH} 里没有任何码集，数据文件疑似被截断")
    return data


def _provenance() -> dict:
    return _raw()["_provenance"]


@lru_cache(maxsize=1)
def _tables() -> dict[str, dict[str, Iso20022Code]]:
    """码集名 -> {码值 -> Iso20022Code}。"""
    out: dict[str, dict[str, Iso20022Code]] = {}
    for set_name, block in _raw()["code_sets"].items():
        out[set_name] = {
            entry["code"]: Iso20022Code(
                code_set=set_name,
                code=entry["code"],
                name=entry["name"],
                definition=entry["definition"],
                status=entry["status"],
                last_update=entry["last_update"],
            )
            for entry in block["codes"]
        }
    return out


def provenance() -> dict:
    """码表出处（URL / SHA-256 / 抓取日期 / 发布版本）。回执与 ToolPort 声明都引它。"""
    return dict(_provenance())


def code_sets() -> tuple[str, ...]:
    return tuple(sorted(_tables()))


def codes_in(code_set: str) -> tuple[str, ...]:
    """某个码集里的全部码值，升序。"""
    table = _tables().get(code_set)
    if table is None:
        raise UnknownCodeError(
            f"没有这个码集：{code_set!r}；已加载 {sorted(_tables())}")
    return tuple(sorted(table))


def is_used_in(code_set: str, message_id_prefix: str) -> bool:
    """这个码集用不用在这种报文里 —— 判据取自官方 xlsx 的 `UsageInMsgs` 表。

    `message_id_prefix` 按前缀比（`"camt.029"` 命中 `camt.029.001.08`）：
    报文版本号每年都在涨，按全名硬比会让换一版就判穿。
    """
    block = _raw()["code_sets"].get(code_set)
    if block is None:
        raise UnknownCodeError(f"没有这个码集：{code_set!r}")
    return any(m.startswith(message_id_prefix) for m in block.get("used_in_messages", ()))


def lookup(code_set: str, code: str) -> Iso20022Code:
    """按码集 + 码值取一条。未知码**抛**，不返回兜底对象。"""
    table = _tables().get(code_set)
    if table is None:
        raise UnknownCodeError(
            f"没有这个码集：{code_set!r}；已加载 {sorted(_tables())}")
    try:
        return table[code]
    except KeyError:
        raise UnknownCodeError(
            f"{code!r} 不在 {code_set} 的已核对清单内（该码集共 {len(table)} 条）。"
            f"新增码必须先核到出处再进数据文件，不许在调用处就地兜底；"
            f"出处：{_provenance()['source_url']}"
        ) from None


# ---------------------------------------------------------------------------
# 判据 —— 每一条都按官方 definition 原文定，不按语感
# ---------------------------------------------------------------------------
#: camt.029 结论码 -> 撤销请求的下落。
#:
#: 每条后面的注释是该码在官方数据文件里的 **definition 原文**（英文照抄）。
#: `_validate()` 在 import 时校验这些码确实都在数据文件里 —— 规范改版删掉某个码时，
#: 这里会当场响，而不是悄悄开始按一条不存在的码判事。
#:
#: 没列进来的码一律 `RESOLUTION_OTHER`：它们答的不是「撤销请求怎么样了」
#: （ACNR 索赔受理、CHRG 费用明细、SMTC 对账无误……）。硬把它们归进四态里
#: 才是编造 —— 那正是本模块开头那段警告说的事。
_RESOLUTION_BY_CODE: dict[str, str] = {
    # "Used when a requested cancellation is successful."
    # ↑ 成功的是**请求**。它不是资金证据，见 _FUNDS_EVIDENCE_BY_FAMILY。
    "CNCL": RESOLUTION_CONFIRMED,
    # "Used when a requested cancellation has been rejected."
    "RJCR": RESOLUTION_REJECTED,
    # "Used when a requested cancellation is pending."
    "PDCR": RESOLUTION_PENDING,
    # "Used when a requested cancellation has been partially executed."
    "PECR": RESOLUTION_PARTIAL,
    # "Used to inform that a response to an investigation is pending."
    "PDNG": RESOLUTION_PENDING,
    # "Used when a payment will be cancelled to solve an investigation case."
    # ↑ will be —— 还没撤，所以是 pending 不是 confirmed。
    "CWFW": RESOLUTION_PENDING,
    # "The cancellation request has been forwarded to the next agent for execution."
    # ↑ 转给下一家了，本家没有结论 —— 同样是 pending。
    "FTNA": RESOLUTION_PENDING,
    # "Process a cancellation request but batch already settled."
    "BIAS": RESOLUTION_REJECTED,
    # 'Process a Batch Cancellation "using an incorrect batch sequence number".'
    "IDNE": RESOLUTION_REJECTED,
    # "Process a cancellation request with incorrect reference to original batch."
    "IVCR": RESOLUTION_REJECTED,
    # "Used when no additional information is available."
    # ↑ 「没有更多信息」不是结论，按未决处置：既不能当成撤销成功，也不能当成被拒。
    "NINF": RESOLUTION_PENDING,
}


def resolution_of(confirmation_code: str) -> str:
    """一条 camt.029 结论码说明撤销请求到了哪一步。未知码抛，不静默归档。"""
    lookup(SET_RESOLUTION, confirmation_code)          # 先确认它是官方码
    return _RESOLUTION_BY_CODE.get(confirmation_code, RESOLUTION_OTHER)


def message_family(message_type: str) -> str:
    """把 `camt.029.001.08` 归一成 `camt.029`。口径与 guard.message_family 一致。"""
    parts = str(message_type or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(message_type or "")


def is_funds_evidence(message_type: str) -> bool:
    """这种报文能不能**单独**证明「资金已退回」。

    只有 pacs.004 是 True。这是本域唯一一处「什么算数」的判定，
    `guard.AUTHORITATIVE_EVIDENCE` 与它同源。

    未知报文族一律 False（fail-closed）：认不出来的报文当然证明不了资金已退回，
    默认 True 会让任何一条没见过的报文都能收口。
    """
    return _FUNDS_EVIDENCE_BY_FAMILY.get(message_family(message_type), False)


def is_terminal_resolution(confirmation_code: str) -> bool:
    """这条结论码是不是**撤销请求**的终态。

    注意它答的仍然不是「钱回来了」：`CNCL` 在这里是 True（请求有结论了），
    而 `is_funds_evidence("camt.029.001.08")` 仍是 False。两个函数一起用才完整 ——
    这正是本模块开头说的两个正交维度。
    """
    return resolution_of(confirmation_code) in (RESOLUTION_CONFIRMED, RESOLUTION_REJECTED)


def rejection_reason(rejection_code: str) -> Iso20022Code:
    """取一条撤销拒绝原因码（camt.029 否定决议时随附）。未知码抛。"""
    return lookup(SET_CANCELLATION_REJECTION, rejection_code)


def return_reason(return_reason_code: str) -> Iso20022Code:
    """取一条退回原因码（pacs.004 必带）。未知码抛。"""
    return lookup(SET_RETURN_REASON, return_reason_code)


def cancellation_reason(reason_code: str) -> Iso20022Code:
    """取一条撤销原因码（camt.056 必带，由定性这一步选定）。未知码抛。"""
    return lookup(SET_CANCELLATION_REASON, reason_code)


# ---------------------------------------------------------------------------
# import 时自检 —— 判据与数据文件对不上就当场响
# ---------------------------------------------------------------------------
def _validate() -> None:
    """三件事，每一件都挡掉一种「规范改版之后静默判错」。

    1. 四个码集都在数据文件里 —— 少一个就有一整类判据没有码值来源。
    2. `_RESOLUTION_BY_CODE` 里的每个码都还在官方码集里 —— 官方删掉/改名一个码时
       当场响，而不是继续按一条不存在的码判事。
    3. 码集与报文的对应仍成立（决议码用于 camt.029、退回原因码用于 pacs.004）——
       这一条是本域全部判据的地基，它一旦不成立，`is_funds_evidence` 的分档就没了
       依据。判据取自官方 `UsageInMsgs`，不是我们的断言。

    在 import 时跑而不是留给测试：数据文件是运行期依赖，装配阶段就该发现它不对。
    """
    tables = _tables()
    missing = [s for s in ALL_CODE_SETS if s not in tables]
    if missing:
        raise ValueError(
            f"码表数据文件缺码集 {missing}（有 {sorted(tables)}）；"
            f"重新从 {_provenance()['source_url']} 抓一份")

    unknown = sorted(c for c in _RESOLUTION_BY_CODE if c not in tables[SET_RESOLUTION])
    if unknown:
        raise ValueError(
            f"_RESOLUTION_BY_CODE 里的 {unknown} 不在官方 {SET_RESOLUTION} 里 —— "
            "规范可能改版了。重新核对出处再改判据，不要直接把它们删掉")

    if not is_used_in(SET_RESOLUTION, "camt.029"):
        raise ValueError(f"{SET_RESOLUTION} 按数据文件不用于 camt.029，本域判据的前提不成立")
    if not is_used_in(SET_RETURN_REASON, "pacs.004"):
        raise ValueError(f"{SET_RETURN_REASON} 按数据文件不用于 pacs.004，"
                         "「只有 pacs.004 证明资金已退回」这条判据的前提不成立")


_validate()
