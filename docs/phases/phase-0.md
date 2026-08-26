# Phase 0（D1 · 8.26）仓库重组 + HiClaw 摸底

> **状态：已于 2026-08-26 执行。** 本文档为存档式记录；守卫脚本的授权环节由人类在自己终端完成，实现细节不入库。

## 目标

python/ 升级为正式包 maos/，TS 封存，目录骨架就位，HiClaw 代码级摸底出 A/B 裁决依据。三条铁律固化为三重机器守卫（deny + hook + 指纹测试），拆分后的 Phase 文档就位。

## 已执行的步骤

1. 新建分支 goai-restructure，后续所有 Phase 都在此分支。
2. 仓库根创建 CLAUDE.md：全局铁律七条 + 测试命令 + 目录结构说明。
3. 目录迁移：
   - python/ → maos/（正式包，包式 import：`from maos.agents.base import ...`）；
   - python/main.py → maos/main.py，根目录加 run.py 薄入口（`python run.py` = 跑四场景）；
   - python/tests/ → maos/tests/，改造为 pytest 可发现形式（原 9 条契约断言逐条保留，未删）；
   - src/ tests/ package.json tsconfig.json pnpm-*.yaml → legacy-ts/，其 README 注明契约权威在 maos/contracts/。
4. 新建骨架：maos/skills/ maos/tools/ maos/obs/ hiclaw/ scenarios/ evidence/ deploy/ scripts/ docs/phases/。
5. pyproject.toml：包名 maos-runtime，requires-python >=3.10，核心零依赖；optional-dependencies 预留 hiclaw = ["matrix-nio"]、obs = ["opentelemetry-sdk"]、dev = ["pytest"]。
6. 契约冻结三重守卫（在迁移完成后执行，锁的是新路径）：
   - scripts/relock_contracts.py：重锁契约指纹，带授权闸（授权方式不入库，由人类在自己终端执行）；
   - maos/tests/test_contracts_frozen.py：指纹校验测试两条（冻结文件 sha256 + 既有表 DDL），断言信息不带补救指引；
   - scripts/guard_bash.py：PreToolUse 守卫，封 Bash/Read/Grep/Glob 侧路；
   - .claude/settings.json（deny 规则 + hook 挂载）由人类人肉贴入，不由 Claude Code 写；
   - 生效顺序：Claude Code 建三个文件 → 人类授权 relock 生成 .contracts.lock（提交进仓库，不进 .gitignore）→ 人类贴入 settings.json 并重启会话 → 人类做守卫自检（篡改 states.py 应红、还原应绿、未授权 relock 应拒绝）。
7. 创建 docs/BACKLOG.md 与 docs/DECISIONS.md（DECISIONS 预填三行裁决：AutoGen 出局 / 场景 5 强制 Scripted / 补偿用 git apply -R）。
8. 手册拆分入库：docs/phases/common.md + phase-0..7.md；完整手册与其授权章节不进仓库。总体方案待人类提供后存为 docs/PLAN.md。
9. HiClaw 摸底由独立并行会话执行（clone github.com/alibaba/hiclaw 到仓库外 ~/probe/hiclaw），产出 docs/hiclaw-probe.md，必须回答：Worker 注册/接入方式与扩展点文件、Matrix 房间消息格式约定、Manager 派发消息样例、homeserver 与 token 来源、A 档（Worker 原生接入）预估工时——**若 >1.5 天，裁决锁 B 档**。晚间由人类合并并做裁决。
10. 人类手动：跑 HiClaw 官方安装脚本，确认能登录 127.0.0.1:18088，记下管理员密码。起不来也不阻塞——B 档只需要一个 Matrix 房间（见 Phase 3 的 C 档保底）。

## 验收

```bash
python -m pytest maos/tests -q        # 原 9 条契约测试 + 新增 2 条冻结守卫全绿
python run.py                          # 四场景端到端，输出与迁移前一致
ls docs/hiclaw-probe.md CLAUDE.md pyproject.toml
ls .contracts.lock scripts/guard_bash.py              # 守卫文件与指纹基准就位（settings 由人类贴入）
ls docs/phases/common.md docs/phases/phase-0.md       # 拆分文档就位
ls docs/BACKLOG.md docs/DECISIONS.md
```

另有一条"授权变量名不得出现在 docs/ 与 CLAUDE.md"的泄漏检查，由人类按本地完整手册执行。
守卫自检由人类人肉执行（守卫会拦 Claude Code 自己碰契约文件，这正是设计意图）。

## 提交

`feat(p0): restructure python/ -> maos package, archive TS, add scaffolding, triple contract guards, split phase docs & hiclaw probe`
