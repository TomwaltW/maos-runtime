"""T188 · 例句扩充与近邻阈值重扫（review/p16-cs-contracts.md §1、§2「T188」）。

话术库按 p16 的九个通用缺口类别（``GAP_CLASSES_P16``，maos/tests/test_cs_eval_p12_t169.py）并进了
``scenarios/cs/kb/additions_p16.json`` 里声明的增补（``scripts/gen_cs_kb.py`` 读它）。本文件量的是
**另写的**说法 —— 不在增补里、不是开发集句子、不是话术库登记过的说法，按同样九类由本轨自写（没有打开
任何评测集；只凭类别名与话术标题）—— 在增补前后的表现，以及不该命中的说法的弃权。

* 「增补前」= 话术库去掉 additions_p16.json 里声明的每一条（monkeypatch ``corpus.load_corpus``），
  其余代码与阈值一字不动；「增补后」= 仓库里的话术库。两边都跑整条 p12 路径前台（不注入端口：
  触发词 → 词法 → 同义归一 → 近邻），一句一个新库。
* 「答对」= route + intent + 转人工原因 + 引用的那一篇都对（篇级，p16 的「零自信答错」口径）。
* 近邻阈值 / 差距重扫（开发集 + 自写句 + 复核轮的 173 句共享词汇探针，扫描表见 docs/DECISIONS.md
  task-t188）：正例读数在 0.25–0.34 一段相同，但门槛低于 0.34 时反例上近邻开始启用（0.30 → 2 句、0.28 → 6、
  0.25 → 8），差距 0.05 配门槛 0.25 时自写句里多出一句经近邻引错篇（:data:`MARGIN_PROBE_T188`）。取值不变
  （0.34 / 0.10），本文件钉「重扫之后仍是这两个数」与那句探针的反向。
* 复核轮：增补与自写句之间也跑 T169 的三道防抄判据（自写句不许是增补的近似抄本）；生成器的声明自检
  （``scripts/gen_cs_kb.py`` 的 ``merged_scripts``）逐条有反例。

实测数字写在各常量旁；地板只许抬。
"""

from __future__ import annotations

import importlib.util
import json
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import corpus, evaluate, scripts, similar
from maos.domain.cs import types as T
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.types import CHANNEL_WECHAT_KF
from maos.ingress.contracts import InboundMessage
from maos.tests import test_cs_eval_p12_t169 as t169
from maos.tests.test_cs_eval_p12_t169 import GAP_CLASSES_P16

TENANT_T188 = "tnt-demo"
KFID_T188 = "wk_eval"
ADDITIONS_PATH_T188 = corpus.CORPUS_PATH.with_name("additions_p16.json")

