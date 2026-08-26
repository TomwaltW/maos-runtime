# DECISIONS —— 执行中的判断记录（铁律 7）

| 日期 | Phase | 情境 | 选择 | 理由 |
|---|---|---|---|---|
| 2026-08-26 | P0 | AutoGen 集成与否 | 复赛代码不集成，降级为方案可选内核 | 执行路径不依赖原生 function calling，删依赖降风险 |
| 2026-08-26 | P0 | 场景 5 模型选择 | 强制 Scripted，忽略 key | 治理路径验收不得依赖模型随机性 |
| 2026-08-26 | P0 | 补偿实现方式 | git apply -R 反向应用正补丁 | 数学精确逆，消除模型写错逆补丁的风险面 |
| 2026-08-26 | P0 | goai-restructure 分支基点 | 从 3f2d5d1 切出（main 与 feat/autogen-worker 当时同指该提交） | 无分叉，且含最新 python/ 骨架 |
| 2026-08-26 | P0 | 守卫脚本中 Store 引用 | 用 maos.core.store.SqliteStore 并显式调 init_schema() | 手册注明"类名按实际调整"；本仓库 store 在 core/ 子包，建表需显式 init_schema |
| 2026-08-26 | P0 | 包内子结构 | 保留 core/ 子包布局（python/ 原样迁入 maos/），不把 store.py 提到包根 | 手册未要求拉平；最小改动降低迁移风险，冻结指纹锁的是实际路径 |
| 2026-08-26 | P0 | phase-0.md 内容口径 | 只留步骤记录、文件清单与验收，不复述守卫脚本实现与授权细节 | 授权环节由人类在自己终端完成，细节不入库；phase-0 已执行完毕，文档作存档 |
| 2026-08-26 | P0 | docs/PLAN.md | 暂缺，待人类提供总体方案文件后入库 | 本地未找到源文件，不代写不编造 |
| 2026-08-26 | P0 | .gitignore 追加 .DS_Store | 忽略 macOS 元数据文件 | 未跟踪垃圾文件持续污染 git status，影响每日附 B 检查可读性 |
| 2026-08-26 | P0 | 本机无 python 命令 | 全部命令用 python3；hook 命令也必须写 python3，否则 hook 报 command not found 被当非阻塞错误放行，Bash 侧路守卫静默失效 | macOS 仅装 Python.framework 3.11，未建 python 别名；GNU 参数同步按 BSD 适配（ls -l -T） |
| 2026-08-26 | P0 | 人类下达 create-pr 指令 | 由 Claude 执行 commit + push + 开 PR；两条冻结守卫测试在 .contracts.lock 生成前保持红，未等全绿即 commit | 人类显式授权覆盖"禁止 push"与"全绿才 commit"的默认时序；授权 relock 环节仍留给人类，红灯状态在 PR 描述如实标注 |
