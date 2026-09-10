# 权威事实边界 —— MAOS 不持有权威事实

评委的第三条诊断是：**「所有 Agent 都回复完成」≠ 业务成功**。

这条诊断的根子不在「Agent 说谎」，在**边界划错**：一个把外部系统的状态直接写进
自己库里当终态的系统，无论 Agent 多诚实，它报出来的「成功」都只是自述。

MAOS 的回答是一条铁律（`CLAUDE.md` 铁律 8）：

> **MAOS 不持有权威事实，只持有观察与推断。订单、支付、库存的权威状态永远归属外部
> 系统。任何把外部状态直接写死为终态的代码都是 bug。**

本文件把这条铁律拆成可以逐条去查的东西。

---

## 1. 边界画在哪

| 事实 | 权威在 | MAOS 里存的是 | 谁能写 |
| :-- | :-- | :-- | :-- |
| 退款到没到账 | 支付网关 | `payment_observation`（**观察记录**，带 `poll_count`） | 只有 `payment.observe` |
| 退款案子的业务状态 | MAOS 自己（这是它自己的业务对象） | `refund_case.biz_status` | `create_case()` / `update_biz_status()` 两个入口，无第三条 |
| 保险赔款到没到账 | 赔付方 | `claim_payment_observation`（带请求编号、观察状态、`poll_count`）；`claim_case.biz_status='paid'` 是到账投影 | 只有 `claim.observe` 能落回执及写入 `paid`，由 `maos/domain/claim/guard.py` 守卫 |
| 银行差错款项是否退回 | 清算方的 `pacs.004` 资金退回报文；`camt.029` 撤销决议不等于资金退回 | `resolution_observation`（带报文类型、原因码与退回金额）；`investigation_case.biz_status='returned'` 是资金退回投影 | 只有 `investigation.observe` 能落观察及写入 `returned`，由 `maos/domain/investigation/guard.py` 守卫 |
| 应付货款是否已划出 | 银行 | `ap_payment_observation`（已付款状态及 `bank_reference` 流水号）；`ap_case.biz_status='settled'` 是银行已付款投影 | 只有 `ap.observe` 能落回单及写入 `settled`，由 `maos/domain/ap/guard.py` 守卫 |
| 测试过没过 | 沙箱里的 `pytest` | `test_report` artifact | 只有 `sandbox.pytest_run` 这个 ToolPort |
| 任务做完没有 | MAOS 自己 | Task 状态机 | 只有 Control Plane |

注意第一行与第二行的区别，这是整套设计的题眼：

- **`biz_status` 是 MAOS 自己的业务对象字段**，它可以自由迁移（`submitted → approved
  → gateway_accepted → processing`）；
- **但其中 `settled` 这一个值是外部权威事实的投影**，它不属于 MAOS。所以
  `settled` 被单独拎出来，做成一个只有观察者写得进的状态。

三个新域遵循同一边界：各域 `guard.py` 的 `AUTHORITATIVE_WRITER` 与
`AUTHORITATIVE_STATES` 分别为 `claim.observe` / `{paid}`、
`investigation.observe` / `{returned}`、`ap.observe` / `{settled}`。
观察与权威状态投影同事务落库，业务状态仍留在业务表，Task 状态机不扩展。
调查域还要求 `pacs.004`、退回金额和退回原因码；仅有 `camt.029/CNCL`
「指令已撤销」不能写 `returned`。AP 则要求已付款回单带银行流水号。
场景 8–10 目前使用 Mock 赔付方 / 清算方 / 银行验证这些边界，真实服务接通另行验收。

```python
# maos/domain/refund/guard.py:27
AUTHORITATIVE_WRITER = "payment.observe"

# maos/domain/refund/guard.py:31 —— 将来若有第二个「外部说了才算」的终态，加进这里
AUTHORITATIVE_STATES = frozenset({"settled"})
```

业务状态机（**不是** Task 状态机，铁律 9：业务状态是业务对象自己的字段）：
`maos/domain/refund/guard.py:34`

