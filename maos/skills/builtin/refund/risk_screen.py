"""refund.risk_screen —— 重复退款 / 退款频率 / 金额异常的确定性筛查。

风险反欺诈岗的全部判定都在这里，Agent 那边一行业务逻辑都没有。三条口径值得先说清：

1. **纯函数**。只读入参：不取 store、不建表、不调模型、不读任何文件、不碰附件字节。
   同一份入参连跑两次，除 `invocation_id` 外逐字一致（有测试钉住）。这不是洁癖 ——
   风控结论要能在事后被复现，「当时算出来是 high」必须能重算一遍还是 high，
   否则它在争议里一文不值。

2. **阈值、权重、分档全是类属性**（参照 T75「取值域枚举改类属性」的做法）。
   风控阈值是**会变的经营口径**，不是代码常量：换个促销季、换个品类，40 分的重复退款
   可能就该记 60 分。写成类属性意味着调阈值不改这个文件，测试也能 monkeypatch
   一个极端值来证明「分档确实读的是这几个属性」，而不是我在某个 if 里写死了数。

3. **只出观察与推断，不出裁定**（铁律 8）。这里给的是 level / score / reasons，
   不是「这单不许退」。是否放行由规则审核岗的政策裁定与人的审批决定 ——
   风险分只是摆在他们面前的一份材料。所以本模块不写任何业务状态、不产生任何副作用。

## 风险历史只来自入参里的 `refund_history`

`refund_history` 是**外部底账**的投影，不是 MAOS 自己攒出来的流水（铁律 8：权威事实
归外部系统）。所以本 skill 看不见「同一个客户十分钟前在本进程里刚退过一次」——
那需要一层持久化的观察记录，而演示链路每次都是新建的 `:memory:` 库。
这条限制已记进 `docs/BACKLOG.md`，别在这里靠模块级缓存去补，那只会造出一个
「进程活着时准、重启后失忆」的假账。

## 日期一律显式解析，解不出就抛

`requested_at` 与历史行的 `decided_at` 都走 `datetime.fromisoformat`；naive 的补 UTC
（口径同 `scripts/run_requests.py::_iso`）—— 不补时区的话，与带时区的时间相减当场
抛 TypeError，且抛在窗口计数中段，报错完全指不到「谁给了一个没时区的日期」。
看不懂的日期**不猜**：猜一个「大概是今天」会让 30 天窗口悄悄算错，而没有任何东西变红。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C


@register_skill
class RefundRiskScreenSkill(Skill):
    contract = SkillContract(
        name="refund.risk_screen",
        version="1.0.0",
        purpose="按底账筛查重复退款、退款频率与金额异常，出风险分档、分数与逐条人话理由",
        input_schema={
            "case_seed": "dict（同 refund.intake 的 case_seed：tenant_id, case_id, order_id,"
                         " order_version, sku, reason_code, amount_claimed, …）",
            "order": "dict（order_snapshot 那一行：amount_paid, paid_at, channel_id；"
                     "payload_json 里可有 customer_id）",
            "customer_orders": "list[dict]（同 customer_id 的全部 order_snapshot 行，含本单，可空）",
            "refund_history": "list[dict{case_id,order_id,customer_id,amount,status,decided_at}]"
                              "（底账 refund_history，可空）",
            "requested_at": "str（ISO8601；naive 视为 UTC，解不出即抛）",
            "customer_id": "str（可选：顶层给了优先用，否则从 order.payload_json 解）",
        },
        output_schema={
            "level": '"low" | "medium" | "high"（按 LEVEL_MEDIUM / LEVEL_HIGH 两个类属性分档）',
            "score": "int（0–100，权重和封顶 100）",
            "reasons": "list[str]（人话，每条一个信号；不含时间戳、不含随机）",
            "signals": "dict{duplicate_refund: bool, already_refunded: bool, frequency_30d: int,"
                       " amount_ratio: float, multi_order_same_account: int, amount_over_paid: bool}",
            "invocation_id": "str",
        },
        preconditions=["case_seed"],
        depends_tools=[],
        # escalate 而不是 retry：本 skill 是确定性纯函数，失败只可能是入参坏了
        # （日期看不懂、实付非正）。同样的入参重试一百次还是同一个异常，
        # 重试只会把「底账有问题」拖成「风控岗好像卡住了」。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读入参，不写库、不调模型、不读文件、不碰附件字节；"
            "只产出观察与推断（level / score / reasons），不改任何业务状态、不做放行裁定"
        ),
        reuse_note=(
            "任何「按历史行为给一个可解释分档」的场景都可照此写："
            "权重与阈值全在类属性上，调判据不改代码；理由逐条对应一个信号，可直接摆给人看"
        ),
        owner_roles=["refund_risk"],
    )

    #: 写成类属性而不是模块常量，是为了让测试能 monkeypatch 一个极端值来证明
    #: 「分档确实读的是这几个属性」；同时 `docs/skill-catalog.md` 是从代码生成的投影，
    #: 记着本类声明行的行号，把常量堆在模块头会把这一行推下去。
    #:
    #: 权重的相对大小是有说法的，不是随手取的数：
    #:   · 已退成（settled）单独 50，且与重复退款的 40 叠加 = 90 —— 「这一单的钱已经
    #:     退出去过」是本域最硬的一个信号，它必须能一条就把分档顶到 high，
    #:     不依赖任何别的信号凑数。
    #:   · 频率两档**只取高档、不叠加**：4 笔的客户与 2 笔的客户是同一件事的不同程度，
    #:     叠加等于把同一个信号数两遍，分数就不再可解释。
    #:   · 超实付只记 15：核算侧本来就会封顶（finance 那边 min 到实付），
    #:     它更像「填表填错了」而不是欺诈，记重了会淹掉真正的信号。
    W_DUPLICATE = 40
    W_ALREADY_REFUNDED = 50
    W_FREQ_MEDIUM = 20
    W_FREQ_HIGH = 35
    W_OVER_PAID = 15
    W_MULTI_ORDER = 10

    #: 触发档位的门槛。
    FREQ_MEDIUM_AT = 2
    FREQ_HIGH_AT = 4
    MULTI_ORDER_AT = 3

    #: 分档阈值（score >= 即进该档）与频率窗口天数。
    LEVEL_MEDIUM = 30
    LEVEL_HIGH = 60
    WINDOW_DAYS = 30

    #: 底账里「这一单正在退或已经退过」的状态取值域。
    ACTIVE_STATUSES = frozenset({"pending", "settled"})
    STATUS_SETTLED = "settled"

    #: 客户标识解不出时的降级说明 —— 频率与多单两个信号在这种入参下**不成立**，
    #: 报 0 / 1 而不是报「没风险」，理由里必须明说，否则读的人会把「没评估」
    #: 当成「评估过了、干净」。
    NOTE_NO_CUSTOMER = "底账无客户标识，频率与多单信号未评估"

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        invocation_id = C.invocation_id_of(ctx)

        seed = payload.get("case_seed") or {}
        order = payload.get("order") or {}
        history = [r for r in (payload.get("refund_history") or []) if isinstance(r, dict)]
        customer_orders = [r for r in (payload.get("customer_orders") or []) if isinstance(r, dict)]

        C.required(payload, "requested_at")
        requested_at = self._parse_dt(payload.get("requested_at"), "requested_at")

        order_id = str(seed.get("order_id") or order.get("order_id") or "").strip()
        amount_claimed = self._amount(seed.get("amount_claimed"), "case_seed.amount_claimed")
        amount_paid = self._amount(order.get("amount_paid"), "order.amount_paid")
        if amount_paid <= 0:
            # 不兜底成「按 0 算比值」：除零要么抛要么得到 inf，两条都会一路穿到
            # 分档里变成一个说不清出处的 high。实付非正只可能是底账坏了。
            raise ValueError(
                f"订单实付金额必须为正，实际 {amount_paid!r}（order_id={order_id!r}）"
                " —— 底账这一行有问题，风险分不猜")

        customer_id = self._customer_id_of(payload, order)

        signals = {
            "duplicate_refund": False,
            "already_refunded": False,
            "frequency_30d": 0,
            "amount_ratio": amount_claimed / amount_paid,
            "multi_order_same_account": 1,
            "amount_over_paid": amount_claimed > amount_paid,
        }

        if order_id:
            same_order = [r for r in history
                          if str(r.get("order_id") or "").strip() == order_id]
            signals["duplicate_refund"] = any(
                str(r.get("status") or "").strip() in self.ACTIVE_STATUSES for r in same_order)
            signals["already_refunded"] = any(
                str(r.get("status") or "").strip() == self.STATUS_SETTLED for r in same_order)

        if customer_id:
            signals["frequency_30d"] = self._frequency_in_window(
                history, customer_id=customer_id, requested_at=requested_at)
            signals["multi_order_same_account"] = len(customer_orders)

        score, reasons = self._score(signals, order_id=order_id,
                                     amount_claimed=amount_claimed, amount_paid=amount_paid)
        if not customer_id:
            reasons.append(self.NOTE_NO_CUSTOMER)

        return {
            "level": self._level(score),
            "score": score,
            "reasons": reasons,
            "signals": signals,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------ 打分
    def _score(self, signals: dict, *, order_id: str,
               amount_claimed: float, amount_paid: float) -> tuple[int, list[str]]:
        """按固定顺序过一遍信号，边加权边攒理由 —— 顺序固定是确定性的一半。"""
        score = 0
        reasons: list[str] = []

        if signals["duplicate_refund"]:
            score += self.W_DUPLICATE
            reasons.append(
                f"订单 {order_id} 在退款底账里已有处理中或已退成的记录，属重复退款申请")

        if signals["already_refunded"]:
            score += self.W_ALREADY_REFUNDED
            reasons.append(
                f"订单 {order_id} 已有一笔退款到账（settled），本次是对同一单的二次退款")

        freq = signals["frequency_30d"]
        if freq >= self.FREQ_HIGH_AT:
            # 只取高档：两档说的是同一件事的不同程度，叠加等于把一个信号数两遍。
            score += self.W_FREQ_HIGH
            reasons.append(f"同一客户近 {self.WINDOW_DAYS} 天内已有 {freq} 笔退款记录，频率显著偏高")
        elif freq >= self.FREQ_MEDIUM_AT:
            score += self.W_FREQ_MEDIUM
            reasons.append(f"同一客户近 {self.WINDOW_DAYS} 天内已有 {freq} 笔退款记录，频率偏高")

        if signals["amount_over_paid"]:
            score += self.W_OVER_PAID
            reasons.append(
                f"申报金额 {amount_claimed:.2f} 高于订单实付 {amount_paid:.2f}"
                f"（比值 {signals['amount_ratio']:.2f}），核算会按实付封顶")

        multi = signals["multi_order_same_account"]
        if multi >= self.MULTI_ORDER_AT:
            score += self.W_MULTI_ORDER
            reasons.append(f"同一客户名下有 {multi} 笔订单快照，存在批量退款的可能")

        return min(100, score), reasons

    def _level(self, score: int) -> str:
        """先判 high 再判 medium：两个阈值被调到反常取值时也不会掉进错档。"""
        if score >= self.LEVEL_HIGH:
            return "high"
        if score >= self.LEVEL_MEDIUM:
            return "medium"
        return "low"

    # ------------------------------------------------------------------ 取数
    def _frequency_in_window(self, history: list[dict], *, customer_id: str,
                             requested_at: datetime) -> int:
        """同客户、`decided_at` 落在 [requested_at − WINDOW_DAYS, requested_at] 的条数。

        两头都是闭区间；**申请之后**才决定的退款不算（那是未来，算进去等于用后见之明
        给当下打分），窗口之前的也不算。`decided_at` 为空的行是「还没决定」，不进计数 ——
        它的 pending 身份已经由 duplicate_refund 那条信号覆盖了。
        """
        window_start = requested_at - timedelta(days=self.WINDOW_DAYS)
        count = 0
        for row in history:
            if str(row.get("customer_id") or "").strip() != customer_id:
                continue
            raw = row.get("decided_at")
            if not str(raw or "").strip():
                continue
            decided_at = self._parse_dt(raw, "refund_history.decided_at")
            if window_start <= decided_at <= requested_at:
                count += 1
        return count

    @staticmethod
    def _customer_id_of(payload: dict, order: dict) -> str:
        """顶层给了优先用，否则从 `order.payload_json` 解；两条都拿不到返回空串。

        只认这两处是有意的：`fixtures.seed_case` 对 `order_snapshot` 的列集合是严格
        校验的，往行里加兄弟键当场抛，所以底账侧的扩展只能进 `payload_json` 字符串。
        解不出**不抛**：老订单的 `payload_json` 就是 `"{}"`，那是合法底账、不是坏数据，
        代价只是两个信号评不了 —— 降级并在理由里说明，比让整个风控岗炸掉合理。
        """
        top = str(payload.get("customer_id") or "").strip()
        if top:
            return top

        raw = order.get("payload_json")
        facts: object
        if isinstance(raw, dict):
            facts = raw
        else:
            try:
                facts = json.loads(str(raw or "").strip() or "{}")
            except (TypeError, ValueError):
                facts = {}
        if not isinstance(facts, dict):
            facts = {}
        return str(facts.get("customer_id") or "").strip()

    @staticmethod
    def _parse_dt(raw: object, field: str) -> datetime:
        """ISO8601 -> 带时区的 datetime。naive 补 UTC，看不懂就抛，不猜。"""
        text = str(raw or "").strip()
        if not text:
            raise ValueError(f"{field} 为空 —— 风险窗口没有基准点，不猜")
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"看不懂的 {field}：{text!r} —— 期望 ISO8601（如 2026-09-03T10:00:00+00:00）"
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _amount(raw: object, field: str) -> float:
        """金额取 float。取不出就抛 —— 兜底成 0 会让比值与超额判定一起失真。"""
        try:
            return float(raw)                       # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 不是一个金额：{raw!r}") from exc
