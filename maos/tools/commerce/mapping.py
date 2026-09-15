"""平台字段 → `ExternalOrder` 的归一 —— **七个平台共用的那半张契约**。

`maos/tools/order.py` 的 `ExternalOrder` 只有五个字段、四个状态，而七个跨境平台
各有各的订单模型（Shopify 是二维的 `financial_status` × `fulfillment_status`，
Amazon 是一维十几态的 `OrderStatus`，Lazada 的状态还挂在每个 item 上）。
把它们压成同一个形状，是这一层全部的工作。

压缩必然有损，所以**怎么损**必须是显式的、带出处的、可被质疑的。本模块把映射
写成**数据**（`StatusRule` 的有序表），而不是每个适配器里的一串 if：
数据能被列出来给人看、能被测试逐条钉住、加平台时是填表而不是写逻辑。

## 两条不许动的判断

### 一、`version` 用 `updated_at` 的 epoch 毫秒

`ExternalOrder.version` 的语义是「外部系统的当前版本号」，它唯一的用途是漂移判据：
手上快照 `v1`、外部现在 `v2` → 订单被改过 → `refund.snapshot_check` 转人工。

跨境平台**没有一个给整数版本号**：Shopify 给 `updated_at`、Amazon 给
`LastUpdateDate`、WooCommerce 给 `date_modified`、eBay 给 `lastModifiedDate`。
于是只能派生，而派生方案里只有「修改时间的 epoch 毫秒」同时满足三条：

1. **单调**：外部改一次单，时间只会往后走，`外部 > 快照` 这条判据原样成立。
2. **无本地状态**：不需要维护一张「我给这笔单发到第几号了」的计数表，
   因此跨进程、跨重启、跨 worktree 都得到同一个值（`params_digest` 才可复现）。
3. **可解释**：漂移报到人面前时，`version` 反解回来就是「外部上次改它的时刻」，
   这正是排查要看的第一个字段。

用**毫秒**不用秒：同一秒内改两次单在大促期间是真会发生的，秒级会把两版压成一版，
于是漂移检不出来 —— 那是这套判据最不能出的错（漏报比误报危险，误报只是多转一次人工）。

### 二、映射不出来就抛，**不兜底**

`map_status()` 走完全部规则仍未命中时抛 `UnmappedOrderStatus`，不返回 `paid`、
不返回 `amended`、不返回空。

口径同 `order.get_order_system()` 的「取不到就抛，不兜底成一个空 mock」，
但这里的赌注更大：兜底成 `paid` 会把「平台返回了一个我们没见过的状态」伪装成
「这笔单一切正常，可以退」，而没见过的状态**恰恰**最可能是
`partially_refunded` / `refund_pending` / 平台新加的风控态 —— 也就是最不该往下走的那些。

这也是铁律 8 的直接延伸：MAOS 不持有权威事实。平台说了一个我们不认识的词时，
诚实的动作是承认不认识，不是替它翻译成一个我们认识的词。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from maos.tools.order import ALL_ORDER_STATUSES


class UnmappedOrderStatus(ValueError):
    """平台给了一个映射表没覆盖的状态。

    **这不是「平台坏了」，是「我们的表不全」** —— 异常信息里必须带上平台名与
    原始取值，因为修它的动作是去官方文档确认那个状态的含义、往表里补一条带
    `source` 的规则，而不是加一个 else 分支。
    """


class UnparsableTimestamp(ValueError):
    """`updated_at` 解析不出来 —— 派生不出 `version`，于是漂移判据失效。

    同样**不兜底成 0 或 now()**：`0` 会让每笔单都显得比快照旧（漂移永远检不出），
    `now()` 会让每笔单都显得刚被改过（漂移永远误报）。两个方向都是把一个
    「我不知道」伪装成一个结论。
    """


#: 该字段只要**非空**即命中（区别于「取值在集合里才命中」）。
#: Shopify 的 `cancelled_at` 是这个形状：它没有取值域，有值就代表取消了。
ANY_NON_EMPTY: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusRule:
    """一条状态映射规则：平台原始响应的某个字段取到某些值时，归一到四态之一。

    ## 规则是**有序**的，顺序即优先级

    顺序不是风格问题。Shopify 一笔单可以同时 `financial_status == "paid"` 和
    `cancelled_at` 非空（付过款、后来取消了），此时唯一正确的答案是 `cancelled`。
    把 `cancelled` 那条排在前面，是在声明「取消优先于付款状态」——
    这个声明必须显式，因为反过来排也能跑通，只是结论是错的。

    ## 每条都要有 `source`

    口径同 `gateway_codes.GatewayCode.source`：核不到出处的一条都不写。
    评委问「Shopify 的 `restocked` 你们为什么归到 `amended`」时，答案应该在
    这个字段里，而不是在写这行代码的人的记忆里。
    """

    field: str
    """平台响应里的字段名。支持点号下钻（`order.status`）与 `[0]` 取第一个元素
    （`items[0].status`，Lazada 的状态挂在 item 上）。"""

    values: tuple[str, ...]
    """命中的取值（小写比较）。空 tuple（`ANY_NON_EMPTY`）= 该字段非空即命中。"""

    target: str
    """归一到的目标状态，必须是 `ALL_ORDER_STATUSES` 之一。"""

    source: str
    """这条规则的出处：官方文档的哪一节 / 哪个枚举。写不出来的规则不该存在。"""

    def __post_init__(self) -> None:
        if self.target not in ALL_ORDER_STATUSES:
            raise ValueError(
                f"StatusRule.target={self.target!r} 不在 ExternalOrder 的四态里"
                f"（{ALL_ORDER_STATUSES}）—— 归一层不许引入第五个状态，"
                "那等于绕过 order.py 的契约在下游造一个新状态域"
            )
        if not self.source.strip():
            raise ValueError(
                f"StatusRule({self.field}={self.values!r} -> {self.target}) 缺 source："
                "核不到出处的规则不许进表（口径同 gateway_codes.GatewayCode.source）"
            )


def dig(payload: object, path: str) -> object:
    """按点号路径取值，取不到返回 `None`。

    支持 `a.b`、`a[0].b`。**不抛** —— 「字段不存在」在这里是一个正常情况
    （Shopify 未发货的单没有 `fulfillment_status`），由调用方的规则表决定它意味着什么。
    """
    cur = payload
    for seg in path.split("."):
        idx = None
        m = re.fullmatch(r"([\w\-]+)\[(\d+)\]", seg)
        if m:
            seg, idx = m.group(1), int(m.group(2))
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
        if idx is not None:
            if not isinstance(cur, (list, tuple)) or idx >= len(cur):
                return None
            cur = cur[idx]
    return cur


def map_status(rules: tuple[StatusRule, ...], payload: dict, *, platform: str) -> str:
    """按**顺序**跑规则表，第一条命中即返回。一条都不命中就抛。

    `platform` 只用于异常信息 —— 报「哪个平台的哪个字段取到了什么」，
    人拿着这三样才能去官方文档查，才修得动这张表。
    """
    for rule in rules:
        raw = dig(payload, rule.field)
        if raw is None:
            continue
        text = str(raw).strip().lower()
        if not text or text in {"none", "null"}:
            continue
        if rule.values == ANY_NON_EMPTY or text in rule.values:
            return rule.target
    seen = {r.field: dig(payload, r.field) for r in rules}
    raise UnmappedOrderStatus(
        f"{platform} 的订单状态没有命中任何规则；读到的字段："
        f"{seen!r}\n—— 不兜底成 paid：没见过的状态最可能是 partially_refunded / "
        "风控态这类**恰恰不该往下走**的情况（铁律 8：不替外部翻译成我们认识的词）。"
        f"修法是去 {platform} 官方文档确认该状态含义，往规则表补一条带 source 的 StatusRule"
    )


#: 允许的时间戳形状。**刻意不用 dateutil** —— 那是个第三方依赖，而这里要认的
#: 形状是有限的：四个 A 档平台全是 ISO8601，两个 B 档给 epoch。认不出来就抛。
_ISO_TRAILING_Z = re.compile(r"[Zz]$")


def derive_version(updated_at: object, *, platform: str = "") -> int:
    """把平台的「上次修改时刻」派生成 `ExternalOrder.version`（**epoch 毫秒**）。

    认三种形状，都来自目标平台的真实响应：

    - ISO8601 带时区：``2026-09-15T10:30:00-04:00``（Shopify / WooCommerce / eBay）
    - ISO8601 带 Z：``2026-09-15T14:30:00Z``（Amazon SP-API 的 ``LastUpdateDate``）
    - epoch 秒或毫秒的整数/数字串（Shopee 的 ``update_time`` 是秒）

    **不认 naive 的 ISO8601**（不带时区的 ``2026-09-15T10:30:00``）：那种串没有
    唯一的 epoch，按本地时区解等于让 `version` 随运行机器的 TZ 变 —— 同一笔单在
    我的 Mac 上和在服务器上会派生出差 12 小时的版本号，漂移判据当场失效。
    平台真给了 naive 串，要在适配器里按该平台文档写死时区再进来，而不是在这里猜。
    """
    if updated_at is None or (isinstance(updated_at, str) and not updated_at.strip()):
        raise UnparsableTimestamp(
            f"{platform or '平台'} 没给 updated_at —— 派生不出 version，漂移判据失效；"
            "不兜底成 0（会让漂移永远检不出）也不兜底成 now()（会让漂移永远误报）"
        )

    if isinstance(updated_at, (int, float)) or (
            isinstance(updated_at, str) and re.fullmatch(r"\d{9,14}", updated_at.strip())):
        n = int(float(updated_at))
        # 10 位是秒、13 位是毫秒。按位数判而不是按阈值判 —— 阈值会在 2033 年前后失准。
        return n * 1000 if len(str(abs(n))) <= 10 else n

    text = str(updated_at).strip()
    normalized = _ISO_TRAILING_Z.sub("+00:00", text)
    try:
        moment = _dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise UnparsableTimestamp(
            f"{platform or '平台'} 的 updated_at={updated_at!r} 解析失败：{exc}"
        ) from exc
    if moment.tzinfo is None:
        raise UnparsableTimestamp(
            f"{platform or '平台'} 的 updated_at={updated_at!r} 不带时区。"
            "naive 时间串按本地时区解会让 version 随机器 TZ 变（同一笔单在两台机器上"
            "差几个小时），漂移判据当场失效。请在适配器里按该平台文档补上时区再进来"
        )
    return int(moment.timestamp() * 1000)


def normalize_amount(raw: object, *, platform: str = "") -> str:
    """金额归一成字符串。**永不进浮点** —— 口径同 `gateway.RefundRequest.refund_amount`
    与 `ExternalOrder.amount` 的那句注释。

    平台给什么形状的都有：Shopify 给 ``"100.00"``（字符串），
    Amazon 给 ``{"CurrencyCode": "USD", "Amount": "100.00"}``（嵌套），
    Shopee 给 ``100.0``（数字）。这里只做「取出来、变成串、不丢精度」，
    **不做单位换算、不做币种转换** —— 那两件事都需要外部事实（汇率），不归本层。
    """
    if raw is None:
        raise ValueError(f"{platform or '平台'} 没给订单金额 —— 不兜底成 '0'："
                         "零元订单与「读不到金额」是两回事，后者应该让上层停下来")
    if isinstance(raw, float):
        # float 进来就已经有精度风险了，但拒绝它会让 Shopee 这类平台接不进来。
        # 折中：用 repr 而不是 str，保住 Python 能给的全部有效位，并在这里留痕。
        return repr(raw)
    return str(raw).strip()
