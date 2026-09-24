#!/usr/bin/env python3
"""客服话术库生成器 —— p12 契约 §1.5 的 19 个方案编号，生成到 `scenarios/cs/kb/cs_scripts.json`。

    python3 scripts/gen_cs_kb.py            # 生成（覆盖写）
    python3 scripts/gen_cs_kb.py --check    # 只校验磁盘上那份是不是当前生成器的产物

## 为什么照 gen_refund_kb.py 的写法

同一套理由（那边模块头写全了）：行按 `kb.DOC_COLUMNS` 逐列构造，列清单漂了当场停；
`embedding` 恒为 None（向量由入库方现算）；时间戳从固定基准推、不用 `now()`；
行按 doc_id 排序、落盘 `sort_keys=True`。验收判据是「连跑两次逐字节相同」，
`--check` 就是那条判据的命令形态。

## 数据性质（铁律 3）

**全部合成。** ADP 课程原文不在本环境，话术由 task-t168 自写；格式照 ADP 课程 3.3
（方案编号｜适用场景｜处理原则｜标准话术）加同义词与例句，不照抄课程原文。
每篇 body 都带 `synthetic: true`，产物的 `_provenance` 里也写着这段分界。

## 标准话术在出库前先过一遍自检

p12 没有任何观察来源，后置校验（契约 §1.4 `check_reply`）拿到的 observations 恒为空，
于是话术里**一个状态字眼都说不得**（`types.STATUS_PATTERNS`），也不许承诺时限、金额、
结果，不许露出内部口径（审批、规则编号、内部岗位名、系统名、斜杠写法）。
这些在生成期就查（`_check_script`）：写出一篇不合格的话术，生成器当场停，
不产半份语料 —— 不合格的话术要是落了盘，症状是前台每回它一次就被校验打回、改走转人工。

## 例句为什么要写错别字和语气词

检索侧（`maos/domain/cs/scripts.py`）按字符二元组算重合度。客户真实的说法是「咋还没发货啊」
「发获时间」这种，例句全写成书面语，口语一来重合度就掉到门槛以下，走兜底。
所以每篇至少五条例句，口语、错别字、语气词三样都要有；同义词写常见的短说法。
「错别字」与「语气词」两样由 `_check_catalog` 在生成期查：每篇的 `typos` 登记本篇例句里
至少一对 (错写, 本字)（拼音输入法的同音错字，如「发获 / 发货」「退宽 / 退款」），
例句里没有登记的错写、或 `typos` 为空，生成器当场停。`typos` 不进产物。
例句不许跨篇重复（生成期查），否则两篇同分，排序只能靠 doc_id 碰运气。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from maos import kb                                    # noqa: E402
from maos.domain.cs import types as T                  # noqa: E402
from maos.domain.cs.scripts import tail_forms          # noqa: E402

OUT_PATH = os.path.join(REPO_ROOT, "scenarios", "cs", "kb", "cs_scripts.json")

#: 话术库的租户。契约 §1.5 冻结：演示租户 tnt-demo，别的租户要话术就另灌一份。
TENANT_ID = "tnt-demo"

#: 所有 `created_at` 的基准。固定字面量而不是 `now()`（确定性）。
_EPOCH = datetime(2026, 9, 24, tzinfo=timezone.utc)

_NOTE = ("客服话术库（p12 契约 §1.5）：19 个方案编号，kind=cs_script、biz_type=cs、"
         "租户 tnt-demo。body 是 JSON：scheme_no / scene / principle / script / intent / "
         "handoff / synonyms / examples / synthetic。handoff 非空的话术，script 是给客户的"
         "过渡话术（要核实订单、已为其转人工）。")

_PROVENANCE = (
    "本文件由 scripts/gen_cs_kb.py 生成，勿手改 —— 手改的那一行下次重跑就没了。"
    "全部为合成数据：ADP 课程原文不在本环境，话术、同义词、例句均由 task-t168 自写；"
    "格式照 ADP 课程 3.3（方案编号｜适用场景｜处理原则｜标准话术），不照抄课程原文。"
    "不得作为真实企业话术或真实客户语料引用。"
)

#: 标准话术里不许出现的字样（状态字眼另由 `types.STATUS_PATTERNS` 查）。
#: 三组：内部口径（斜杠写法、审批、规则编号、系统名、内部岗位名）、
#: 时限承诺、金额 / 结果承诺。
_FORBIDDEN_IN_SCRIPT = (
    "/", "审批", "规则编号", "MAOS", "maos",
    "售后主管", "财务复核", "支付运维", "区域经理", "主管",
    "supervisor", "finance", "payment_ops", "region_manager",
    "天内", "小时内", "工作日内", "分钟内", "当天", "马上", "立即", "立刻", "尽快",
    "保证", "一定", "肯定", "承诺", "元", "块钱", "全额", "包退", "包换", "补发",
    "赔", "退款成功",
)
_DIGIT_RE = re.compile(r"[0-9０-９]")

#: 每篇例句下限（契约派单：≥ 5，口语 / 错别字 / 语气词都要有）。
MIN_EXAMPLES = 5
#: 语气词：每篇至少一条例句带其中一个字（生成期查；测试另有一份，不从这里取）。
_PARTICLES = "啊呀吗嘛呢吧哦哈不么啦拉呗亲哇"


# ---------------------------------------------------------------- 话术目录
#: 编号、意图、适用场景、转人工标记四列逐字照契约 §1.5 的冻结目录。
#: 其余四列（处理原则、标准话术、同义词、例句）由本轨自写。
_LOOKUP = T.HANDOFF_NEEDS_ORDER_LOOKUP

SCRIPTS = (
    {
        "scheme_no": "LOG-001", "intent": T.INTENT_LOGISTICS, "handoff": "",
        "scene": "下单后多久发货（发货时效）",
        "principle": "发货时效以商品详情页的说明为准，不替仓库承诺具体时间；"
                     "客户问的是某一笔订单为什么还没发，按 LOG-004 转人工核实。",
        "script": "您好，商品的发货时效以商品详情页的说明为准，仓库会按付款顺序安排出库。"
                  "如果您想了解某一笔订单的具体进度，可以告诉我，我帮您转人工核实。",
        "synonyms": ["多久发货", "几天发货", "什么时候发货", "发货时间", "发货时效",
                     "多久能发出", "下单后多久发", "啥时候发货", "几天能寄出"],
        "examples": ["下单之后一般多久发货啊", "你们家几天能发出来呀", "今天拍的啥时候能发货",
                     "请问发获时间是多久", "买了东西一般要等几天才发嘛", "付款后多长时间出库呢"],
        "typos": (("发获", "发货"),),
    },
    {
        "scheme_no": "LOG-002", "intent": T.INTENT_LOGISTICS, "handoff": "",
        "scene": "用哪家快递、配送范围",
        "principle": "合作快递与配送范围以结算页显示为准；收货地址能在结算页正常提交即在配送范围内；"
                     "指定快递只登记需求，不承诺满足。",
        "script": "您好，我们合作的快递公司和可配送的地区以下单时结算页面的显示为准；"
                  "收货地址能在结算页正常提交，就说明该地区在配送范围内。"
                  "如果您有指定快递的需求，可以在订单备注里说明，能否安排以仓库实际情况为准。",
        "synonyms": ["发什么快递", "用哪家快递", "哪个快递公司", "配送范围", "能不能送到",
                     "送不送得到", "发顺丰吗", "快递公司", "哪家物流"],
        "examples": ["你们发的是什么快递呀", "能发顺丰不", "新疆能送到吗", "偏远地区配送吗亲",
                     "用的哪家快第啊", "我这边乡下能不能送"],
        "typos": (("快第", "快递"),),
    },
    {
        "scheme_no": "LOG-003", "intent": T.INTENT_LOGISTICS, "handoff": "",
        "scene": "运费规则、包邮条件",
        "principle": "运费与包邮门槛以商品页和结算页为准，不口头承诺包邮或减免运费。",
        "script": "您好，运费和包邮条件以商品页面与结算页面显示的为准，不同商品、不同收货地区可能不一样。"
                  "下单前您可以在结算页看到这一单需要支付的运费。",
        "synonyms": ["运费多少", "包邮吗", "包邮条件", "邮费", "运费怎么算", "满多少包邮",
                     "要不要运费", "免运费", "运费规则"],
        "examples": ["包邮吗亲", "运费咋算的", "邮费要多少钱啊", "买几件才能包油",
                     "满多少免运费呀", "这个还要另外付运费吗"],
        "typos": (("包油", "包邮"),),
    },
    {
        "scheme_no": "LOG-004", "intent": T.INTENT_LOGISTICS, "handoff": _LOOKUP,
        "scene": "查某个订单的物流进度、催发货",
        "principle": "要看具体订单的物流进度，本期前台不查单，转人工核实；"
                     "不替物流给出任何进度或时间。",
        "script": "您好，查询具体订单的物流进度需要核实您的订单信息，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会根据订单情况和您继续沟通。",
        "synonyms": ["查物流", "物流到哪了", "快递到哪了", "催发货", "订单进度",
                     "怎么还没发货", "物流信息", "物流没更新", "包裹到哪了"],
        "examples": ["我的快递到哪了啊", "帮我查下物流呗", "我那单咋还没发货", "催一下我的订单行不",
                     "物流好几天没更新了呢", "我昨天买的耳机现在到哪儿了",
                     "物流咋一直没跟新"],
        "typos": (("跟新", "更新"),),
    },
    {
        "scheme_no": "LOG-005", "intent": T.INTENT_LOGISTICS, "handoff": _LOOKUP,
        "scene": "改收货地址、改约配送",
        "principle": "改地址、改配送时间要先核实订单与出库情况，前台不改单，转人工确认。",
        "script": "您好，修改收货地址或配送时间需要先核实您的订单信息，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会和您确认能否修改。",
        "synonyms": ["改地址", "修改收货地址", "地址填错了", "换个地址", "改配送时间",
                     "改约配送", "改收货人", "换收货地址"],
        "examples": ["地址写错了能改吗", "我想换个收货地址", "能不能帮我改下地止",
                     "收件人名字填错了咋办", "送货时间能改到周末不", "快递能不能晚两天再送呀"],
        "typos": (("地止", "地址"),),
    },
    {
        "scheme_no": "LOG-006", "intent": T.INTENT_LOGISTICS, "handoff": _LOOKUP,
        "scene": "物流显示签收但本人没收到、包裹丢失或破损",
        "principle": "签收异常、丢件、破损都要核实具体订单与物流记录，转人工跟进；"
                     "不承诺补发、退款或任何赔付。",
        "script": "您好，很抱歉给您带来不便。包裹签收异常、丢失或破损的情况需要核实您的订单和物流记录，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会继续跟进。",
        "synonyms": ["显示签收没收到", "包裹丢了", "快递丢件", "包裹破损", "东西压坏了",
                     "没收到货", "被别人签收", "快递丢了", "外包装破了"],
        "examples": ["显示签收了但我没收到啊", "快递被谁签了我都不知道", "包裹丢了怎么办",
                     "箱子都压扁了东西也坏了", "物流说送到了可我没拿到呢", "快递盒子破了个大洞",
                     "显示钱收了可我压根没收到"],
        "typos": (("钱收", "签收"),),
    },
    {
        "scheme_no": "PAY-001", "intent": T.INTENT_REFUND_PAYMENT, "handoff": "",
        "scene": "退款多久能退回（原路退回规则，以支付渠道为准）",
        "principle": "退款按原支付方式原路退回，到账时间以支付渠道的处理为准，不承诺具体时间；"
                     "客户问的是某一笔退款的进度，按 PAY-003 转人工核实。",
        "script": "您好，退款会按您付款时使用的支付方式原路退回，具体的到账时间以支付渠道的处理进度为准，"
                  "不同渠道会有差异。如果您想确认某一笔退款的进度，可以告诉我，我帮您转人工核实。",
        "synonyms": ["退款多久到", "退款退到哪", "原路退回", "钱退到哪里", "退款到账时间",
                     "多久能收到退款", "退款几天到", "退款退回原账户"],
        "examples": ["退款一般多久能到账啊", "钱会退回到哪里呀", "退的钱是原路返回吗",
                     "退款要几天才到", "微信付的钱退款退到零钱吗", "退款到帐要多久呢"],
        "typos": (("到帐", "到账"),),
    },
    {
        "scheme_no": "PAY-002", "intent": T.INTENT_REFUND_PAYMENT, "handoff": "",
        "scene": "支持哪些支付方式",
        "principle": "支付方式以收银台显示的选项为准，不口头承诺某种支付方式一定可用。",
        "script": "您好，本店支持的支付方式以下单时收银台显示的选项为准，"
                  "您可以在付款页面选择适合自己的方式。",
        "synonyms": ["支付方式", "能用什么付款", "支持微信支付吗", "支付宝能付吗",
                     "可以刷信用卡吗", "花呗分期", "货到付款", "付款方式"],
        "examples": ["能用支付宝付吗", "可以花呗分期不", "支持货到付款嘛", "信用卡能刷吗亲",
                     "付款方式有哪些啊", "微信能支付不", "能用云闪付么",
                     "能用支付包付款吗"],
        "typos": (("支付包", "支付宝"),),
    },
    {
        "scheme_no": "PAY-003", "intent": T.INTENT_REFUND_PAYMENT, "handoff": _LOOKUP,
        "scene": "查某笔退款的进度、钱到没到",
        "principle": "某一笔退款的进度与到账与否是外部支付渠道的事实，本期前台不查单、"
                     "不读支付观察，转人工核实；不替渠道说到没到。",
        "script": "您好，查询某一笔退款的具体进度需要核实您的订单和支付信息，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会帮您核实这笔退款的情况。",
        "synonyms": ["退款进度", "退款到哪了", "钱到没到", "退款怎么还没到", "查退款",
                     "退款没收到", "退款还没到账", "退款处理到哪一步"],
        "examples": ["我的退款到哪一步了", "退款咋还没到账啊", "帮我查下那笔退款呗",
                     "说退款了钱还没收到呢", "申请退款好几天了没动静", "我的钱退了没有啊",
                     "我的退宽到哪了呀"],
        "typos": (("退宽", "退款"),),
    },
    {
        "scheme_no": "PAY-004", "intent": T.INTENT_REFUND_PAYMENT, "handoff": _LOOKUP,
        "scene": "支付失败、重复扣款",
        "principle": "支付失败、重复扣款要核对具体订单与支付记录，转人工；不替渠道判断是否扣款成功。",
        "script": "您好，支付失败或重复扣款的情况需要核实您的订单和支付记录，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会帮您核对这笔款项。",
        "synonyms": ["支付失败", "付款失败", "重复扣款", "扣了两次钱", "扣款了订单没成功",
                     "付不了款", "多扣钱", "重复付款"],
        "examples": ["付款一直失败咋回事", "怎么扣了我两次钱", "钱扣了订单却没生成",
                     "支付不成功啊一直转圈", "被重复扣费了呀", "付钱的时候提示失败了呢",
                     "怎么老是支付失拜啊"],
        "typos": (("失拜", "失败"),),
    },
    {
        "scheme_no": "PAY-005", "intent": T.INTENT_REFUND_PAYMENT, "handoff": "",
        "scene": "开发票",
        "principle": "发票在订单详情页自助申请，类型与开具进度以页面显示为准；抬头有误走页面的重开入口。",
        "script": "您好，如需开具发票，可以在订单详情页找到申请发票的入口，按页面提示填写抬头等信息后提交；"
                  "发票类型和开具进度以页面显示为准。",
        "synonyms": ["开发票", "要发票", "开票", "增值税发票", "电子发票", "发票抬头",
                     "补开发票", "专用发票"],
        "examples": ["能开发票吗", "我要开个专票", "发票怎么申请啊", "电子发漂在哪下载",
                     "公司报销要发票咋弄", "发票抬头写错了能重开不"],
        "typos": (("发漂", "发票"),),
    },
    {
        "scheme_no": "RET-001", "intent": T.INTENT_RETURN_EXCHANGE, "handoff": "",
        "scene": "七天无理由退货的条件",
        "principle": "适用范围与条件以商品页标注和平台规则为准，商品需完好、不影响二次销售；"
                     "不替具体订单判定能不能退，要办具体订单按 RET-005 转人工。",
        "script": "您好，支持七天无理由退货的商品会在商品页面标注，一般要求商品及包装保持完好、"
                  "不影响二次销售，具体以商品页面和平台规则的说明为准。"
                  "如果您想为某个订单办理退货，可以告诉我，我帮您转人工核实。",
        "synonyms": ["七天无理由", "无理由退货", "不想要了能退吗", "退货条件", "拆封能退吗",
                     "七天能退吗", "无理由退换"],
        "examples": ["七天无理由退货有啥条件啊", "拆了包装还能退吗", "不喜欢可以退不",
                     "买错了能无理由退嘛", "七天内可以随便退吗", "用过一次还能退货吗亲",
                     "七天无理有退货吗"],
        "typos": (("无理有", "无理由"),),
    },
    {
        "scheme_no": "RET-002", "intent": T.INTENT_RETURN_EXCHANGE, "handoff": "",
        "scene": "退货流程怎么操作",
        "principle": "讲自助流程：订单详情页申请售后、按页面给的地址寄回、填写寄回单号；"
                     "客户要前台替他办某一单时按 RET-005 转人工。",
        "script": "您好，退货可以在订单详情页点击申请售后，选择退货原因并按页面提示提交；"
                  "提交后请按页面给出的退货地址寄回商品，并填写寄回的快递单号，处理进度以售后页面显示为准。",
        "synonyms": ["怎么退货", "退货流程", "退货步骤", "在哪申请退货", "退货怎么操作",
                     "退货寄到哪", "退货入口", "申请售后"],
        "examples": ["退货咋操作啊", "在哪里申请退货呀", "退货的流程是怎样的", "东西要寄回哪里",
                     "退货入口找不到了", "怎么退或啊"],
        "typos": (("退或", "退货"),),
    },
    {
        "scheme_no": "RET-003", "intent": T.INTENT_RETURN_EXCHANGE, "handoff": "",
        "scene": "质量问题换货（要提供照片）",
        "principle": "质量问题走售后换货，请客户上传能看清问题的照片；处理结果以核实后的售后页面为准，"
                     "不在对话里承诺一定能换。",
        "script": "您好，如果商品存在质量问题，可以在订单详情页申请售后并选择换货，"
                  "同时上传能看清问题的商品照片，方便核实情况；处理结果以核实后售后页面的显示为准。",
        "synonyms": ["质量问题换货", "坏了能换吗", "有质量问题", "换货", "有瑕疵",
                     "拍照换货", "商品有问题", "换个新的"],
        "examples": ["东西有质量问题能换个新的吗", "刚用就坏了咋办", "收到的有瑕疵呀想换",
                     "换货要拍照片吗", "屏幕有划痕能换不", "质量太差了想换货呢",
                     "有瑕疵想换获可以吗"],
        "typos": (("换获", "换货"),),
    },
    {
        "scheme_no": "RET-004", "intent": T.INTENT_RETURN_EXCHANGE, "handoff": "",
        "scene": "退货运费谁承担",
        "principle": "退货运费的承担方与退货原因、是否带运费险有关，以售后申请页面显示的规则为准，"
                     "不口头承诺报销运费。",
        "script": "您好，退货运费由哪一方承担，与退货原因以及商品是否带运费险有关，"
                  "具体以售后申请页面显示的规则为准；提交售后申请时，页面会提示这一单的运费承担方式。",
        "synonyms": ["退货运费谁出", "退货邮费", "运费险", "退货要自己付运费吗", "寄回运费",
                     "退货运费报销", "退回的运费"],
        "examples": ["退货运费谁出啊", "寄回来的邮费要我自己掏吗", "有运费险嘛",
                     "退回去的快递费能报销不", "质量问题退货运费谁承担呀", "退货还得自己付邮费么",
                     "退货的运废谁出呀"],
        "typos": (("运废", "运费"),),
    },
    {
        "scheme_no": "RET-005", "intent": T.INTENT_RETURN_EXCHANGE, "handoff": _LOOKUP,
        "scene": "为某个订单申请退货 / 退款（要办具体订单）",
        "principle": "要办具体订单的退货或退款，需要核实订单，本期前台不查单、不代办，转人工。",
        "script": "您好，为具体订单办理退货或退款需要核实您的订单信息，"
                  "我这边已为您转接人工客服，请您稍候，人工客服会和您确认这笔订单的售后办理。",
        "synonyms": ["帮我退货", "我要退款", "给我退了", "申请退款", "这单我要退",
                     "帮我办退货", "我要退货"],
        "examples": ["帮我把这单退了吧", "我不想要了给我退钱", "昨天买的鞋子我要退",
                     "给我办个退货呗", "这个订单申请退款", "刚下的单能帮我退掉不",
                     "帮我申情退款吧"],
        "typos": (("申情", "申请"),),
    },
    {
        "scheme_no": "GEN-001", "intent": T.INTENT_GENERAL, "handoff": "",
        "scene": "问候",
        "principle": "礼貌回应，引导客户直接描述问题。",
        "script": "您好，欢迎咨询，请问有什么可以帮您？"
                  "您可以直接描述遇到的问题，比如物流、退换货或支付方面的问题。",
        "synonyms": ["你好", "您好", "在吗", "有人吗", "哈喽", "早上好", "晚上好"],
        "examples": ["你好呀", "在吗在吗", "有人在不", "哈喽亲", "你嚎", "在马亲"],
        "typos": (("你嚎", "你好"), ("在马", "在吗")),
    },
    {
        "scheme_no": "GEN-002", "intent": T.INTENT_GENERAL, "handoff": "",
        "scene": "感谢与道别",
        "principle": "礼貌收尾，不追加推销。",
        "script": "不客气，很高兴能帮到您。之后如果还有其他问题，欢迎再来咨询，祝您生活愉快！",
        "synonyms": ["谢谢", "感谢", "好的谢谢", "再见", "拜拜", "没问题了", "辛苦了", "多谢"],
        "examples": ["谢谢啦", "好的感谢", "明白了谢谢你", "没事了再见哈", "辛苦了亲", "谢谢拉好滴"],
        "typos": (("谢谢拉", "谢谢啦"),),
    },
    {
        "scheme_no": "GEN-003", "intent": T.INTENT_GENERAL, "handoff": "",
        "scene": "人工客服的服务时间",
        "principle": "服务时间以店铺客服页面公示为准，不在话术里写死具体钟点；"
                     "客户点名要人工时走转人工触发词，不走本篇。",
        "script": "您好，人工客服的服务时间以店铺客服页面公示的时间为准；"
                  "在服务时间内，您回复「转人工」即可联系人工客服。",
        "synonyms": ["客服几点上班", "客服服务时间", "客服上班时间", "客服几点下班",
                     "晚上有客服吗", "周末有客服吗", "客服在线时间"],
        "examples": ["你们客服几点下班啊", "晚上还有客服在吗", "周末客服上班不",
                     "客服上班时间是几点呀", "半夜有没有客服呢", "客服啥时候在线哇",
                     "你们课服几点上班呀"],
        "typos": (("课服", "客服"),),
    },
)


def _at(offset: int) -> str:
    """第 `offset` 天的时间戳。确定性的唯一来源。"""
    return (_EPOCH + timedelta(days=int(offset))).isoformat()


def _body(payload: dict) -> str:
    """body 一律紧凑 JSON（口径同 gen_refund_kb._body）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _check_script(scheme_no: str, script: str) -> list[str]:
    """标准话术的出库自检。返回问题清单，空 = 合格。

    状态字眼用契约冻结的 `types.STATUS_PATTERNS`（后置校验扫的就是它），不另抄一份。
    """
    problems = []
    for pattern in T.STATUS_PATTERNS:
        found = pattern.search(script)
        if found:
            problems.append(f"{scheme_no}: 命中状态字眼 {found.group(0)!r}")
    for word in _FORBIDDEN_IN_SCRIPT:
        if word in script:
            problems.append(f"{scheme_no}: 含禁词 {word!r}")
    if _DIGIT_RE.search(script):
        problems.append(f"{scheme_no}: 含数字（时限 / 金额承诺的载体）")
    return problems