# ---------------------------------------------------------------------------
# 自写说法（按 GAP_CLASSES_P16 的九类；不带具体地名 / 商品 / 日期 / 数字 / 单号）
# ---------------------------------------------------------------------------
#: 类别 → [(句子, 该答 / 该转的方案编号)]。
CLASS_SENTENCES_T188: dict[str, tuple[tuple[str, str], ...]] = {
    "寒暄与致谢": (
        ("哈喽，有人在吗在吗", "GEN-001"), ("hi，在不在", "GEN-001"), ("你好你好", "GEN-001"),
        ("您好，请问现在有人吗", "GEN-001"), ("嘿，在线吗", "GEN-001"), ("在嘛在嘛", "GEN-001"),
        ("好的谢谢，没啥事了", "GEN-002"), ("明白啦，感恩", "GEN-002"), ("多谢解答，拜拜", "GEN-002"),
        ("了解了，谢谢哈", "GEN-002"), ("ok谢谢你呀", "GEN-002"), ("感谢感谢，辛苦啦", "GEN-002"),
    ),
    "客服时间": (
        ("你们客服几点开工", "GEN-003"), ("客服晚上几点就不回了", "GEN-003"),
        ("周六周日客服上不上班", "GEN-003"), ("过年期间有没有客服", "GEN-003"),
        ("客服的在线时间是多久到多久", "GEN-003"), ("凌晨还能找到客服吗", "GEN-003"),
        ("你们客服每天什么时候上线", "GEN-003"), ("中午客服休息吗", "GEN-003"),
        ("客服啥时间段有人", "GEN-003"), ("放长假客服还回消息吗", "GEN-003"),
        ("客服下班了吗现在", "GEN-003"),
    ),
    "发货时效与快递": (
        ("买了以后大概几天能发出", "LOG-001"), ("你们一般多久安排发货呀", "LOG-001"),
        ("发货快吗", "LOG-001"), ("下完单多长时间能寄", "LOG-001"), ("现货一般当天能发不", "LOG-001"),
        ("付款后几天出货呀", "LOG-001"),
        ("用的是什么物流公司", "LOG-002"), ("能不能发别的快递", "LOG-002"), ("农村地区送吗", "LOG-002"),
        ("寄的哪个快递啊", "LOG-002"), ("边远的地方能到吗", "LOG-002"),
        ("可以换成我指定的快递吗", "LOG-002"),
    ),
    "运费与包邮": (
        ("寄过来还得另外给钱不", "LOG-003"), ("这个包邮不包邮", "LOG-003"), ("运费要另外付吗", "LOG-003"),
        ("买多少钱可以免邮", "LOG-003"), ("下单要付快递费吗", "LOG-003"), ("运费可以减免吗", "LOG-003"),
        ("退货的邮费谁来付", "RET-004"), ("退回来的快递费谁负责", "RET-004"),
        ("退货运费需要我承担吗", "RET-004"), ("有运费险的话退货免费吗", "RET-004"),
        ("换货寄回去的运费谁出", "RET-004"),
    ),
    "订单异常要查单": (
        ("我的包裹咋还没到", "LOG-004"), ("物流一直停着没更新", "LOG-004"),
        ("帮我查一下快递到哪儿了", "LOG-004"), ("地址填错了想改一下", "LOG-005"),
        ("能不能把收货人改一下", "LOG-005"), ("我想改一下送货时间", "LOG-005"),
        ("明明没收到货却说已经送达", "LOG-006"), ("包裹里的东西摔坏了", "LOG-006"),
        ("快递丢了咋办", "LOG-006"), ("退款到现在还没到账", "PAY-003"), ("我申请的退款钱呢", "PAY-003"),
        ("退的钱迟迟没收到", "PAY-003"), ("被扣了双份钱", "PAY-004"), ("支付一直失败怎么回事", "PAY-004"),
    ),
    "退款规则与支付": (
        ("退款退到哪里呀", "PAY-001"), ("退款是原路退回吗", "PAY-001"), ("退款多久能到账", "PAY-001"),
        ("申请退款后钱几天能回来", "PAY-001"), ("退款会退到微信零钱吗", "PAY-001"),
        ("退款到账要多长时间", "PAY-001"),
        ("能用微信付款吗", "PAY-002"), ("刷卡付钱行不行", "PAY-002"), ("可以货到付款不", "PAY-002"),
        ("有哪些付款方式", "PAY-002"), ("能不能用花呗", "PAY-002"),
    ),
    "开票": (
        ("可以开发票吗", "PAY-005"), ("发票怎么开啊", "PAY-005"), ("我需要开个增值税专票", "PAY-005"),
        ("电子发票去哪找", "PAY-005"), ("发票抬头能写公司吗", "PAY-005"), ("能补开发票不", "PAY-005"),
        ("开票需要提供什么信息", "PAY-005"), ("发票开错了能重开吗", "PAY-005"),
        ("发票是纸的还是电子的", "PAY-005"), ("要报销，发票怎么弄", "PAY-005"),
    ),
    "退换货政策": (
        ("七天之内可以无理由退吗", "RET-001"), ("拆封了还能退吗", "RET-001"), ("穿过一次能退不", "RET-001"),
        ("退货的条件是啥", "RET-001"),
        ("怎么申请退货呀", "RET-002"), ("退货流程麻烦吗", "RET-002"), ("退货去哪里申请", "RET-002"),
        ("退货寄到什么地址", "RET-002"),
        ("东西坏了能换新的吗", "RET-003"), ("有质量问题可以换货吗", "RET-003"),
        ("收到的东西有瑕疵想换一个", "RET-003"), ("质量有问题换货要拍照吗", "RET-003"),
    ),
    "办具体退货退款": (
        ("这单帮我退了", "RET-005"), ("给我退款", "RET-005"), ("我想把这个订单退掉", "RET-005"),
        ("帮我办一下退款吧", "RET-005"), ("申请退货，这个不想要了", "RET-005"),
        ("我要把买的东西退回去", "RET-005"), ("帮我退了这个订单", "RET-005"), ("不想要了，帮我退", "RET-005"),
        ("请帮我发起退款", "RET-005"), ("我要退掉刚买的", "RET-005"),
    ),
}

#: 不该命中的说法：商品咨询、闲聊、无关问题、否定句、第三人称、售前 / 使用。
NEGATIVES_T188: tuple[str, ...] = (
    # 商品咨询
    "这个有黑色的吗", "这款适合送人吗", "鞋子码数标准吗", "这个质量怎么样", "有没有买一送一",
    "能便宜点吗", "这个是正品吗", "保质期多长", "这个手机壳适配吗", "有没有更大的尺寸",
    "这款什么时候上新", "店里有活动吗", "有现货吗",
    # 闲聊
    "今天好无聊", "你吃饭了吗", "你几岁了", "你喜欢猫吗", "周末去哪玩好", "心情不好", "哈哈哈哈",
    # 无关问题
    "怎么减肥", "股票能买吗", "明天会下雨吗", "给我讲个故事", "数学题怎么做", "附近哪有银行",
    "怎么做红烧肉",
    # 否定句
    "不用退了，我自己留着", "我不需要发票", "不是要换货，我只是问问", "没有要改地址",
    "我不打算退款了",
    # 第三人称
    "我朋友的快递丢了", "听说你们发货很快", "我同事买了觉得不错", "他们家包邮吗", "我妈想问能不能退",
    # 售前 / 使用
    "这个怎么用", "说明书在哪", "怎么清洗", "能刻字吗", "有没有礼盒包装", "能开个会员吗",
)

