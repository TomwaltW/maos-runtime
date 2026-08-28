# BACKLOG —— 发现但不当场处理的问题（铁律 4）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-26 | P0 | docs/PLAN.md 暂缺：本地未找到《MAOS-GOAI-复赛总体方案.md》源文件 | 各 Phase 文档引用的总体方案 §2/§3/§5.1/§9/§11/§12 暂无仓库内对应 | 人类提供文件后随下一个 commit 入库，最迟 Phase 6 前 |
| 2026-08-26 | P0 | 根 README.md 仍描述旧目录/双轨结构 | 新克隆者可能按旧说明操作 | Phase 6 第 6 步整体重写（手册已排期，不提前动） |
| 2026-08-26 | P0 | ~~.claude/settings.json 仍缺 deny 规则与 PreToolUse hook 挂载~~ | ~~铁律 1 宣称的三重机制当前实际只有指纹测试一重生效~~ | **resolved 2026-08-27**：deny 三条 + PreToolUse hook 均已挂载并实测生效（本会话 Read/Bash 一触碰 settings.json 即被拦）。顺带复核两条旧判断：`Edit(/maos/contracts/**)` 单斜杠是作用域根相对，规则一直生效（原判"匹配不上"有误）；allow 已含 `Bash(python3 -m:*)`，与本机无 `python` 不冲突 |
| 2026-08-27 | P0 | 项目级 hook 仅在从仓库根启动时加载 | 从 `~` 或其它目录启动的会话守卫完全不生效（8-27 实测：家目录会话可自由改写 `guard_bash.py` 本身） | 候选解：`~/.claude/settings.json` 用户级 hook + 绝对路径；代价为全局每次工具调用多一次进程启动。复赛后评估，赛前不动 |
| 2026-08-27 | P0 | MAOS_RELOCK 授权只有"整晚敞着"一种用法 | hook 是独立进程，内联 `MAOS_RELOCK=1 cmd` 和 Bash 内 export 都传不到；唯一生效方式是启动 claude 前 export，此后整个会话对**全部**受保护文件放行 | 改为从命令文本识别前缀做单条授权；风险高于常规改动，复赛后单独处理 |
| 2026-08-27 | P0 | 守卫按命令文本匹配路径，只读与 git 操作一律拦 | ①无法 Read/grep `.claude/settings.json` 核对 deny 与 hook（复核旧判断时即被拦，只能靠 hook 报错反推它在生效）；②`git add .contracts.lock` 同样被拦，而 phase-5.md:23 要求该文件**必须提交** —— Claude 侧无法完成入库，只能由人类在自己终端做 | 与上面两条一并在复赛后收敛：读与 git 索引操作放行、写照拦。赛前不动，`.contracts.lock` 由人类手工提交 |
| 2026-08-28 | P1 | 多轨判断记录分叉：track-a 新建 `docs/decisions/task-a.md`，主干是单文件 `docs/DECISIONS.md` | 分文件是 track-a 为避开多轨同改冲突的**有意选择**（文件抬头写明理由），本身合理；缺的是回收规则 —— 六轨各写一份，谁在何时折回 `DECISIONS.md`、评审时以哪份为准，都没定。另：该目录当前 `??` 未跟踪，若 track-a 只暂存白名单内的代码文件，这份判断记录会随 worktree 一起丢 | 合并 track-a 前定回收规则（谁折、何时折）；`docs/decisions/` 是否入库当场决定。**不是大小写撞名** —— `DECISIONS.md` 与 `decisions` 去掉大小写仍不同名，本机 `ls docs/` 两者已并存，验证通过 |
| 2026-08-28 | P1 | 守卫逐行分词，跨行或落单的 ASCII 引号必被拦 | `check_bash` 把命令 `.split("\n")` 后**逐行** `shlex` 分词，任一行内 `'` 或 `"` 不成对即 `No closing quotation` → fail-closed 整条拦掉。**与中文、heredoc、反斜杠续行都无关** —— 续行已被上一句 `raw.replace` 提前折平，heredoc 正文引号成对即放行（8-28 用 10 个用例直跑守卫本体实测）。实际会咬到的只有两种：①单个 `-m` 的引号内含真换行；②heredoc 正文某行有孤立撇号（`it's`）。②对 `cat > msg.txt <<EOF` 同样成立 —— **改成写文件并不能绕开** | 免疫通道只有一条：message 用 **Write 工具**落临时文件（守卫对 Edit/Write 只查路径，`content 一律不看`）再 `git commit -F <file>`。已写进 common.md 铁律 5 附注。守卫侧修法（按 shell 语法而非换行切分、识别 heredoc 边界）与上面四条一并复赛后收敛，赛前不动 |