```text
submitted ─→ approved ─→ gateway_accepted ─→ processing ─→ settled
    │            │              │                 │
    └→ rejected  └→ rejected    └→ compensated    └→ compensated
                 └→ compensated
```

---

## 2. settled guard：代码在哪、拦什么

**唯一写入路径**：`update_biz_status()` — `maos/domain/refund/guard.py:125`

四道拦截，顺序有讲究：

| # | 判据 | 代码位置 | 拦下时做什么 |
| :-- | :-- | :-- | :-- |
| ① | 写 `AUTHORITATIVE_STATES` 的不是 `payment.observe` | `guard.py:150` | 落 `AuthoritativeFactViolation` 事件 **+ 抛异常** |
| ② | 递交 `observation` 回执的不是 `payment.observe` | `guard.py:160` | 同上 —— 否则等于给别人开了个伪造回执的口子 |
| ③ | 迁移不在 `BIZ_STATUS_FLOW` 里 | `guard.py:172` | 抛 `BizStatusTransitionError` |
| ④ | 写 `settled` 却没带完整回执（`request_id` / `gateway_code` / `observed_state` 三字段缺一不可） | `guard.py:179` | 落事件 + 抛 —— **没有回执的 settled 就是把外部状态写死为终态** |

写 `settled` 与插 `payment_observation` **同事务**（`guard.py:190` 起，借 Store 自己那把
`RLock`）：状态与回执要么一起进库，要么都不进 —— 否则「settled 必有回执」这条断言
会在并发下偶发地不成立。

两个设计取舍，都写在源码注释里：

- **①放在存在性检查之前**。对一个不存在的 case 越权写 `settled`，也要留下越权记录 ——
  先查存在性会让这种试探以 `LookupError` 收场，证据就没了，而那恰恰是最该留痕的一种。
- **越权不静默失败**：抛异常 **并且** 落一条 `AuthoritativeFactViolation` 事件。理由与
  `scripts/guard_bash.py` 相同：**「系统拒绝了一次越权写入」本身就是要拿给评委看的证据**，
  吞掉就没了。

**旁路也堵死了**：域内唯一的写入口 `objects.execute()`（`maos/domain/refund/objects.py:72`）
每条 SQL 都先过 `_guarded()`（`objects.py:63`），见到针对 `refund_case` 的写语句直接抛
`BypassedGuardError` —— 绕过 guard 直接写 SQL 这条路在运行时就不通。

**提交前还有一道 grep 级守卫**，钉成了单测：

```bash
python3 -m pytest maos/tests -q -k test_no_bypass_writes_settled
```

（`maos/tests/test_refund_flow.py::test_no_bypass_writes_settled` —— 全仓库只有
`payment.observe` 调得出 `update_biz_status(..., "settled", ...)`。）

---

## 3. verify.py 第 3 项：核验器怎么验这条边界

`scripts/verify.py` 的第 3 项 `authoritative-fact`（`scripts/verify.py:262`）
**两头都查**：

1. 每个 `biz_status='settled'` 的 case，必须有对应的 `payment_observation` 行；
   没有 → `FAIL：外部状态被直接写死为终态`。
2. 每条回执的 `actor_invocation_id`，必须真的属于一次 `payment.observe` 调用 ——
   做法是从 `event_log` 里把所有 `SkillInvoked` 且 `detail.skill == payment.observe`
   的 `invocation_id` 收成集合，回执的 actor 必须落在集合里；
   不在 → `FAIL：权威事实边界被绕过`。

**第 2 条是关键**。只查第 1 条的话，任何一个 skill 自己伪造一条 `payment_observation`
就能过关 —— 边界就成了摆设。第 2 条把「谁写的」也钉进证据里。

反面情形也印出来但不判负：有回执、案子却没到 `settled`（观察到了但没收口），
`warn` 点名。

---

## 4. 实况：这个核验器真的抓到过一次绕过