def _check_typos(spec: dict) -> list[str]:
    """错别字例句的自检：`typos` 是本篇例句里的 (错写, 本字) 对，至少一对。

    错写必须真出现在某条例句里；本字必须是本篇自己的说法（场景 / 同义词 / 例句 /
    话术里出现过）—— 错的是这篇要认的那个词，不是随便一个字；错写不许出现在场景、
    同义词、话术、处理原则里 —— 在那里出现就说明它不是错字。`typos` 只活在生成器里，
    不进 body（body 的键是契约 §1.5 冻结的）。
    """
    no = spec["scheme_no"]
    pairs = spec.get("typos") or ()
    if not pairs:
        return [f"{no}: 没有登记错别字例句（typos 为空）"]
    problems = []
    own = " ".join([spec["scene"], spec["script"], *spec["synonyms"], *spec["examples"]])
    formal = " ".join([spec["scene"], spec["script"], spec["principle"], *spec["synonyms"]])
    for wrong, right in pairs:
        if not wrong or wrong == right:
            problems.append(f"{no}: 错别字对 {(wrong, right)!r} 不成立")
            continue
        if not any(wrong in ex for ex in spec["examples"]):
            problems.append(f"{no}: 登记的错写 {wrong!r} 不在任何一条例句里")
        if right not in own:
            problems.append(f"{no}: 错写 {wrong!r} 对应的本字 {right!r} 不是本篇的说法")
        if wrong in formal:
            problems.append(f"{no}: 错写 {wrong!r} 出现在场景 / 同义词 / 话术 / 原则里")
    return problems