## fix-1

改受保护路径判定时发现、按铁律 4 不当场处理的三条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | fix-1 | `docs/phases/phase-2.md:28` 写「沿用 PROTECTED_PATHS」，该常量本轮已改名 `PROTECTED_SEGMENTS`，且语义由「路径前缀」变为「目录名分段」 | Phase 2 落 `sandbox.git_apply` 的路径校验时，照抄旧名会 ImportError（当场可见，无害）；照抄旧语义才危险 —— 往新清单里塞 `"tests/"` 这种带斜杠的条目，分段相等下永远匹配不上，不报错只放行，正是本轮修掉的那个失效形态 | Phase 2 开工时按新名与新语义接。手册那一行归主线改，本轨白名单外不动 |
| 2026-08-28 | fix-1 | `conftest.py` 绕过口仍开着 | `tests` 段只挡 `tests/` 目录**下**的文件；仓库根或任意非 tests 目录下的 `conftest.py` 一律放行，而它在 pytest collection 阶段先于一切用例执行，是绕过「tests/ 禁改」的标准路径（`phase-2.md:28` 已点名）。当前仓库 `find` 不到任何 conftest.py，故是纯潜在口子、非现存漏洞 | 已排期 Phase 2（手册明写「conftest.py（任意层级）显式列入禁改」）。本轮不提前动：派单范围只到分段匹配，且加文件名级条目要先定「按段名还是按 basename」的第二套口径 |
| 2026-08-28 | fix-1 | 本 skill 不做仓库内含性校验，路径逃逸只要不撞受保护目录名就放行 | `/etc/passwd`、`../../../.ssh/id_rsa` 规范化后分段是 `etc/passwd`、`.ssh/id_rsa`，不在 `PROTECTED_SEGMENTS` 里 → 放行。当前无实害：skill 契约明写「自身不落盘、不执行补丁」，真正写盘要等 Phase 2 的 `sandbox.git_apply` | Phase 2 落沙箱时补「补丁路径必须落在 workdir 内」的内含性校验 —— 那一层才有 workdir 可比对，放在 skill 里没有基准路径可判 |
## fix-2

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | fix-2 | `maos/artifacts.py::validate_artifact` 在生产路径上零调用方 | 该函数的文件抬头自称「跨轨共用的唯一一份口径」，`_check_patch_set` 里明写 `self_check` 必须是 dict、取值必须是 pass / fail。但全仓 grep `validate_artifact`，除自身定义外只有 `maos/tests/test_registry_autodiscovery.py:21/261/294/296` 引用；artifact 真正入库的地方 `maos/core/control_plane.py:150-156` 是 `insert_artifact(... art.get("content", {}))`，**不校验任何形状**。所以「畸形 `self_check` 到得了 Gate」不是纸面推演，本轮 fix-2 的 P0 成立；反过来，这份「唯一口径」当前只是测试断言用的工具函数，没有任何东西保证它与实际入库的数据一致，两者迟早分叉 | 接线归属不在 fix-2 范围（本轮只准改 `maos/runtime/gate.py` 与新建 `maos/tests/test_gate.py`），故不当场修。建议随 fix-wiring 或合并闸一并定死二选一：要么在 `on_task_result` 入库前调 `validate_artifact`、把返回的错误列表转成 findings（它的返回值本就设计成「可以直接写进 findings 的东西」），要么明确宣布它只作测试断言用并改掉「唯一一份口径」的抬头表述。**注意即使接线也不能取消 Gate 侧的 `isinstance` 兜底** —— Gate 是独立判定面，别的产出路径同样会喂进来 |
## fix-4

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P1 | 守卫对「`cd <仓库外目录> && cat > <相对路径> <<EOF … EOF`」误报。原命令是往 `~/.claude/jobs/<id>/tmp/` 写一个一次性脚本再 `python3` 跑它，**全程不碰仓库任何文件**，却被拦下并报 `blocked: 该操作触碰受保护面 maos/contracts/events.py（写入/执行位置）` —— 而该路径在命令文本里根本没出现过 | ①合法的仓库外临时脚本被 fail-closed 拦掉；②更麻烦的是**报错路径具有误导性** —— 它指名一个命令压根没提到的文件，照着这条报错查会直接查错方向（本轨即先怀疑是自己命令有问题，才转去做隔离实验）。与上一条「逐行分词」不是同一个成因：那条报的是 `No closing quotation`，这条报的是受保护面命中 | **成因未隔离，勿照抄推测**。已排除的：单纯 heredoc（放行）、heredoc 正文含 `Documents-MAOS` 字样（放行）、heredoc + `python3` 执行且全用绝对路径（放行）。未能复现的那一档是带 `cd` 前缀 + 相对写入路径的组合 —— 本轨进 worktree 后该形式被另一重「worktree 隔离」守卫先行拦下，无法继续二分。留给 fix-1（`fix/protected-paths`）从这三条已排除项接着做，与守卫其它五条一并复赛后收敛，赛前不动 |
## fix-5

