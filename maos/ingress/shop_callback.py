"""电商平台 webhook 入站 —— **回调只落原文 + 触发回源，永不写任何状态**。

## 这条通道为什么存在

`tools/commerce/` 那一半是「拉」：执行前主动 `order.query()` 读一次当前版本。
但订单在 MAOS 不看的时候也会变 —— 客户改了地址、客服取消了单、平台判了一笔
仅退款。跨境平台全都用 webhook 把这些事推过来（Shopify 的 `orders/updated`、
`refunds/create`，Amazon 的 SQS 通知，Woo 的 `order.updated`）。不接这条通道，
MAOS 对「两次执行之间发生了什么」是瞎的。

## D1：回调不写状态，只落原文 + 触发回源

与支付面同一条判断（`docs/DECISIONS.md` 的 p11 那行、`docs/payment-inbound-plan.md`
§1 D1），理由在电商侧一字不改地成立：

1. **签名只证明「这条消息来自平台」，不证明「这是当前最新状态」。** webhook 会乱序：
   先收到 `cancelled` 后收到 `paid` 的时候，照单写等于把外部状态写死为终态（铁律 8）。
2. **webhook 会重放。** 时间戳闸挡重放、幂等键闸挡平台自己的重推，
   两道闸挡的**不是同一件事**，两道都要（`crypto.MAX_CLOCK_SKEW` 的注释已把这点说透）。
3. 平台自己也这么建议：Shopify 的文档明写 webhook 只应作为「去查一下」的信号。

所以 `ingest()` 的全部动作是：**验签 → 去重 → 落原文 → 返回一个「值不值得回源」的判决**。
它不 import 任何 domain 模块，不碰 `order_snapshot`，不改任何 Task 状态。
回源是调用方的事（拿判决里的 `order_ref` 去走 `order.query` 那条既有路径）。

这不是保守，是让这条通道**天生合规**：它在结构上就写不进状态，于是不需要任何
守卫去看着它。

## 三条硬要求（与支付面 T154 同口径）

1. **验签失败的也落库**，`verify_result` 写清是哪一种。那是攻击证据，吞掉就没了 ——
   同 `scripts/guard_bash.py` 的口径。
2. **落原文，不落解析结果**。验签是对原始字节算的，存解析结果等于把唯一能复算的
   东西丢掉；平台日后改了字段命名，历史回调也就再也复算不了了。
3. **去重走 `store.claim_idempotency`，不新建去重表**。`processed_key` 已经是
   全系统通用的幂等闸，再建一张等于让同一件事有两个真相。

## 没有事件 id 一律拒

`ingress/router.py` 那条「没有 msg_id 一律放行」在这里**反过来**：IM 消息丢一条
是少回一句话，而电商回调丢的是去重能力 —— 没有 id 就没法判重投，平台的自动重试
会让同一笔单被回源 N 次。拒绝（400）比放行安全，且平台看到 400 会重试并带上 id。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from maos.ingress.crypto import VerifyError, check_timestamp, equal


_FRAGMENT_PATH = Path(__file__).with_name("schema_p11_t160.sql")

#: 幂等闸上的 op 名。与支付面的 `pay.callback` 并列，两条通道各自去重、互不串账。
IDEMPOTENCY_OP = "shop.callback"

# -- verify_result 的取值域 -------------------------------------------------
#
# 刻意**不复用** gateway 的四态：那四个说的是「一笔退款走到哪了」，这里说的是
# 「这条消息可不可信」，两回事。混用会让 `payment_observation.observed_state`
# 的取值域被污染（跨轨契约明写：回调不得引入第五个值）。
VERIFY_OK = "ok"
VERIFY_BAD_SIGNATURE = "bad_signature"
VERIFY_STALE_TIMESTAMP = "stale_timestamp"
VERIFY_DEP_MISSING = "dep_missing"
VERIFY_NO_EVENT_ID = "no_event_id"
VERIFY_NO_VERIFIER = "no_verifier"

ALL_VERIFY_RESULTS = (VERIFY_OK, VERIFY_BAD_SIGNATURE, VERIFY_STALE_TIMESTAMP,
                      VERIFY_DEP_MISSING, VERIFY_NO_EVENT_ID, VERIFY_NO_VERIFIER)


@dataclass(frozen=True)
class VerifiedCallback:
    """验签通过后，verifier 从这条回调里认出来的东西。

    verifier **只负责认**，不负责判断该不该回源 —— 后者是 `ingest()` 的事，
    因为那需要看去重结果，而 verifier 看不到 store。
    """

    event_id: str
    """平台给这条投递的唯一 id（Shopify 的 `X-Shopify-Webhook-Id`、
    Woo 的 `X-WC-Webhook-Delivery-ID`）。去重键就是它，**没有它就拒**。"""

    topic: str
    """平台给的事件主题（`orders/updated` / `refunds/create`）。只记录，不分流 ——
    分流是调用方的事，这一层不认识业务语义。"""

    order_ref: str = ""
    """这条回调指向哪笔订单。**可以为空**（有些平台的 shop/app 级事件不带订单），
    空就意味着没什么可回源的，`should_resync` 会是 False。"""


@dataclass(frozen=True)
class CallbackVerdict:
    """`ingest()` 的判决。**没有任何一个字段是订单状态** —— 见模块头 D1。"""

    accepted: bool
    http_status: int
    verify_result: str
    event_id: str = ""
    topic: str = ""
    order_ref: str = ""
    duplicate: bool = False

    @property
    def should_resync(self) -> bool:
        """要不要拿 `order_ref` 去走一次 `order.query` 回源。

        三个条件缺一不可：验签过了、不是重投、确实指向一笔订单。
        **重投不回源**：平台的自动重试会把同一条投递推很多次，每次都回源等于
        自己给自己造限流。
        """
        return self.accepted and not self.duplicate and bool(self.order_ref)


class CallbackVerifier(Protocol):
    """一个平台的验签器。**只认、不判** —— 认不出来抛 `VerifyError`。"""

    def __call__(self, *, raw: bytes, headers: dict[str, str]) -> VerifiedCallback:
        ...


_VERIFIERS: dict[str, CallbackVerifier] = {}


def register_verifier(platform: str, verifier: CallbackVerifier) -> CallbackVerifier:
    """登记一个平台的验签器。各平台轨在自己的模块里调这个。"""
    _VERIFIERS[str(platform)] = verifier
    return verifier


def reset_verifiers() -> None:
    """只给测试用，保证用例之间不互相串登记表。"""
    _VERIFIERS.clear()


def header(headers: dict[str, str], name: str, default: str = "") -> str:
    """大小写不敏感取头 —— HTTP 头名不区分大小写，而平台文档里的写法五花八门。"""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return default


def hmac_sha256_base64(secret: str, raw: bytes) -> str:
    """`base64(hmac_sha256(secret, raw))` —— Shopify / WooCommerce / TikTok Shop
    三家共用的签名形状，避免三轨各写一遍（写三遍就会有一遍用了 `==` 比对）。

    **对原始字节算**，不对解析后的 JSON 重新序列化算：重序列化会改键序和空白，
    签名必然对不上，而这个坑每个接过 webhook 的人都踩过一次。
    """
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_hmac_header(secret: str, raw: bytes, supplied: str) -> None:
    """比对 HMAC 签名头，不等就抛 `VerifyError`。**常量时间比对**，见 `crypto.equal`。"""
    if not supplied:
        raise VerifyError("请求没带签名头")
    if not equal(hmac_sha256_base64(secret, raw), supplied):
        raise VerifyError("签名不匹配")


def ingest(platform: str, raw: bytes, headers: dict[str, str], *,
           store, now: float | None = None) -> CallbackVerdict:
    """收一条电商回调。**全部动作：验签 → 去重 → 落原文 → 返回判决。**

    无论验签成败都会落一行 `shop_callback`（要求 1）。返回的 `http_status` 是
    该回给平台的码：验签失败 400（平台会停止重投并告警），成功或重投 200
    （重投回 200 是为了让平台停止重试 —— 那条投递我们确实已经收到了）。

    **本函数不 import 任何 domain 模块，也不写任何状态**，见模块头 D1。
    """
    verifier = _VERIFIERS.get(str(platform))
    if verifier is None:
        _record(store, platform, raw, headers, VERIFY_NO_VERIFIER, "", "", "")
        return CallbackVerdict(False, 400, VERIFY_NO_VERIFIER)

    try:
        seen = verifier(raw=raw, headers=headers)
    except VerifyError as exc:
        result = _classify(exc)
        _record(store, platform, raw, headers, result, "", "", str(exc))
        return CallbackVerdict(False, 400, result)

    if not seen.event_id.strip():
        # router.py 那条「没有 msg_id 一律放行」在这里反过来，理由见模块头。
        _record(store, platform, raw, headers, VERIFY_NO_EVENT_ID,
                "", seen.topic, "平台没带投递 id，无法去重")
        return CallbackVerdict(False, 400, VERIFY_NO_EVENT_ID, topic=seen.topic)

    key = f"{platform}:{seen.event_id}"
    prior = store.claim_idempotency(key, IDEMPOTENCY_OP, "")
    duplicate = prior is not None
    _record(store, platform, raw, headers, VERIFY_OK,
            seen.event_id, seen.topic, "duplicate" if duplicate else "")
    return CallbackVerdict(
        accepted=True, http_status=200, verify_result=VERIFY_OK,
        event_id=seen.event_id, topic=seen.topic, order_ref=seen.order_ref,
        duplicate=duplicate,
    )


def _classify(exc: VerifyError) -> str:
    """把 `VerifyError` 归到 `verify_result` 的某一档。

    按异常消息里的关键词分 —— 不理想，但 `crypto` 那几个函数抛的是同一个异常类型，
    而改 `crypto.py` 的异常层级会动到飞书/企微两条已经跑通的通道（不在本轨白名单）。
    分不出来的一律归 `bad_signature`：**fail-closed**，把不认识的失败当成最坏的那种。
    """
    text = str(exc)
    if "时间戳" in text:
        return VERIFY_STALE_TIMESTAMP
    if "依赖" in text or "ChannelDepMissing" in type(exc).__name__:
        return VERIFY_DEP_MISSING
    return VERIFY_BAD_SIGNATURE


def _record(store, platform: str, raw: bytes, headers: dict[str, str],
            verify_result: str, event_id: str, topic: str, note: str) -> None:
    """落一行原文。**落失败不许把整条请求带崩** —— 那会让一次落库故障变成
    「平台以为我们挂了、开始重投、雪崩」。落不进去时判决照常返回，
    审计的缺口由 `event_log` 那条兜（调用方走 ToolPort 时会留痕）。
    """
    ensure_schema(store)
    row = {
        "platform": str(platform),
        "event_id": event_id,
        "topic": topic,
        "verify_result": verify_result,
        "note": note,
        # 原文按 base64 存：回调体可能不是 UTF-8（平台会发压缩或二进制签名块），
        # 直接 decode 会在入库这一步就把证据毁掉。
        "raw_body_b64": base64.b64encode(raw).decode("ascii"),
        "headers_json": json.dumps(_redact_headers(headers), ensure_ascii=False,
                                   sort_keys=True),
    }
    try:
        store.insert_shop_callback(row)
    except AttributeError:
        _insert_direct(store, row)
    except Exception:                                    # noqa: BLE001
        return


#: 这些头带的是凭据而不是签名，落库前抹掉（铁律 6：密钥不许进任何输出）。
#: 签名头**要留** —— 那是复算验签的必要材料，也是攻击取证的核心。
_SECRET_HEADERS = frozenset({"authorization", "cookie", "x-api-key", "access-token"})


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _SECRET_HEADERS else v)
            for k, v in (headers or {}).items()}


_schema_ready: set[int] = set()


def ensure_schema(store) -> None:
    """首次使用时把 `schema_p11_t160.sql` 应用到库上。

    形态同 `case_pack.ensure_t116_schema()`（跨轨契约 §B.1：不改 `schema.sql` 主文件，
    每轨自带片段、自己加载）。加载器**解析文件**而不是在代码里另抄一张 DDL 清单 ——
    抄一份的后果是哪天有人改了 .sql、代码里那份没跟着改，而两边都不报错。
    """
    marker = id(store)
    if marker in _schema_ready:
        return
    conn = getattr(store, "_conn", None)
    if conn is None:
        _schema_ready.add(marker)
        return
    for statement in parse_fragment(_FRAGMENT_PATH.read_text(encoding="utf-8")):
        conn.execute(statement)
    conn.commit()
    _schema_ready.add(marker)


#: 片段里只收这两种语句（跨轨契约 §B.3 的允许子集在本轨的对应物）。
#: 收窄的理由同 `case_pack._parse_fragment`：一个能执行任意 SQL 的加载器，
#: 哪天片段里混进一句 `DROP` / `ALTER` 既有表，就直接踩穿铁律 1 而没人拦得住。
_ALLOWED_PREFIXES = ("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS")


def parse_fragment(text: str) -> list[str]:
    """把片段解析成可执行语句。**逐行剥注释**，不是按 `;` 切了再判整段。

    按段判会踩一个很隐蔽的坑：注释块跟在语句前面时，整段是以 `--` 开头的，
    于是那条 `CREATE TABLE` 连同注释一起被当成注释跳过 —— 而下一条
    `CREATE INDEX` 照常执行，报的却是「no such table」，排查方向当场偏到建表顺序上。
    形状对齐 `case_pack._parse_fragment`（那份也是逐行）。
    """
    body = "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("--"))
    statements: list[str] = []
    for chunk in body.split(";"):
        statement = " ".join(chunk.split())
        if not statement:
            continue
        if not statement.upper().startswith(_ALLOWED_PREFIXES):
            raise ValueError(
                f"{_FRAGMENT_PATH.name} 里有一条不在允许子集里的语句：{statement[:80]!r}。"
                f"片段只收 {_ALLOWED_PREFIXES}（铁律 1：既有表结构禁改，只许新增表）")
        statements.append(statement)
    return statements


def _insert_direct(store, row: dict) -> None:
    """store 上没有 `insert_shop_callback` 时直接走连接 —— 与 T116/T117 同一形态：
    新增表的写入方法由使用方持有，不往 `store.py` 的抽象基类上加（铁律 1：
    表结构禁改，只许新增表；抽象基类是既有面）。"""
    conn = getattr(store, "_conn", None)
    if conn is None:
        return
    conn.execute(
        "INSERT INTO shop_callback (platform, event_id, topic, verify_result,"
        " note, raw_body_b64, headers_json) VALUES (?,?,?,?,?,?,?)",
        (row["platform"], row["event_id"], row["topic"], row["verify_result"],
         row["note"], row["raw_body_b64"], row["headers_json"]),
    )
    conn.commit()
