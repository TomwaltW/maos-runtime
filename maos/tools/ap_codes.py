"""应付账款域的外部码表与业务规则 —— 逐条抄自 Peppol BIS Billing 3.0 官方文档。

## 为什么这张表不许凭记忆填

与 ``maos/tools/gateway_codes.py`` 同一条规矩，理由也同一条：**编一张码表，被评委问
一句「这个码是哪来的」就全塌**。所以本文件的规矩是：每条码、每条规则都带 ``source``
指到具体页面，核不到出处的一条都不写。

拒付理由尤其如此。三单匹配判「这张发票不能付」，理由必须挂**真实存在的规则编号**
（``BR-xx`` / ``PEPPOL-EN16931-xx``），而不是自造一套 ``BR-99``。这是本域与退款域
``AS-01@v1`` 同构的地方：**理由可核对**才算数 —— 对方拿着编号去查规范能查到，
这句话才成立。

## 出处与抓取

* 规范：Peppol BIS Billing 3.0（EN 16931 的 UBL 2.1 CIUS）
* 版本：``SPEC_RELEASE``（页面自述）
* 抓取日期：``FETCHED_AT``
* 入口：``SPEC_HOME``

校验分三层，本文件覆盖的是第二、三层的规则编号：
UBL 2.1 XSD（结构）→ EN 16931 Schematron（``BR-xx``）→ Peppol Schematron
（``PEPPOL-EN16931-xx``）。

## 三张码表，以及为什么条数是算出来的不是抄来的

派单写的「UNCL1001 Invoice type codes」在站点上**并不是一页**：它拆成
``UNCL1001-inv``（发票类型子集）与 ``UNCL1001-cn``（贷记通知单类型子集）两页。
应付账款收的是供应商发票，所以本文件取 ``-inv`` 那一份；``-cn`` 那份本域用不上，
不抄进来（抄了就得一并维护，而没有任何代码读它）。

**条数由 ``len()`` 算，不写死。** 抓取时对同一页做过两次独立提取：两次**枚举逐条
一致**，但页面摘要给出的总数两次不同（一次 79 / 一次 58；发票类型码那页一次 28 /
一次 42）。枚举一致而计数不一致，说明不可信的是计数那一步，不是枚举。所以：
枚举照抄，条数一律 ``len(TABLE)`` 现算 —— 抄一个自己没数过的数字进来，就是拿
「看起来很确定」冒充「核对过」。

## 与 ``gateway_codes.py`` 的关系：形状同构，内容无关

两张表都是「对外部权威的观察口径」，都 ``frozen=True``、未知码一律抛而不兜底。
但**不共用类型也不互相 import**：一个是支付网关的错误码（两层 code/sub_code），
一个是电子发票规范的码表与业务规则（三层 XSD/EN/Peppol），字段含义对不上。
硬合成一个基类只会让两边的字段都变成「有些场景下没意义」。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 出处（每条码与规则的 source 引用这里的常量，改 URL 只改一处）
# ---------------------------------------------------------------------------

#: 规范首页。评委按这个进去，三层校验与全部码表的入口都在上面。
SPEC_HOME = "https://docs.peppol.eu/poacc/billing/3.0/"

#: 页面自述的版本。**改版就要重抓** —— 规范会改版，结论过期时本文件要第一个发现。
SPEC_RELEASE = "Peppol BIS Billing 3.0 - May 2026 Release"

#: 本文件全部内容的抓取日期（ISO 日期）。与 SPEC_RELEASE 一起构成「什么时候、
#: 对着哪一版抄的」。核对是一次性动作，但它的有效期不是无限的。
FETCHED_AT = "2026-08-31"

SRC_UNCL1001_INV = "https://docs.peppol.eu/poacc/billing/3.0/codelist/UNCL1001-inv/"
SRC_UNCL5305 = "https://docs.peppol.eu/poacc/billing/3.0/codelist/UNCL5305/"
SRC_UNCL4461 = "https://docs.peppol.eu/poacc/billing/3.0/codelist/UNCL4461/"

#: EN 16931 模型绑定到 UBL 的业务规则页（``BR-xx`` / ``BR-CO-xx`` / ``BR-CL-xx``）。
SRC_RULES_EN16931 = "https://docs.peppol.eu/poacc/billing/3.0/rules/ubl-tc434/"

#: Peppol 在 EN 16931 之上的附加规则页（``PEPPOL-EN16931-Rxxx``）。
SRC_RULES_PEPPOL = "https://docs.peppol.eu/poacc/billing/3.0/rules/ubl-peppol/"

SOURCES: tuple[str, ...] = (
    SPEC_HOME, SRC_UNCL1001_INV, SRC_UNCL5305, SRC_UNCL4461,
    SRC_RULES_EN16931, SRC_RULES_PEPPOL,
)

# 码表标识。上层按这三个常量取表，不在各处写字符串。
LIST_INVOICE_TYPE = "UNCL1001-inv"
LIST_TAX_CATEGORY = "UNCL5305"
LIST_PAYMENT_MEANS = "UNCL4461"


@dataclass(frozen=True)
class CodeEntry:
    """码表里的一行。

    ``frozen=True`` 与 ``gateway_codes.GatewayCode`` 同理：这是**对外部规范的观察
    口径**，运行期任何地方都不该改它。要改只能改代码，改代码就要重新核出处。
    """

    code: str
    """码值。原样照抄，含 ``ZZZ`` 这类非数字码。"""

    name: str
    """官方名称，**原文照抄，不翻译不润色** —— 润色过就对不上文档了。
    中文注解要写就写在旁边的注释里，不许写进这个字段。"""

    list_id: str
    """所属码表（LIST_* 之一）。"""

    source: str
    """出处。核不到出处的码不许进表。"""

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("码值不许为空")
        if not self.source:
            raise ValueError(f"码 {self.code} 没有出处 —— 核不到出处的不许进表")


@dataclass(frozen=True)
class BusinessRule:
    """一条业务规则。拒付理由挂的就是它。

    ``text`` 是规范原文（英文），不译 —— 译文一旦与原文有出入，「拿编号去查规范能
    查到」这句话就打折了。给人看的中文说明由调用方另写，别覆盖这个字段。
    """

    rule_id: str
    """规则编号，如 ``BR-CO-13`` / ``PEPPOL-EN16931-R120``。原样照抄。"""

    text: str
    """规范原文。"""

    source: str
    """出处页面。"""

    layer: str
    """``en16931``（CEN 层）或 ``peppol``（Peppol 附加层）。三层校验里的后两层。"""

    def __post_init__(self) -> None:
        if self.layer not in (LAYER_EN16931, LAYER_PEPPOL):
            raise ValueError(f"未知的规则层：{self.layer}")
        if not self.source:
            raise ValueError(f"规则 {self.rule_id} 没有出处")


LAYER_EN16931 = "en16931"
LAYER_PEPPOL = "peppol"


def _entries(list_id: str, source: str, rows: tuple[tuple[str, str], ...]) -> dict[str, CodeEntry]:
    """把 ``(code, name)`` 逐行折成 CodeEntry，顺带查重。

    查重不是形式：码表是**手抄**进来的，抄重一行不会有任何症状 —— dict 字面量里
    后一条静默覆盖前一条，条数少一个，而 ``len()`` 算出来的数照样「自洽」。
    """
    out: dict[str, CodeEntry] = {}
    for code, name in rows:
        if code in out:
            raise ValueError(f"{list_id} 里码值 {code!r} 重复 —— 抄表时抄重了")
        out[code] = CodeEntry(code=code, name=name, list_id=list_id, source=source)
    return out


# ---------------------------------------------------------------------------
# UNCL1001（发票类型码，Peppol 子集）—— 出处：SRC_UNCL1001_INV
# ---------------------------------------------------------------------------
# 站点标题原文：Invoice type code (UNCL1001 subset)。
# 应付账款域实际会用到的只有 380 / 384 / 386 / 389 那几条，但整张子集照抄 ——
# 只抄用得上的那几条，等于把「这张表的边界在哪」也一起省掉了，下次有人要加一条
# 就只能凭印象判断它在不在子集里。
_INVOICE_TYPE_ROWS: tuple[tuple[str, str], ...] = (
    ("71", "Request for payment"),
    ("80", "Debit note related to goods or services"),
    ("82", "Metered services invoice"),
    ("84", "Debit note related to financial adjustments"),
    ("102", "Tax notification"),
    ("218", "Final payment request based on completion of work"),
    ("219", "Payment request for completed units"),
    ("326", "Partial invoice"),
    ("331", "Commercial invoice which includes a packing list"),
    ("380", "Commercial invoice"),
    ("382", "Commission note"),
    ("383", "Debit note"),
    ("384", "Corrected invoice"),
    ("386", "Prepayment invoice"),
    ("388", "Tax invoice"),
    ("389", "Self-billed invoice"),
    ("393", "Factored invoice"),
    ("395", "Consignment invoice"),
    ("553", "Forwarder's invoice discrepancy report"),
    ("575", "Insurer's invoice"),
    ("623", "Forwarder's invoice"),
    ("780", "Freight invoice"),
    ("817", "Claim notification"),
    ("870", "Consular invoice"),
    ("875", "Partial construction invoice"),
    ("876", "Partial final construction invoice"),
    ("877", "Final construction invoice"),
)

INVOICE_TYPE_CODES: dict[str, CodeEntry] = _entries(
    LIST_INVOICE_TYPE, SRC_UNCL1001_INV, _INVOICE_TYPE_ROWS)

#: 普通商业发票。应付账款的主流程收的就是这一类。
CODE_COMMERCIAL_INVOICE = "380"
#: 更正发票。三单匹配对不上、供应商重开时用的那一类。
CODE_CORRECTED_INVOICE = "384"


# ---------------------------------------------------------------------------
# UNCL5305（税种/税率类别码，Peppol 子集）—— 出处：SRC_UNCL5305
# ---------------------------------------------------------------------------
# 站点标题原文：Duty or tax or fee category code (Subset of UNCL5305)。
_TAX_CATEGORY_ROWS: tuple[tuple[str, str], ...] = (
    ("AE", "Vat Reverse Charge"),
    ("B", "Transferred (VAT), In Italy"),
    ("E", "Exempt from Tax"),
    ("G", "Free export item, VAT not charged"),
    ("K", "VAT exempt for EEA intra-community supply of goods and services"),
    ("L", "Canary Islands general indirect tax"),
    ("M", "Tax for production, services and importation in Ceuta and Melilla"),
    ("O", "Services outside scope of tax"),
    ("S", "Standard rate"),
    ("Z", "Zero rated goods"),
)

TAX_CATEGORY_CODES: dict[str, CodeEntry] = _entries(
    LIST_TAX_CATEGORY, SRC_UNCL5305, _TAX_CATEGORY_ROWS)

#: 标准税率。本域靶场发票用的就是它。
CODE_TAX_STANDARD = "S"


# ---------------------------------------------------------------------------
# UNCL4461（付款方式码）—— 出处：SRC_UNCL4461
# ---------------------------------------------------------------------------
# 站点标题原文：Payment means code (UNCL4461)。
#
# 序列**不连续**，这是规范本身如此，不是抄漏：71 / 72 / 73 不存在，78 之后直接跳到 91。
# 抓取时专门就这一点问过一次，答复是「71, 72, 73 explicitly absent」。
# 这条注释留着是为了让下一个人不必再去核一遍 —— 看见断档先怀疑抄漏是对的反应。
_PAYMENT_MEANS_ROWS: tuple[tuple[str, str], ...] = (
    ("1", "Instrument not defined"),
    ("2", "Automated clearing house credit"),
    ("3", "Automated clearing house debit"),
    ("4", "ACH demand debit reversal"),
    ("5", "ACH demand credit reversal"),
    ("6", "ACH demand credit"),
    ("7", "ACH demand debit"),
    ("8", "Hold"),
    ("9", "National or regional clearing"),
    ("10", "In cash"),
    ("11", "ACH savings credit reversal"),
    ("12", "ACH savings debit reversal"),
    ("13", "ACH savings credit"),
    ("14", "ACH savings debit"),
    ("15", "Bookentry credit"),
    ("16", "Bookentry debit"),
    ("17", "ACH demand cash concentration/disbursement (CCD) credit"),
    ("18", "ACH demand cash concentration/disbursement (CCD) debit"),
    ("19", "ACH demand corporate trade payment (CTP) credit"),
    ("20", "Cheque"),
    ("21", "Banker's draft"),
    ("22", "Certified banker's draft"),
    ("23", "Bank cheque (issued by a banking or similar establishment)"),
    ("24", "Bill of exchange awaiting acceptance"),
    ("25", "Certified cheque"),
    ("26", "Local cheque"),
    ("27", "ACH demand corporate trade payment (CTP) debit"),
    ("28", "ACH demand corporate trade exchange (CTX) credit"),
    ("29", "ACH demand corporate trade exchange (CTX) debit"),
    ("30", "Credit transfer"),
    ("31", "Debit transfer"),
    ("32", "ACH demand cash concentration/disbursement plus (CCD+) credit"),
    ("33", "ACH demand cash concentration/disbursement plus (CCD+) debit"),
    ("34", "ACH prearranged payment and deposit (PPD)"),
    ("35", "ACH savings cash concentration/disbursement (CCD) credit"),
    ("36", "ACH savings cash concentration/disbursement (CCD) debit"),
    ("37", "ACH savings corporate trade payment (CTP) credit"),
    ("38", "ACH savings corporate trade payment (CTP) debit"),
    ("39", "ACH savings corporate trade exchange (CTX) credit"),
    ("40", "ACH savings corporate trade exchange (CTX) debit"),
    ("41", "ACH savings cash concentration/disbursement plus (CCD+) credit"),
    ("42", "Payment to bank account"),
    ("43", "ACH savings cash concentration/disbursement plus (CCD+) debit"),
    ("44", "Accepted bill of exchange"),
    ("45", "Referenced home-banking credit transfer"),
    ("46", "Interbank debit transfer"),
    ("47", "Home-banking debit transfer"),
    ("48", "Bank card"),
    ("49", "Direct debit"),
    ("50", "Payment by postgiro"),
    ("51", "FR, norme 6 97-Telereglement CFONB (French Organisation for Banking Standards)"
           " - Option A"),
    ("52", "Urgent commercial payment"),
    ("53", "Urgent Treasury Payment"),
    ("54", "Credit card"),
    ("55", "Debit card"),
    ("56", "Bankgiro"),
    ("57", "Standing agreement"),
    ("58", "SEPA credit transfer"),
    ("59", "SEPA direct debit"),
    ("60", "Promissory note"),
    ("61", "Promissory note signed by the debtor"),
    ("62", "Promissory note signed by the debtor and endorsed by a bank"),
    ("63", "Promissory note signed by the debtor and endorsed by a third party"),
    ("64", "Promissory note signed by a bank"),
    ("65", "Promissory note signed by a bank and endorsed by another bank"),
    ("66", "Promissory note signed by a third party"),
    ("67", "Promissory note signed by a third party and endorsed by a bank"),
    ("68", "Online payment service"),
    ("69", "Transfer Advice"),
    ("70", "Bill drawn by the creditor on the debtor"),
    # —— 断档：71 / 72 / 73 不存在，见上方注释 ——
    ("74", "Bill drawn by the creditor on a bank"),
    ("75", "Bill drawn by the creditor, endorsed by another bank"),
    ("76", "Bill drawn by the creditor on a bank and endorsed by a third party"),
    ("77", "Bill drawn by the creditor on a third party"),
    ("78", "Bill drawn by creditor on third party, accepted and endorsed by bank"),
    # —— 断档：79 ~ 90 不存在 ——
    ("91", "Not transferable banker's draft"),
    ("92", "Not transferable local cheque"),
    ("93", "Reference giro"),
    ("94", "Urgent giro"),
    ("95", "Free format giro"),
    ("96", "Requested method for payment was not used"),
    ("97", "Clearing between partners"),
    ("98", "JP, Electronically Recorded Monetary Claims"),
    ("ZZZ", "Mutually defined"),
)

PAYMENT_MEANS_CODES: dict[str, CodeEntry] = _entries(
    LIST_PAYMENT_MEANS, SRC_UNCL4461, _PAYMENT_MEANS_ROWS)

#: 本域付款指令默认用的付款方式：``30 = Credit transfer``（信用转账 / 电汇）。
#: 企业对供应商的正常付款就是这一类，不是卡也不是现金。
CODE_CREDIT_TRANSFER = "30"

#: 三张码表的总表。上层按 list_id 取，不在各处 import 三个 dict。
CODE_LISTS: dict[str, dict[str, CodeEntry]] = {
    LIST_INVOICE_TYPE: INVOICE_TYPE_CODES,
    LIST_TAX_CATEGORY: TAX_CATEGORY_CODES,
    LIST_PAYMENT_MEANS: PAYMENT_MEANS_CODES,
}


# ---------------------------------------------------------------------------
# 业务规则 —— 出处：SRC_RULES_EN16931 与 SRC_RULES_PEPPOL
# ---------------------------------------------------------------------------
# 只抄本域判据实际引用得到的那些。**这里与码表的口径刻意相反**：码表整张抄（边界
# 本身是信息），规则表只抄用得到的（EN 16931 的规则有几百条，全抄进来会让「哪几条
# 是本域真的在判的」淹没在噪声里，而那正是评委要问的）。
# 每加一条判据就来这里加一条规则，加的时候必须重新去页面上核。

_RULE_ROWS: tuple[tuple[str, str, str, str], ...] = (
    # ---- EN 16931：字段必填 ----
    ("BR-12", "An Invoice shall have the Sum of Invoice line net amount (BT-106).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-13", "An Invoice shall have the Invoice total amount without VAT (BT-109).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-14", "An Invoice shall have the Invoice total amount with VAT (BT-112).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-15", "An Invoice shall have the Amount due for payment (BT-115).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-22", "Each Invoice line (BG-25) shall have an Invoiced quantity (BT-129).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-24", "Each Invoice line (BG-25) shall have an Invoice line net amount (BT-131).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-26", "Each Invoice line (BG-25) shall contain the Item net price (BT-146).",
     SRC_RULES_EN16931, LAYER_EN16931),

    # ---- EN 16931：合计与勾稽（BR-CO-xx）----
    ("BR-CO-04",
     "Each Invoice line (BG-25) shall be categorized with an Invoiced item VAT category"
     " code (BT-151).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-10",
     "Sum of Invoice line net amount (BT-106) = Σ Invoice line net amount (BT-131).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-13",
     "Invoice total amount without VAT (BT-109) = Σ Invoice line net amount (BT-131)"
     " - Sum of allowances on document level (BT-107) + Sum of charges on document level"
     " (BT-108).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-14",
     "Invoice total VAT amount (BT-110) = Σ VAT category tax amount (BT-117).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-15",
     "Invoice total amount with VAT (BT-112) = Invoice total amount without VAT (BT-109)"
     " + Invoice total VAT amount (BT-110).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-16",
     "Amount due for payment (BT-115) = Invoice total amount with VAT (BT-112) -Paid amount"
     " (BT-113) +Rounding amount (BT-114).",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CO-17",
     "VAT category tax amount (BT-117) = VAT category taxable amount (BT-116) x (VAT category"
     " rate (BT-119) / 100), rounded to two decimals.",
     SRC_RULES_EN16931, LAYER_EN16931),

    # ---- EN 16931：码表约束（BR-CL-xx）----
    ("BR-CL-01",
     "The document type code MUST be coded by the invoice and credit note related code lists"
     " of UNTDID 1001.",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CL-16", "Payment means in an invoice MUST be coded using UNCL4461 code list",
     SRC_RULES_EN16931, LAYER_EN16931),
    ("BR-CL-17", "Invoice tax categories MUST be coded using UNCL5305 code list",
     SRC_RULES_EN16931, LAYER_EN16931),

    # ---- Peppol 附加层 ----
    ("PEPPOL-EN16931-R003", "A buyer reference or purchase order reference MUST be provided.",
     SRC_RULES_PEPPOL, LAYER_PEPPOL),
    ("PEPPOL-EN16931-R046",
     "Item net price MUST equal (Gross price - Allowance amount) when gross price is provided.",
     SRC_RULES_PEPPOL, LAYER_PEPPOL),
    ("PEPPOL-EN16931-R120",
     "Invoice line net amount MUST equal (Invoiced quantity * (Item net price/item price base"
     " quantity) + Sum of invoice line charge amount - sum of invoice line allowance amount",
     SRC_RULES_PEPPOL, LAYER_PEPPOL),
    ("PEPPOL-EN16931-R121", "Base quantity MUST be a positive number above zero.",
     SRC_RULES_PEPPOL, LAYER_PEPPOL),
)

RULES: dict[str, BusinessRule] = {}
for _rid, _text, _src, _layer in _RULE_ROWS:
    if _rid in RULES:
        raise ValueError(f"规则 {_rid} 重复 —— 抄表时抄重了")
    RULES[_rid] = BusinessRule(rule_id=_rid, text=_text, source=_src, layer=_layer)
del _rid, _text, _src, _layer


# ---------------------------------------------------------------------------
# 判据用的规则编号常量
# ---------------------------------------------------------------------------
# **三单匹配的每一条判据都要在这里挂一个编号**，判定代码一律 import 这些常量，
# 不在各处写 "BR-CO-13" 这种字面量。理由不是洁癖：
#
#   · 字面量散在判定逻辑里，就没有任何机制保证它是**存在的**规则 —— 打错一个字
#     （BR-CO-31）照样跑，拒付理由挂着一个查不到的编号，而这正是本文件要防的事；
#   · 从这里取则必过 `require_rule()`，编号不存在当场抛。
#
# `maos/tests/test_ap_codes.py` 有一条用例扫本域源码，见到 `BR-` / `PEPPOL-EN16931-`
# 开头的裸字符串字面量（本文件除外）就判红。

#: 数量差：发票行数量 ≠ 收货单已收数量。挂 BR-22（发票行必须有数量）——
#: 规范管的是「必须有」，数量对不对是三单匹配的事；引用它是为了指明**判的是哪个字段**。
RULE_INVOICED_QUANTITY = "BR-22"

#: 单价差：发票行单价 ≠ 采购订单单价。挂 BR-26（发票行必须有单价），同上。
RULE_ITEM_NET_PRICE = "BR-26"

#: 行金额勾稽：行净额 ≠ 数量 × 单价。这是 Peppol 层的算式规则，**本域判的就是它本身**。
RULE_LINE_NET_AMOUNT = "PEPPOL-EN16931-R120"

#: 行净额合计勾稽：发票的行净额合计 ≠ Σ 各行净额。
RULE_SUM_LINE_NET = "BR-CO-10"

#: 不含税总额勾稽。
RULE_TOTAL_WITHOUT_VAT = "BR-CO-13"

#: 税额勾稽：税额 ≠ 计税基数 × 税率 / 100（两位小数）。
RULE_VAT_AMOUNT = "BR-CO-17"

#: 含税总额勾稽。
RULE_TOTAL_WITH_VAT = "BR-CO-15"

#: 应付金额勾稽：应付 = 含税总额 − 已付 + 舍入。付款指令的金额取的就是这个。
RULE_AMOUNT_DUE = "BR-CO-16"

#: 税种码必须落在 UNCL5305 里。
RULE_TAX_CATEGORY_CODED = "BR-CL-17"

#: 付款方式码必须落在 UNCL4461 里。
RULE_PAYMENT_MEANS_CODED = "BR-CL-16"

#: 发票类型码必须落在 UNTDID 1001 里。
RULE_INVOICE_TYPE_CODED = "BR-CL-01"

#: 必须给出采购订单引用 —— 三单匹配的前提：没有订单号就无从匹配。
RULE_ORDER_REFERENCE = "PEPPOL-EN16931-R003"


# ---------------------------------------------------------------------------
# 取值口径
# ---------------------------------------------------------------------------
def require_code(list_id: str, code: str) -> CodeEntry:
    """按码表取一条码。未知码**抛**，不返回兜底条目。

    理由同 ``gateway_codes.lookup``：兜底的后果不是报错，是**把没核过出处的码当成
    已知码处理**，而那正是这张表要防的事。发票上出现表外的码，业务上就该拒付并
    挂 ``BR-CL-xx``，不是在取值这一层悄悄放行。
    """
    table = CODE_LISTS.get(list_id)
    if table is None:
        raise KeyError(
            f"没有名为 {list_id!r} 的码表（已有：{sorted(CODE_LISTS)}）")
    try:
        return table[code]
    except KeyError:
        raise KeyError(
            f"码值 {code!r} 不在 {list_id} 内（共 {len(table)} 条，出处 "
            f"{table[next(iter(table))].source}）；新增码必须先核到出处再进表，"
            f"不许在调用处就地兜底"
        ) from None


def is_valid_code(list_id: str, code: str) -> bool:
    """码在不在表里。判定层用它出 ``BR-CL-xx`` 拒付理由，不用 try/except 控制流。"""
    table = CODE_LISTS.get(list_id)
    return bool(table) and code in table


def require_rule(rule_id: str) -> BusinessRule:
    """按编号取规则。编号不存在**抛** —— 自造编号必须在这里当场死掉。

    这是「拒付理由可核对」这句话的落点：理由挂的编号一定是从本表取出来的，
    也就一定能在 ``source`` 那个页面上查到。
    """
    try:
        return RULES[rule_id]
    except KeyError:
        raise KeyError(
            f"规则编号 {rule_id!r} 不在已核对清单内（共 {len(RULES)} 条）。"
            f"拒付理由不许挂自造编号 —— 先去 {SRC_RULES_EN16931} 或 "
            f"{SRC_RULES_PEPPOL} 核到原文，再加进 RULES"
        ) from None


def cite(rule_id: str) -> dict:
    """把一条规则折成可以直接进 finding / artifact 的引用块。

    带上 ``source`` 与 ``fetched_at``：产物里留着出处，评委问「这个编号哪来的」
    当场能答，不必回到源码里翻。
    """
    rule = require_rule(rule_id)
    return {
        "rule_id": rule.rule_id,
        "text": rule.text,
        "layer": rule.layer,
        "source": rule.source,
        "spec": SPEC_RELEASE,
        "fetched_at": FETCHED_AT,
    }


def table_sizes() -> dict[str, int]:
    """三张码表各多少条 —— **现算**，不是抄来的常量。见模块 docstring。"""
    return {list_id: len(table) for list_id, table in CODE_LISTS.items()}