改 `maos/model/client.py` 时看到、按铁律 4 与派单边界不当场动的两条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P1 | 同 origin 的 301/302/303 会被 urllib 默认 handler 把 POST 静默改写成 GET | `HTTPRedirectHandler.redirect_request` 对 301/302/303 + POST 的处理是造一个**不带 body 的 GET**（只剥 Content-* 头）。fix-5 放行同 origin 跳转后这条路径仍在：网关一个补斜杠的 302 就会让 `messages` 整个丢掉，然后我们拿那个 GET 的响应当 completion 解析。不是密钥问题（没换主机），是「请求内容静默变了而调用方无感」 | 候选修法：同 origin 也只放行 307/308（这两个规范要求保持方法与 body），301/302/303 一律拒。改动仍在 `_SameOriginRedirectHandler` 一处，但会缩小兼容面，需要拿真 Higress 的行为定，故不在本轨拍板。接真网关时（Track B）一并定 |
| 2026-08-28 | P1 | `HigressModelClient` 占位类把 key 放在**公开**属性 `self.api_key`，且没有 `__repr__` 兜底 | 与同文件 `GatewayModelClient` 的 `_api_key` + 不含 key 的 `__repr__` 两道防线不一致。当前 `complete()` 一进来就 `raise NotImplementedError`，不出网，所以只是「key 会进 repr / pytest 对象打印 / traceback」的隐患，不是现行泄漏 | Track B 真正实现这个类时按 `GatewayModelClient` 的写法对齐（私有属性 + `__repr__`）。本轨派单只准改重定向/timeout/usage 三处，且它属另一个类，不顺手动 |

## orchestration-p3

编排侧在 v4 手册入库（`docs/EXECUTION.md`）时对账发现、不阻塞退款域本轮的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P1 | **v4 手册 P1 第 7 步的 StorePort 抽象从未落地** —— `maos/store/` 目录不存在，`port.py` / `sqlite_store.py` / `pg_store.py` 三个文件一个都没有。手册原文称其为「整个 v4 的地基，做不好后面全塌」 | **本轮（退款域 R 轮）不受影响**：R-1 的 `schema.sql` 走 `objects.py::ensure_schema(store)` 直接读文件建表，用的是现有 `maos/core/store.py`，不经 StorePort。**受影响的是 P5**：手册 P5 第 2 步要求检索器的全文通道走「SQLite FTS5 / PG tsvector」、向量通道走「纯 Python 余弦 / pgvector」，第 3 步要求 `pg_store.py` 填实 + `MAOS_STORE_BACKEND` / `MAOS_PG_DSN` 切换 —— 这些全部挂在 StorePort 的 `fts_search` / `vector_search` / `dialect` 三个方法上。地基不在，P5 的「后端可插拔」无处落脚，验收命令 `MAOS_STORE_BACKEND=postgres ... python3 run.py` 直接无从谈起 | **P5 开工前补**，不要塞进本轮。补的时候按手册 P1 第 7 步原样做：`sqlite_store.py` 是**适配器不是重写**（禁改现有 `store.py` 任何方法签名），`pg_store.py` 本可先留 `NotImplementedError` 空壳。另需注意：P1 手册还要求补 `maos/tests/test_store_port.py`（≥3 条），一并欠着 |
| 2026-08-28 | P3 | 手册 P5 的场景 R5（RAG 有无对照）、R3a/R3b（租户对照）、R4a/R4b（渠道对照）、R6（政策版本对照）**整数编号未裁决** | D-05 只裁决了 R1→6、R2→7，`ALL_SCENARIOS` 扩到 `(1..7)` 就到顶了。P5 要新增对照场景时，`--scenario` 的 choices 还得再扩一次 —— 而 `main.py` 是冻结面，D-05 明写「这是 main.py 冻结后唯一一次修改」。届时要么再破一次冻结，要么给对照实验换个不占 `--scenario` 的入口（如 `MAOS_KB_ENABLED` 那样走环境变量 + 复用场景 6） | P5 开工前一并裁决。倾向后者：R5 本就是「同一个 case 跑两次」，用环境变量开关比占两个场景号更贴合手册语义，也不用再动冻结的 `main.py` |
## task-B