def _norm(text: str) -> str:
    return "".join(kb.tokenize(text))


def _check_catalog() -> None:
    """目录级自检：编号唯一、例句数够、handoff / intent 取值合法、变体不跨篇重复。"""
    problems: list[str] = []
    seen_no: set[str] = set()
    owner: dict[str, str] = {}
    for spec in SCRIPTS:
        no = spec["scheme_no"]
        if no in seen_no:
            problems.append(f"{no}: 编号重复")
        seen_no.add(no)
        if spec["intent"] not in T.INTENTS:
            problems.append(f"{no}: intent {spec['intent']!r} 不在 types.INTENTS")
        if spec["handoff"] and spec["handoff"] not in T.HANDOFF_REASONS:
            problems.append(f"{no}: handoff {spec['handoff']!r} 不在 types.HANDOFF_REASONS")
        if len(spec["examples"]) < MIN_EXAMPLES:
            problems.append(f"{no}: 例句只有 {len(spec['examples'])} 条，不足 {MIN_EXAMPLES}")
        if not any(ch in ex for ex in spec["examples"] for ch in _PARTICLES):
            problems.append(f"{no}: 没有一条带语气词的例句")
        problems.extend(_check_typos(spec))
        problems.extend(_check_script(no, spec["script"]))
        for variant in [spec["scene"], *spec["synonyms"], *spec["examples"]]:
            if not _norm(variant):
                problems.append(f"{no}: 变体 {variant!r} 规整后为空")
                continue
            # 撞车按检索侧「原样命中」的口径查（去句尾语气词后的各个形式）：两篇各登记了
            # 「退款吗」「退款呢」，客户说「退款啊」两篇都满分，谁排第一只能看 doc_id。
            for key in tail_forms(variant):
                if key in owner and owner[key] != no:
                    problems.append(f"{no}: 变体 {variant!r} 与 {owner[key]} 撞车（{key!r}）")
                owner.setdefault(key, no)
    if problems:
        raise SystemExit("话术目录自检不过：\n  - " + "\n  - ".join(problems))


