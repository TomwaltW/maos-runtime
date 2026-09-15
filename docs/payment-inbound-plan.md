# 支付面接入方案 —— 从「单向出款 + 轮询」补成「双向 + 回调驱动 + 对账」

> 来源：读 `https://github.com/Alex647648/saas-skills-suite`（10 个 Claude Code Skill，
> 核心是 `saas-skills-suite:mvp-billing-system/SKILL.md` 1344 行
> + `saas-skills-suite:stripe-payments/SKILL.md` 225 行，
> 栈是 Next.js + Supabase + Stripe）之后，逐条对照 MAOS 现状得出的增量。
>
> 本文件是**方案**不是执行手册：它说清「接什么、为什么、落在哪个文件哪张表」，
> 具体步骤在派单里。写于 2026-09-14，基线 `65bd0a5`。

---

## 0. 一页总览

一句话：**那个仓库真正能给 MAOS 的只有一件事 —— 支付事实可以被外部「推」进来，
而 MAOS 现在只会「拉」。** 其余 1300 行里的幂等、审计、错误处理，MAOS 已有的版本
更严；它们值得读，但不值得抄。

| | MAOS 现状 | saas-skills-suite | 结论 |
|---|---|---|---|
| 出款（退款） | `tools/gateway.py`：四态回执、`out_request_no` 原生幂等、unknown 必须 query、三个适配器 | Stripe refund API，二态 | **MAOS 更强**，不抄 |
| 幂等 | `processed_key` 表 + `store.claim_idempotency()`，通用到所有 op | `stripe_webhook_events` UNIQUE，只管 webhook | **MAOS 更通用**，不抄 |
| 审计 | `event_log` + `ToolInvoked` + `actor_invocation_id` 全链溯源 | `admin_audit_log` 一张表 | **MAOS 更强**，不抄 |
| 验签 | `ingress/crypto.py`：常量时间比对、时钟偏移 300s、企微 block=32 那个坑 | `stripe.webhooks.constructEvent` 一行 | **基座已有**，支付侧没接 |
| 权威边界 | `domain/refund/guard.py`：settled 只有 `payment.observe` 写得进，且必须同事务附回执 | 无此概念，webhook 说什么写什么 | **MAOS 高一层** |
| **回调入站** | **无**。外部事实只能靠 `payment.observe` 主动轮询，上限 5 次 | webhook 是主路径 | 🔴 **纯缺口** |
| **收款** | **无**。`gateway.py` 只有 refund/query | Checkout Session / 订阅 / 充值 | 🔴 **纯缺口** |
| **对账** | **无**。本地观察从没跟外部流水比对过 | health-check SQL + 指标 | 🔴 **纯缺口** |
| **金额交叉校验** | **无**。回执金额没跟 `order_snapshot` 比过 | Amount Cross-Validation | 🔴 **缺口**，且正是铁律 8 的延伸 |
| 双桶钱包 / 积分计费 | 无（`finance_entry` 是退款域分录，不是余额） | 双桶 + append-only 流水 | ⚪ **另一件事**，见 §6 |

---

## 1. 缺口 1（核心）：支付事实只能「拉」，不能「推」

### 现象

`skills/builtin/refund/payment_observe.py` 是全系统唯一写得进 `settled` 的地方，
而它拿到终态的唯一手段是循环调 `gateway.query()`，上限写死在同文件：

```python
DEFAULT_MAX_POLLS = 5   # 到顶仍非终态就如实返回「还没问出来」，不许改判成失败
```

这个「不许改判成失败」是对的，但它留下一个洞：**问不到就没有下文了**。
一笔 10 分钟后才结算的退款，MAOS 在第 5 次 query 之后就再没有任何东西会把它捞回来。
场景 7 的 `task-s7-payment` 终态是 `FAILED`（`evidence/scenario-7/result.json`），
正是这个形状在演示里的样子。

真实世界里支付宝/微信/Stripe 都是**异步回调**：结算发生时平台主动 POST 过来。
MAOS 不接这条通道，等于把支付集成里最主要的那半条路让给了轮询。

### 补法：入站回执通道，作为 `payment.observe` 的第二个事实来源

**不是替代轮询，是并联。** 两条路最终都收敛到同一个落点 `payment_observation`。

```
拉：payment.observe ──► gateway.query() ──┐
                                          ├──► payment_observation ──► guard 放行 settled
推：平台 POST /callback/pay/<gw> ─────────┘
```

### D1（本方案最重要的一条）：回调只落观察 + 触发回源，**永不直接写终态**

`mvp-billing-system` 的做法是：webhook 说 `succeeded` → 写库 `status=active`，
再用 Amount Cross-Validation 兜底。**MAOS 不能这么做，也不需要这么做。**

理由三条，每条都不是洁癖：

1. **签名只证明「这条消息来自平台」，不证明「这是当前最新状态」。** 回调会乱序到达。
   先收到 `settled` 后收到 `processing` 的时候，直接写终态就是把外部状态写死为终态 —— 铁律 8 的原话。
