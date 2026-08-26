# MAOS 复赛仓库 —— 会话须知

本仓库正在执行复赛改造（8.26 → 9.2），复赛 2026-09-22/23 在杭州。每个 Phase 的具体步骤在 docs/phases/phase-\<N\>.md，
全局约定与检查单在 docs/phases/common.md。本文件是每个会话自动加载的最小铁律集。

## 全局铁律（七条，机器守卫强制，不是口头约定）

1. 冻结契约：maos/contracts/events.py 与 maos/contracts/states.py 禁止任何修改，除非手册某 Phase 明确列出的增量；maos/core/store.py 现有表结构禁改，只允许**新增**表。本条由三重机制强制：.claude/settings.json 的 deny 规则、PreToolUse 守卫 hook（scripts/guard_bash.py，封 Bash 侧路）、maos/tests/test_contracts_frozen.py 指纹校验。不是口头约定。
2. 每个 Phase 结束时，存量测试 + 该 Phase 新增测试必须全绿：`python -m pytest maos/tests -q`。
3. 证据必须真实：evidence/ 下所有文件必须来自真实命令输出，禁止手写或编造。每个 evidence 文件首行必须是 `# generated at <ISO8601> from <git sha>`，由生成脚本自动写入。
4. 不做手册范围外的"顺手优化"；发现问题记入 docs/BACKLOG.md，不当场改。
5. 提交规范：`feat(p<N>): <一句话>`，一个 Phase 至少一个 commit，验收全绿才许 commit。只许本地 commit，**禁止 push**，推送由人类手动做。
6. 任何需要真实密钥的配置只读环境变量，禁止把密钥写进任何文件。也禁止让密钥出现在 evidence/ 的任何输出里——凡是可能回显 env 或 URL 的命令，输出前必须过脱敏。
7. 凡是没有严格按手册执行、或手册没覆盖而自行做了判断的地方，必须在 docs/DECISIONS.md 追加一行：`<日期> | Phase N | 情境 | 选择 | 理由`。

## 常用命令

```bash
python3 -m pytest maos/tests -q    # 全量测试（在仓库根目录执行）
python3 run.py                     # 四场景端到端（Scripted 模式，无需任何 key）
```

## 目录结构

```
maos/            正式 Python 包
  contracts/       冻结契约：events.py / states.py（禁改）
  core/            store.py（表结构禁改，只许新增表）/ control_plane.py / eventbus.py
  agents/          base.py / manager.py / coding.py（Phase 2 补全其余角色）
  model/           client.py（Phase 1 加真模型客户端）
  runtime/         gate.py / worker.py
  skills/          Skill 层（Phase 1 起）
  tools/           ToolPort 与沙箱（Phase 2 起）
  obs/             可观测（Phase 5 起）
  tests/           pytest 测试（含契约冻结守卫）
hiclaw/          HiClaw(Matrix) 对接层（Phase 3 起）
scenarios/       演示靶场与多源输入信号
evidence/        验收证据（只放真实命令输出）
deploy/          沙箱 Dockerfile / docker-compose（Phase 2/5）
scripts/         守卫与生成脚本
docs/phases/     各 Phase 执行文档（common.md + phase-0..7.md）
legacy-ts/       早期 TS 契约参考实现（已封存，权威以 maos/contracts/ 为准）
run.py           薄入口
```
