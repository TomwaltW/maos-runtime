# Phase 3（D4 · 8.29）HiClaw 对接（B 档）

## 目标

全过程在 Matrix 房间可见，审批在 Element 聊天室完成。

## 步骤

1. hiclaw/matrix_bus.py：MatrixEventBus(inner_bus, config) 装饰器模式包住现有 EventBus：

   - publish() 先走 inner，再把消息**镜像**进 Matrix 房间：一行人话摘要（[task-xxx] RUNNING → AWAITING_REVIEW (submit_result)）+ 折叠的 Envelope JSON 代码块；
   - 状态迁移镜像：在 Control Plane 外挂一个 event_log 轮询器（或在 _transit 后回调），把每条 StateTransition 也发进房间——**不改 control_plane.py 的迁移逻辑本身**；
   - 监听房间消息：/approve <task_id> 与 /reject <task_id> [原因] → 调 HumanApprovalQueue.decide()；只接受配置里 MAOS_APPROVERS 名单内的 Matrix 用户，其余回一句"无审批权限"并记 event_log；
   - 依赖 matrix-nio（pip install -e .[hiclaw]）；**连接失败自动降级为 log-only 模式**，场景照跑，只是不进房间——测试和 CI 永远用降级模式。🆙 matrix-nio **不装 [e2e]**；遇到加密房间不尝试解密，直接降级 log-only 并在日志写明原因。

2. 配置走环境变量：MATRIX_HOMESERVER / MATRIX_USER / MATRIX_TOKEN / MATRIX_ROOM_ID / MAOS_APPROVERS，在 .env.example 留样例。🆕 确认 .env 已在 .gitignore 里。

3. run.py 加 --matrix 开关：开了就用 MatrixEventBus 包装。

4. 🆙 **建房时必须显式关闭端到端加密（三档通用）**：Element 新建私密房间默认勾选 E2EE；一旦建成无法回退，只能弃房重建。房间加密后 matrix-nio 需要 [e2e] extra + libolm + 设备验证一整套，这是 D4 最容易蒸发三小时的地方。房间来源按优先级三选一（人类手动完成其一，把参数填进 .env）：

   - **B 档标准**：HiClaw 起来了 → 用它的 Matrix homeserver，在其管理界面建房/拿 token；
   - **C 档保底 1**：HiClaw 不配合 → docker run 一个官方 Synapse，注册两个账号（maos-bot、人类），建房；
   - **C 档保底 2**：连本地 Synapse 都有问题 → matrix.org 公网账号 + 私密房间（演示够用）。
   - 三档对五项映射叙事零影响，docs/hiclaw-probe.md 里补一行记录最终选了哪档、为什么。

5. 新增测试：MatrixEventBus 降级模式下行为与 inner bus 完全一致；审批命令解析（合法/非法/越权）单测。

## 验收

```bash
python -m pytest maos/tests -q
MAOS_LLM_API_KEY=... python run.py --scenario 3 --matrix
# 手机/浏览器开 Element：能看到全过程消息流；发 /approve task-xxx → 任务走到 DONE
# 再跑一次发 /reject → 走到 FAILED；两次的房间截图存 evidence/scenario-3/
```

🆕 截图前确认 Element 界面里没有露出 homeserver token 或 API key（房间设置页、开发者工具都别开着截）。

## 提交

`feat(p3): MatrixEventBus mirror + in-room approval commands (B-tier hiclaw integration, E2EE off)`