**这一段才是本文件最值钱的部分。** 一个能自己发现「权威边界被绕过」的核验器，
比一句「我们划分了边界」有说服力得多 —— 下面是它抓到的那一次。

**现象**：整合轮合并后，`verify.py` 第 3 项 **FAIL**：
`payment_observation.actor_invocation_id` 不属于任何一次 `payment.observe` 调用。

**根因**（不是假阳性）：`SkillInvoker` 生成的官方 `invocation_id` **到不了 skill 里** ——
`maos/skills/invoker.py` 生成后只放进 `SkillResult` 与落库那行，没有塞进
`SkillContext.extras`。退款域这一轨够不着 `invoker.py`（不在它的边界内），
于是按「调用方传入 + skill 本地兜底 uuid4」的口径实现，两个 id 都非空、都能对上账，
**但不是同一个值**。

**为什么两轨各自全绿、合并才暴露**：

- 写退款域那一轨：`invoker.py` 明写属别人的面，它只能兜底，且它的单测只验「id 非空」；
- 写核验器那一轨：只写核验器、不改被验对象，它手上没有真数据可跑。

两边都对，缝里漏了。**这正是证据束存在的理由** —— 它是唯一一个把两轨的产出放在
一起重放的地方。

**修法**：改 `maos/skills/invoker.py` **一行**，让官方 id 进 `SkillContext.extras`：

```python
extras = {**extras, "invocation_id": invocation_id}
```

**故意覆盖**调用方传入的同名键 —— 官方 id 只有 invoker 生成的那一个，
skill 侧那个是 invoker 补齐前的兜底，两个都在时必须以**事件里落了的那个**为准。
四个退款 skill 与 `_common.invocation_id_of()` 一行未动，兜底分支保留。

完整记录：`docs/DECISIONS.md` 的 `## integrate-round-2` 小节（`docs/DECISIONS.md:321`）。

---

## 5. 场景 7：边界在失败路径上的样子

失败路径（`python3 run.py --scenario 7`）是这条边界最直白的演示 ——
网关返 `ACQ.SYSTEM_ERROR`，`payment.observe` 轮询 3 次仍问不出终态：

```text
  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['manual_ticket', 'refund_request_revoked']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
```

（本机实跑输出，`exit=0` —— **场景本身跑成功了，业务结果是失败**，这两件事在
MAOS 里是分开记的。）

题眼：**问不出终态时，系统什么都不写**。不猜、不推断、不「大概率成功了」。
四个 Agent 全部回复完成、Plan 走完全程，而业务状态收在 `compensated`，
Plan 终态是 `FAILED(human_reject)` —— 系统如实记录了「这一单没成」。

可核验：

```bash
sqlite3 <db> "select count(*) from payment_observation where observed_state='settled'"  # 0
sqlite3 <db> "select biz_status from refund_case"                                       # compensated
sqlite3 <db> "select count(*) from event_log where event_type='CompensationExecuted'"   # >0
```

---

## 5b. 业务结果的四判据：到账 / 客户确认 / 人工纠错 / 投诉

上一节说的是「问不出终态时系统什么都不写」。本节说的是它的另一半：**写下来的那些，
分别归谁**。评委第三条原话：

> 应以**退款到账、客户确认、人工纠错和投诉结果**验证整个 DAG。「所有 Agent 都回复
> 完成」只表示协作结束，不代表业务成功。

四判据落在 `case_outcome` 表（`maos/domain/refund/outcome.py`），取值域是跨轨契约的
一部分，不许自造措辞：

| 判据 | 取值 | 权威在谁手上 | 入口 |
|---|---|---|---|
| `arrival` | `settled` / `unsettled` / `unknown` | **支付网关** | `payment.observe` 写 `payment_observation` |
| `customer_confirmation` | `confirmed` / `disputed` / `none` | 客户 | `scripts/case_inbound.py --confirm｜--dispute` |
| `manual_correction` | `none` / `overridden` / `compensated` | 线下操作 | 补偿工单 / 人工回执 |
| `complaint` | `none` / `open` / `closed` | 客户 | `scripts/case_inbound.py --complain` |