落容器沙箱时看到、按铁律 5 与派单边界不当场处理的四条（分支 `task/b-sandbox`，基线 `59196ba`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P2 | `PROTECTED_SEGMENTS` / `_path_segments` 住在 skills 层，而 tools 层要用它，模块级 import 会成环 | 实测环路：`maos.tools.sandbox` → `skills.builtin.code_repo_patch` → 触发 `builtin/__init__` 的 `discover()` → import `test_verify` → 回到还没定义完的 `maos.tools.sandbox`，在 `PYTEST_RUN_PORT` 上抛 `ImportError: cannot import name ... from partially initialized module`。这不是纸面推演，是本轨第一次冒烟当场炸的。现已改成在 `_check_path` 里延迟 import 绕过 —— 能用，但**依赖方向是反的**：tools 在 skills 下面，不该 import 上层。fix-1 那条「若认为该下沉到 tools 层，记 BACKLOG」现在有了具体证据 | 派单明写「只许 import 复用，不许改那个文件」，故不当场搬。建议合并期或 Phase 3 把两者下沉到 `maos/tools/paths.py`，skills 与 tools 都从那里取 —— 判定仍只留一处，环也就没了。搬的时候连 `code_repo_patch.py` 的 import 一起改，别留两个入口 |
| 2026-08-28 | P2 | `python:3.11-slim` 基础镜像自带 `GPG_KEY` 环境变量 | 隔离探针 `test_no_host_secrets` 起初写了「扫一遍名字里含 KEY / TOKEN 的变量」，在容器里被 `GPG_KEY` 打红（它是镜像用来校验 Python 源码包签名的，不是宿主漏下来的）。已改成按 `MAOS_` / `MATRIX_` 前缀扫。记这条是为了防**将来有人觉得前缀扫不够严又把泛化词扫加回去** —— 加回去的当天容器路径就恒红，而症状看起来像「隔离失效」，会把人引向完全错误的方向 | 不需要处理，属口径备查。若日后换基础镜像，先跑一次 `docker run --rm maos-sandbox env` 看有没有新的同类变量 |
| 2026-08-28 | P2 | 容器路径把 junit 报告写进 bind mount 的 workdir，写入方是容器内的 uid 1000 | macOS 的 Docker Desktop 会做属主重映射，本机实测正常。但在 Linux 上宿主 uid 通常不是 1000，`--user 1000:1000` 对挂进去的目录没有写权限，`--junitxml` 写不出来 → 走 `tool_error`「没产出 junit 报告」。症状是「本机好好的，CI 上沙箱全报工具失败」 | 本轨只在 macOS 上验过，不替 Linux 拍板。上 Linux CI 时二选一：`--user` 改成跟宿主 uid 走（`os.getuid()`），或让报告落 `--tmpfs /tmp` 再 `docker cp` 出来。Ω 轨接 CI 时一并定 |
| 2026-08-28 | P2 | `sandbox_pytest_run` 跑的是 workdir 里的**全部**用例，靶场的三条隔离探针也在其中 | 场景 1/2 的 Gate 会看到 5 条 case，其中 3 条是探针而不是业务用例。探针挂了当然该拦（隔离失效比用例挂严重得多），但「passed=4」这个数字对 Coding 返工没有指导意义，findings 里混进探针也会让模型去改它读不懂的东西 | 探针要不要计入验收判据、要不要在转 findings 时按 `id` 前缀过滤掉，是 Gate 侧的判据问题，归 Task-C 第 7 步。本轨只保证报告里 `id` 带得全（`tests.test_isolation_probe::` 前缀可直接用来过滤），不替 C 决定 |
## task-C

