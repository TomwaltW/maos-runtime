# C1 回执：三个新业务域接入 CLI 与证据链

已完成实现与逐条验收。新增 35 项测试，实际全套为 **1986 passed, 41 skipped**；默认 CLI 仍跑 1–7，默认生成 8 束，默认核验 **8/8 PASS + 1 行 warn**，与修改前完整输出逐字节一致。扩展核验为 **14/14 PASS + 3 SKIP**。

场景 8/9/10 使用已有 Mock 赔付方、清算方与银行验证观察和编排边界；没有声称本轮连接了真实金融服务。新域没有历史案例晋升数据，三个 history-case 如实 SKIP。

证据由真实命令生成，130 份文本产物的出处首行均已检查；数据库按仓库既有规则不纳入 Git。出处记录执行时的 `6086dfcb33454aea486e08632797eeed70731b24-dirty`，保留提交前真实工作区来源，没有手改为提交后的 SHA。

## 1. 验收命令的真实输出尾部

以下内容读取本机命令 stdout/stderr 日志，退出码来自实际子进程。空输出命令保留空代码块；默认核验完整保留，以便核对 warn。

### `python3 -m pytest maos/tests -q`

退出码：0。

```text
................................sssssssssssssssssssssssssssss........... [ 67%]
........................................................................ [ 71%]
........................................................................ [ 74%]
........................................................................ [ 78%]
........................................................................ [ 81%]
........................................................................ [ 85%]
........................................................................ [ 88%]
........................................................................ [ 92%]
........................................................................ [ 95%]
........................................................................ [ 99%]
...........                                                              [100%]
1986 passed, 41 skipped in 55.12s
```

### `python3 run.py`

退出码：0。

```text
    task-s7b-finance  PENDING          -> DISPATCHED       [dispatch]
    task-s7b-finance  DISPATCHED       -> RUNNING          [claim]
    task-s7b-finance  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s7b-finance  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7b-finance  BLOCKED          -> DONE             [human_approve]
    task-s7b-payment  PENDING          -> DISPATCHED       [dispatch]
    task-s7b-payment  DISPATCHED       -> RUNNING          [claim]
    task-s7b-payment  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s7b-payment  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s7b-payment  BLOCKED          -> FAILED           [human_reject]

全部场景通过：事件契约与状态机在真实链路上成立，可以进入并行分轨。
```

### `python3 run.py --scenario 8`

退出码：0。

```text
    task-s8c-adjudicate  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s8c-adjudicate  AWAITING_REVIEW  -> DONE             [gate_pass]
    task-s8c-settle  PENDING          -> DISPATCHED       [dispatch]
    task-s8c-settle  DISPATCHED       -> RUNNING          [claim]
    task-s8c-settle  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s8c-settle  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s8c-settle  BLOCKED          -> DONE             [human_approve]
    task-s8c-pay  PENDING          -> DISPATCHED       [dispatch]
    task-s8c-pay  DISPATCHED       -> RUNNING          [claim]
    task-s8c-pay  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s8c-pay  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s8c-pay  BLOCKED          -> FAILED           [human_reject]
```

### `python3 run.py --scenario 9`

退出码：0。

```text
  业务状态  : compensated（全程没有经过 returned）
  returned 观察: 0 条 —— 没问出资金证据就一条都不该有
  真实观察  : 5 条 {'cancellation_confirmed': 4, 'pending': 1}
  补偿记录  : 2 行 ['cancellation_withdrawn', 'manual_reconciliation_ticket']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）

========================================================================
场景 9 收口：两条路径共用同一句 CNCL，一条拿到 pacs.004 收口 returned，
            另一条拿不到就一个字都不写 —— 差别不在 Agent 说了什么，
            在于外部权威到底给没给出资金证据。
========================================================================
```

### `python3 run.py --scenario 10`

退出码：0。

```text
    task-s10b-pay  PENDING          -> DISPATCHED       [dispatch]
    task-s10b-pay  DISPATCHED       -> RUNNING          [claim]
    task-s10b-pay  RUNNING          -> AWAITING_REVIEW  [submit_result]
    task-s10b-pay  AWAITING_REVIEW  -> BLOCKED          [gate_needs_human]
    task-s10b-pay  BLOCKED          -> FAILED           [human_reject]

  业务状态  : compensated（全程没有经过 settled）
  settled 观察: 0 条 —— 没问出终态就一条都不该有
  补偿记录  : 2 行 ['payment_instruction_revoked', 'reconciliation_ticket']
  补偿事件  : 1 条 CompensationExecuted
  Plan 终态 : FAILED（主管驳回，业务确实没成功）
  Agent 自述 : {'task-s10b-intake': 'ok', 'task-s10b-match': 'ok', 'task-s10b-plan': 'ok', 'task-s10b-pay': 'ok'} —— 四个全 ok，而案子没成
```