#: 每类「答对」的实测数（增补前 → 增补后）。增补后的数是地板，只许抬；增补前是去掉声明增补的
#: 固定话术库上的读数（基线说法由 T169 的指纹钉住，所以它是确定的），逐字钉住。
RECALL_BEFORE_T188 = {"寒暄与致谢": 10, "客服时间": 9, "发货时效与快递": 7, "运费与包邮": 6,
                      "订单异常要查单": 11, "退款规则与支付": 10, "开票": 9, "退换货政策": 12,
                      "办具体退货退款": 8}
RECALL_AFTER_FLOOR_T188 = {"寒暄与致谢": 10, "客服时间": 9, "发货时效与快递": 7, "运费与包邮": 7,
                           "订单异常要查单": 11, "退款规则与支付": 10, "开票": 9, "退换货政策": 12,
                           "办具体退货退款": 8}
#: 合计：增补前 82 / 103、增补后 83 / 103；落兜底的 14 → 13。
#: 复核轮（L2-2 / L3-1）：首版读数 84 → 88 里有一句是增补的近似抄本（中午客服休息吗 ~ 放假期间客服休息吗），
#: 另有三句自写句与增补撞 T169 的防抄判据，换成了另写的句子；撤掉 37 条「单个生僻词就够过词法门槛」的增补后，
#: 独立量得的增益只剩 +1（这个包邮不包邮）。增益小是实情：词法二元组重排对例句没有「须含本篇核心词」的约束，
#: 例句一多就把只沾一个词的无关句拉进来（BACKLOG task-t188），本轨只能撤增补、不能改词法。

#: 词法检索（不是近邻）在自写句上**增补前就有**的答错 / 转错篇（七句，增补前后逐句相同，近邻一次都没启用）：
#: 「多长时间」把发货问法拉到退款时长篇、「指定 / 别的快递」被查单篇的「快递」拉走、「快递费」被退货运费篇
#: 拉走、「申请 + 退」被流程篇 / 运费篇拉走、「退款钱呢」被办退款篇拉走。本轨不改词法（similar.py 只许动
#: 阈值），记 BACKLOG task-t188。只许这张封闭表，别的一律要对。
KNOWN_LEXICAL_WRONG_T188 = frozenset({
    "下完单多长时间能寄", "能不能发别的快递", "可以换成我指定的快递吗", "下单要付快递费吗",
    "我申请的退款钱呢", "申请退货，这个不想要了", "我要把买的东西退回去",
})
#: 不该命中的说法里**增补前就被词法答掉**的四句（否定句两句、第三人称一句、「开个会员」被开票篇拉走），
#: 增补前后相同，近邻一次都没启用。封闭表。
KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188 = frozenset({
    "不用退了，我自己留着", "我不需要发票", "他们家包邮吗", "能开个会员吗",
})

#: 差距探针：本句在缺省门槛（0.34 / 0.10）下落兜底；差距放到 0.05（门槛 0.25）时经近邻被引到
#: LOG-006（该是 LOG-004）—— 路由与意图都对、篇错了，正是 p16 要收的篇级自信答错。
MARGIN_PROBE_T188 = ("我的包裹咋还没到", "LOG-004", "LOG-006")