```text
business_success = (arrival == "settled") and (customer_confirmation != "disputed")
                                          and (complaint != "open")
```

### `arrival` 只由 payment_observation 的行决定

这是本节的题眼，也是这张表最容易写错的一处。`refund_case.biz_status` 上有一个
`settled`，读它算 `arrival` 只要一行代码，还永远对得上账 —— 因为两者本来就是同一份
数据的两次拷贝。但那样一来「到账」就退化成「我们自己认为到账了」：`biz_status` 是
MAOS 的推断，`payment_observation` 才是网关给的观察。

所以 `compute_case_outcome()` 的入参里**根本没有 `biz_status`**，只有观察行；
`arrival_basis` 必须指回具体那一行：

```text
arrival_basis = payment_observation:<request_id>@<observed_at>
```

`unsettled` 与 `unknown` 也不许混：网关明确回了 `failed` 是 `unsettled`（外部结果明确，
只是明确地失败了）；轮询到顶仍问不出终态是 `unknown`（**外部结果不明确**，那笔钱
可能已经出去了）。把 `unknown` 当成 `unsettled`，账面上就会凭空少一笔。

同理，**客户说收到了也换不来「到账」**：`--confirm` 只写 `customer_confirmation` 与
`notification.ack_at`，一个字都不碰 `arrival`。确认过的案子照样可能 `arrival=unknown`
—— 这不是矛盾，这正是四判据要分开的原因。

### 「全 DONE 但业务没成功」跑得出来

```bash
env -u MAOS_LLM_API_KEY -u MAOS_LLM_BASE_URL -u MAOS_LLM_MODEL \
  python3 scripts/run_case.py scenarios/custom/refund-case.json --stall
```

`--stall` 让 `MockGateway` 永不返终态、`payment.observe` 轮询到 `max_polls` 上限。
于是**每个任务都走到 DONE、Plan 也是 DONE**，而 `payment_observation` 表是空的：

```text
  任务终态  : 5 个任务，全 DONE = True（DONE）
  Plan      : DONE
  支付观察  : 0 条 —— 轮询到 max_polls 上限仍非终态，`payment.observe` 一行都不写
  到账      : unknown（依据 无观察行）
  业务成功  : False
```

（本机实跑输出，`exit=0`。）这是「所有 Agent 都回复完成 ≠ 业务成功」最直白的一张证据：
协作完成了，业务没有。

### 核验器怎么验这条边界

`scripts/verify.py` 第 10 项 `check_case_outcome`：四判据齐全且取值合法、
`business_success` 等于那三个值算出来的、`arrival` 与库里的观察行**重新数一遍**对得上、
`arrival == settled` 时 `arrival_basis` 回查得到那一行且它确实是 `settled`。
第 6 项同时加了一口牙：**DONE 且 `arrival != settled` ⇒ 必须 `business_success=false`**。

```bash
sqlite3 <db> "select arrival, arrival_basis, business_success from case_outcome"
sqlite3 <db> "select count(*) from payment_observation where observed_state='settled'"
```

---

## 6. 同一条边界在软件域的样子

退款域的权威在支付网关；软件域的权威在**沙箱里真跑出来的 pytest 结果**：

- Coding Agent 的 `self_check` 全写 `pass` **不构成验收证据**。验收闸
  （`maos/runtime/gate.py:146`）认的是同一 attempt 的 `test_report`，**没有报告 = blocker，
  无降级**，不回落 `self_check`。
- 那份报告来自 `sandbox.pytest_run` 这个 ToolPort，跑在 `--network none --read-only
  --user 1000:1000` 的容器里，MAOS 控制不了它的结论。
- 场景 2 就是这条的正面演示：第一轮补丁 `self_check` 全 pass、四道旧闸一条都拦不住，
  拦下它的是 `test_report` 里那条真挂掉的用例。

**两个域，同一句话：终态由外部说了算，MAOS 只负责如实记录它观察到了什么。**
