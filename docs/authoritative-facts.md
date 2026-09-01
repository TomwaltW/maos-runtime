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
| 测试过没过 | 沙箱里的 `pytest` | `test_report` artifact | 只有 `sandbox.pytest_run` 这个 ToolPort |
| 任务做完没有 | MAOS 自己 | Task 状态机 | 只有 Control Plane |

注意第一行与第二行的区别，这是整套设计的题眼：

- **`biz_status` 是 MAOS 自己的业务对象字段**，它可以自由迁移（`submitted → approved
  → gateway_accepted → processing`）；
- **但其中 `settled` 这一个值是外部权威事实的投影**，它不属于 MAOS。所以
  `settled` 被单独拎出来，做成一个只有观察者写得进的状态。

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