def _doc(spec: dict, offset: int) -> dict:
    """按 `kb.DOC_COLUMNS` 造一行。列清单从常量取，不在这里手抄第二份。"""
    scheme_no = spec["scheme_no"]
    body = {
        "scheme_no": scheme_no,
        "scene": spec["scene"],
        "principle": spec["principle"],
        "script": spec["script"],
        "intent": spec["intent"],
        "handoff": spec["handoff"],
        "synonyms": list(spec["synonyms"]),
        "examples": list(spec["examples"]),
        "synthetic": True,
    }
    row = {
        "tenant_id": TENANT_ID, "doc_id": f"kb-cs-{TENANT_ID}-{scheme_no}",
        "biz_type": T.BIZ_TYPE_CS,
        "channel_id": None, "region": None, "sku": None,
        "policy_version": None, "workflow_version": None,
        "rule_no": scheme_no, "gateway_code": None, "kind": T.CS_KB_KIND,
        "title": spec["scene"], "body": _body(body), "embedding": None,
        "outcome": None, "source_case_id": None, "created_at": _at(offset),
    }
    missing = set(kb.DOC_COLUMNS) - set(row)
    extra = set(row) - set(kb.DOC_COLUMNS)
    if missing or extra:                     # 列清单漂了就当场停，不产半份语料
        raise SystemExit(f"列清单与 kb.DOC_COLUMNS 不一致：多 {sorted(extra)}，缺 {sorted(missing)}")
    return row