2. **回调会重放。** `ingress/crypto.py` 的 `MAX_CLOCK_SKEW` 注释已经把这件事说透了：
   时间戳闸挡重放，幂等键闸挡平台自己的重推，**两道闸挡的不是同一件事**，两道都要。
3. **`guard.py` 已经把路堵死了**，而且堵得对：`AUTHORITATIVE_WRITER = "payment.observe"`。
   回调通道**写不进** `settled`，只能落观察。这不是要绕开的限制，这是要沿用的设计 ——
   回调通道天生合规，一行 guard 都不用改。

所以回调到达后的动作是：

```
验签 → 去重（claim_idempotency）→ 落 payment_callback 原文 →
落一条 payment_observation（observed_state = 回执里的 status）→
唤醒 payment.observe 做一次回源 gateway.query() →
回源说 settled，才由 payment.observe 写 settled
```

评委问「你怎么知道这笔退款成功了」，答案仍然是那句 —— 只是现在多了一句：
**「而且这次是平台先告诉我们的，我们仍然回源问了一次才认。」**

### D2：走 ingress 的基座，但不进 IM 渠道表

复用 `ingress/server.py` 的 HTTP 壳 + `ingress/crypto.py` 的密码学 + `claim_idempotency` 的去重，
但**新开路径**，不塞进 `ROUTES`（那张表是 IM 渠道的）。

理由：IM 消息进 `router.handle()` 是「变成一次动作」（认命令、分发、回帖），
支付回调进的是「变成一条观察」。处理链完全不同，共用 router 会把两件事揉在一起。
共用的只有那三样基座能力。

---

## 2. 缺口 2：没有收款侧

`gateway.py` 只有 `refund` / `query`。真实链条是 订单 → **收款** → （可能）退款，
MAOS 现在演示的是中间那一段。补上收款，链条才闭合，而且收款侧天然带来
「回调驱动」的最强演示场景（支付成功的通知只有回调，没有别的来源）。

### D3：收款域按 refund 域同构建，不加任何 Task 状态（铁律 9）

`charge_case.biz_status` 是业务对象自己的字段，与 `refund_case.biz_status` 同构。
`contracts/states.py` 一个字不改。

收款侧回执状态沿用 gateway 的四态语义（`STATUS_*`），且同样 **`create()` 永不返回终态** ——
理由和退款一模一样：下单成功 ≠ 付款成功。

---

## 3. 缺口 3：没有对账

MAOS 的观察从来没跟外部权威比对过。这不只是运维缺项 ——
它是「MAOS 不持有权威事实」这条论证最强的证据，现在缺席了：

> 你说权威在外部。那你怎么发现自己记的和外部不一致？

### D4：对账只读，产出差异清单 + 转人工，**不自动改**

拉外部流水（沙箱/Mock 给同一个接口）与本地 `payment_observation` 逐笔比，
差异落 `reconcile_diff`，处置是转人工 —— 与 `refund.snapshot_check` 见到订单漂移时的
处置口径一致（那个 skill 的处置就是转人工，不是自动重读快照往下跑）。

差异分三类，各自的判据写死：
- `missing_local`：外部有、本地无 → 漏收观察（回调丢了且轮询也没捞到）
- `missing_remote`：本地有、外部无 → **最严重**，本地凭空多了一条观察
- `amount_mismatch`：两边都有但金额不等 → 见 §4

---

## 4. 缺口 4：金额没有交叉校验

退款金额来自 `order_snapshot`（MAOS 执行前读到的那一版），
而回执里的实际金额从来没跟它比过。`mvp-billing-system` 的 Amount Cross-Validation
做的就是这件事，它的形态（比对期望值，不等就中止）可以直接拿来，
只是落点要改成 MAOS 的口径：**不中止，转人工**。

判据放在 `payment.observe` 落 observation 的同一个事务里：
回执金额 ≠ 快照金额 → 落一条 `reconcile_diff(kind='amount_mismatch')` 并把案子转人工，
**不许把 settled 写进去**（哪怕回执 status 确实是 settled）。

理由：金额对不上的 settled 是一笔「成功了，但成功的不是我们以为的那笔」的退款。
这比失败更危险 —— 失败会被补偿流程接住，而这个不会。

---

## 5. 落地形状（逐文件点名，全部是新增）

零修改冻结面：`contracts/events.py`、`contracts/states.py`、`core/store.py` 现有表
一个字节不动。下面全是新文件 / 新表。

### 新文件

```
maos/tools/callback.py              入站回调 ToolPort：验签 + 去重 + 落原文 + 落观察
maos/tools/callback_codes.py        三平台回调码表（形状抄 gateway_codes.py，含官方原文出处）
maos/ingress/pay_callback.py        HTTP 处理器，路径 /callback/pay/<gateway>
maos/tools/charge.py                收款 ToolPort：charge.create / charge.query，四态回执
maos/tools/charge_codes.py          收款错误码表
maos/domain/charge/                 收款域：schema.sql / objects.py / guard.py / fixtures.py / projection.py
maos/skills/builtin/charge/         charge.create / charge.observe
maos/tools/reconcile.py             对账 ToolPort：拉外部流水
maos/skills/builtin/refund/payment_reconcile.py   对账 skill（退款侧先做）
```

