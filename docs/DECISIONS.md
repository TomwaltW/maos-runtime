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
| 2026-08-27 | P0 | build() 缺省模型的构造路径 | 直构 ScriptedModelClient(script)，不经 select_model_client | C-3 明文规定「model=None 时按 script 构造 ScriptedModelClient(script)」，属冻结核心契约；A-12 只规定 select_model_client 自身语义，未要求 build() 调用它。Task-A 填完真模型分支后 build() 仍须保持确定性输出 |
| 2026-08-27 | P0 | registry 需要可注册的对象，但 A-1~A-3 只列了三个 dataclass | 在 maos/skills/contract.py 增补 Skill 抽象基类（contract 属性 + run(payload, ctx)） | @register_skill 必须按 cls.contract.name/version 入表，没有基类则每个 skill 各自定义形状；照 agents 的 BaseAgent + @register 同构，给 A/B/D 的六个 skill 一个统一继承点 |
| 2026-08-27 | P0 | agents 与 skills 互相引用（A-5 要复用 agents.base.PermissionDenied，A-9 要 base.py 挂 SkillInvoker） | 延迟 import 放在 skills 侧：invoker.invoke() 内部 import PermissionDenied，base.py 顶层正常 import SkillInvoker | 环必须在一处断开。断在 skills 侧才能让 maos.skills.* 独立 import——builtin 动态发现与其测试都依赖这一点；断在 agents 侧则 skills 反而拖上整个 agent 包 |
| 2026-08-27 | P0 | A-5 只说 preconditions「逐条检查」，未定义检查语义 | 每条 precondition 视为 payload 的必填键，缺失即返回 failed + precondition_failed:<键名> | 当前无任何 skill 声明 preconditions，取最小可判定语义先冻住行为；若后续轨需要表达式求值，属契约变更，走 BLOCKED 升级 |
| 2026-08-27 | P0 | compensation 的 mode 是否锁死取值域 | validate_artifact 只校验字段名、嵌套层级与类型，不限制 mode 的取值 | C-5 的反例针对的是「字段名与嵌套层级各凭记忆」，冻的是形状；把 mode 硬锁成单值 reverse，会在 Task-D 需要别的补偿模式时变成一条假红。**【superseded 2026-08-27 G2，见下方并稿后 C-5 那一行】** |
| 2026-08-27 | P0 | A-8 只点名 insert_knowledge/list_knowledge 两个方法，未说是否进 ABC | 两个方法同时加进 Store 抽象基类与 SqliteStore；tags 交集在 Python 侧过滤，排序按 created_at 升序 | store.py 的设计前提是「换 PolarDB 时 PolarStore 照着 ABC 实现同样方法」，不进 ABC 换库时必漏。SQLite 无数组类型可查，tags 存 JSON 只能解码后过滤；升序与 list_tasks/list_artifacts 保持一致 |
| 2026-08-27 | P0 | 手册未冻结场景模块内的函数名，但 main.py 此后冻结、scenario_3/4 无人再碰 | 每个 flows/scenario_N.py 统一暴露 run(*, matrix=False) -> int | 分发器只能靠固定签名调用；签名不先定死，后面谁改场景内函数名都会连累已冻结的 main.py |
| 2026-08-27 | P0 | **supersedes 上方「compensation 的 mode 是否锁死取值域」那一行**：并稿后的 C-5（`e08cd49`）新增字段级约束，明写「`content.mode` 恒为 `reverse`，本阶段不定义第二种 mode，出现别的值即非法」，与原决定相反；G2 自检发现实现放行了 `rollback`/`forward` | validate_artifact 改为锁死 `mode == "reverse"`，非 reverse 一律拒绝（`maos/artifacts.py::MODE_REVERSE` + `_check_compensation`），并补断言 `test_compensation_mode_is_locked_to_reverse` | 定稿契约给出的理由强于我原来的顾虑：这条是「零模型补偿」（phase-4.md:18）的落点——逆补丁不由模型生成，只做反向应用，因此本阶段根本不存在第二种 mode，原先担心的「假红」不成立。反过来，放行别的 mode 不会当场报错，而是让补偿走不到反向应用分支，症状是「补偿静默不执行、日志一片正常」，要到演示现场才发现文件没还原——正是 C-5 反例段点名的最坏形态 |
| 2026-08-27 | P0 | 手册要求按 effect_risk=H 触发补偿引用，但 TaskAssignment payload 无 effect_risk 字段、events.py 又已冻结，Agent 根本拿不到该字段（附录 A-13 备案，Task-0 仅录入，机制归 Task-D 落实） | 改由 Control Plane 在 on_task_result 收到 patch_set 时自动附着补偿引用 | 机制等价，且「按影响面决定要不要留补偿」本就属于控制面职责而非 Agent 职责；为此去给 events.py 加字段会直接破铁律 1 |
| 2026-08-27 | P0 | C-8 的验证命令带 -v，其期望值「无匹配输出且 exit=1」在 git 下不可达 | 按 C-8 约定原样追加两行，验收第 4 条如实报不符并出 BLOCKED；不改契约、不动 .env.* 规则 | git check-ignore -v 会把命中的否定规则一并打印并返 0，「无输出且 exit=1」只在完全无规则命中时成立，而 .env.* 必须保留（它是真 .env 的防线）。不带 -v 时实测无输出且 exit=1，C-8 的实质目标（两个模板文件不被忽略）已达成 |
| 2026-08-27 | P0 | 授权跑 relock 后发现 `.contracts.lock` 只锁 5 张表，Task-0 在 `e9b0e2f` 新增的 `knowledge` 表从未进过指纹 | 按原样跑 `relock_contracts.py`，接受它把 `knowledge` 一并锁进去（表数 5→6），不改 `FROZEN_FILES`、不做任何裁剪 | relock 的语义就是「按当前 store 的建表结果重新取指纹」，这正是人类授权 relock 要达到的效果。此前该表 DDL 改了不会被 `test_contracts_frozen` 发现，等于 store.py「表结构禁改」在 knowledge 上有个洞；锁上之后 Task-D 若要改它会当场变红并升级人类，符合铁律 1 |
| 2026-08-27 | P0 | 人类指令说「补记 14:37–17:00 的六个 commit」，实测该时间窗内只有 4 个（`e9b0e2f`/`e08cd49`/`9190fde`/`0d0ccfe`）；而看板 §3 记的 HEAD 停在 `76ea101`、§7 日志停在 14:08 | 按「未上板的 commit」补记六条，即 `2afa5ef`(09:14) 起到 `0d0ccfe`(17:00)，覆盖人类指定的四条并补齐前两条 | 指令意图是让看板与 `0d0ccfe` 对齐；只补 14:37 之后那 4 条会在 `76ea101`→`e9b0e2f` 之间留两条空档（其中 `3a36f37` 正是解掉 §3「settings.json untracked」⚠️ 的那一条），看板仍对不齐。时间窗口的出入已在回执中如实报出 |
| 2026-08-27 | P0 | 「CLAUDE.md 必须问的四类第 1 条扩成同样四条路径」有两种读法：READ_OK 四条，或原有两条 + 新增两条 | 取后者：`maos/contracts/**`、`.contracts.lock`、`docs/parallel/contracts.md`、`maos/artifacts.py` | 两种读法都是四条，但后者不丢掉原有的 `.contracts.lock`——它是 `PROT_PATHS` 里连读都不许的一条，从必须问清单里掉出去会开口子 |