### `python3 scripts/make_evidence.py`

退出码：0。

```text
证据束生成 · sha=6086dfcb33454aea486e08632797eeed70731b24-dirty · 场景=[1, 2, 3, 4, 5, 6, 7] + R5 · 输出=/Users/shensikai/Documents/MAOS/evidence
  [OK] evidence/scenario-1  spans=37 events=26
  [OK] evidence/scenario-2  spans=48 events=35
  [OK] evidence/scenario-3  spans=17 events=12
  [OK] evidence/scenario-4  spans=10 events=7
  [OK] evidence/scenario-5  spans=32 events=26
  [OK] evidence/scenario-6  spans=54 events=41
  [OK] evidence/scenario-7  spans=103 events=80
  [OK] evidence/scenario-R5  spans=117 events=87
  [AUX] evidence/room  文件 9（不由本脚本产，仅登记）

完成：8 场景落盘，0 场景缺模块。
```

### `python3 scripts/verify.py`

退出码：0。

```text
[PASS] hash-integrity       93/93
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
         · info: scenario-7 case=case-s7-0001: 有回执且案子收口在 biz_status=compensated（非 settled 终态） —— 预期行为：settled 是权威终态，收口在别处的案子本来就不该有 settled 观察
         · warn: scenario-7 case=case-s7-0002: 有回执但案子停在中间态 biz_status=gateway_accepted —— 观察到了但没收口（既没到 settled，也没落到 ['compensated', 'rejected'] 任何一个终态）
[PASS] trace-tree           29/29
         · info: scenario-1: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-2: 3 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-3: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-5: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-6: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-7: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1
[PASS] cost-attribution     57/57

RESULT: 8/8 PASS
证据来源：scenario-1, scenario-2, scenario-3, scenario-4, scenario-5, scenario-6, scenario-7, scenario-R5
```

### `python3 scripts/check_docs.py`

退出码：0。

```text

### docs/matrix-room-runbook.md
  [A-lang] docs/matrix-room-runbook.md:716  代码围栏没写语言标注
  [A-lang] docs/matrix-room-runbook.md:728  代码围栏没写语言标注
  [A-lang] docs/matrix-room-runbook.md:746  代码围栏没写语言标注
  [A-lang] docs/matrix-room-runbook.md:764  代码围栏没写语言标注

============================================================
共 43 条（阻断 0 / 提示 43）
  A-lang         41  advisory
  C-h1            1  advisory
  C-skip          1  advisory
```

### `git diff --stat maos/contracts/`

退出码：0。

```text

```

### `python3 scripts/make_evidence.py --domains`

退出码：0。

```text
  [OK] evidence/domains/scenario-2  spans=48 events=35
  [OK] evidence/domains/scenario-3  spans=17 events=12
  [OK] evidence/domains/scenario-4  spans=10 events=7
  [OK] evidence/domains/scenario-5  spans=32 events=26
  [OK] evidence/domains/scenario-6  spans=54 events=41
  [OK] evidence/domains/scenario-7  spans=103 events=80
  [OK] evidence/domains/scenario-8  spans=44 events=33
  [OK] evidence/domains/scenario-9  spans=84 events=66
  [OK] evidence/domains/scenario-10  spans=41 events=31
  [OK] evidence/domains/scenario-R5  spans=117 events=87

完成：11 场景落盘，0 场景缺模块。
```

### `python3 scripts/verify.py --evidence evidence/domains --domains`

退出码：0。