补四个 Agent 与改 Gate 判据时看到、按铁律 4 与派单边界不当场动的五条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P2 | 场景 1/2/3 的测试报告是**场景预置的脚手架**（`agents/testing.py::seed_scripted_report` + 三处调用点），不是跑出来的 | 演示看起来是「真实测试报告驱动返工」，实际报告由场景写死。这不是掩盖 —— Gate 的判据、findings 的形状、返工链路都是真的，假的只有报告的**来源**；但只要 `test.verify` 一天没落地，场景 1/2 就一天证明不了「测试真的跑了」 | Task-B 沙箱合并当天：`test.verify` 注册后 Testing Agent 走真跑分支，报告经 `target_task_id` / `target_attempt` 被 Gate 认领到 coding 任务（第二条解析路径已就位，Gate 不用改）。届时删除 `seed_scripted_report`、三处调用点、以及各场景的 PASS_REPORT / FAIL_REPORT 常量。哨兵已埋：`test_agents_gate.py::test_test_verify_is_still_unregistered_in_parallel_phase` 会在那天变红 |
| 2026-08-28 | P2 | 「`effect_risk=H` 但本轮压根没有 compensation 产物」当前不判 | 补偿干跑闸只在存在补偿产物时才跑（否则场景 3 会撞上 Task-B 的 `NotImplementedError` 桩）。于是一个高风险任务只要**不产出补偿方案**，就能完全绕开这道闸 —— 症状是闸看起来在、实则空转，与 C-5 反例「补偿静默不执行、日志一片正常」是同一类失效 | 补偿产出归 Task-D，判据要两轨一起定：是「H 风险必须带补偿产物，否则 blocker」，还是「由 Task-D 在产出侧保证」。D 轨接线当天定死，不要各判各的 |
| 2026-08-28 | P2 | `maos/artifacts.py` 没有 `requirement` 这个 kind，也没有它的 checker | 本轨的 Requirement Agent 产出 `kind="requirement"` 的 artifact，走 `validate_artifact` 会得到「未知 artifact kind」。当前无实害（该函数在生产入库路径上零调用方，见 fix-2 那条），但「跨轨共用的唯一一份口径」里缺了一个真实在用的 kind，两者已经分叉 | 与 fix-2 记的那条一并处理：`artifacts.py` 是冻结面，加 kind 属跨轨决策，合并期统一定 —— 要么补 `KIND_REQUIREMENT` + checker，要么明确宣布 requirement 产物不进形状校验 |
| 2026-08-28 | P2 | `maos/tests/test_registry_autodiscovery.py:169` 的函数名 `test_agent_pool_is_exactly_coding` 与它现在断言的五角色口径已不符 | 纯可读性：名字说「恰好只有 coding」，断言查的是五个 role。照名字找测试的人会以为它没被更新 | 派单限定本轨只许改 `:170` 那一条断言、其余一行不动（`:257` 归 Task-E），故不顺手改名。合并期由持有该文件的人改成 `test_agent_pool_is_exactly_five_roles` 之类 |
| 2026-08-28 | P2 | `review_after_gate()` 直接调 `store.insert_artifact` 落 review_note，绕过 `control_plane.on_task_result` 那条入库路径 | Reviewer 不经 worker 队列（它的位置由流程决定），所以没有 TaskResult 可走。代价是这条产出既不过幂等闸门、也不写 StateTransition，审计上看不到它是谁在哪一步产的 —— 与 fix-2 记的「artifact 入库不校验任何形状」是同一处地基问题的两个侧面 | 与 fix-2 那条一并定：要么给 Control Plane 开一个「非任务产物入库」的正式入口（带审计行），要么承认 review_note 是流程附属物、不进审计链。Phase 5 做可观测时必须有结论，否则 Trace 里会凭空多出一份没有来源的产物 |

## task-R1