#: 与增补 / 话术共享词汇、但大多不是来问店铺政策的说法（复核 L3-1：单个生僻词就能让词法过 0.25）。
#: 按增补的用词反着写：把增补里的词拿出来放进闲聊 / 别的领域 / 去掉核心词的残句。复核轮用它们逐批定位并整条
#: 撤掉了 37 条增补（DECISIONS task-t188）；173 句全体在增补前后逐句对照，变了的只许是下面两张封闭表。
VOCAB_PROBES_T188: tuple[str, ...] = (
    "online game好玩吗", "express yourself", "放假期间去哪玩好", "大半夜的睡不着", "学校宿舍几点熄灯", "不合身的衣服怎么改", "开线了怎么缝",
    "invoice是什么意思", "退款周期是什么意思", "有色差吗", "清蒸蟹蟹好吃吗", "蟹蟹怎么做", "嗨嗨嗨今天好开心", "哈罗单车怎么骑", "好滴我去睡觉了",
    "拜拜了您嘞这首歌", "预售的票还有吗", "海外生活怎么样", "驿站在哪里", "吊牌价是多少", "凑单攻略有吗", "先用后付是什么", "搬家公司怎么找", "外包装好看吗",
    "学校放假了", "信用卡分期划算吗", "支付宝怎么注册", "电子票怎么检票", "报销流程是什么", "页面打不开", "好不好使", "配送员工资多少", "签收人是谁",
    "单号是什么意思", "下线了吗游戏", "收件人写谁", "手机卡住了", "钱包丢了", "不喜欢这个颜色", "发哪个好呢", "嗨嗨你好帅", "ok的我知道了", "好滴好滴",
    "问题解决了吗数学", "主播什么时候下线", "速度与激情好看吗", "预售款手机值得买吗", "不会要下雨吧", "海外代购靠谱吗", "我的件衣服好看吗", "等了好久了公交还不来",
    "我搬家了好累", "人不在家怎么浇花", "改天再约吧", "快递员工资高吗", "外包装盒能回收吗", "驿站老板人好吗", "支付不了房租咋整", "同一首歌听了好几次",
    "重复播放怎么关", "原路返回会不会迷路", "时效性是什么意思", "信用卡怎么还款", "支付宝余额宝收益多少", "电子票据是什么", "纸质书还是电子书好",
    "报销用的车票怎么打印", "拆开看了一眼好看", "不喜欢了怎么办", "教我一下怎么画画呗", "页面在哪个地方设置", "单号填错了怎么办", "流程图怎么画",
    "我要这一单的游戏皮肤", "麻烦帮我走个流程", "先用后付靠谱吗", "物流专业好就业吗", "地址栏怎么清空", "扣了分怎么办驾照", "钱扣了还显示没付款怎么回事",
    "哈罗哈罗这个词什么意思", "ok没问题了我去上课", "好滴那先这样吧我去吃饭", "谢啦兄弟帮我占座", "一般啥时候下线打游戏", "多快能跑完五公里", "网速怎么样啊",
    "不会要等很久吧排队", "能指定座位吗电影院", "海外留学要多少钱", "范围是什么意思", "免个单呗老板", "凑单满减怎么算", "来回的车费谁出", "自己掏钱请客吗",
    "商家入驻怎么弄", "卡着不动了电脑", "帮我看看这道题", "我的狗咋还在叫", "一直不动啥情况鼠标", "要改一下作文", "收件箱满了", "写成别人的名字了",
    "来得及不赶火车", "外面都烂了这个苹果", "放驿站的东西会丢吗", "没拿到奖学金", "还没到我家呢朋友", "说好的聚餐怎么还没", "显示完成了任务",
    "订单没了是啥意思外卖", "老是提示内存不足", "没付款的外卖", "原路走回去吗", "规定是什么", "只能用手机吗", "纸质版的书有吗", "填啥好呢名字", "吊牌怎么剪",
    "难道不能退吗这个人", "怎么弄啊这个游戏", "单号是什么意思呀", "走什么流程结婚", "不要了你拿走吧", "哈喽宝贝今天吃啥", "你们老板在吗", "ok那我明天再说",
    "好的拜拜晚安", "谢啦今天的电影票", "客服这个工作累吗", "发出去的朋友圈能删吗", "海外能不能用微信", "配送范围是什么意思数学", "运费险是什么东西",
    "包邮区是哪些省", "额外的作业要做吗", "免个费呗朋友", "退回去的礼物怎么说", "谁承担责任交通事故", "物流行业前景怎么样", "快递到哪儿了这首歌",
    "我的件毛衣起球了", "物流单是什么", "收货地址怎么写英文", "收件地址用英语怎么说", "改地址要去派出所吗", "外包装设计怎么做", "驿站怎么加盟", "签收是什么意思",
    "退款显示完成了是真的吗", "退的钱怎么记账", "到卡上的工资", "扣了钱的罚单", "提示失败怎么解决手机", "显示没付款的发票", "重复的句子怎么删",
    "原路返回是什么意思", "退款时效是什么意思", "信用卡分期利息多少", "先用后付是不是贷款", "纸质发票和电子发票区别", "开发票要交税吗", "没拆封的书能送人吗",
    "吊牌是什么", "不喜欢了就分手吧", "退货怎么弄啊这个成语", "教我一下怎么退烧呗", "退货在哪个页面点不出来", "退货地址在哪看不懂", "寄回的信单号填哪儿",
    "制量是什么词", "我要退这一单外卖", "我不要了你给我吧",
)
#: 增补后**新答了**（增补前兜底 / 转人工）的探针句 → 引用的那篇。封闭表，逐句判过「答这一篇不算答错」：
#: 招呼句答招呼、收尾句答收尾、问先用后付 / 退款时效 / 退货页面与地址的本来就是在问这几篇。
ACCEPTED_NEW_ANSWERS_T188: dict[str, str] = {
    "嗨嗨你好帅": "GEN-001", "好滴那先这样吧我去吃饭": "GEN-002",
    "先用后付是什么": "PAY-002", "先用后付靠谱吗": "PAY-002", "先用后付是不是贷款": "PAY-002",
    "退款时效是什么意思": "PAY-001",
    "退货在哪个页面点不出来": "RET-002", "退货地址在哪看不懂": "RET-002",
}
#: 增补后**新转人工**（增补前兜底）的探针句 → 转人工所据的那篇。p16 契约 §2 T189：转人工不计自信答错
#: （客户被交给真人，不拿到不对题的政策话术），但仍是误命中，记 BACKLOG task-t188；封闭表。
NEW_HANDOFFS_T188: dict[str, str] = {
    "卡着不动了电脑": "LOG-004", "一直不动啥情况鼠标": "LOG-004",
    "要改一下作文": "LOG-005", "写成别人的名字了": "LOG-005", "收件地址用英语怎么说": "LOG-005",
    "外面都烂了这个苹果": "LOG-006", "放驿站的东西会丢吗": "LOG-006",
    "还没到我家呢朋友": "PAY-003", "显示完成了任务": "PAY-003", "退款显示完成了是真的吗": "PAY-003",
    "老是提示内存不足": "PAY-004", "没付款的外卖": "PAY-004", "显示没付款的发票": "PAY-004",
    "我要退这一单外卖": "RET-005",
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _all_positive_t188() -> list[tuple[str, str, str]]:
    return [(cls, text, want) for cls, items in CLASS_SENTENCES_T188.items() for text, want in items]


def _additions_t188() -> list[dict]:
    return list(json.loads(ADDITIONS_PATH_T188.read_text(encoding="utf-8"))["additions"])


def _strip_additions_t188(monkeypatch) -> None:
    """「增补前」：灌库时去掉 additions_p16.json 里声明的每一条，别的一字不动。"""
    declared = {(e["scheme_no"], e["variant"]) for e in _additions_t188()}
    original = corpus.load_corpus

    def patched(path=corpus.CORPUS_PATH):
        rows = original(path)
        for row in rows:
            body = json.loads(row["body"])
            no = body["scheme_no"]
            body["synonyms"] = [v for v in body["synonyms"] if (no, v) not in declared]
            body["examples"] = [v for v in body["examples"] if (no, v) not in declared]
            row["body"] = json.dumps(body, ensure_ascii=False, sort_keys=True)
        return rows

    monkeypatch.setattr(corpus, "load_corpus", patched)


def _bodies_t188() -> dict[str, dict]:
    return {row["rule_no"]: json.loads(row["body"]) for row in corpus.load_corpus()}


def _scheme_of_t188(doc_id: str) -> str:
    return doc_id.removeprefix(f"kb-cs-{TENANT_T188}-")


def _one_t188(text: str) -> tuple[str, str, str, str, bool]:
    """p12 路径（不注入端口）跑一句：(route, intent, 原因, 引用编号, 是否经近邻)。一句一个新库。"""
    store = SqliteStore(":memory:")
    seed_cs_kb(store)
    desk = FrontDesk(store, CsConfig(tenants={KFID_T188: TENANT_T188}, handoff_target=None))
    res = desk.handle(InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id="wm_t188", sender="wm_t188",
                                     text=text, msg_id="m-t188", raw={"open_kfid": KFID_T188}))
    rows = [r for r in store.list_event_log(T.plan_id_for(res.conversation_id))
            if r["event_type"] == "KbRetrieved"]
    fired = any((r["detail"].get("query") or {}).get("channel") == similar.CHANNEL for r in rows)
    cite = _scheme_of_t188(res.draft.citations[0]) if res.draft.citations else ""
    return res.route, res.intent, res.handoff_reason, cite, fired


