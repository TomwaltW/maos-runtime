# MAOS 最小闭环骨架（第一步）

零依赖，纯标准库。目的**不是**做出能用的产品，而是用一条真实链路验证事件契约和状态机
本身成立——契约错了，两条轨道分头写完再发现，返工成本是现在的十倍。

```bash
python3 main.py                  # 四个场景端到端
python3 tests/test_contracts.py  # 九条契约边界
```

## 已验证的四条链路

| 场景 | 验证什么 |
|---|---|
| 正常闭环 | `PENDING → DISPATCHED → RUNNING → AWAITING_REVIEW → DONE` |
| 返工闭环 | Gate 判 rework，结构化 findings 喂回 Agent，第二轮修好 |
| 人工审批 | `effect_risk=H` 的任务 Gate 过了也停在 BLOCKED，等人放行 |
| 幂等 | 同一 `idempotency_key` 重投两次，零额外状态迁移 |

## 冻结契约（改动需双方确认）

```
contracts/events.py   四类事件 + Envelope + validate()
contracts/states.py   任务/计划状态迁移表 + 双风险定义
core/store.py         表结构（对齐附录 B 的 PolarDB DDL）
```

跑挂 `tests/test_contracts.py` 任何一条 = 动到了共享契约，先停下同步。

## 三条铁律（实现时不要为了方便破例）

1. **只有 Control Plane 写状态。** Agent、Worker、Gate 都不碰 task/plan 表。
2. **所有迁移过 `assert_transition()`。** 非法迁移抛异常，不静默改写。
3. **所有外部事件先过幂等闸门。** 这是换 RocketMQ 的前提——MQ 只保证至少一次投递。

## 两个风险字段不能合并

| 字段 | 含义 | 谁检查 |
|---|---|---|
| `risk_level` | Agent **执行**任务的风险 | Agent Identity 的 `max_risk` |
| `effect_risk` | 产物**落地**的风险（合主干、改生产） | Reviewer Gate → 人工审批 |

合成一个字段的后果：一个高风险变更任务，Coding Agent 因 `max_risk=M` 拒绝执行，
重试耗尽直接 FAILED，人工审批环节永远走不到。这个缺陷是跑场景 3 时暴露的。

## 分轨接手点

**Track A（状态与控制平面）**
- `core/eventbus.py` → 实现 `RocketMQEventBus`，`EventBus` 接口不动
- `core/store.py` → 实现 `PolarStore`，`Store` 接口不动
- `core/control_plane.py` → 补 claim 超时重投、DAG 多任务依赖、Plan 级重规划
- `runtime/gate.py` → 四道闸接真实测试报告与静态扫描
- 删掉 `main.py` 的 `run_until_settled`（换成常驻消费者后不需要）

**Track B（执行与网关）**
- `agents/` → 按 Identity 契约补 requirement / architecture / testing / reviewer
  （继承 `BaseAgent` + `@register`，不用改任何其他文件）
- `model/client.py` → 实现 `HigressModelClient`，tier 作为路由 header
- `runtime/worker.py` → `_invoke` 换成 AutoGen 的 agent.run，上下游契约不变
- 沙箱执行器：目前 Coding Agent 只做路径白名单校验，需接真实隔离环境

**后续叠加（不阻塞主线）**：`_transit()` 里挂 OpenTelemetry span；`event_log` 表直接喂
AgentScope Studio；前端 UI。

## 目前的刻意简化

- `ScriptedModelClient` 是假模型——第一步验证的是契约不是模型输出质量
- EventBus 单线程串行 drain——要的是可复现的执行顺序，不被并发掩盖问题
- Gate 是纯规则的——判定必须可复现可审计；语义审查交给 Reviewer Agent
- 无沙箱隔离、无 Skill 注册表、无知识沉淀