做退款域地基时看到、按铁律 4 与派单边界不当场处理的六条
（分支 `task/r1-refund-core`，基线 `b2319df`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | **基线 `b2319df` 本身是红的**：`python3 -m pytest maos/tests -q` → 2 failed / 179 passed，两条都在 `maos/tests/test_agents_gate.py`（`test_test_verify_is_still_unregistered_in_parallel_phase`、`test_testing_agent_soft_falls_back_without_raising`）；`python3 run.py` → 退出码 1，场景 1 挂在 `flows/scenario_1.py:113` 的 `PlanState.DONE` 断言 | 根因是 B/C 合并没收口：Task-B 注册了 `test.verify`，Task-C 那条哨兵测试的 docstring 原文就写着「Task-B 合并当天这条会红，提醒下面两个软兜底断言换成真调用」——**按设计响了，但没人接**。连带效应是场景 1 的 `s1-test` 真去跑 `test.verify`，`workdir=/tmp/x` 不存在 → Gate 连判两次 rework 后 `gate_reject_final`，plan 到不了 DONE。**R-1 的两条验收命令（全量测试全绿、`run.py` 退出 0）在这个基线上不可能满足**，与 R-1 的改动无关 | **R 轮合并闸之前**，由 B/C 合并的收口方处理，不要塞给退款域任何一轨。注意 `task/d-governance` 与 `task/e-matrix` 都会重写 `test_agents_gate.py`（各 302 行删改），收口动作应排在 D/E 合并**之后**，否则要做两遍 |
| 2026-08-28 | P3 | **D/E 两轨未并入 `goai-restructure`**：`git branch --no-merged goai-restructure` 列出 `task/d-governance`、`task/e-matrix` | 派单写的基线是「B/C/E/D 四轨全部 MERGED 后的收口提交」，该提交至今不存在。R-1 实际开在 `b2319df`（只并了 B/C）。已实测两轨与 R-1 独占文件零交集（都不含 `maos/main.py`、`maos/domain/**`），合并冲突面为空，但 R-3/R-2/Ω 的基线口径需要一并澄清 | 合并 D/E 时一并处理；R-3 开工前把实际基线 sha 写进它的派单，不要再照抄「四轨 MERGED 后」这句 |
| 2026-08-28 | P3 | `maos/domain/refund/objects.py::_conn()` 直接取 `SqliteStore` 的**私有**属性 `_conn`，`lock_of()` 同理取 `_lock` | 依赖方向没错（domain 在 store 之上），但依赖的是私有面。换后端时这两个函数要各加一条分支，漏了就是运行时 `TypeError`。当前只有 `SqliteStore` 一个实现，故是隐患不是现存故障 | **StorePort 落地时一并改**（`## orchestration-p3` 已记 StorePort 从未落地、P5 之前必须补）。届时 `_conn` / `lock_of` 换成走 StorePort 的 `execute` / `transaction`，`objects.py` 对外的 `execute` / `query` 签名不变，退款域其余代码零改动 |
| 2026-08-28 | P3 | `payment_observation` 的主键含 `observed_at`（`PK(tenant_id, case_id, request_id, observed_at)`） | 同一笔请求在**同一时间戳**上的两次观察会撞主键。ISO8601 带微秒，正常轮询撞不上；但网关重试风暴或造数据时会撞。本轨 `test_settled_rolls_back_when_receipt_insert_fails` 反过来利用了这一点做同事务反证 —— 改主键时那条测试要同步换构造方式 | **P4 接真轮询时评估**。加一列自增 `seq` 是最省事的解法，但那会让 `schema.sql` 的列清单偏离派单原文，需先确认 |
| 2026-08-28 | P3 | `schema.sql` 由 `ensure_schema()` 一次性 `executescript` 执行，全部是 `CREATE TABLE IF NOT EXISTS`，**没有迁移路径** | 加表可以（新表直接生效），但**改列不行**：往已建好的表加一列、改类型、改主键，现有机制一律静默无效 —— 表已存在，`IF NOT EXISTS` 直接跳过，跑起来一切正常，直到某条 INSERT 报 `no such column`。R-2/R-3 若要动这 14 张表的列，会踩到 | **R-2 开工前明确**：本轮内若需改列，直接改 `schema.sql` 并重建库（演示期都是 `:memory:`，无历史数据）；真要上持久库再谈迁移工具。别在没有迁移机制的前提下默认「改了就生效」 |
| 2026-08-28 | P3 | `maos/main.py` 的模块 docstring 仍只列「场景 1..5」，`ALL_SCENARIOS` 已扩到 `(1..7)` | 纯可读性：照 docstring 找场景的人看不到 6/7。判定逻辑不受影响（`argparse` 读的是 `ALL_SCENARIOS`） | **R-2 落地场景 6/7 时一并补**。本轨不动是因为派单对 `main.py` 写死「仅 D-05 那一处，其余一行不动」 |