def _expected_t188(scheme_no: str, bodies: dict[str, dict]) -> tuple[str, str, str]:
    body = bodies[scheme_no]
    return ((T.ROUTE_HANDOFF if body["handoff"] else T.ROUTE_ANSWER), body["intent"],
            body["handoff"] or "")


def _measure_t188() -> dict:
    """整批跑一遍：每类答对数、答错 / 转错篇的句子、经近邻的句子（与对错）、反例的判定。"""
    bodies = _bodies_t188()
    per_class = {cls: 0 for cls in CLASS_SENTENCES_T188}
    wrong, fallback, via_ok, via_bad = set(), set(), set(), set()
    for cls, text, want in _all_positive_t188():
        route, intent, reason, cite, fired = _one_t188(text)
        ok = (route, intent, reason) == _expected_t188(want, bodies) and cite == want
        if ok:
            per_class[cls] += 1
        elif route == T.ROUTE_FALLBACK:
            fallback.add(text)
        else:
            wrong.add(text)
        if fired:
            (via_ok if ok else via_bad).add(text)
    neg_answered, neg_fired = set(), set()
    for text in NEGATIVES_T188:
        route, _intent, _reason, _cite, fired = _one_t188(text)
        if route == T.ROUTE_ANSWER:
            neg_answered.add(text)
        if fired:
            neg_fired.add(text)
    return {"per_class": per_class, "wrong": wrong, "fallback": fallback, "via_ok": via_ok,
            "via_bad": via_bad, "neg_answered": neg_answered, "neg_fired": neg_fired}