```text
[PASS] hash-integrity       159/159
[PASS] business-ref         35/35
[PASS] authoritative-fact   6/6
         · info: scenario-7 case=case-s7-0001: 有回执且案子收口在 biz_status=compensated（非 settled 终态） —— 预期行为：settled 是权威终态，收口在别处的案子本来就不该有 settled 观察
         · warn: scenario-7 case=case-s7-0002: 有回执但案子停在中间态 biz_status=gateway_accepted —— 观察到了但没收口（既没到 settled，也没落到 ['compensated', 'rejected'] 任何一个终态）
         · info: scenario-8/runtime-2 case=clm-s8-0002: 有回执且案子收口在 biz_status=compensated（非 paid 终态） —— 预期行为：paid 是权威终态，收口在别处的案子本来就不该有 paid 观察
         · info: scenario-9 case=case-s9-0002: 有回执且案子收口在 biz_status=compensated（非 returned 终态） —— 预期行为：returned 是权威终态，收口在别处的案子本来就不该有 returned 观察
[PASS] trace-tree           44/44
         · info: scenario-1: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-2: 3 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-3: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-5: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-6: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-7: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-8: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-8/runtime-2: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
[PASS] kb-hit               7/7
[PASS] business-outcome     17/17
[PASS] history-case         1/1
[PASS] cost-attribution     68/68
[PASS] claim/authoritative-fact 1/1
         · info: scenario-8/runtime-2 case=clm-s8-0002: 有回执且案子收口在 biz_status=compensated（非 paid 终态） —— 预期行为：paid 是权威终态，收口在别处的案子本来就不该有 paid 观察
[PASS] claim/business-outcome 3/3
[SKIP] claim/history-case   (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)
[PASS] investigation/authoritative-fact 1/1
         · info: scenario-9 case=case-s9-0002: 有回执且案子收口在 biz_status=compensated（非 returned 终态） —— 预期行为：returned 是权威终态，收口在别处的案子本来就不该有 returned 观察
[PASS] investigation/business-outcome 2/2
[SKIP] investigation/history-case (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)
[PASS] ap/authoritative-fact 1/1
[PASS] ap/business-outcome  2/2
[SKIP] ap/history-case      (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)

RESULT: 14/14 PASS, 3 SKIP（claim/history-case, investigation/history-case, ap/history-case）—— 不计入分子
证据来源：scenario-1, scenario-10, scenario-10/runtime-2, scenario-2, scenario-3, scenario-4, scenario-5, scenario-6, scenario-7, scenario-8, scenario-8/runtime-2, scenario-9, scenario-R5
```

### `python3 scripts/verify.py --domains`

退出码：0。

```text
[PASS] hash-integrity       93/93
[PASS] business-ref         35/35
[PASS] authoritative-fact   3/3
         · info: scenario-7 case=case-s7-0001: 有回执且案子收口在 biz_status=compensated（非 settled 终态） —— 预期行为：settled 是权威终态，收口在别处的案子本来就不该有 settled 观察
         · warn: scenario-7 case=case-s7-0002: 有回执但案子停在中间态 biz_status=gateway_accepted —— 观察到了但没收口（既没到 settled，也没落到 ['compensated', 'rejected'] 任何一个终态）
[PASS] trace-tree           29/29
         · info: scenario-1: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-2: 3 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate；maos.flows.common.patch_verifier。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-3: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-5: 2 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.testing.seed_scripted_report。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-6: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
         · info: scenario-7: 1 份产物走旁路入库（未经 on_task_result），来源已由 ArtifactSeeded 事件点名：maos.agents.reviewer.review_after_gate。这说的是入库路径，不是内容真伪 —— 判真伪看 sandbox.mode
[PASS] kb-hit               7/7
[PASS] business-outcome     10/10
[PASS] history-case         1/1
[PASS] cost-attribution     57/57
[SKIP] claim/authoritative-fact (本轮证据里没有 claim_case / claim_payment_observation 表)
[SKIP] claim/business-outcome (空转：证据束里没有待核验的业务 Plan 终态，本项判据一次都没执行)
[SKIP] claim/history-case   (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)
[SKIP] investigation/authoritative-fact (本轮证据里没有 investigation_case / resolution_observation 表)
[SKIP] investigation/business-outcome (空转：证据束里没有待核验的业务 Plan 终态，本项判据一次都没执行)
[SKIP] investigation/history-case (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)
[SKIP] ap/authoritative-fact (本轮证据里没有 ap_case / ap_payment_observation 表)
[SKIP] ap/business-outcome  (空转：证据束里没有待核验的业务 Plan 终态，本项判据一次都没执行)
[SKIP] ap/history-case      (kb 层未落地：本轮无 kb_doc 表，history_case 这一类知识尚不存在（P5 才建）)

RESULT: 8/8 PASS, 9 SKIP（claim/authoritative-fact, claim/business-outcome, claim/history-case, investigation/authoritative-fact, investigation/business-outcome, investigation/history-case, ap/authoritative-fact, ap/business-outcome, ap/history-case）—— 不计入分子
证据来源：scenario-1, scenario-2, scenario-3, scenario-4, scenario-5, scenario-6, scenario-7, scenario-R5
```

