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
| 2026-08-26 | P0 | .contracts.lock 首次生成的执行人 | 由 Claude 在本会话跑 relock，未留给人类终端 | phase-0.md:21/24-25 与 DECISIONS 上一行均把授权 relock 划归人类；本次系人类在会话中显式指令"按项目文档命令重新生成"。执行前已验证 `git diff f104161 -- maos/contracts/ maos/core/store.py` 为空，锁的是原封基线而非掩盖改动；结果 2 files/5 tables，11 条测试转全绿 |
| 2026-08-26 | P0 | 提前创建 maos/tools/sandbox.py（仅签名桩，无实现） | 在 P0 就建桩并标注实现归 Phase 2 | 手册把 sandbox.py 排在 phase-2.md 第 3 步；但 Gate 干跑闸（phase-4.md:19）与补偿执行器都要 import 这两个函数，若不预先冻结签名，各并行任务只能各写各的桩，合并必互相覆盖。桩只有签名与 NotImplementedError，不含任何实现，不侵占 Phase 2 的工作 |
| 2026-08-26 | P0 | sandbox_git_apply 签名与手册的差异 | reverse 改为 keyword-only，并增加 check_only 参数 | phase-2.md:28 写作 `sandbox.git_apply(patch_set, workdir, reverse=False)`；改 keyword-only 更严格且兼容手册全部调用写法。check_only 对应 phase-4.md:19 明文要求的 `git apply -R --check` 干跑，非自创语义 |
| 2026-08-27 | P0 | BACKLOG 清账时人类所列 b/c 两条在文件中并不存在 | 不新增两行"已解决"，把复核结论并入 settings.json 那条的 resolved 备注；旧 MAOS_RELOCK 行（归因为"hook 读自身进程 env"）直接被新描述替换而非并列 | b 条（`Edit(/maos/contracts/**)` 匹配不上）仓库内无任何记录，c 条只见于 REVIEW.md:63 的 Low 表，两者均从未落进 BACKLOG；同一问题保留两行会让"清账"本身变成新的账 |