@pytest.fixture(scope="module")
def after_t188() -> dict:
    return _measure_t188()


# ---------------------------------------------------------------------------
# 1. 说法是另写的
# ---------------------------------------------------------------------------
def test_sentences_are_self_written_and_classed_t188():
    """每类 ≥ 10 句、反例 ≥ 40 句；不是开发集句子、不是话术库登记过的说法（含增补，按检索侧「原样命中」
    的口径比）、不带数字；每句期望的编号属于它的类别。"""
    assert set(CLASS_SENTENCES_T188) == set(GAP_CLASSES_P16)
    for cls, items in CLASS_SENTENCES_T188.items():
        assert len(items) >= 10, cls
        assert {want for _t, want in items} <= GAP_CLASSES_P16[cls], cls
    assert len(NEGATIVES_T188) >= 40
    texts = [t for _c, t, _w in _all_positive_t188()] + list(NEGATIVES_T188)
    assert len(set(texts)) == len(texts)
    dev = {t for c in evaluate.load_cases() for t in c.turns}
    dev |= {t for c in evaluate.load_cases(evaluate.P13_EVAL_PATH) for t in c.turns}
    registered = set()
    for body in _bodies_t188().values():
        for v in [body["scene"], *body["synonyms"], *body["examples"]]:
            registered |= scripts.tail_forms(v)
    for text in texts:
        assert text not in dev, text
        assert not (scripts.tail_forms(text) & registered), text
        assert not re.search(r"[0-9０-９]", text), text
    assert not _copy_pairs_t188(texts, [e["variant"] for e in _additions_t188()])


def _copy_pairs_t188(texts, variants) -> list[tuple[str, str]]:
    """T169 的三道防抄判据（≥5 字片段 / 一字之差 / 换词），两个方向都查：增补 vs 自写句。"""
    norm_texts = [t169._norm_t169(t) for t in texts]
    norm_vars = [t169._norm_t169(v) for v in variants]

    def hits(item, pool):
        return (t169._eval_fragments_t169(item, turns=pool) or t169._near_copies_t169(item, turns=pool)
                or t169._word_swap_copies_t169(item, turns=pool))

    pairs = [(v, "增补→自写") for v in variants if hits(v, norm_texts)]
    pairs += [(t, "自写→增补") for t in texts if hits(t, norm_vars)]
    return pairs


def test_copy_check_between_additions_and_self_written_can_fail_t188():
    """反向：复核 L2-2 点名的三对近似抄本，这道查法必须判出来。"""
    assert _copy_pairs_t188(["中午客服休息吗"], ["放假期间客服休息吗"])
    assert _copy_pairs_t188(["快递显示签收了但我没拿到"], ["显示签受了但我没拿到"])
    assert _copy_pairs_t188(["支持信用卡吗"], ["支持信用卡分期不"])


def test_additions_are_in_the_corpus_and_typos_are_registered_t188():
    """声明的每一条都按 kind 进了该篇的 synonyms / examples；登记的错写都真出现在该篇的增补例句里。"""
    bodies = _bodies_t188()
    field = {"synonym": "synonyms", "example": "examples"}
    entries = _additions_t188()
    assert entries
    for e in entries:
        assert e["variant"] in bodies[e["scheme_no"]][field[e["kind"]]], e
    typos = json.loads(ADDITIONS_PATH_T188.read_text(encoding="utf-8"))["typos"]
    for no, pairs in typos.items():
        mine = [e["variant"] for e in entries if e["scheme_no"] == no and e["kind"] == "example"]
        for wrong, right in pairs:
            assert wrong != right and any(wrong in v for v in mine), (no, wrong)
    # 增补说法本身也不带数字（不凑具体日期 / 金额 / 单号）
    assert not [e["variant"] for e in entries if re.search(r"[0-9０-９]", e["variant"])]


# ---------------------------------------------------------------------------
# 2. 召回：每类、增补前后
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", sorted(CLASS_SENTENCES_T188))
def test_per_class_recall_after_additions_t188(after_t188, cls):
    got = after_t188["per_class"][cls]
    print(f"{cls}: 答对 {got}/{len(CLASS_SENTENCES_T188[cls])}（增补前 {RECALL_BEFORE_T188[cls]}）")
    assert got >= RECALL_AFTER_FLOOR_T188[cls], (cls, got)