### `python3 /tmp/maos-c1/verify_invariants.py`

退出码：0。

```text
Default verify byte-for-byte equal: True
Default warn count: 1
Frozen files and core/store.py SHA256 unchanged: 6
Pre-existing user edits preserved; only recorded historical citation adjusted
Generated text artifacts with provenance header: 130
Default CLI contains no new-domain headings: True
Default business outcome JSON byte-for-byte equal on real database plans: 11
```

## 2. 域注册表最终形态

文件：`maos/domain/__init__.py`，公开 `DOMAIN_REGISTRY: dict[str, DomainSpec]`。权威写入者、状态集、回执状态和业务终态直接来自各域 guard 常量；没有修改 Task 状态或迁移。

| 域 | 业务表 / 主键列 | 观察表 | 权威写入者 | 权威状态 |
|---|---|---|---|---|
| refund | refund_case / case_id | payment_observation | payment.observe | settled |
| claim | claim_case / claim_id | claim_payment_observation | claim.observe | paid |
| investigation | investigation_case / case_id | resolution_observation | investigation.observe | returned |
| ap | ap_case / case_id | ap_payment_observation | ap.observe | settled |

每项另注册 `observation_fields`、`receipt_states`、`terminal_states`、`required_fields`，供导出与回查同源使用。调查还引用 `AUTHORITATIVE_EVIDENCE`，核验 pacs.004、退回金额与退回原因码；AP 必须有 bank_reference。历史案例按域、租户、案号定位，不能借其他域或租户同号成功案例背书。

显式生成使用 `python3 scripts/make_evidence.py --domains`，根目录为 `evidence/domains/`；11 个场景中，8 与 10 各保留一个 runtime-2 失败路径子束，共核验 13 个独立数据库。父束和子束各自保留真实事件序号与快照，不做数据库合并。

## 3. 对照表中核实后标为 ⚠️ 的行

- **Gate**：七道闸框架共用，但财务闸限定 refund，网关码闸只认 receipt.code；三个新域的业务回执由各自 Skill/guard 负责。
- **replan**：三个新域场景没有注入 replanner，未演练重规划。
- **补偿**：共用 SkillInvoker 和 CompensationExecuted 审计事件；域内补偿没有逆补丁产物，不经过补偿干跑闸，也不代表外部资金已经撤回。

事件契约、Task 状态机、Control Plane、Worker 与 HITL 五行已核实共用实现；对应代码与历史接入提交的空差异命令见 `docs/domain-portability.md` §1.1。冻结面与 core/store.py 指纹共 6 个文件均未变化；flow 与 runtime 也没有改动。

## 4. BACKLOG / DECISIONS 追加内容

另有一个为消除本次新增路径歧义所必需的旧引用修正：BACKLOG 中短路径 scenario-6/business-objects.json 补全为 `evidence/scenario-6/business-objects.json`。用户原有申请表相关改动保留，不纳入 C1 提交。

### BACKLOG.md

```text
## task-C1（多域证据核实的范围外缺口，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | C1 | 场景 8–10 均未注入 replanner，域内补偿没有逆补丁产物，不经过 `_gate_compensation` 干跑；财务闸仍限定 refund | 内核实现可共用，但三新域的重规划、补偿预验证及通用财务闸尚无端到端证据；网关码闸限制已见 task-T37 / task-T39 | 后续由域流程与运行时负责轨补覆盖；本轮仅如实标 ⚠️，不改内核 |
| 2026-09-05 | C1 | `collect_business_objects` / `check_business_ref` 仍只认退款 `business_ref`，未接理赔 `claim_business_ref` 与 AP `ap_business_ref`；调查本来没有引用表（已见 task-T38） | 本次三项核心核验覆盖新域，但第 2 项及 business-objects.json 尚不核验理赔/AP 的业务引用，不能把聚合 PASS 当作该项已跨域覆盖 | 下一次扩展第 2 项时接各域现有 resolver 与引用表；本轮派单只要求第 3/6/7 项，不顺手扩大 |
| 2026-09-05 | C1 | `scripts/demo_preflight.sh` 的 EXPECT_TESTS_NOPG 仍为 1476，已落后于本轮实测的 1986；默认束数仍正确固定为 8 | 直接运行完整预检会因测试数量旧值报错，独立执行本派单逐条验收可通过 | 后续整合轮刷新测试数量；当前可显式设置 MAOS_EXPECT_TESTS=1986，C1 不改预检脚本及冻结束数 |
```

