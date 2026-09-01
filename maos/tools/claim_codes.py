"""保险理赔调整码表 —— 每一条都从 X12 官方码表页逐条核对后写入。

## 为什么这张表不许凭记忆填

编一张码表，被评委问一句「这个码是哪来的」就全塌。所以本文件的规矩与退款域的
``maos/tools/gateway_codes.py`` 完全一致：**每条码都带 ``source`` 与 ``fetched_at``，
指到具体页面与抓取日期；核不到出处的一条都不写。**

下面 13 条 CARC + 4 条 Group Code 全部在 2026-08-31 当天用 WebFetch 逐条核过，
``description`` 是页面原文**逐字照抄**（含末尾的 ``Usage:`` 段），不润色、不缩写 ——
润色过就对不上文档了。页头当天的 ``Status Last Reviewed`` 是 ``8/1/2026``，
三张表**均无需登录、无需会员**即可查看。

> **为什么不是 ACORD。** 保险业更"正统"的选择是 ACORD 的 Claims 标准与 Code Manual，
> 但它要会员资格才取得到。拿不到就没法逐条核对，等于把码表变成凭记忆编造 ——
> 那正是本文件通篇在防的事。所以换成公开可查的 X12 CARC/RARC 体系（见 DECISIONS）。

## 两层结构（这是 X12 835 的真实结构，不是我们的发明）

X12 835（Health Care Claim Payment/Advice）的 CAS 段里，每一笔调整都由**两个码**
共同描述，缺一不可：

  · **Group Code**（``CO`` / ``PR`` / ``OA`` / ``PI``）—— 这笔被调整掉的钱**由谁承担**。
    ``PR`` 归被保险人自担、``CO`` 归合同约定（赔付方与服务方之间），承担方不同，
    案子的收口动作就不同。
  · **CARC**（Claim Adjustment Reason Code）—— **为什么**调整。

只拿 CARC 判事是不够的：同一条 ``45`` 挂在 ``CO`` 下是合同折扣（赔付方照赔，只是
按约定价），挂在 ``PR`` 下就是让被保险人自己补差价。两个码要一起读。

## effect 与 recourse 是两个正交的维度（本文件最重要的一条）

只有一个 "denied / not denied" 的 bool 是**不够**的，而且不够的地方正好踩在铁律 8 上。
这一课直接抄自退款域码表的 ``retriable`` × ``outcome``：

``effect`` 答的是「这条码对**赔款**做了什么」；``recourse`` 答的是「**MAOS 这边**还
能做点什么」。两者正交：

    effect=EFFECT_DENIED      这一项不赔
    effect=EFFECT_REDUCED     照赔，但金额被削减（合同价 / 已包含在别的项目里）
    effect=EFFECT_PATIENT     照赔，但这部分转由被保险人自担（起付线 / 共保 / 自付额）

    recourse=RECOURSE_NONE          机器没有别的招，终态
    recourse=RECOURSE_RESUBMIT      补齐信息或单据之后可以重报
    recourse=RECOURSE_OTHER_PAYER   该送给另一个赔付方，不是这一个
    recourse=RECOURSE_HUMAN         只有人能推进（申诉 / 补授权 / 线下沟通）

🔴 **``effect`` 与 ``recourse`` 都是 MAOS 侧的处置口径，不是 X12 原文。**
X12 的码表只给「码 + 描述 + 起用/修订日期」三样，**没有**机器可读的处置字段。
所以本文件把两类字段分栏放：``description`` / ``start`` / ``last_modified`` 是
**规范原文**，``effect`` / ``recourse`` 是**我们读了原文之后的判断**。
混在一起写，下一个人会以为处置口径也有官方背书 —— 那就是另一种形式的编造。
每条的 ``rationale`` 写明这个判断是从描述原文的哪半句读出来的。

## 最要紧的一条：CARC **永远不等于「已赔付」**（铁律 8）

``1`` / ``2`` / ``3`` / ``45`` / ``97`` 这几条的 ``effect`` 不是 DENIED，读起来像
「那这笔是赔了的」。**不许这么推断。** 一条调整码只说明「赔付方对这笔账做了一次
调整」，它不是到账回执。这笔钱到没到账，权威在赔付方的付款回执上，只有
``claim.observe`` 问出 ``observed_state == "paid"`` 才写得进 ``paid``（见
``maos/domain/claim/guard.py``）。

拿 ``effect != DENIED`` 当成「赔付成功」，就是把外部状态直接写死为终态 —— 那是 bug
不是功能。本文件为此**不提供**任何 ``is_paid(code)`` 之类的函数：不给这条路留入口。

## 与第七道闸的关系（不要接错）

``maos/runtime/gate.py`` 的第七道闸 ``_gate_gateway`` 按数据形状触发：任一 artifact 的
``content["receipt"]`` 是带 ``code`` 的 dict 就进闸，然后拿这个 code 去查
**``gateway_codes.py``**（支付宝那张表）。CARC 的码值空间与它完全不相交，``96`` 送进去
只会得到一条「未知错误码」的 blocker。

所以理赔域的回执一律挂在 ``content["payer_receipt"]`` 上，**不占用 ``receipt`` 这个键**。
这不是绕开闸，是**两张码表本来就不该混查**：拿支付宝的码表去解释 X12 的码，判出来
的任何结论都是错的。这条边界有回归测试守着（``test_claim_gate_isolation.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 出处（每条码的 source 字段引用这里的常量，改 URL 只改一处）
# ---------------------------------------------------------------------------

#: X12 官方 Claim Adjustment Reason Codes（CARC）全表。
#: 2026-08-31 实测：无需登录、无需会员即可看到全表，每条带 Start / Last Modified。
SRC_CARC = "https://x12.org/codes/claim-adjustment-reason-codes"

#: X12 官方 Claim Adjustment Group Codes 全表（CO / OA / PI / PR 四条）。
SRC_GROUP = "https://x12.org/codes/claim-adjustment-group-codes"

#: X12 官方 Remittance Advice Remark Codes（RARC）全表。
#: CARC ``16`` / ``96`` / ``252`` 的原文都写着「At least one Remark Code must be
#: provided」—— 备注码是它们的必备伴随物，出处一并留在这里。
SRC_RARC = "https://x12.org/codes/remittance-advice-remark-codes"

#: 本表逐条核对的日期。规范会改版：核对日期与页面上的 Last Modified 一起，
#: 才说得清「我们照的是哪一版」。**过期时要第一个发现的就是这个日期。**
FETCHED_AT = "2026-08-31"

#: 抓取当天页面上的复核状态。三张表当天都是这个值。
STATUS_LAST_REVIEWED = "8/1/2026"


# ---------------------------------------------------------------------------
# 两个正交维度的取值域。**都是 MAOS 侧口径，不是 X12 原文** —— 见模块 docstring。
# ---------------------------------------------------------------------------

#: 这一项不赔。
EFFECT_DENIED = "denied"
#: 照赔，但金额被削减（合同价 / 已含在别的项目里）。
EFFECT_REDUCED = "reduced"
#: 照赔，但这部分转由被保险人自担（起付线 / 共保 / 自付额）。
EFFECT_PATIENT = "patient_share"

EFFECTS = (EFFECT_DENIED, EFFECT_REDUCED, EFFECT_PATIENT)

#: 机器没有别的招了，终态。
RECOURSE_NONE = "none"
#: 补齐信息或单据之后可以重报。
RECOURSE_RESUBMIT = "resubmit_after_fix"
#: 该送给另一个赔付方。
RECOURSE_OTHER_PAYER = "route_other_payer"
#: 只有人能推进（申诉 / 补授权 / 线下沟通）。
RECOURSE_HUMAN = "human_appeal"

RECOURSES = (RECOURSE_NONE, RECOURSE_RESUBMIT, RECOURSE_OTHER_PAYER, RECOURSE_HUMAN)


@dataclass(frozen=True)
class AdjustmentCode:
    """一条官方 CARC。

    frozen=True 是有意的：这张表是**对外部规范的观察口径**，运行期任何地方都不该
    改它。要改只能改代码，改代码就要重新核出处。

    字段分两栏，顺序即分界：上面四个是**规范原文**，下面三个是**我们的判断**。
    """

    # ---- 规范原文（逐字照抄 SRC_CARC，不许润色）----------------------------
    code: str
    """码值。X12 CARC 是数字串（"96"）或字母数字混排（"A1" / "P1"）。"""

    description: str
    """官方英文描述，**原文照抄**，含末尾的 ``Usage:`` 段。润色过就对不上文档了。"""

    start: str
    """页面上的 Start 日期，原样 ``MM/DD/YYYY``。"""

    last_modified: str
    """页面上的 Last Modified 日期；页面没给就是空串（不是 "无"、不是 None）。"""

    # ---- MAOS 侧口径（**不是** X12 原文）------------------------------------
    effect: str
    """这条码对赔款做了什么：denied / reduced / patient_share。见模块 docstring。"""

    recourse: str
    """MAOS 这边还能做什么：none / resubmit_after_fix / route_other_payer / human_appeal。"""

    rationale: str
    """上面两个判断是从 ``description`` 的哪半句读出来的 —— 中文，写给评审看。"""

    # ---- 溯源 ---------------------------------------------------------------
    source: str = SRC_CARC
    fetched_at: str = FETCHED_AT

    def __post_init__(self) -> None:
        if self.effect not in EFFECTS:
            raise ValueError(f"未知的 effect：{self.effect}")
        if self.recourse not in RECOURSES:
            raise ValueError(f"未知的 recourse：{self.recourse}")
        if not self.source:
            raise ValueError(f"CARC {self.code} 没有出处 —— 核不到出处的不许进表")
        if not self.fetched_at:
            raise ValueError(f"CARC {self.code} 没有抓取日期 —— 说不清照的是哪一版")
        if not self.rationale:
            raise ValueError(
                f"CARC {self.code} 的 effect/recourse 没写判断依据；"
                "这两个字段不是官方原文，不写依据就与凭记忆编造无异")


@dataclass(frozen=True)
class GroupCode:
    """一条官方 Claim Adjustment Group Code —— 这笔调整**由谁承担**。

    四条全部取自 SRC_GROUP，2026-08-31 核过，Start 一律 ``05/20/2018``。
    """

    code: str
    description: str
    """官方英文描述，原文照抄。"""

    start: str
    bearer: str
    """承担方的中文说法。**MAOS 侧口径**，是对 description 的翻译式解释，不是原文。"""

    source: str = SRC_GROUP
    fetched_at: str = FETCHED_AT

    def __post_init__(self) -> None:
        if not self.source or not self.fetched_at:
            raise ValueError(f"Group Code {self.code} 缺出处或抓取日期")


# ---------------------------------------------------------------------------
# Group Code 四条 —— 出处：SRC_GROUP
# ---------------------------------------------------------------------------

GROUP_CO = GroupCode(
    code="CO",
    description="Contractual Obligation",
    start="05/20/2018",
    bearer="合同约定：赔付方与服务方之间按合同价消化，不向被保险人转嫁",
)

GROUP_PR = GroupCode(
    code="PR",
    description="Patient Responsibility",
    start="05/20/2018",
    bearer="被保险人自担：起付线 / 共保 / 自付额这一类",
)

GROUP_OA = GroupCode(
    code="OA",
    description="Other Adjustment",
    start="05/20/2018",
    bearer="其他调整：既不归合同也不归被保险人，多用于转付其他赔付方",
)

GROUP_PI = GroupCode(
    code="PI",
    description="Payor Initiated Reduction",
    start="05/20/2018",
    bearer="赔付方主动扣减：赔付方认为责任不在被保险人、也无合同依据时使用",
)

ALL_GROUP_CODES: dict[str, GroupCode] = {
    g.code: g for g in (GROUP_CO, GROUP_OA, GROUP_PI, GROUP_PR)
}


# ---------------------------------------------------------------------------
# CARC 十三条 —— 出处：SRC_CARC
#
# 收录标准：**能演出本域四条判据**（终态拒赔 / 补件重报 / 转其他赔付方 / 只能人工），
# 外加三条纯分摊码（1/2/3）用来证明「CARC 不等于拒赔、更不等于已赔付」。
# 想加第十四条，先去 SRC_CARC 把原文抄回来，不许照着记忆补。
# ---------------------------------------------------------------------------

# ---- 分摊类：照赔，但一部分转由被保险人自担 --------------------------------
DEDUCTIBLE = AdjustmentCode(
    code="1",
    description="Deductible Amount",
    start="01/01/1995",
    last_modified="",
    effect=EFFECT_PATIENT,
    recourse=RECOURSE_NONE,
    rationale="起付线本来就该由被保险人承担，这不是错误也不是拒赔，没有可补救的动作。"
              "它通常挂在 PR 组下 —— 组码才说明由谁承担，见模块 docstring 的两层结构。",
)

COINSURANCE = AdjustmentCode(
    code="2",
    description="Coinsurance Amount",
    start="01/01/1995",
    last_modified="",
    effect=EFFECT_PATIENT,
    recourse=RECOURSE_NONE,
    rationale="共保比例部分同起付线：保单本来就约定了这一份由被保险人分担。",
)

COPAY = AdjustmentCode(
    code="3",
    description="Co-payment Amount",
    start="01/01/1995",
    last_modified="",
    effect=EFFECT_PATIENT,
    recourse=RECOURSE_NONE,
    rationale="定额自付部分同上，属保单约定的分摊，不是赔付方对本次申请的否定。",
)

# ---- 削减类：照赔，但金额被压下来 ------------------------------------------
FEE_SCHEDULE_EXCEEDED = AdjustmentCode(
    code="45",
    description=(
        "Charge exceeds fee schedule/maximum allowable or contracted/legislated fee "
        "arrangement. Usage: This adjustment amount cannot equal the total service or "
        "claim charge amount; and must not duplicate provider adjustment amounts "
        "(payments and contractual reductions) that have resulted from prior payer(s) "
        "adjudication. (Use only with Group Codes PR or CO depending upon liability)"
    ),
    start="01/01/1995",
    last_modified="07/01/2017",
    effect=EFFECT_REDUCED,
    recourse=RECOURSE_NONE,
    rationale="原文明写「This adjustment amount cannot equal the total service or claim "
              "charge amount」—— 调整额不得等于总额，也就是说这条码**必然**留下一部分照赔，"
              "所以 effect 是削减而不是拒赔。超出合同价的部分再报一次还是同一个价，"
              "机器没有别的招，recourse=none。",
)

BUNDLED = AdjustmentCode(
    code="97",
    description=(
        "The benefit for this service is included in the payment/allowance for another "
        "service/procedure that has already been adjudicated. Usage: Refer to the 835 "
        "Healthcare Policy Identification Segment (loop 2110 Service Payment Information "
        "REF), if present."
    ),
    start="01/01/1995",
    last_modified="07/01/2017",
    effect=EFFECT_REDUCED,
    recourse=RECOURSE_NONE,
    rationale="原文「included in the payment/allowance for another service/procedure that "
              "has already been adjudicated」—— 这一项的赔款已经含在另一项里付过了，"
              "不是没赔；重复报只会再被并一次。",
)

# ---- 拒赔 + 可补救：补齐信息 / 单据后重报 -----------------------------------
LACKS_INFORMATION = AdjustmentCode(
    code="16",
    description=(
        "Claim/service lacks information or has submission/billing error(s). Usage: Do not "
        "use this code for claims attachment(s)/other documentation. At least one Remark "
        "Code must be provided (may be comprised of either the NCPDP Reject Reason Code, or "
        "Remittance Advice Remark Code that is not an ALERT.) Refer to the 835 Healthcare "
        "Policy Identification Segment (loop 2110 Service Payment Information REF), if present."
    ),
    start="01/01/1995",
    last_modified="03/01/2018",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_RESUBMIT,
    rationale="原文「lacks information or has submission/billing error(s)」说的是申报件本身"
              "缺东西或填错了 —— 补齐再报是有意义的，与「不在保障范围」那一类不同。"
              "具体缺什么由伴随的 RARC 说明（原文要求「At least one Remark Code must be "
              "provided」），所以本域的回执一律带 remark_codes。",
)

DOCUMENTATION_REQUIRED = AdjustmentCode(
    code="252",
    description=(
        "An attachment/other documentation is required to adjudicate this claim/service. At "
        "least one Remark Code must be provided (may be comprised of either the NCPDP Reject "
        "Reason Code, or Remittance Advice Remark Code that is not an ALERT)."
    ),
    start="09/30/2012",
    last_modified="06/02/2013",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_RESUBMIT,
    rationale="原文「An attachment/other documentation is required to adjudicate」—— 缺的是"
              "附件，补上就能继续裁定。与 16 分开收录是因为 16 的原文明确写着"
              "「Do not use this code for claims attachment(s)/other documentation」，"
              "两条的适用面被官方划开了，合并成一条就抹掉了这条边界。",
)

# ---- 拒赔 + 改送别家 --------------------------------------------------------
WRONG_PAYER = AdjustmentCode(
    code="109",
    description=(
        "Claim/service not covered by this payer/contractor. You must send the claim/service "
        "to the correct payer/contractor."
    ),
    start="01/01/1995",
    last_modified="01/29/2012",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_OTHER_PAYER,
    rationale="原文第二句「You must send the claim/service to the correct payer/contractor」"
              "**直接写出了处置动作**，这是整张表里唯一一条自带 remedy 的码 —— 所以这一条的"
              "recourse 有官方原文背书，不全是我们的判断。",
)

# ---- 拒赔 + 只有人能推进 ----------------------------------------------------
NO_PRECERT = AdjustmentCode(
    code="197",
    description="Precertification/authorization/notification/pre-treatment absent.",
    start="10/31/2006",
    last_modified="05/01/2018",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_HUMAN,
    rationale="事前授权缺失。原样重报还是缺，补授权要人去和赔付方谈（有的允许追溯授权，"
              "有的不允许）—— 机器没有确定的下一步，只能转人工，不许自旋重报。",
)

# ---- 拒赔 + 终态 ------------------------------------------------------------
NON_COVERED = AdjustmentCode(
    code="96",
    description=(
        "Non-covered charge(s). At least one Remark Code must be provided (may be comprised "
        "of either the NCPDP Reject Reason Code, or Remittance Advice Remark Code that is "
        "not an ALERT.) Usage: Refer to the 835 Healthcare Policy Identification Segment "
        "(loop 2110 Service Payment Information REF), if present."
    ),
    start="01/01/1995",
    last_modified="07/01/2017",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_NONE,
    rationale="「Non-covered charge(s)」= 这项费用不在保障范围内。不是填错、也不是缺件，"
              "补什么都改不了这个结论，重报必然撞同一个码 —— 场景 8 的失败路径用它，"
              "演的正是「机器再试一次也没有意义」这一格。",
)

NOT_MEDICALLY_NECESSARY = AdjustmentCode(
    code="50",
    description=(
        "These are non-covered services because this is not deemed a 'medical necessity' by "
        "the payer. Usage: Refer to the 835 Healthcare Policy Identification Segment (loop "
        "2110 Service Payment Information REF), if present."
    ),
    start="01/01/1995",
    last_modified="07/01/2017",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_HUMAN,
    rationale="原文「not deemed a 'medical necessity' by the payer」—— 这是赔付方的**判断**，"
              "不是事实认定，所以与 96 不同：它是可申诉的，但申诉只有人做得了。",
)

FILING_LIMIT_EXPIRED = AdjustmentCode(
    code="29",
    description="The time limit for filing has expired.",
    start="01/01/1995",
    last_modified="",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_NONE,
    rationale="报案时限已过。时间不可逆，重报一次只会再过期一次 —— 这是整张表里最干净的"
              "一条终态，收录它是为了让「重报无意义」这一档有一个不掺申诉余地的样本。",
)

BENEFIT_MAX_REACHED = AdjustmentCode(
    code="119",
    description="Benefit maximum for this time period or occurrence has been reached.",
    start="01/01/1995",
    last_modified="02/29/2004",
    effect=EFFECT_DENIED,
    recourse=RECOURSE_NONE,
    rationale="本期或本次事故的赔付上限已用尽。额度是保单条款定的，重报改不了额度 ——"
              "要动只能等下一个保单期或改保单，都不是本次理赔能做的事。",
)


#: 全表。按码值索引，供 MockPayer 与上层按码取判据。
ALL_CODES: dict[str, AdjustmentCode] = {
    c.code: c
    for c in (
        DEDUCTIBLE,
        COINSURANCE,
        COPAY,
        LACKS_INFORMATION,
        FILING_LIMIT_EXPIRED,
        FEE_SCHEDULE_EXCEEDED,
        NOT_MEDICALLY_NECESSARY,
        NON_COVERED,
        BUNDLED,
        WRONG_PAYER,
        BENEFIT_MAX_REACHED,
        NO_PRECERT,
        DOCUMENTATION_REQUIRED,
    )
}

#: 本域必须覆盖的四类处置 -> 本表中的代表码。
#: 场景与测试按它取码，不在各处抄字面量；少一类就说明码表塌了一格。
REQUIRED_RECOURSES: dict[str, tuple[str, ...]] = {
    RECOURSE_NONE:        ("96", "29", "119"),
    RECOURSE_RESUBMIT:    ("16", "252"),
    RECOURSE_OTHER_PAYER: ("109",),
    RECOURSE_HUMAN:       ("197", "50"),
}

#: 原文里明写「At least one Remark Code must be provided」的那几条。
#: 这些码的回执**必须**带 RARC，缺了就是回执不完整 —— 判据由 claim.py 强制。
NEEDS_REMARK_CODE: frozenset[str] = frozenset({"16", "96", "252"})


def lookup(code: str) -> AdjustmentCode:
    """按码值取判据。未知码**抛**而不是返回一个「默认拒赔」的兜底。

    兜底的后果不是报错，是**把没核过出处的码当成已知码处理** —— 那正是这张表要防
    的事。未知码应该在上层被当作「未知外部状态」显式处理（口径同
    ``gateway_codes.lookup``）。
    """
    try:
        return ALL_CODES[code]
    except KeyError:
        raise KeyError(
            f"未知理赔调整码 {code!r}：不在已核对 X12 官方码表的清单内。"
            f"新增码必须先到 {SRC_CARC} 核到原文再进 ALL_CODES，不许在调用处就地兜底"
        ) from None


def lookup_group(code: str) -> GroupCode:
    """按 Group Code 取承担方。未知组码同样抛，理由同 ``lookup``。"""
    try:
        return ALL_GROUP_CODES[code]
    except KeyError:
        raise KeyError(
            f"未知调整组码 {code!r}：X12 只定义了 {sorted(ALL_GROUP_CODES)} 四条"
        ) from None


def effect_of(code: str) -> str:
    """这条码对赔款做了什么。未知码抛 KeyError，不静默放行。"""
    return lookup(code).effect


def recourse_of(code: str) -> str:
    """MAOS 这边还能做什么。**这不是官方 remedy**，见模块 docstring。"""
    return lookup(code).recourse


def is_denial(code: str) -> bool:
    """这条码是不是拒赔。

    刻意**没有**对偶的 ``is_paid()``：``effect != DENIED`` 只说明赔付方对这笔账做了
    一次调整，**不说明钱到账了**。到账是外部权威事实，只能由 ``claim.observe`` 问出来
    （铁律 8）。给这条路留一个函数入口，迟早有人拿它去写 ``paid``。
    """
    return lookup(code).effect == EFFECT_DENIED


def machine_can_retry(code: str) -> bool:
    """机器自己还能不能再报一次（补件重报算，转其他赔付方**不算**）。

    ``RECOURSE_OTHER_PAYER`` 单独排除：那不是「再报一次」，是换一个收件人 ——
    要先确定新的赔付方是谁，而那是人的决定。混进来会让机器把同一份申报
    原样投给一个还没确定的对象。
    """
    return lookup(code).recourse == RECOURSE_RESUBMIT


#: 供文档与回执引用：本表全部出处。
SOURCES: tuple[str, ...] = (SRC_CARC, SRC_GROUP, SRC_RARC)