def test_before_and_after_additions_t188(monkeypatch, after_t188):
    """增补前后对照：每类答对数前者逐字等于 RECALL_BEFORE_T188；后者不低于前者、合计至少多 1 句；
    答错 / 转错篇的句子前后是同一张封闭表（增补没有引入任何一句新的答错）。"""
    _strip_additions_t188(monkeypatch)
    before = _measure_t188()
    assert before["per_class"] == RECALL_BEFORE_T188
    for cls in CLASS_SENTENCES_T188:
        assert after_t188["per_class"][cls] >= before["per_class"][cls], cls
    total_before = sum(before["per_class"].values())
    total_after = sum(after_t188["per_class"].values())
    print(f"合计答对 {total_before} → {total_after} / {len(_all_positive_t188())}；"
          f"兜底 {len(before['fallback'])} → {len(after_t188['fallback'])}")
    assert (total_before, len(before["fallback"])) == (82, 14)
    assert total_after >= total_before + 1
    assert before["wrong"] == KNOWN_LEXICAL_WRONG_T188
    assert before["neg_answered"] == KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188


# ---------------------------------------------------------------------------
# 3. 零自信答错（篇级）与反例弃权
# ---------------------------------------------------------------------------
def test_no_new_confident_wrong_and_similar_hits_are_right_t188(after_t188):
    """答错 / 转错篇的只有增补前就有的那七句（词法，近邻没启用）；经近邻的每一句都答对篇。"""
    assert after_t188["wrong"] == KNOWN_LEXICAL_WRONG_T188, sorted(after_t188["wrong"])
    assert after_t188["via_bad"] == set(), sorted(after_t188["via_bad"])


def test_negatives_abstain_t188(after_t188):
    """不该命中的 43 句：近邻通道一次都不启用（弃权率 43/43 = 100%）；整条前台上答了的只有增补前就被
    词法答掉的那四句（39/43 不答）。"""
    assert len(NEGATIVES_T188) == 43
    print(f"近邻弃权 {len(NEGATIVES_T188) - len(after_t188['neg_fired'])}/{len(NEGATIVES_T188)}；"
          f"前台不答 {len(NEGATIVES_T188) - len(after_t188['neg_answered'])}/{len(NEGATIVES_T188)}")
    assert after_t188["neg_fired"] == set(), sorted(after_t188["neg_fired"])
    assert after_t188["neg_answered"] == KNOWN_LEXICAL_NEGATIVE_ANSWERS_T188, sorted(
        after_t188["neg_answered"])


# ---------------------------------------------------------------------------
# 4. 阈值 / 差距重扫
# ---------------------------------------------------------------------------
def test_thresholds_after_rescan_t188():
    """重扫后取值不变（DECISIONS task-t188 的扫描表）：门槛不低于词法门槛（近邻那篇以余弦进 ScriptHit）。"""
    assert (similar.MIN_SIMILARITY, similar.MIN_MARGIN) == (0.34, 0.10)
    assert similar.MIN_SIMILARITY >= scripts.MIN_SCRIPT_SCORE


def test_margin_probe_t188(monkeypatch):
    """反向：差距放到 0.05（门槛 0.25）时，探针句经近邻被引到错篇；缺省门槛下它落兜底。"""
    text, want, wrong_doc = MARGIN_PROBE_T188
    route, _intent, _reason, cite, fired = _one_t188(text)
    assert (route, cite, fired) == (T.ROUTE_FALLBACK, "", False)
    real = similar.nearest
    monkeypatch.setattr(similar, "nearest",
                        lambda index, t, **kw: real(index, t, min_similarity=0.25, min_margin=0.05))
    route, _intent, _reason, cite, fired = _one_t188(text)
    assert fired and cite == wrong_doc != want


# ---------------------------------------------------------------------------
# 5. 共享词汇的无关句：增补前后逐句对照（复核 L3-1）
# ---------------------------------------------------------------------------
def _route_cite_t188(texts) -> dict[str, tuple[str, str, bool]]:
    return {t: (lambda r: (r[0], r[3], r[4]))(_one_t188(t)) for t in texts}