### 新表（只新增，不改现有）

```sql
-- 回调原文，append-only。原文必须存，验签失败的也存（那是攻击证据）
CREATE TABLE IF NOT EXISTS payment_callback (
    tenant_id     TEXT NOT NULL,
    gateway       TEXT NOT NULL,
    callback_id   TEXT NOT NULL,      -- 平台事件 id，幂等键的来源
    raw_body      TEXT NOT NULL,      -- 原文，不是解析后的
    verify_result TEXT NOT NULL,      -- ok | bad_signature | stale_timestamp | dep_missing
    received_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, gateway, callback_id, received_at)
);

-- 对账批次与差异
CREATE TABLE IF NOT EXISTS reconcile_run (
    tenant_id   TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    gateway     TEXT NOT NULL,
    window_from TEXT NOT NULL,
    window_to   TEXT NOT NULL,
    local_count  INTEGER NOT NULL,
    remote_count INTEGER NOT NULL,
    diff_count   INTEGER NOT NULL,
    ran_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE IF NOT EXISTS reconcile_diff (
    tenant_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- missing_local | missing_remote | amount_mismatch
    request_id TEXT NOT NULL,
    local_json  TEXT NOT NULL DEFAULT '{}',
    remote_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT,
    PRIMARY KEY (tenant_id, run_id, kind, request_id)
);

-- 收款域三表，与 refund 域同构（charge_case / charge_request / charge_observation）
-- 字段照 domain/refund/schema.sql 的 refund_case / refund_request / payment_observation 抄形状
```

### 沿用不改

- `store.claim_idempotency(key, op, task_id)` —— 回调去重直接用，`op='pay.callback'`
- `invoke_tool()` —— 所有新 ToolPort 一律走它，否则没有 `ToolInvoked` 审计行
- `domain/refund/guard.py` 的 `AUTHORITATIVE_WRITER` / `AUTHORITATIVE_STATES` —— 一个字不改
- `ingress/crypto.py` 的 `MAX_CLOCK_SKEW` 与常量时间比对 —— 支付验签复用同一套

---

## 6. 本方案明确不做

1. **双桶钱包 / 积分计费**（`billing_wallet` + `billing_wallet_tx` 那一套）。
   那是「MAOS 自己作为 SaaS 卖订阅」的产品形态，与「MAOS 编排的业务里有收付款」是两件事。
   真要做是另一个方向，前置问题是「MAOS 卖给谁、按什么计量」，不是技术问题。
2. **Stripe**。复赛演示在国内，已有 `AlipaySandboxAdapter` 的坑位，优先把它填实。
   Stripe 的价值在它的**方法论**（Test Clocks 时间旅行、CLI 重放 webhook），
   这两样可以在不接 Stripe 的前提下等价实现 —— 见派单里的重放测试轨。
3. **改任何冻结面**。见 §5 抬头。
4. **订阅生命周期 / 自动续费**。没有钱包就没有续费的意义。

---

## 7. 风险与退路

| 风险 | 判据 | 退路 |
|---|---|---|
| 回调通道成了绕过 guard 的后门 | 提交前 grep：`grep -rn "biz_status.*=.*'settled'" maos/ \| grep -v guard.py \| grep -v observe` 必须为空 | 回调通道永不写 biz_status，只写 observation，这条是 D1 的硬边界 |
| 验签实现写错了也照样跑 | 抄 `ingress/crypto.py` 的纪律：常量时间比对 + 时间戳校验 + 显式抛 `ChannelDepMissing` | 该文件的 docstring 已经把三个坑写死了，逐条对 |
| 收款域把 Task 状态撑爆 | `contracts/states.py` diff 必须为空（铁律 9） | biz_status 走业务对象自己的列 |
| 对账在 PG 后端跑不起来 | 已知 PRAGMA 盲点（见 BACKLOG，`compensate.py::_add_column_if_missing`） | 新 schema 一律用 `CREATE TABLE IF NOT EXISTS`，不用 PRAGMA 探列 |

---

## 8. 与已有文档的关系

- `docs/authoritative-facts.md` §1 画的边界，本方案在**入站方向**上把它延长了一段：
  外部推过来的也只是观察。那份文档的 §5b「业务结果的四判据」需要补第五条：**对账一致性**。
- `docs/gateway-rationale.md` 的「一眼总表」是「八个组件用没用」，本方案不进那张表 ——
  它接的不是推荐工具链的组件，是支付平台本身。
- `docs/BACKLOG.md` 里与支付相关的旧账（补偿干跑闸从没跑过、PG 束只有 happy）
  与本方案不冲突，但对账轨会碰到 PG 那条，已写进 §7。