### DECISIONS.md

```text
## task-C1（多域 CLI 与证据链，2026-09-05）

| 日期 | Phase | 情境 | 选择 | 理由 |
|---|---|---|---|---|
| 2026-09-05 | C1 | 默认输出须逐字节兼容，同时缺少新域证据须显式 SKIP | 默认核验仍保留八项汇总并遍历已有域；`verify.py --domains` 额外显示三新域的三项结果，空分母为 SKIP；`make_evidence.py --domains` 缺省落到 `evidence/domains/`，可用 `--out` 指定位置 | 默认场景仍为 1–7 + R5，扩展证据不混入默认场景扫描；分域缺失可直接核对 |
| 2026-09-05 | C1 | 各域主键与权威回执形状不同，只有表名和写入者不足以回查 | 注册表补主键列、观察字段及从 guard 读取的回执判据；生成器和核验器共用注册表，调查仍要求 pacs.004、金额与原因码，AP 仍要求银行流水 | 保持域业务对象与状态机原样，不能把退款字段套到其他域而误判 |
| 2026-09-05 | C1 | 旧生成器将每次 build 绑定同一文件库，实际导致场景 8 的失败路径读到成功路径 paid 观察；场景 10 有相同隔离要求 | 新域每个运行时独立落库，首个保留场景根目录，后续放 `runtime-2` 等子束；run 原有断言全部完成后分别导出、索引与核验 | 合并会碰撞事件序号及保单快照 read_at；保留原始库完整保存事实，不改已跑通的 flow，也不削弱全库断言 |
| 2026-09-05 | C1 | 旧测试把 ALL 与 DEFAULT 相等、判据 kind 源码字面量及空分母 PASS 当作固定规则 | 仅更新直接冲突的默认序列、索引、对照束、判据集合与空分母断言，并补显式入口、真实多域导出和篡改负例 | C1 明确要求 ALL=1–10、DEFAULT=1–7、注册表同源与空分母 SKIP；不通过删除守卫换取测试通过 |
| 2026-09-05 | C1 | 新域文档核查发现旧正文“两域”、财务闸可直接跨域和无需接 CLI 的口径失真 | 同步更正直接矛盾，历史端点和数字保留；Gate、replan、补偿标 ⚠️ | 同一实现与已覆盖业务能力必须分别证明，避免新对照表与旧正文互相矛盾 |
| 2026-09-05 | C1 | 调查成功案保留 pending、camt.029/CNCL 与 pacs.004 三次真实观察，前两者不能证明 returned | 新域导出只把满足 guard 权威终态判据的观察列入成功依据，数据库和 trace 保留完整观察历史；退款导出保持原样 | 避免把过程观察当成成功凭据，也不删除真实轮询事实 |
| 2026-09-05 | C1 | 扩展证据新增同名文件后，文档守卫无法唯一解析 BACKLOG 既有短路径 scenario-6/business-objects.json，新增 E-missing 阻断 | 仅给该处历史引用补全 evidence/ 前缀，其余旧内容保持不变 | 修复本次新增目录直接造成的路径歧义，恢复文档检查原有 0 阻断口径；不修改守卫规则 |
```

## 5. 验收过程中发现并处理的问题

- 旧生成器破坏场景 8/10 运行时隔离：通过生成器独立落库和核验子束解决，原 flow 断言未改。
- 调查成功案的 pending/CNCL 过程观察被列作成功依据：仅筛选真正支持 returned 的观察作为成功凭据，原库和 trace 保留完整历史。
- 原有零分母 PASS 测试与派单矛盾：改为 SKIP，未删除核验项。
- 受限沙箱无法访问 Docker 导致两条额外降级 warn：获准后使用本机 Docker 完成全套验收，未调整 warn 基线。