def test_vocab_sharing_probes_before_and_after_t188(monkeypatch):
    """173 句共享词汇的探针：近邻一次都不启用；增补前后 (route, 引用篇) 变了的句子恰好是
    ACCEPTED_NEW_ANSWERS_T188 ∪ NEW_HANDOFFS_T188，且变成表里写的那样（答 / 转那一篇）；
    没有一句从「答」变成另一篇的「答」或从兜底变成表外的「答」。"""
    assert len(VOCAB_PROBES_T188) == len(set(VOCAB_PROBES_T188)) == 173
    assert not (set(ACCEPTED_NEW_ANSWERS_T188) & set(NEW_HANDOFFS_T188))
    assert set(ACCEPTED_NEW_ANSWERS_T188) | set(NEW_HANDOFFS_T188) <= set(VOCAB_PROBES_T188)
    after = _route_cite_t188(VOCAB_PROBES_T188)
    _strip_additions_t188(monkeypatch)
    before = _route_cite_t188(VOCAB_PROBES_T188)
    assert not [t for t, v in after.items() if v[2]], "探针句上近邻启用了"
    changed = {t for t in VOCAB_PROBES_T188 if after[t][:2] != before[t][:2]}
    answered_after = sum(v[0] == T.ROUTE_ANSWER for v in after.values())
    answered_before = sum(v[0] == T.ROUTE_ANSWER for v in before.values())
    print(f"探针 173 句：答了 {answered_before} → {answered_after}；前后变了 {len(changed)} 句")
    assert changed == set(ACCEPTED_NEW_ANSWERS_T188) | set(NEW_HANDOFFS_T188), sorted(
        changed ^ (set(ACCEPTED_NEW_ANSWERS_T188) | set(NEW_HANDOFFS_T188)))
    for text, doc in ACCEPTED_NEW_ANSWERS_T188.items():
        assert after[text][:2] == (T.ROUTE_ANSWER, doc), (text, after[text])
    for text, doc in NEW_HANDOFFS_T188.items():
        assert after[text][:2] == (T.ROUTE_HANDOFF, doc), (text, after[text])
    assert (answered_before, answered_after) == (26, 34)


# ---------------------------------------------------------------------------
# 6. 生成器的声明自检（复核 L2-1）
# ---------------------------------------------------------------------------
def _gen_t188():
    path = corpus.CORPUS_PATH.parents[3] / "scripts" / "gen_cs_kb.py"
    spec = importlib.util.spec_from_file_location("gen_cs_kb_t188", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_additions_t188(tmp_path, additions, typos=None):
    path = tmp_path / "additions_p16.json"
    path.write_text(json.dumps({"additions": additions, "typos": typos or {}}, ensure_ascii=False),
                    encoding="utf-8")
    return str(path)


_GOOD_T188 = {"scheme_no": "GEN-001", "variant": "反向用例说法呀", "gap": "寒暄与致谢",
              "kind": "example", "reason": "反向用例"}


def test_generator_merges_declared_typos_into_the_doc_check_t188():
    """仓库里的声明文件：每篇登记的错别字对都并进了该篇 typos（_check_typos 真的看得到它们）。"""
    gen = _gen_t188()
    merged = {s["scheme_no"]: s for s in gen.merged_scripts()}
    typos = json.loads(ADDITIONS_PATH_T188.read_text(encoding="utf-8"))["typos"]
    assert typos
    for no, pairs in typos.items():
        for wrong, right in pairs:
            assert (wrong, right) in merged[no]["typos"], (no, wrong)
    gen._check_catalog(gen.merged_scripts())      # 仓库里那份整体过自检


@pytest.mark.parametrize("additions,typos,needle", [
    ([{**_GOOD_T188, "scheme_no": "XXX-999"}], None, "编号不在目录里"),
    ([{**_GOOD_T188, "scheme_no": ["GEN-001"]}], None, "编号不在目录里"),
    ([{**_GOOD_T188, "kind": "phrase"}], None, "kind 只许"),
    ([{**_GOOD_T188, "kind": ["example"]}], None, "kind 只许"),
    ([{**_GOOD_T188, "reason": " "}], None, "理由为空"),
    ([_GOOD_T188, dict(_GOOD_T188)], None, "重复声明"),
    ([{**_GOOD_T188, "variant": "你好"}], None, "该篇已有这条说法"),
    ([_GOOD_T188], {"XXX-999": [["a", "b"]]}, "编号不在目录里"),
    ([_GOOD_T188], {"GEN-001": [["a"]]}, "每对须是"),
    ([_GOOD_T188], {"GEN-001": "ab"}, "每对须是"),
], ids=["bad-scheme", "unhashable-scheme", "bad-kind", "unhashable-kind", "empty-reason",
        "duplicate", "already-there", "typo-bad-scheme", "typo-short-pair", "typo-not-list"])
def test_generator_declaration_checks_stop_t188(tmp_path, additions, typos, needle):
    gen = _gen_t188()
    with pytest.raises(SystemExit) as exc:
        gen.merged_scripts(_write_additions_t188(tmp_path, additions, typos))
    assert needle in str(exc.value), str(exc.value)


def test_generator_typo_pair_must_name_the_docs_own_wording_t188(tmp_path):
    """并进来的错别字对照走 _check_typos：本字不是本篇说法 → 目录自检停（复核 L2-1 的变异样例）。"""
    gen = _gen_t188()
    ok = gen.merged_scripts(_write_additions_t188(tmp_path, [{**_GOOD_T188, "variant": "蟹蟹在不"}],
                                                  {"GEN-001": [["蟹蟹", "谢谢"]]}))
    with pytest.raises(SystemExit) as exc:
        gen._check_catalog(ok)
    assert "不是本篇的说法" in str(exc.value), str(exc.value)