def build() -> str:
    """生成产物，返回文件内容。**不落盘** —— 落盘与校验共用这一份。"""
    _check_catalog()
    docs = [_doc(spec, idx) for idx, spec in enumerate(SCRIPTS)]
    docs.sort(key=lambda r: r["doc_id"])
    payload = {"_note": _NOTE, "_provenance": _PROVENANCE, "kb_doc": docs}
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="不写盘，只校验磁盘上那份与当前生成器的产物逐字节一致")
    args = parser.parse_args(argv)

    built = build()
    rel = os.path.relpath(OUT_PATH, REPO_ROOT)
    if args.check:
        try:
            with open(OUT_PATH, encoding="utf-8") as fh:
                on_disk = fh.read()
        except FileNotFoundError:
            print(f"{rel}: 磁盘上没有这份产物，请重跑 python3 scripts/gen_cs_kb.py")
            return 1
        if on_disk != built:
            print(f"{rel}: 与当前生成器的产物不一致，请重跑 python3 scripts/gen_cs_kb.py")
            return 1
        print(f"--check 通过：{rel} 与生成器逐字节一致")
    else:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            fh.write(built)
        print(f"已生成 {rel}")

    docs = json.loads(built)["kb_doc"]
    handoff = sum(1 for d in docs if json.loads(d["body"])["handoff"])
    print(f"  cs_script {len(docs):>3}（其中带转人工标记 {handoff}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
