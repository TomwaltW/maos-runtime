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

## task-E

落 Matrix 镜像总线时发现、按铁律 4 与派单边界不当场处理的五条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P2 | `.env.example` 未投放 —— 权限层 deny 规则把它连同真 `.env` 一起拦了（Write 报 `File is covered by a Read deny rule`） | C-8 要求该文件入库，`.gitignore:18` 的 `!.env.example` 放行也已实测生效（`git check-ignore .env.example` 无输出、exit=1），唯独文件本体写不进去。演示前没人补上的话，新克隆者拿不到任何环境变量清单，`--matrix` 与真模型两条路都得靠翻源码才知道该配什么 | 人类执行一条 `cp` 即可，内容已备好（见本轨回执）。同时建议把 deny 规则从 `.env*` 收窄成「`.env` 与 `.env.*` 但排除 `.example` 结尾」—— 现在这个形态与 C-8 直接冲突，而冲突只在有人真去投放模板时才暴露 |
| 2026-08-28 | P2 | `_NioChannel` 这条真房间路径未经任何实测 | 本机 matrix-nio 未安装（`import nio` -> ModuleNotFoundError），该类构造即 ImportError，测试与 CI 恒走降级分支。三处只能照 matrix-nio 文档写、无法验证：①判加密房用的是「`room_get_state_event` 返回不是 `RoomGetStateEventError` 即已加密，`M_NOT_FOUND` 才是未加密」；②`sync_forever` 与私有事件循环的配合；③直接赋 `access_token` 是否足以鉴权 | Phase 4 接真房间时逐条实测。注意这三条错了的症状都是「降级」而不是「崩」，所以不会拖垮演示 —— 但也意味着**它们不会自己暴露**，必须主动去验，否则会一直以为「接上就能镜像」 |
| 2026-08-28 | P2 | 状态迁移（StateTransition）没有镜像进房间 | `phase-3.md:12` 要求「在 Control Plane 外挂一个 event_log 轮询器（或在 `_transit` 后回调），把每条 StateTransition 也发进房间」，而本轨派单第 2 步只覆盖了 EventBus 三方法的镜像，没有这一项。结果是房间里能看到事件流（TaskAssignment / TaskResult / ReviewVerdict / Rework），却看不到 `RUNNING → AWAITING_REVIEW` 这类迁移轨迹 —— 而 phase-3.md 举的那个摘要例子正是后者 | Phase 4 补。两种挂法都不用改 `control_plane.py` 的迁移逻辑本身：轮询 `list_event_log(plan_id)` 取增量，或给 ControlPlane 加一个可选回调。优先前者，它一行生产代码都不动 |
| 2026-08-28 | P2 | `maos/tests/test_registry_autodiscovery.py:256` 的分节注释 `# --- C-6 Task-0 期 matrix 恒回退 ---` 已过时 | 本轨落地后该函数验的是「降级模式下行为等价」，不再是「恒回退」。注释就在被改函数的正上方，读代码的人先看到它，会得到与断言相反的印象 | 派单写死「该文件其余一行不动」，故不当场改。合并期（Ω）改成 `# --- C-6 matrix 降级等价 ---` 即可，一行的事 |
| 2026-08-28 | P2 | 房间监听没有接进任何运行路径 | `run.py --matrix` 当前只装了镜像，没有起监听循环，所以「在 Element 里发 `/approve`」这条链路是不通的 —— `RoomApprovalBridge` 有完整单测但没有生产调用方，`_NioChannel.listen()` 同理。派单第 3 步只要求实现审批命令本身，接线（谁在什么时候起监听、场景 3 怎么等人类回话）没有归属 | Phase 4 与真房间联通一并做。注意它会逼出一个当前没定的东西：场景 3 现在是同步跑完就退出，接了房间审批就得阻塞等人 —— 是给 `--matrix` 加一个超时等待，还是把场景 3 拆成两段，得先定下来再动手 |

## task-D

落地聚合 / 知识 / 补偿 / Replan 时看到、按铁律 4 与派单边界不当场动的四条。前两条是**合并期核对项**，不是可选项。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P2 | **【合并期必查】** C 轨的第五道闸 `_gate_compensation` 必须在**全量** `store.list_artifacts(task_id)` 里按 `kind == "compensation"` 找补偿引用，**不能**在 `_review` 已按 `version == task["attempt"]` 过滤后的 `artifacts` 列表里找 | 本轨把 compensation 的 `version` 定为 **0**（理由见 DECISIONS `## task-D` 第 2 行：不这么做，四道产物闸会误伤 compensation，场景 3 当场从 pass 变 rework 直到 FAILED）。代价是：若 C 的第五道闸沿用 `_review` 里那个已过滤的 `artifacts` 局部变量，它**永远找不到** compensation → effect_risk=H 的任务恒判 blocker → 场景 3 挂。两轨各自都绿，只在合并后炸 | **D 合并当天第一件事**（合并顺序 B→C→E→**D**，C 已在库）。核对 `gate.py` 第五道闸的取数来源；若确为过滤后列表，二选一：闸改成从全量 artifacts 取（一行），或与 C 一起重定 compensation 的 version 口径。改哪边都要重跑 `python3 run.py` 场景 3 + `pytest -k governance` |
| 2026-08-28 | P2 | ReviewerGate 的四道闸把**所有** kind 的 artifact 都当 patch_set 判 | `_gate_acceptance` 对任何没有 `self_check` 的 artifact 判 2 条 major，`_gate_evidence` 对任何没有 `summary` 的判 1 条 minor。踩到的不只是 compensation：Task-B 的 `test_report`（C-7 schema，同样没有这两个字段）合并后会踩同一个坑，而且它的 version **就是** attempt，躲不过过滤 | 归 Task-C（`gate.py` 所有者）。建议四道产物闸统一加一句 `if a["kind"] != KIND_PATCH_SET: continue`。本轨不当场改：`gate.py` 在白名单外，且 compensation 这一侧已用 version=0 绕开，不构成现存故障 |
| 2026-08-28 | P2 | `scenarios/inputs/` 的多源信号未接线到场景 1 | `phase-4.md:12` 要求「`run.py --scenario 1` 的入口从手写 goal 改为先过 aggregate」，但 `flows/scenario_1.py` 归 Task-C（附录 D）。本轨改接在场景 5（已记 DECISIONS），所以场景 1 的 goal 仍是手写的 | 合并后由 C 或 Ω 决定要不要把场景 1 也改成聚合入口。接线代码现成：`scenario_5._intake_goal()` 可原样搬，`load_signal_findings()` 已按包位置定位、不依赖 cwd |
| 2026-08-28 | P2 | 补偿的沙箱工作目录口径待与 Task-B 对齐 | 本轨读 `MAOS_SANDBOX_WORKDIR`（缺省 `"."`）传给 `sandbox_git_apply`。B 的真实沙箱大概率有自己的 workdir 来源（容器内路径 / 每次 run 的临时目录），两边对不上时补偿会去错的目录打反向补丁 —— 而 `git apply -R` 在错目录下多半报「补丁不适用」，看起来像补丁坏了，不像路径错了 | D 合并当天连同上面第一条一起验：`MAOS_SANDBOX_WORKDIR` 指向 B 的沙箱工作目录，跑通 C-7 的合并期验收「reject → 文件真实还原」。在那之前 `_execute_compensation` 捕获 `NotImplementedError` 并如实记 `ok=False, stage="sandbox_unavailable"`，不伪装成成功 |

## merge-p2

B/C/E/D 四轨合并期发现、按铁律 4 不当场处理的账（目标分支 `goai-restructure`，合并前基线 `f83c374`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P2 | **演示链路仍未真连沙箱**。B 的靶场与两个 ToolPort 都在，但场景 1/2 的 `workdir` 是硬编码字符串 `/tmp/maos-sandbox`，**全仓没有任何一处准备它** —— `prepare_sandbox_workdir()` 的调用方只有 B 自己的 `test_sandbox_isolation.py`。且 `common.py::GOOD_PATCH` 是一条指向 `src/auth.py` 的假 diff，而靶场里的文件叫 `auth/session.py`，它打不上去也修不了那个时区 bug | C 的 DECISIONS 原计划「B 合并后删掉 `seed_scripted_report`，让真报告经 `target_task_id` 进闸，Gate 一行不改」**现在还做不到**：真造 workdir 后靶场本来就有一条计划内的挂（`test_expired_session`，B 埋的时区 bug），而修它的补丁不存在，于是 Gate 会从「tool_error 挡闸」变成「真挂一条挡闸」，一样到不了 DONE。实测：`prepare_sandbox_workdir` + `sandbox_pytest_run` 得 `passed=3 failed=1`，`tool_error=None` | 下一轮（R 轮）一并做，要三件同时到位才有意义：①场景按 run 现造 workdir（`prepare_sandbox_workdir()`，跑完清理）；②`GOOD_PATCH` 换成能打在 `auth/session.py` 上的**真 diff**（B 的 `test_sandbox_isolation.py` 已有「改好文件→`git diff`→还原」的现成造法，照抄即可）；③届时才删 `seed_scripted_report` 与各场景的 PASS_REPORT/FAIL_REPORT。三件缺一件场景就红，所以不许拆开做 |
| 2026-08-28 | P2 | 靶场的三条隔离探针会计入 `test.verify` 的报告（`test_no_network` 在降级路径下 skipped，另两条 passed） | B 的 BACKLOG 已记过「探针混进业务报告」这条，归 Gate 判据。合并后实测确认症状属实：真跑一次报告是 5 条 case，其中 3 条是探针。当前因演示链路还没真连沙箱而未暴露 | 与上一条同批做 —— 演示链路真连沙箱的那一刻，探针就会出现在 Gate 的 findings 里。按 B 的建议用 `id` 前缀 `tests.test_isolation_probe::` 过滤 |
| 2026-08-28 | P2 | `_execute_compensation` 的 workdir 缺省取 `"."`，即**仓库根**（`control_plane.py:515`） | Task-B 合并前无害（`sandbox_git_apply` 恒抛 `NotImplementedError`）；合并后它是真实现，任何没显式设 `MAOS_SANDBOX_WORKDIR` 的补偿调用都会拿补丁对本仓库工作区跑一次 `git apply -R`。当前全部用例都打不上而侥幸无害 —— 那是运气不是设计，一旦某份补丁的上下文恰好对得上，就会真改到仓库文件 | **已处理（2026-08-28，合并期，人类裁决）**：缺省改必填 —— 取不到 `MAOS_SANDBOX_WORKDIR` 即抛 ValueError，与 C-5「补偿必须硬失败」同口径。回归守卫 `test_missing_workdir_env_raises_instead_of_guessing`，谁把缺省值加回来立刻红。演示侧代价为零：全部场景都不走驳回（场景 3 是 `approved=True`），`run.py` 任何路径都到不了补偿。**唯一要注意的是房间演示** —— 在 Matrix 房间里打 `/reject` 会走到这里，演示前需 `export MAOS_SANDBOX_WORKDIR=<目录>`，否则房间回执是「审批未生效」而非「已驳回」 |

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

## task-R3

落支付网关 ToolPort 时发现、按铁律 4 与派单边界不当场处理的五条（分支 `task/r3-gateway`，基线 `90251b3`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | `invoke_tool` 的 `params_digest` 把 **gateway 实例本身**算进去，而 `_digest` 对不可 JSON 序列化的对象走 `default=str` 兜底 —— 即落到 `__repr__`。`MockGateway` 与 `AlipaySandboxAdapter` 都已显式写了不含内存地址的 `__repr__`，所以本轨两个实现的 digest 稳定（`test_params_digest_is_stable_across_calls` 守着）。但这是**实现方的自觉**，不是机制保证：任何第三个 `GatewayPort` 实现只要不写 `__repr__`，默认 repr 带 `0x7f...` 地址，同样参数每次算出不同 digest，审计就对不上账，而且**不报错、无症状** | 当前无实害（就两个实现，都写了）。风险在于它是一条**静默失效**的路径：症状是「审计里同一个调用看起来每次都不一样」，而没有任何东西会红。与 fix-2 记的 `validate_artifact` 零调用方是同一类——「口径靠自觉维持，迟早分叉」 | 候选修法二选一：①`gateway_refund` / `gateway_query` 不收 gateway 实例，改成从注册表按名字取（`params` 里只放字符串），digest 天然稳定；②在 `ToolPort` 层面约定「params 里不许放对象」并加一条检查。前者更彻底但要定网关注册表的位置，后者动 `port.py`（冻结面）。**都不在本轨派单范围**（只准改 gateway 三个文件），且都牵涉跨轨口径，建议 Ω 收口或 P5 可观测时一并定 |
| 2026-08-28 | P3 | 本轨只收录了 8 条业务错误码，而 `alipay.trade.refund` 官方表共 **31 条**（全部已核到原文，见 DECISIONS R3-04 的出处） | 未收的 23 条（`ACQ.INVALID_PARAMETER`、`ACQ.TRADE_HAS_CLOSE`、`ACQ.TRADE_STATUS_ERROR`、`ACQ.BUYER_NOT_EXIST`、`ACQ.NOT_ALLOW_PARTIAL_REFUND`、`ACQ.REASON_TRADE_BEEN_FREEZEN` 等）一旦真网关返回，`lookup()` 会**抛 KeyError**。这是**有意的**设计（未知码不许兜底成「默认可重试」），但意味着接真网关那天，任何一条未收录的码都会让调用炸在工具层而不是被当作业务失败处理 | 接真沙箱/真网关前（AlipaySandboxAdapter 填实那一刻）补齐。补的时候仍按本轨规矩：逐条核 `aipay.alipay.com` 那张表的原文描述与解决方案，`retriable` 按 remedy 原文定不按语感。**同时要决定**上层如何接住 KeyError——是转成一条「未知外部状态」的 finding 转人工（推荐，与铁律 8 一致），还是在 Port 边界统一兜成 `outcome=unknown`。后者更省事但会悄悄放宽「未知码必须显式处理」这条 |
| 2026-08-28 | P3 | `MockGateway` 没有任何**重试退避**逻辑，`20000` / `ACQ.SYSTEM_ERROR` 的官方 remedy 是「稍后重试」「保持参数不变重试」，但「稍后」是多久、重试几次封顶、是否指数退避，本轨一概没定 | 演示无影响（mock 不真等）。上真网关后是实打实的坑：不带退避的重试遇上 `40005`（调用频次超限）会**越重试越限流**；不带次数上限则是评委点名的「无限自旋」反模式（D 轨的 replan 上限守的正是同一件事） | Track B 接真网关时定，与 D 轨 replan 的上限口径**对齐着定**，不要两处各自拍一个数。注意退避策略要区分本轨定的两档：`outcome=unknown` 那档**重试前必须先 query**（否则可能产生第二笔），`outcome=failed` 那档（如 `40005`）才可以直接退避重发 |
| 2026-08-28 | P3 | 谁来调 `gateway.query` 这条链路本轨没有落点。派单把 `payment.observe` 点名为「`refund()` 一步返回 settled 就没有存在理由」的那个 skill，但该 skill 属 R-2 的 6 个 skill 之一，本轨独占文件里没有它 | 当前 `GATEWAY_QUERY_PORT` 有实现、有测试、有审计，但**生产路径上零调用方** —— 与 fix-2 记的 `validate_artifact` 同一个形态。轮询到终态的完整链路只在本轨测试与端到端演示脚本里跑过，没有场景在跑 | R-2 合并时接线（合并顺位 R-1 → R-3 → **R-2**，R-2 开工时本轨已 MERGED，签名可直接用）。接的时候注意 `poll_count` 要落进产物：它是「终态是**问出来的**、不是本地推断的」这件事的**唯一审计证据**，丢了这个字段，铁律 8 在 Trace 上就证不出来了 |
| 2026-08-28 | P3 | `GatewayReceipt` 不是 `maos/artifacts.py` 里的任何一个 kind，也没有对应 checker | 与 task-C 记的 `requirement` kind 那条同类：回执若要作为 artifact 入库（R-2 的 `payment.observe` 很可能要这么做），走 `validate_artifact` 会得到「未知 artifact kind」。当前无实害——本轨不入库任何 artifact，回执只作函数返回值与 event_log detail | 与 task-C / fix-2 记的两条**一并**处理，不要单独为 receipt 开一个 kind。`artifacts.py` 是冻结面，加 kind 属跨轨决策：要么统一补 `KIND_RECEIPT` + checker，要么明确宣布回执只走 event_log 不进 artifact 形状校验。R-2 接线前必须有结论，否则 R-2 会被迫当场自己拍一个 |

## task-R0

第六道闸落地过程中，范围外的发现四条。前两条是**跨轨接缝**，R-2 接线当天必须先看。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | 【R-2 接线必查】承载 `finance_entry` 的那份 artifact，其 `version` **必须等于该 task 当时的 `attempt`**。第六道闸按 F-1 取 `_review` 里已按 `version == attempt` 过滤后的列表，取不到就当作「没有财务凭据」 | 这是 `## task-D` 第 2 行那个坑的**镜像**：那次是 compensation 把 version 定成 0、第五道闸却在过滤后的列表里找，两轨各自都绿、合并后场景 3 当场从 pass 变 rework。这次方向相反 —— 若产数侧沿用 compensation 的「引用类产物 version=0」写法，第六道闸会**恒判 blocker**，退款场景永远到不了 DONE，而两轨单测依然全绿 | R-2 接线当天第一件事。核对产出 `finance_entry` 的那份 artifact 落库时 `version` 取的是不是 `task["attempt"]`；跑一次退款场景确认 `gate_results["finance"] == "pass"`。改哪边都行，但**必须两轨一起确认口径**，不要各判各的 |
| 2026-08-28 | P3 | 第六道闸只验 artifact `content` 一侧，`finance_entry` **表**那一侧当前无人验。F-1 给 R-2 的义务是「content 带键 + 同时写库表，两件都做，缺一件闸就判错」，但闸本身按铁律 9 推论不能查表，所以「写了表没有」这件事，闸看不见 | 若产数侧只写 content 不写表，闸照样放行，而审计链缺了一半：财务凭据在演示里「有」，在业务库里查不到。症状是演示全绿、评委问「这笔核算落在哪张表」时当场答不上来 | R-2 自测里两侧都验（F-1 已写明这是 R-2 的义务）。若要机器强制，只能加在**领域侧或端到端层**，不能加进 `maos/runtime/**` —— 加进去就是本轨拒绝做的那件事。建议 Ω 收口时在退款场景的端到端断言里补一条查表 |
| 2026-08-28 | P3 | `docs/EXECUTION.md:368` 与 `:392` 的措辞（「Gate 会查 `finance_entry`」「无 `finance_entry` = blocker」）读起来是查退款域的表，与 F-1 的 artifact content 判据字面冲突。本轨已按事实源优先级取 F-1，但**手册那两行没改** | 后来人照手册正文实现或复核这道闸，会再破一次铁律 9 推论（在 `maos/runtime/` 里 import `maos/domain/refund/`）。当前有 `test_runtime_and_core_do_not_import_refund_domain` 兜底：真写了会红，但那是事后拦，不是事前说清 | Ω 收口或下一次手册修订时，把那两行改成「Gate 查 artifact `content` 里的 `finance_entry`；`finance_entry` 表由 `finance.settle` 写并自验」。手册是禁改面之外的文件，但改它属跨轨口径，不在本轨派单范围，故不当场改 |
| 2026-08-28 | P3 | 派单 R-0 的**第 1、2、5 步未做**：场景按 run 现造 workdir、`GOOD_PATCH` 换成能打在 `auth/session.py` 上的真 diff、删 `seed_scripted_report` 及三处调用点。本次只交了第 3 步（第六道闸）与第 4 步（两条存量账） | 派单验收里的 `grep -rn "seed_scripted_report\|/tmp/maos-sandbox" maos/` 仍有输出，`run.py --scenario 1` 的「真补丁 + 真 pytest 全过」这条至今未达成 —— 「报告是脚手架，回答就不成立」那条账没有结。**R-0 不能按已完成计**（本次交付未覆盖软件域封版） | 人类当时只派了「按 F-1 写第六道闸 + 改 test_registry_autodiscovery」两件，其余三步需单独开工。三步耦合（手册明写缺一件场景就红），下次要一次做完，不要再拆 |
## task-R2

落退款域六 Skill / 四 Agent / 场景 6 时发现、按铁律 4 与派单边界不当场处理的六条
（分支 `task/r2-refund-skills`，基线 `f63de8b`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | **`maos/tests/test_registry_autodiscovery.py::test_agent_pool_is_exactly_coding` 变红**。该断言写死 `sorted(AGENT_POOL) == ["architecture","coding","requirement","reviewer","testing"]`，本轨投放四个退款 Agent 后池子变成 9 个角色 | 全量测试 **1 failed / 321 passed**，唯一的红点就是它。这是**预期变红**（派单原文：「那条断言归 R-0 本轮改名、不改语义」），不是回归 —— `test_agents_gate.py::test_four_new_agents_are_registered_by_file_drop` 用的是子集断言，不受影响 | **R-0 本轮处理**（合并顺位 R-0 → R-2）。本轨不动那个文件：一处两人改必冲突（边界第 1 条）。改名时建议连同断言方式一起换成「五个内核角色是子集 + ManagerAgent 不在池中」，否则每加一个业务域都要再改一次 |
| 2026-08-28 | P3 | `SkillInvoker` 生成的 `invocation_id` 没有放进 `SkillContext.extras`（invoker.py:69 只塞进 `SkillResult` 与落库那行） | skill 内部拿不到「自己这次调用」的官方 id，凡是要写 actor 锚点的（本域全部四个写库 skill）只能由调用方传或本地生成。结果是 `SkillInvoked` 那行的 id 与 `payment_observation.actor_invocation_id` **不是同一个值**，溯源要经 plan_id/task_id/skill 名对齐，多一跳 | `invoker.py` 的属主轨处理。修法是一行：`ctx = SkillContext(..., extras={**extras, "invocation_id": invocation_id})`。改完之后本域的 `_common.invocation_id_of()` 自动优先用它，四个 skill 零改动 —— 兜底分支保留即可 |
| 2026-08-28 | P3 | `flows/common.py::build()` 里写死 `SqliteStore()`（默认 `:memory:`），没有留库路径入口 | 派单验收里的 `sqlite3 <db> "select ..."` 四条命令无处执行；本轨取证与端到端测试都得在进程内 monkeypatch `maos.flows.common.SqliteStore`。演示无影响，但**任何要看落库结果的验收都得会这一招**，而这一招没写在任何文档里 | Ω 收口或 P5 可观测时给 `build()` 补一个可选参数（如 `db_path: str = ":memory:"`），缺省不变即零破坏。`common.py` 是冻结装配层，属跨轨决策，本轨不动 |
| 2026-08-28 | P3 | `maos/main.py` 的模块 docstring 仍只列「场景 1..5」，`ALL_SCENARIOS` 已是 `(1..7)`，场景 6 已落地 | 纯可读性：照 docstring 找场景的人看不到 6/7。R-1 的 BACKLOG 把这条建议给了「R-2 落地场景 6/7 时一并补」，但本轮派单把 `main.py` 重新列为**禁改面**（「D-05 已落，重新冻结」），两处指示冲突 | 按派单优先（事实源优先级：派单 > BACKLOG），本轨不动。建议 Ω 收口时统一补，并同时删掉 R-1 BACKLOG 里那条已失效的指派 |
| 2026-08-28 | P3 | 本轨新增的六个 artifact kind（`refund_case_draft` 等）在 `maos/artifacts.py` 里**没有 checker**，`validate_artifact` 对它们返回「未知 artifact kind」 | 与 task-C 记的 `requirement` kind、fix-2 记的 `validate_artifact` 零调用方、task-R3 记的 `GatewayReceipt` 无 kind 是**同一个缺口**：产物形状靠各轨自觉，没有统一校验。当前无实害（Gate 对非代码类产物只查 `summary` 与 `self_check`，不查 kind 白名单） | 与前述三条**一并**处理，不要为退款域单开。`artifacts.py` 是冻结面，加 kind 属跨轨决策：要么统一补 checker，要么明确宣布「域内 kind 不进 ALL_KINDS、形状由域内测试守」——后者是当前的事实口径，本轨已用测试守住（`test_finance_agent_artifact_carries_finance_entry` 断言 `finance_entry` / `summary` / `self_check` 三件齐全） |
| 2026-08-28 | P3 | `payment.observe` 的 `needs_compensation=True` 目前**没有消费方**：场景 7（退款失败路径）不在本轨独占文件里 | 与 R-3 记的「`GATEWAY_QUERY_PORT` 生产路径零调用方」同一形态，只是往下挪了一层。网关明确失败时本域会正确地记下观察、不推进状态、把标记挂进 `AgentOutput.open_questions`，但没有任何场景在跑这条路 —— `MockGateway(script=...)` 的错误注入能力至今只在 R-3 自己的测试里用过 | 场景 7 落地时接线。接的时候注意两点：①`compensated` 这一跳的写入方要想清楚（本域目前没有任何 skill 写它）；②失败路径同样要证明「终态是问出来的」，`poll_count` 与 `remedy` 要落进产物 |
## task-omega

落 Trace / 证据束 / `verify.py` / 部署时发现、按铁律 4 与派单边界**不当场处理**的八条（分支 `task/omega-evidence`，落地基线 `f63de8b`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | **`deploy/.env.example` 写不进去**：撞上与根目录 `.env.example` 同一条权限层 deny 规则（`.gitignore` 的 C-8 放行两行本身是生效的，拦它的是 Claude Code 的 Read deny 规则）。派单预告了这个坑并要求「撞同一条规则就停下报告，不要绕」，本轨照办，没有改名或换位置替代 | compose 的 `env_file` 指向 `deploy/.env`（标了 `required: false`，缺省即降级，空环境也跑得通），但**评委拿不到一份可 `cp` 的全量样例**。需要的键名清单在此列全：`MAOS_LLM_BASE_URL` / `MAOS_LLM_API_KEY` / `MAOS_LLM_MODEL` / `MAOS_LLM_TIMEOUT`、`MAOS_SANDBOX_WORKDIR` / `MAOS_SANDBOX_TIMEOUT` / `MAOS_SANDBOX_FORCE_SUBPROCESS`、`MATRIX_HOMESERVER` / `MATRIX_USER` / `MATRIX_TOKEN` / `MATRIX_ROOM_ID`、`MAOS_APPROVERS` / `MAOS_MAX_REPLAN` / `MAOS_FINANCE_THRESHOLD`、`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` / `MAOS_PG_DSN` | **人类放开 deny 规则后由任意一轨补**，内容照上面的清单，值一律占位符。或者改一个不被 deny 的文件名（如 `deploy/env.sample`）并同步 compose 注释 —— 但那要先确认换名不违反 C-8 的冻结口径 |
| 2026-08-28 | P3 | `review_after_gate()` 直接 `store.insert_artifact` 落 review_note、场景层 `seed_scripted_report()` 预置 test_report，两者都不经 `on_task_result`，因此**没有任何事件行能指到它们**（`## task-C` 第 5 条预告的问题，在 Trace 上落实了） | 本轨已让它可见：这类产物在 trace 里标 `provenance="unknown"` 并计进 `summary.unsourced_artifacts`，`verify.py` 第 4 项把数量印出来。实测各场景无来源产物数：场景 1 = 2、场景 2 = 3、场景 3 = 1、场景 5 = 2。**这不是修复，是让洞可见** —— 审计上仍然查不出这些产物是谁在哪一步产的 | 跨轨决策，本轨按派单只报不修。两条路：①给 Control Plane 开一个「非任务产物入库」的正式入口（带审计行），review_note 与预置件都走它；②明确宣布这两类是流程附属物、不进审计链，并在 trace 里把 `provenance=unknown` 改成一个更准确的名字。**P5 做可观测前必须有结论** |
| 2026-08-28 | P3 | `flows/scenario_5.py` 的 `_intake_goal()` 在 `create_plan` 之前调 `issue.aggregate`，落的 SkillInvoked 行 `plan_id` 与 `trace_id` 都是**空串** | 按 plan 查 event_log 永远查不到它（`list_event_log` 是 `WHERE plan_id=?`）。本轨已在 `trace.py::stray_events()` 里单独把这类事件点名，写进 `trace.json` 的 `stray_events`，`verify.py` 第 4 项印成 warn。但它仍然不属于任何一棵 span 树 —— 一次真实发生的 skill 调用在 Trace 上无处安放 | D 轨或 P5 处理。最小改法是把 intake 挪到 `create_plan` 之后、或先建 plan 再聚合；如果「建 Plan 之前就要调 skill」是有意的设计，那就该给这类调用一个正式的归属（比如一个 bootstrap plan_id），而不是留空串 |
| 2026-08-28 | P3 | `evidence/` 目前这一批产物生成于 `f63de8b`，而 R-0（场景 1/2 改真连沙箱）与 R-2（场景 6/7 退款域）都还没合入 | 产物**必然过期**：场景 6/7 目录根本不存在，场景 1/2 的 test_report 现在还是预置件（`provenance=unknown`、第 6 项 warn「来源未审计」）。每个文件首行的 git sha 会与届时的 HEAD 对不上，过期是自证的，但不会有人自动重跑 | **R-0 / R-2 合并后由收口方整体重跑**：`python3 scripts/make_evidence.py && python3 scripts/verify.py --evidence evidence/ --db evidence/`。届时第 2、3 项应从 SKIP 转为真跑（退款场景会建出 `business_ref` / `refund_case` / `payment_observation`），第 6 项的 warn 应减少 |
| 2026-08-28 | P3 | `verify.py` 第 5、7 项（kb-hit / history-case）本轮恒为 SKIP —— 没有 `kb_doc` 表，也没有 `KbRetrieved` 事件（全仓 grep 零命中） | 两项的判定代码已写好且有 SKIP 语义的测试，但**正例从未真跑过**。SKIP 已按派单显式点名、不计进 PASS 分子，所以不会伪装成通过 | P5 落 kb 层时一并验：建 `kb_doc` 表、`kb.retrieve` 开始落 `KbRetrieved` 事件之后，这两项会自动从 SKIP 转为真跑，届时补正负例测试（本轨的测试文件里已有对应的 SKIP 断言可以直接改写） |
| 2026-08-28 | P3 | `*.db` 不入库（派单要求），而 `verify.py` 需要库才能重放校验 | 克隆仓库的人**不能直接跑 `verify.py`**，必须先 `make_evidence.py` 重建库。`verify.py` 在库缺失时会明确报「缺数据库，先跑 make_evidence.py」并以非零退出（有测试守着），不会伪装成全过。但这确实让「一条命令验真伪」变成了两条 | README 里把两条命令一起写在最显眼处（本轨未改 README —— 不在独占文件里）。若希望评委真正一条命令搞定，可考虑让 `verify.py` 在库缺失时自动调 `make_evidence.py`，但那会让「核验」与「生成」耦合，不推荐 |
| 2026-08-28 | P3 | `maos/flows/common.py::build()` 的 `SqliteStore()` 路径写死为 `:memory:`，没有任何配置口 | 任何需要持久化的用途（证据、调试、换 PG 后端）都只能靠外部替换类来实现 —— 本轨的 `make_evidence.py` 就是这么做的。这条注入路径**没有测试守着**：`build()` 里那行改个写法（比如改成局部 import 或直接 `SqliteStore(":memory:")`），证据生成会静默失效。本轨已加一道兜底 —— 子进程退出码为 0 却没落库时硬失败并说明「注入点可能已失效」 | `build()` 加一个 `store=` 注入口最干净（与已有的 `model=` 注入口同形），但 `build()` 签名是 C-3/C-4 冻结契约，属跨轨决策。**P5 换 PG 后端时必然要面对**，建议那时一并定 |
| 2026-08-28 | P3 | `invoke_tool` 的 `params_digest` 把工具实例算进 digest（`## task-R3` 第 1 条留给「Ω 收口或 P5 可观测时一并定」的那条） | 本轨看过：`trace.py` 只是把 event_log 里已有的 `params_digest` 原样搬进 span，不参与它的计算，所以 Trace 侧不受影响；`verify.py` 第 1 项只校验它是 64 位十六进制、不重算。**问题仍在**（第三个 `GatewayPort` 实现若不写 `__repr__`，同参数每次 digest 不同，审计对不上账且无症状），只是不在本轨的可改面内（`maos/tools/port.py` 是冻结面） | 维持 R-3 的建议：候选修法二选一（工具按名字取实例 / 在 `ToolPort` 层面禁止 params 放对象），都要动冻结面，属跨轨决策。**P5 可观测收口时定** |

## integrate-round-2

三轨合并后的整体验收发现两条，均**不在本轮可改面内**，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P3 | `maos/main.py:25` 的 `DEFAULT_SCENARIOS = (1, 2, 3, 4)`，行内注释写的是「场景 5 未实现，不进缺省序列」——**该注释已过时**：场景 5 早已落地（`--scenario 5` exit=0），场景 6 本轮由 R-2 落地（exit=0），两者都不在缺省序列里 | CLAUDE.md 与 README 里的验收命令 `python3 run.py` 号称「四场景端到端」，实测确实只跑 1-4：**退款域整条链路（含第六道闸的人工放行）不被缺省序列覆盖**。演示现场若只跑 `run.py`，评委看不到本轮最重要的两轨产出，且不会有任何报错提示他们漏了 | `main.py` 是禁改面（R-2 记「D-05 已落，重新冻结」），需人类解冻后改。修法一行：`DEFAULT_SCENARIOS = (1, 2, 3, 4, 5, 6)`，并同步那条行内注释与 `run.py` docstring 的「四场景」措辞。**建议在演示前做掉** —— 这是三轨全绿之后唯一一处「跑了也看不见」的缺口。**✅ 已于 2026-08-28 收尾时解**：人类当场授权解冻，`DEFAULT_SCENARIOS=(1..6)` + 四处措辞同步，见 DECISIONS `## integrate-round-2` 第 4 条 |
| 2026-08-28 | P3 | 场景 7（退款失败路径）未落地：`ALL_SCENARIOS` 已声明 7，但 `maos/flows/scenario_7.py` 不存在，`run.py --scenario 7` 直接 `ModuleNotFoundError` 退出码 1 | 与 R-2 记的「`payment.observe` 的 `needs_compensation=True` 没有消费方」是同一个缺口的两端：网关明确失败时本域会正确记观察、不推进状态、挂 `open_questions`，但**没有任何场景在跑这条路**。`--scenario 7` 是 argparse 的合法取值，任何人照着 `--help` 试一次就会撞见一个未捕获的 traceback | 场景 7 落地时一并解。在此之前若要避免那个 traceback，只能改 `ALL_SCENARIOS`（禁改面）或在 `_run_scenario` 里捕获 ImportError（同一文件），都要人类解冻 —— 故本轮不动。落地时注意 R-2 记的两点：`compensated` 的写入方要想清楚，失败路径同样要证明「终态是问出来的」 |

## task-W1

造退款域语料与三组对照数据集时发现、按铁律 4 与派单边界**不当场处理**的三条
（分支 `task/w1-refund-corpus`，基线 `01bc8d8`）。本轨零代码，三条都在可改面之外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P5 | `policy.match` 的 approve/reject **只判「有没有命中 AS- 前缀的规则」**（`policy.py:120`），不评估任何规则参数：`no_reason_days` 窗口、`warranty_basis` 在保判定、`min_evidence_count` 举证数都没有判定器。`finance.settle` 也只消费 `refund_ratio` / `deduct_fee` 两个键 | 三组对照里 **R3（租户维度）在今天的代码路径上跑不出「驳回」**：租户 A 与 B 都命中 `AS-001@v1`，只是 `params.no_reason_days` 分别是 30 与 7，而 30 与 7 的区别没有代码去看，两侧都会 approve。R4（渠道，靠 `channel_scope` 过滤）与 R6（版本，靠 `version <= pinned`）不受影响，已实测在现有匹配器上直接成立。数据侧已按窗口参数造好，`_expected` 里写明了预期结论 | W-3 或 P5 落 `kb/guardrails.py` 时一并做。最小形态是一个只读判定器：读 `matched_rules[].params` 与案件的申请时刻，产出 eligible/ineligible 与依据，**不写任何状态**。注意别把它塞进 `policy.match` 的 decision 里就完事 —— 「命中了哪几条」与「按这几条该不该退」是两个问题，混成一个字段之后审计就说不清是规则没命中还是条件不满足 |
| 2026-08-28 | P5 | `refund_case` 表**没有「客户申请时刻」这一列**。表里只有 `created_at`，而它是 `guard.create_case` 落库那一刻（`guard.py:119` 写的是 `_now()`），不是客户提出退款诉求的时刻 | 上一条那个窗口判定器**缺输入**：`no_reason_days` 要拿「签收/支付时刻」与「申请时刻」求差，前者在 `order_snapshot.paid_at` 里有，后者库里根本没有。本轨只能把 `requested_at` / `elapsed_days` 放进 case 文件的 `_expected` 块 —— 那是给人和给对照实验看的，不是能进 SQL 的列。补救路径也堵着：`refund_case` 的现有列不许改（铁律 2 只许新增表） | 与上一条同时定。两条路：①新增一张 `refund_intent(tenant_id, case_id, requested_at, source, ...)` 表（合规，只新增）；②把申请时刻塞进 `refund_case` 之外的既有落点。倾向 ①，因为多源诉求聚合本来就有「诉求是什么时候、从哪来的」这组事实要存，`refund.intake` 已经在做聚合却没把它落下来 |
| 2026-08-28 | P5 | 本轨产出的 7 份数据文件**目前零消费方**：五份对照 case 接不进演示（场景 6 把业务对象内联写死在 `maos/flows/scenario_6.py` 的 `seed_domain()`（:178-200）与 `case_seed`（:124），没有读 JSON 的通路，且该文件不在本轨可改面内）；24 条历史案例无处入库（`kb_doc` 表 P5 才建，W-3 轨在做） | 数据造出来了但**没有任何东西会因为它变红或变绿** —— 一旦哪天字段与 `schema.sql` / `kb_doc` 列清单分叉，不会有任何报错。README 里给了三条可手跑的自校验命令（JSON 可解析、错误码 ⊆ `ALL_CODES`、case 与政策语料零漂移）作为临时兜底，但它们不在 pytest 里，没人会自动跑 | W-3 建 `kb_doc` 时把 `history/history_cases.json` 作为入库来源，顺手就能把列清单对齐这件事变成有人守。对照 case 的接入通路建议见 `docs/DECISIONS.md` 的 `## task-W1` 末条 —— 要点是**给场景 6 加一个可选的 seed 文件入参、缺省仍走内联常量**，不要改内联那份（场景 6 的验收之一是「连跑两次输出逐条一致」，换成读文件会把这条验收的前提换掉） |

## task-W2

补 StorePort 抽象时看到、按铁律 4 与派单边界不当场处理的六条
（分支 `task/w2-storeport`，基线 `01bc8d8`）。前两条是 SQLite FTS5 自身的行为，
**都不报错**，所以 W-3 的检索器不知道就会踩；第三、四条是迁移与接线的悬空点。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P5 | **FTS5 的 trigram 分词器对 <3 字符的查询切不出任何 token，恒返回空集**，且不报错。中文两字词（「退款」「超时」「订单」）在 trigram 影子表上一条都命不中；实测「退款政策」（4 字）能命中「退款政策超时未到账」，「退款」（2 字）返回空 | 检索器会把「查询太短」误读成「库里没有这条知识」，然后把一个空召回当成正常结果往下传。RAG 场景里这几乎必然发生 —— 用户就是会输入两个字。**换缺省的 unicode61 更糟**：它把一整串汉字切成一个 token，「退款政策超时未到账」整条是一个词，连「退款政策」都命不中（同样不报错）。两种分词器的实测对照见本轨回执 | 归 W-3（检索器侧）。适配器不替 SQLite 兜底 —— 兜了就是在存储层自造一套分词语义，而真正该做的事（中文分词、查询扩展）不在存储层。检索器侧二选一：查询串 <3 字符时直接走向量通道，或先做一次查询扩展再进全文通道。`test_fts_search_chinese_needs_trigram_and_three_chars` 已把这两条事实钉成断言，改行为会红 |
| 2026-08-28 | P5 | **bm25 的 IDF 在「词出现在过半文档里」时归零**，此时同一批命中的分数全是 0.0，排序退化成按 id | 拿分数做阈值过滤（`score > 0.5` 之类）的检索器会在小库上把**全部**命中过滤掉 —— 库越小越容易触发，而演示库正是小库。这是 BM25 本身的性质，不是排序坏了 | 归 W-3。建议全文通道只用分数**排序**、不用它做绝对阈值；真要阈值就在混排时按名次而不是按分值。`test_fts_search_hits_and_orders_by_score` 用的语料刻意避开了这个区间（分数严格递减），换语料时注意 |
| 2026-08-28 | P5 | `objects.py::ensure_schema()` 走的是 `executescript`（一次跑整份 schema.sql），而 F-2 的五个方法里没有对应物 | 退款域从私有 `_conn` 迁到 StorePort 时，`execute` / `query` 都换得掉，只有建表这一步没有落点。不先定下来，迁移那天会在现场临时决定，多半就顺手往 Port 上加第六个方法了 —— 而那是 W-3 已经照着写的冻结面 | 与 `## task-R1` 第 3 条一并处理（DECISIONS `## task-W2` 已写明整套换法）。二选一：把 schema.sql 按语句切开逐条 `execute`，或给 sqlite 适配器加一个**不属于 Port** 的 `executescript`。**不许动 F-2 那五个签名** |
| 2026-08-28 | P5 | 本包落地后**零调用方**：`maos/store/` 是纯新增，主链路一个 import 都没接。而 `maos/flows/common.py::build()` 里 `SqliteStore()` 写死 `:memory:`、没有注入口（`## task-omega` 第 2 条已记同一处） | 现状是对的（派单要求缺省路径逐字节不变，383 条测试是判据），但意味着 StorePort 目前只有测试在跑。P5 要「后端可插拔」，总得有人把 `build()` 的 store 换成经 `create_store()` 造出来的 —— 而 `build()` 签名是 C-3/C-4 冻结契约，属跨轨决策 | P5 接线时定，与 `## task-omega` 第 2 条一起。接的那一轨自己验缺省路径不变（`run.py` 输出除随机 id 外应逐字节一致，本轨用 `plan_`/`task_`/`actor=` 归一化后比对过，可照抄这个手法） |
| 2026-08-28 | P5 | `create_store()` 里 postgres 那条 `NotImplementedError` 是 P5 填实时**必须拆掉**的一行，而拆掉它就同时废掉了 `test_postgres_backend_raises_and_never_falls_back` 这条守卫 | 那条测试守的是「不许静默回落 sqlite」。P5 填 PG 时若只顾着让它跑通、把测试删了了事，就再没有东西守着回落这件事了 —— 而回落恰恰是 PG 后端最容易出的那种无症状错误（连不上就悄悄用 sqlite） | P5 填 `pg_store.py` 时，把那条测试**改造**而不是删除：改成「PG 后端在 DSN 缺失或连不上时抛错，不回落 sqlite」。`PgStorePort.__repr__` 对 DSN 的脱敏（铁律 6）同理要保住，`test_pg_dsn_comes_from_env_and_repr_hides_it` 守着 |
| 2026-08-28 | P5 | `scripts/verify.py` 在新建的 worktree 里跑不了：它要 `evidence/*.db`，而 `.db` 不入库（`## task-omega` 第 1 条记过「必须先跑 make_evidence.py」） | 本轨实测确认了这条，并且发现直接跑 `make_evidence.py` 会改写仓库 `evidence/` 下 38 个已入库文件 —— 对任何「只准改独占文件」的分轨来说，这等于验收命令和派单边界互相冲突 | 补一句可直接用的解法（不改任何脚本）：`make_evidence.py --out <tmp>/evidence` 配 `verify.py --evidence <tmp>/evidence`，证据落到仓库外，`evidence/` 一个字节不动，结果一样是 5/5 PASS + 2 SKIP。建议写进 `docs/EXECUTION.md` 或各派单的验收段，省得每轨自己撞一次 |

## task-W6

场景 7 落地时发现、按铁律 4 与派单边界**不当场处理**的四条（分支 `task/w6-refund-failure`，基线 `01bc8d8`）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P4 | **replan 触发源缺口**：手册 R2 与派单 §8 的「网关可重试错误码 → replan 换渠道重试 → 仍失败 → 达 replan 上限 → needs_human」这一段没有落地。新增触发源要改 `ControlPlane._should_replan`（现有两条触发线是「单轮 blocker ≥ 2」与「同一任务第 2 次 rework」），而 `control_plane.py` 按 `docs/parallel/contracts.md:147` 归 Task-D，不在 W-6 白名单 | 场景 7 走的是同一条 HITL 收口路径的另一个入口（付款任务 `effect_risk=H`，Gate 过后停 BLOCKED），收口断言与题眼**完全一致**：`biz_status=compensated`、全库 `settled` 观察 0 条、Plan FAILED(`human_reject`)。缺的是**演示叙事**里「系统自己试过换渠道、试到上限才转人工」那一段 —— 现在是「一次就转人工」。`MAOS_MAX_REPLAN` 与超限转 needs_human 的机制本身早已存在且由场景 5 覆盖，缺的只是把网关错误码接成第三条触发线 | **人类授权改 `control_plane.py` 后补**。改动面很小：`_should_replan` 增一条「本轮 findings 里有 `gate == "gateway"` 且 `retriable` 为真的 finding」，判据一律查 `maos/tools/gateway_codes.py` 的 `ALL_CODES`，不自判语感。同时要有人产出那条 finding —— 当前没有任何一道闸认识网关回执，这是同一个决策的两半，建议一并定 |
| 2026-08-28 | P4 | `maos/main.py` 的 `DEFAULT_SCENARIOS = (1,2,3,4,5,6)` 不含 7，且 module docstring 里「场景 7：退款失败路径 **未落地**，`--scenario 7` 会 ModuleNotFoundError」这句已过时 | `python3 run.py` 无参仍只跑 1-6，**失败路径不进缺省序列**。而 main.py 自己的行内注释写着「排除标准是「模块不存在」，不是「谁负责」，所以 scenario_7.py 一落地就该进来」—— 现在它落地了，注释与常量对不上。演示时只跑 `run.py` 的话，评委看不到本轮唯一一条失败路径，且不会有任何报错提示他们漏了 | `main.py` 是派单级冻结面（§0 「一个字不许动」），需人类解冻。修法两处：`DEFAULT_SCENARIOS=(1,...,7)` + 把 docstring 那行「未落地」改掉。**建议在演示前做掉** —— 与 `## integrate-round-2` 第 1 条是同一类缺口（「跑了也看不见」），那条已经解过一次 |
| 2026-08-28 | P4 | `refund_request` 表**没有状态列**（`tenant_id / case_id / request_id / amount / gateway / idempotency_key / submitted_at`），域内补偿没法在这张表上打「已作废」的标 | 作废只能落在 `compensation_record`（`kind='refund_request_revoked'`）里。查一笔请求还有没有效必须联查两张表，单看 `refund_request` 会以为它仍然在途。当前只有一个消费方（本场景），影响有限；多一个消费方就容易漏 | `maos/domain/refund/**` 归 R-1，本轨不改。两条路：①给 `refund_request` 加一个可空的 `revoked_at`（只加列，不动既有列，与「表结构禁改、只许新增」的口径相容）；②明确宣布「请求的有效性以 `compensation_record` 为准」并写进域文档。**下一次动退款域时定** |
| 2026-08-28 | P4 | 派单 §9 写「高 `effect_risk` 的退款任务必须产出 `compensation` artifact，否则第五道闸判 blocker」—— 与代码不符。`ReviewerGate._gate_compensation` 在**没有** compensation 产物时直接 `return []`（源码里写明「本轨不替它判定『高风险任务却没有补偿方案』，那条缺口已记 BACKLOG，留 D 轨接线时定」） | 场景 7 的付款任务 `effect_risk=H` 且没有 compensation 产物，第五道闸原样放行 —— 这是当前代码的既定行为，不是本轨造成的。派单据此写的那句「否则判 blocker」如果被当成事实去核对，会核出一个不存在的问题 | 与 `## task-C` / `## task-D` 里那条同源缺口一起定。**注意**：退款域产的不是 `patch_set`，逆补丁补偿对它不适用，所以「高风险任务必须有补偿方案」这条一旦补上，判据不能只认 `KIND_COMPENSATION` —— 否则退款任务会恒 blocker。建议届时把判据放宽成「高风险任务必须有**某种**已登记的补偿手段」，域内补偿（`refund.compensate`）算一种 |

## task-W7

软件域封版（分支 `task/w7-software-seal`，基线 `01bc8d8`）。本轨把场景 1/2 的
test_report 从预置常量换成真跑产物，`## merge-p2` 第 1 条要求的三件（现造 workdir、
真 diff、删预置报告）与第 2 条（探针不进业务判据）一并落地。以下三条是本轨发现、
按铁律 4 不当场改的账 —— 三条都落在本轨独占文件之外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P4 | **真报告仍被审计链判成「预置件」**。`flows/common.py::patch_verifier` 直接 `store.insert_artifact` 落 test_report（与 `seed_scripted_report` 同一条路），绕开了 `on_task_result`，因此没有来源事件 | `maos/obs/trace.py` 照旧把它标成 `provenance="unknown"`，`scripts/verify.py` 第 6 项因此 warn「N 条外部判据来源未审计（**场景预置件，非实跑产出**）」——**这句措辞现在是错的**：场景 1/2 的报告确实是真跑 pytest 出来的，只是插入路径证不了。核验仍 5/5 PASS（warn 不判负），但评委读证据时会被这句话误导，把已经兑现的「外部权威判据」重新读成脚手架 | 两条修法都出本轨的面：①`trace.py` / `verify.py`（Ω 的面）把措辞从「预置件」改成「无来源事件」，并区分「场景预置」与「演示装配层现跑」；②给控制面一条「带来源的外部产物入库」路径，让现跑的报告也有 StateTransition 可挂。**建议 ①，成本一行措辞**；② 要动控制面，留 P5 可观测收口 |
| 2026-08-28 | P4 | `maos/tools/sandbox.py::_docker_ready()` 用 `docker image inspect <IMAGE>` 探镜像。本轨实测该命令在 Docker Desktop **29.6.1** 上会**瞬时失败**：连试三次 exit=1（`No such image: maos-sandbox`），而同一时刻 `docker image ls maos-sandbox` 列得出、`docker run --rm maos-sandbox python -V` 跑得通；几分钟后 inspect 自行恢复 exit=0 | 命中那一刻沙箱**静默降级**成裸 subprocess，只留一条 `log.warning`，演示屏幕上看不出任何差别 —— `--network none` / `--read-only` / `--user 1000:1000` 全部失效，而 `test_no_network` 会从 passed 变成 skipped、报告仍然全绿。「容器隔离」这句话当场不成立而没有人知道，正是本轨要拆的那类假绿的孪生形态 | `maos/tools/sandbox.py` 是 Task-B 的面（本轨只 import）。修法建议：探测改成 `docker image inspect --type=image <IMAGE>:latest`（实测该形式全程 exit=0），或降级时把原因**打进 test_report 的 summary**，让它随证据一起落盘而不是只进日志。**演示前值得做掉**——现场撞上这一次就白演 |
| 2026-08-28 | P4 | 补丁的应用与回归执行落在**演示装配层**（`flows/common.py::patch_verifier`），不在 Testing Agent 里 | 这是 DAG 成环逼出来的（理由见 DECISIONS `## task-W7` 第 1 条），不是设计首选。代价：Testing 节点在演示里跑的是「补丁已经打好之后的第二遍回归」，它自己那份报告不构成 coding 任务的验收证据 —— 谁只读 `agents/testing.py` 会以为那一节点就是证据来源 | 等控制面支持「同一 attempt 内先跑验证任务、再判被验任务」时收回 Testing Agent。在那之前**不要**为了好看把这段挪进 Agent —— 挪进去当天 coding 过闸拿不到报告，场景 1/2 直接红 |

## task-W3

KB/RAG 层落地时发现，均**不在本轨可改面内**，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-28 | P5 | `flows/scenario_6.py:228` 用 `ManagerAgent(model)` 老写法构造 Manager，`SkillInvoker.store is None`；而该场景的规划来自 `ScriptedModelClient` 写死的 PLAN_JSON | 规划期检索在场景 6 这条链路上**恒返回空**，`MAOS_KB_ENABLED` 开关对它没有任何影响（两种开关下 `run.py --scenario 6` 都 exit=0，输出逐字节相同）。所以场景 6 的证据束里没有 kb_doc 表、没有 KbRetrieved 事件——RAG 的证据全部落在 `evidence/scenario-R5/` 里 | 若希望演示时「场景 6 本身就带 RAG」，最小改法是把构造换成 `ManagerAgent(model, store=store)` 并给 `mgr.plan()` 传 context（本轨已把 `context` 做成可选参数，接线是一行）。但 `flows/**` 是本轨禁改面，且场景 6 的 PLAN_JSON 含 finance，接上检索也不会改变它的 DAG——真要有对照必须换一份不含 finance 的脚本。**建议演示前由持有 flows 的一轨决定** |
| 2026-08-28 | P5 | 规划期检索发生在 `create_plan` **之前**，落的 `KbRetrieved` 与 `SkillInvoked` 两行 `plan_id` 是空串 | 与 `## task-omega` 记的 `scenario_5.py` 那条是同一个缺口：按 plan 查 event_log 查不到它们，`verify.py` 第 4 项印成 warn（`scenario-R5: 2 条事件的 plan_id 指不到任何 plan`）。一次真实发生的检索在 Trace 上无处安放 | 与那条一并解。根因是 `ControlPlane.create_plan` 自己生成 plan_id、不接受外部传入，所以规划期拿不到它。给这类「建 Plan 之前的调用」一个正式归属（bootstrap plan_id，或让 create_plan 接受预生成的 id）比继续留空串好，但两条路都要动 `core/**` |
| 2026-08-28 | P5 | 第六道闸 `_gate_finance` 的 F-1 口径下，「漏排财务复核」**判不出 blocker** —— 闸按 `task.inputs` 的 `biz_type + amount_claimed` 触发，漏排意味着没有任务带申报金额，闸没有可判的对象 | `gate.py:363` 的注释写着「没检索到历史案例 -> 计划里漏排财务复核 -> 在这里被拦下」，与实际行为不符。R5 实测的真实拦点是 `payment.execute` 的「没有 finance_entry，金额未经核算，不许发起付款」（见 DECISIONS `## task-W3` 第 2 条） | 两条路：①改注释，承认这道闸守的是「带了金额却交不出凭据」而不是「漏排」；②给闸加一条 plan 级判据（这个 Plan 里有 refund 任务却没有任何 finance_entry）。②更贴合注释的原意，但闸目前逐任务判、且不许 import 业务域，加 plan 级判据要重新想清楚判据落在哪个数据形状上。**`runtime/**` 是本轨禁改面，留给持有它的一轨** |
| 2026-08-28 | P5 | 知识晋升目前是**手动**的（`experiment.promote_history_case` 显式调用），自动晋升调度器按派单第 7 步「写进 BACKLOG，不实现」 | Plan 走到终态后没有任何东西会自动把够格的 case 沉淀进 `kb_doc`。`PlanFinalizer` 已经在轮询终态并调 `kb.sink`，但 `kb.sink` 写的是 `knowledge` 表（复盘条目），不是 `kb_doc`（结构化知识层）——两张表当前没有打通 | 最自然的落点是 `PlanFinalizer.poll()` 里在 `kb.sink` 之后加一步晋升判定，调用 `guardrails.classify_case`（已实现且有单测）。需要动 `maos/runtime/plan_finalizer.py` 与 `maos/skills/builtin/kb_sink.py`，两者都不在本轨可改面内 |
| 2026-08-28 | P5 | W-1 轨的 `scenarios/refund/` 语料（政策 + 20-30 条历史案例 + 三组对照 case）尚未到位 | R5 的靶场数据（订单快照、政策 AS-01、客户 ack）是本轨自造的**最小集**，只够跑通链路。检索质量在这份语料上说明不了什么——候选集只有 1 条，四通道的融合排序在单测里验，不在 R5 里验 | W-1 合并后把 `experiment._seed()` 换成读 `scenarios/refund/` 的语料即可，晋升与检索链路不用动。届时 R5 的 `candidate_count` 会从 1 变成几十，融合排序才开始有话可说 |
| 2026-08-28 | P5 | W-2 轨的 `maos/store/port.py`（StorePort）尚未合并 | `_fts_scores` / `_vector_scores` 目前恒走本地实现（SQLite FTS5 + 纯 Python 余弦）。接 StorePort 的分支已写好并按**能力探测**接（store 上有 `fts_search` / `vector_search` 就用），但**从未被真正走过**——这条分支没有测试守着 | W-2 合并当天验一次：确认 `SQLiteStore` 实现了那两个方法后，检索结果与本地实现一致（分数可以不同，命中集合应该一致）。若 PG 后端接上，`vector_search` 走 pgvector 时的分数量纲需要与本地余弦对齐，否则融合权重的含义会漂 |

## task-X1

演示链路收口时发现、按铁律 4 与派单边界**不当场处理**的五条（分支 `task/x1-demo-seal`，
基线 `4a70cb0`）。前两条是本轨改动的**已知副作用**，后三条是被本轨改动带出来的过时措辞。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | 场景 6 接上检索后，`KbRetrieved` 恒为 `candidate_count=0` / `hit_count=0` —— `seed_domain()` 只播订单/商品/政策，不播 `kb_doc`，库里没有任何可召回的知识 | 检索**真的发生了**（事件在、query 四维在、`duration_ms` 在、`kb_doc` 全套表建起来了），但演示时 detail 里是 `docs: []`。「RAG 接上了」这句话在场景 6 上只能证明到「链路通」，证明不到「召回准」—— 后者的证据仍然只在 `evidence/scenario-R5/`（对照实验）里 | 本轨刻意不 seed（理由见 DECISIONS `## task-X1` 第 3 条：属派单外的顺手优化，且有改变 DAG 的风险）。**出口是 `## task-W3` 第 5 条**：W-1 的 `scenarios/refund/` 语料到位后，把它播进场景 6 的 `seed_domain()`，`candidate_count` 会从 0 变成几十，命中才有话可说。届时要一并检查 `_merge_kb_suggestions` 会不会往 DAG 里加任务 —— 场景 6 的 PLAN_JSON 已含 finance，按 task_key 去重后**大概率不变**，但这条必须实测，不能推断（`test_kb_switch_does_not_change_the_dag` 已把它钉成断言，变了会红） |
| 2026-08-29 | P5 | 场景 6 的证据束新增 **2 条游离事件**（`KbRetrieved` + `SkillInvoked`，`plan_id` 为空串），`verify.py` 第 4 项 warn 从 `scenario-5` 扩散到 `scenario-6`：`scenario-6: 2 条事件的 plan_id 指不到任何 plan，不在任何一棵树内` | 核验仍 **7/7 PASS**（warn 不判负），但「一次真实发生的检索在 Trace 上无处安放」这个缺口的暴露面从 1 个场景变成 2 个。根因不是本轨引入的 —— 规划期检索发生在 `create_plan` **之前**，那时 plan_id 还不存在 | 与 `## task-W3` 第 2 条、`## task-omega` 里 `scenario_5.py` 那条是**同一个缺口**，三条一并解。根因是 `ControlPlane.create_plan` 自己生成 plan_id、不接受外部传入，所以规划期拿不到它。两条路（bootstrap plan_id / 让 create_plan 接受预生成 id）都要动 `core/**`，本轨禁改面。⚠️ 本轮 X-2 轨正在动 `core/control_plane.py`，**若那一轨顺手改了 create_plan 的签名，这三条可以一并收口** |
| 2026-08-29 | P4 | `CLAUDE.md:75` 的常用命令注释仍写 `python3 run.py  # 场景 1-6 端到端`，本轨改后实际跑 1-7 | 每个会话自动加载 `CLAUDE.md`，这句是**所有会话看到的第一份事实**。留着它，下一个会话会照着「1-6」去复核 `run.py` 的输出，然后把多出来的场景 7 当成异常 —— 与 `## integrate-round-2` 第 1 条「跑了也看不见」是同一类缺口的镜像（这次是「跑了但文档说不该跑」） | `CLAUDE.md` **不在本轨白名单**（派单 §3 只列 `main.py` / `scenario_6.py` / 测试 / 两份账本），故不改。**建议编排侧收口时一行改掉**：`场景 1-6 端到端` → `场景 1-7 端到端`。同类措辞已在本轨独占面内全部同步（`main.py` 5 处 + `run.py` 1 处） |
| 2026-08-29 | P5 | `maos/agents/manager.py:50` 的 `plan()` docstring 写着「场景 1-6 的 `mgr.plan(GOAL)` 一行不用改，输出也一个字节不变」—— 本轨把场景 6 改成了 `mgr.plan(GOAL, context=kb_context)`，这句已不成立 | 纯可读性，不影响行为（`context` 仍是可选参数，场景 1/2/5/7 确实一行没改）。但读这句的人会以为「全部场景都没接 context」，从而错过场景 6 这个**唯一的**演示期接线点 | `maos/agents/manager.py` 不在本轨白名单，不改。修法一行：把「场景 1-6」改成「场景 1/2/5/7」，或改成「不传 context 的场景」。**下一次动 `agents/**` 的轨顺手改掉**即可；W-3 是这句的作者轨 |
| 2026-08-29 | P4 | 守卫 hook 对**只读**命令同样按路径字面量拦：`git diff --stat <sha> -- ... .contracts.lock ...` 被判 `blocked: 该操作触碰受保护面 .contracts.lock（读取位置）`。而这条命令恰恰是**派单 §5 自己要求**用来自证冻结面为空的 | 派单要求的验收命令跑不了。本轨改用全量 `git diff --stat` 等价自证（证明力更强，见 DECISIONS `## task-X1` 第 4 条），但下一个照派单原文执行的会话会**在同一处被拦**，并可能误以为自己碰了禁改面而停手报告 —— 一次无谓的停摆 | 两条路：①改派单模板，把冻结面自证命令换成不含受保护路径字面量的全量 `git diff --stat` + `git status --short`；②给守卫的 `PROT_PATHS` 匹配加一条例外：`git diff` / `git log` / `git show` 这类纯只读子命令不拦读取位置。**倾向 ①**（零风险，改的是文档不是守卫）；② 更根治但要动 `scripts/guard_bash.py`，那是全局禁改面，且放宽守卫的判定面本身要谨慎 —— 守卫宁可误伤不可漏放 |

## task-X2

replan 第三条触发线落地时发现，均**不在本轨可改面内**或**超出派单范围**，
按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | 四象限里 `retriable=False + failed`（终态失败）与未知码这两种情形，派单写的处置是「转人工或改单」，但**当前实现只做到「不重规划」**：闸判 blocker -> 普通返工 -> 重试到 `max_attempts` 耗尽 -> `FAILED("返工次数耗尽")`，中间没有任何一步停在 BLOCKED 等人 | 收敛是对的（不自旋、不假绿），但**收敛的姿势不对**：一笔「交易不存在」的退款会被原样重发两次才失败，而这两次重发从第一次就注定不可能成功。演示时看到的是三条一样的失败日志，不是一次干净的转人工 | 要给 rework 分支加第三个出口（网关处置为 human 时直接 `AWAITING_REVIEW->BLOCKED("gateway_needs_human")`），落点在 `on_review_verdict`。这会改变既有 rework 语义、影响所有闸，超出「增第三条触发线」的范围，本轨不当场做。**建议与 `## task-W3` 第 3 条（闸的 plan 级判据）一并想清楚再动** |
| 2026-08-29 | P4 | `flows/scenario_7.py` 的模块 docstring「已知缺口」那一段（`scenario_7.py:41-48`）现在**过期了**：它写着 R2 的 replan 段「没有落在本文件里」「已记 `## task-W6`」。机制现已落地并有 19 条测试守着，但**没有任何场景把它演出来** —— 场景 7 走的仍是 `effect_risk=H` 那条 HITL 入口 | Demo 分镜 02:30 要的是「网关返可重试错误码 -> 换渠道 -> 达上限 -> 转人工」这条**可见**的链路。现在它只在 `test_replan_gateway.py` 里跑得通，评委在屏幕上看不到。`scenario_7.py` 是本轨只读面，且它的收口断言是 W-6 的验收，一个字不许变 | 两条路：①在场景 7 之前插一段叙事，让付款任务先撞一次 `40005`（`retriable=True + failed`）触发换渠道，再撞 `ACQ.SYSTEM_ERROR` 走现有收口 —— 收口断言完全不用动；②新开一个场景专演 R2。**建议 ①**，成本是给 `MockGateway` 的 script 多注一个码。由持有 `flows/**` 的一轨做 |
| 2026-08-29 | P4 | `docs/` 与 `README.md` 里多处写着「六道闸」（`gate.py` 的 docstring 本轨已同步改成七道，文档侧没有） | 文档与代码对不上。评委按文档数闸会少一道，而少的那一道恰好是本轮新增的 R2 触发线 | `docs/*` 与 `scripts/gen_docs.py` 是 W-5 的面。**W-5 合并时重跑一次 `gen_docs.py` 即可**；若文档里的「六道闸」是手写而非生成，需手工过一遍 |
| 2026-08-29 | P4 | `maos/agents/refund/payment_agent.py::_open_questions`（`payment_agent.py:114-130`）与第七道闸对同一份回执各判一次：前者按 `needs_compensation` 分「网关明确失败 / 轮询到顶」两句话，后者按码表四象限判处置 | 两处**当前都对**，但判据不同源 —— 码表将来加一条码或改一个 `outcome`，只有闸会跟着变，Agent 那句措辞会悄悄漂。这类漂没有症状：日志照样正常，只是那句话开始说错 | `maos/agents/refund/**` 不在本轨可改面内。最小改法是让 `_open_questions` 也走 `gateway_codes` 的四象限判据（闸已经把它抽成 `ReviewerGate._gateway_finding`，可直接复用其 `disposition`）。**不急，但别拖到码表下一次变更之后** |
| 2026-08-29 | P4 | 本文件 `## task-W6` 第 1 条把触发条件写成「`retriable` 为真」 | 这个口径**不完整且踩铁律 8**：`retriable=True + outcome=unknown` 的两条码（`20000` / `ACQ.SYSTEM_ERROR`）按它会被判成可重试并直接重发，而那正是「重发造成第二笔退款」的那一格。本轨已按 `gateway_codes.py:23-44` 的四象限原文实现，与那条 BACKLOG 的字面口径**不一致** | 那条 BACKLOG 项已由本轨落地，可结掉；结的时候请一并把口径改成四象限，别让下一个人照着「retriable 为真」再写一遍。四象限的机器化版本在 `test_replan_gateway.py::test_every_official_code_lands_in_exactly_one_quadrant` |

## task-X3

RAG 检索质量收口时发现、按铁律 4 与派单边界**不当场改**的五条
（分支 `task/x3-rag-quality`，基线 `4a70cb0`）。前两条是同一个决策的两半：
知识层与存储层的主键口径没有对齐，而两边各自的文档都写得对。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **F-2 约定源表主键列名固定为 `id`，而 `kb_doc` 的主键是 `(tenant_id, doc_id)`、影子表 `kb_doc_fts` 存的也是 `doc_id`。** 本轨实测：真 `SqliteStorePort` 在这份 schema 上两条通道**都抛** `LookupError: no such column: id`（`fts_search("kb_doc","body",...)` 与 `vector_search("kb_doc","embedding",...)` 各一次） | 检索器接 StorePort 的那条分支即使被走到也走不通。改动前每次检索都抛一次再吞掉，只留一条 `log.warning`，日志被刷满而看不出通道一直没通；本轨已改成探一次记一次、只告警一次，但**分叉本身没解**。PG 后端接上时这条会原样重现，且届时「召回悄悄变少」比现在更难查 | 需人类裁决，三条路：①给 `kb_doc` / `kb_doc_fts` 各加一个 `id` 列（`kb/schema.sql` 是 W-3 的面，且多租户主键压不进单列 `id`，要想清楚 `id` 填什么）；②放宽 F-2 的「列名固定 `id`」为「由调用方指定主键列名」——**要动 F-2 五个签名，属冻结面**；③在知识层与存储层之间加一层视图适配（FTS5 虚表套不了视图，全文那条走不通）。本轨倾向 ①，但三条都出本轨的面。`test_real_sqlite_store_port_diverges_from_kb_schema_on_the_key_column` 已把现状钉成断言，哪天不抛了说明有人对齐了列名，那条该跟着改而不是删 |
| 2026-08-29 | P5 | **能力探测在真链路上恒不成立**：核心 `SqliteStore` 没有 `fts_search` / `vector_search`；而 `SqliteStorePort` 故意不叫 `_conn`（`sqlite_store.py` 开头写明是有意的收口），传它当 store 会让 `kb.query` 抛 TypeError。**没有任何一个真实对象同时满足两边** | `_fts_scores` / `_vector_scores` 的 StorePort 分支在缺省路径上一次都走不到。本轨用一个符合 F-2 口径的 store 把分支跑起来并断言「命中集合与次序逐条一致」，但那是测试造的对象，不是链路上的对象 | 与上一条一并定。真要让主链路走 StorePort，得先决定检索器拿到的 `store` 到底是核心 Store 还是 Port —— 而 `kb/__init__.py` 的 `execute` / `query` / `ensure_schema` 全部按核心 Store 的 `_conn` 写，换过去是知识层整层的接线改动，与 `## task-W2` 第 3 条（`executescript` 在 F-2 里没有落点）是同一个决策 |
| 2026-08-29 | P5 | `scripts/verify.py` 第 7 项要求库里**每一条** `history_case` 的 `source_case_id` 都回查得到一条 `biz_status='settled'` 的本库 `refund_case` | 这条规则挡住了「**外部导入的历史知识**」进任何证据库 —— 导入的知识按定义没有本库记录，而给它造一条就是伪造证据（铁律 3）。本轨因此只把 W-1 的 16 条政策投影进 R5 的库，24 条历史案例改由 `maos/tests/test_kb_corpus.py` 全量装载与断言。规则本身是对的（它守的是「RAG 命中不是编的」），但判据太窄 | `scripts/verify.py` 是 X-4 的面。建议把判据从「每条 history_case 都要能回查」放宽成「**本库晋升出来的** history_case 都要能回查」——区分标志现成：本库晋升的 `source_case_id` 在 `refund_case` 里有行，导入的没有。放宽时要保住「一条都回查不到」这种全空情形仍判负，否则守卫会退化成空转 |
| 2026-08-29 | P5 | `scripts/make_evidence.py` **不产 `scenario-R5`** —— 它按 `maos.main.ALL_SCENARIOS` 跑 1-7，R5 的证据束由 `maos/kb/experiment.py::write_evidence()` 单独落盘（入口 `python3 -m maos.kb.experiment`） | 只跑 `make_evidence.py --out <dir>` 再 `verify.py --evidence <dir>`，结果是 **5/5 PASS + 2 SKIP**（kb-hit / history-case 判 SKIP）而不是 7/7 —— 而 SKIP 不计入分子，屏幕上不像出了问题。任何按派单验收段照抄这两条命令的人都会撞一次，本轨撞了一次 | 两条修法：①`make_evidence.py` 的场景循环之后补一句调 `experiment.write_evidence(out_root)`（`scripts/` 是 X-4 的面）；②退而求其次，把「R5 要单独跑一条」写进 `docs/EXECUTION.md` 的验收段与各派单的 §5，与 `## task-W2` 第 6 条（`--out` 到临时目录）并列。**演示前建议做 ①** |
| 2026-08-29 | P5 | W-1 语料的 `workflow_version` 是字符串（`"1.0.0"` / `"1.1.0"`），而 `kb/schema.sql` 里 `kb_doc.workflow_version` 声明的是 `INTEGER`；`experiment.promote_history_case` 落的是整数 `1` | SQLite 是动态类型，字符串照落不报错。但阶段一的预过滤按 `workflow_version = ?` 严格相等比对 —— 查询传整数 `1` 时，语料里那 17 条 `"1.0.0"` 一条都匹配不上，而症状只是「候选集少了些」。R5 的检索上下文当前不带这一维，所以现在不触发 | 语料在 `scenarios/refund/**`（W-1 的面），列声明在 `kb/schema.sql`（W-3 的面），两边都不在本轨可改面内。建议统一成字符串并把列声明改成 `TEXT`（版本号本来就是 `1.0.0` 这种形状，塞不进 INTEGER），或统一成整数。**谁先动这两个面谁一并定**；`test_kb_corpus.py` 的漏斗断言会在语料改动时变红，届时能看见 |

## task-X4

拆最后两处假绿（分支 `task/x4-antifake`，基线 `4a70cb0`）：沙箱静默降级 + 审计措辞错判。
本轨把「这一次到底在哪儿跑的」做成可机读字段（`sandbox_mode` / `degraded_reason`），
经 `obs/trace.py` 进 trace.json、由 `scripts/verify.py` 印出来。以下四条是本轨发现、
按铁律 4 不当场改的账 —— 四条都落在本轨独占文件之外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P6 | **执行路径在装配层被丢掉**。`sandbox_pytest_run` 现在返回 `sandbox_mode` / `degraded_reason`，但 `agents/testing.py::make_test_report` 按固定六键装配报告，`flows/common.py::verify_patch_in_sandbox` 也只逐字段搬那六个 —— 两个字段都到不了 artifact | 真实证据束里**每一份** test_report 都缺 `sandbox_mode`，`verify.py` 第 4 项因此对 scenario-1/2/3/5 各印一条「执行路径不可审计」。降级 warn 那条分支在真实证据上永远触发不了（只在单测里验得到）。注意 `summary` 是搬得过去的：走 `test.verify` skill 的那份报告已经带上了「沙箱回归（容器隔离）：5 过 / 0 挂 / 0 错」，而 `patch_verifier` 那条路径不传 summary，仍是 `make_test_report` 的默认文案 | 最小改法是在 `make_test_report` 加两个可选参数并在 `_normalize` / `verify_patch_in_sandbox` 里透传（两处各一行）。`agents/**` 与 `flows/**` 都不在本轨可改面内。**做掉之后那 4 条「不可审计」warn 会自动消失**，降级 warn 才开始在真实证据上有话可说 |
| 2026-08-29 | P6 | `ControlPlane.create_plan` 自己生成 plan_id、不接受外部传入，所以规划期（建 Plan 之前）发生的调用没有 plan_id 可写，只能落空串 | `scenario-R5` 的 `KbRetrieved` / `SkillInvoked` 两行、`scenario_5.py` 的一行，按 plan 查 event_log 查不到，`verify.py` 第 4 项印 warn。本轨只改了措辞（说清是「建 Plan 之前的调用」而不是「事件丢了」），**没碰根因**，warn 条数一条没变 | 与 `## task-W3` 第 2 条、`## task-omega` 那条是同一笔账。给这类调用一个正式归属：bootstrap plan_id，或让 `create_plan` 接受预生成 id。`maos/core/**` 是本轨禁改面 |
| 2026-08-29 | P6 | **`## task-W7` 第 2 条建议的探测形式在本机不成立**。该条建议改用 `docker image inspect --type=image <IMAGE>:latest`，但 `--type` 是老式 `docker inspect` 的参数，`docker image inspect` 子命令没有它：本机 Docker 29.6.1 实测连三次 **exit=125 `unknown flag: --type`** | 照该建议改会让探测**恒定失败**，沙箱被永久钉死在降级路径 —— 比原 bug 更糟，且同样无症状。本轨改用 `docker image inspect <IMAGE>:latest`（只补 tag，实测 3/3 exit=0） | 已在本轨修掉，此条只为**作废 W-7 那半句建议**留痕，免得后来者照着改回去。真实根因不是「瞬时失败」而是**裸仓库名的 tag 解析**：裸名 3/3 exit=1，带 `:latest` 3/3 exit=0，同一台机器同一时刻 |
| 2026-08-29 | P6 | `scripts/make_evidence.py` 只跑 `maos.main.ALL_SCENARIOS`（1-7），**不产 `scenario-R5`**；而 R5 是唯一带 `kb_doc` 表的场景，`verify.py` 第 5/7 两项全靠它 | 只跑 `make_evidence.py` 再 `verify.py`，拿到的是 **5/5 PASS + 2 SKIP**，不是各处文档里写的 7/7。而补 R5 的官方入口 `python3 -m maos.kb.experiment` 调的是 `write_evidence()` 无参形式，**默认写进仓库 `evidence/`** —— 想落到仓库外必须自己调 `write_evidence('<仓库外目录>')` | 给 `experiment.py` 的 `__main__` 补一个 `--out` 参数（一行 argparse），或让 `make_evidence.py --scenarios` 认得 `R5`。`maos/kb/**` 与 `scripts/make_evidence.py` 都不在本轨可改面内。**在那之前，任何"复现 7/7"的说明都必须把这两步写全**，否则照做的人拿到 5/5 会以为是回归 |

## task-W5

写材料时撞见、按铁律 4 与派单边界**不当场处理**的六条（分支 `task/w5-docs`，基线 `4a70cb0`）。
本轨只写文档与 `scripts/gen_docs.py`，六条全部落在可改面之外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | `maos/main.py` 的 `DEFAULT_SCENARIOS = (1,…,6)` 不含 7，且 module docstring 仍写「场景 7 **未落地**，`--scenario 7` 会 ModuleNotFoundError」 | 与 `## task-W6` 第 2 条同源，本轨从 README 侧再撞一次：写「快速开始」时无法写成「一条命令跑全部七场景」，只能如实写「`run.py` 无参跑 1–6，失败路径必须 `--scenario 7` 单跑」。而失败路径是本仓库**唯一**一条「业务确实没成功」的演示路径，评委只跑 `run.py` 会整条漏掉，且没有任何提示 | `main.py` 是派单级冻结面，需人类解冻。**在录 Demo 与提交前做掉**；做掉后 README §4 那个警示框和 `docs/demo-script.md` 的对应说明要一并撤 |
| 2026-08-29 | P7 | **新克隆按直觉操作（`make_evidence.py` → `verify.py`）必然 exit=2，而错误提示会让人原地打转。** `make_evidence.py` 只产 `scenario-1..7`（`ALL_SCENARIOS` 到 7 为止），`scenario-R5` 归 `python3 -m maos.kb.experiment` 单产；而 `evidence/scenario-R5/` 这个**目录是入库的、库不入库**，于是核验器把它当一个 case 读、找不到 `maos.db`，抛 `缺数据库: evidence/scenario-R5/maos.db（先跑 python3 scripts/make_evidence.py）`——**按这句提示做，产不出 R5 的库，再跑再报同一句** | 本轨实测三种组合：①什么都不跑 → `缺数据库: scenario-1/maos.db`，exit=2；②只跑 `make_evidence.py` → `缺数据库: scenario-R5/maos.db`，exit=2（提示指向一条解决不了它的命令）；③两条都跑 → **7/7 PASS, exit=0**。这是评委最可能踩的一脚，且踩下去看不出该往哪走。README §3 已写明三条命令与这个坑，但**脚本自身零提示** | 两个小改，都出本轨的面（`scripts/` 归 Ω）：①`verify.py` 的 `缺数据库` 报错按目录名分支——`scenario-R5` 缺库时提示 `python3 -m maos.kb.experiment`，其余提示 `make_evidence.py`；②`make_evidence.py` 结尾加一行「RAG 对照证据另跑 `python3 -m maos.kb.experiment`」。**①必须做**，它出现在评委正看着的那一屏上。另可考虑让 `make_evidence.py` 直接把 R5 纳入（它已经在复用 `write_bundle`），那样三条命令收敛成两条 |
| 2026-08-29 | P7 | `maos/agents/manager.py:33` 的 `ManagerAgent` **没有 `@register`**，`AGENT_POOL` 实际只有 9 个角色，而手册与派单都写「十角色（软件域 6 + 退款域 4）」 | 不是 bug（Manager 是规划者，由流程层直接构造并调 `plan()`，不接 `TaskAssignment`），但两个数字对不上，材料里很容易被读成「漏了一个」。本轨的处理是让 `gen_docs.py` 如实印出「10 个类 / 9 个注册」并解释差在哪 —— 治标 | 若希望两个数字一致，两条路：①给 Manager 加 `@register` 并让 `WorkerRuntime` 跳过不接派单的角色（动 `maos/agents/**` 与 `runtime/**`）；②统一措辞，手册与 PPT 里「十角色」一律改成「10 个 Agent 身份，其中 9 个可被派单」。**建议 ②**，成本只有措辞 |
| 2026-08-29 | P7 | `docs/hiclaw-probe.md` 不存在，而 `docs/EXECUTION.md:488` 与 `docs/phases/phase-3.md:25` 两处都要求「补一行记录最终选了哪档、为什么」 | 「最终选了 C 档、理由是时间盒不是技术受限」这条记录一直没有落盘。本轨已把它写进 `docs/agentteams-mapping.md` 的「最终采用哪一档」一节（含当前真实状态：真房间未接通、`_NioChannel` 未经实测） | 两条路：①补一份 `docs/hiclaw-probe.md`（手册指名的文件名）；②认可 `agentteams-mapping.md` 已经承载了这条记录，在手册那两处标注改指向。**建议 ②**，别为了对齐一个文件名再写一份会分叉的文档 |
| 2026-08-29 | P7 | 「评审四维」的官方名称与权重、Demo 视频的官方规格（时长上限 / 分辨率 / 格式 / 大小 / 字幕），**仓库任何文件里都没有** | `docs/submission-checklist.md` 要求「PPT 逐页 ↔ 评审四维对照」，本轨拒绝编造，改按手册附 C 的十三条评委要求组织对照表，并把两处标成「待确认」 | **人类照官方通知补**。补齐后按四维重排 `docs/submission-checklist.md` 的 B 段表格，视频段把「待确认」换成实数 |
| 2026-08-29 | P7 | 证据文件首行是 `# generated at ...` 注释，直接 `json.load()` 会抛 `JSONDecodeError`；跳过首行的读取辅助 `load_evidence_json` 只存在于 `scripts/verify.py` 内部，未对外导出 | 评委若自己写脚本读 `evidence/*.json`（这恰恰是「可核验」鼓励他做的事），第一步就会炸，而报错信息指向 JSON 语法，看不出是首行注释 | 低优先。可选修法：`make_evidence.py` 里把该函数提成公开工具并在 README 证据索引段点名，或在 `INDEX.json` 的说明里写一句。本轨已在 README §6 与 `demo-script.md` 各写了一句提示，够用 |

## integrate-round-4

X 轮四轨 + W-5 合并后的整体验收发现三条，均**不在整合轮可改面内**
（派单 §2：整合轮只做合并 + 验证 + 刷过期事实，业务逻辑问题交下一轮），
按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`docs/domain-portability.md` 的「领域无关」论证，其证据区间 `90251b3..HEAD` 现在混进了非退款域的改动。** 该文档用「退款域上线前后 `git diff` 逐面为零」来论证内核领域无关，但本轮 X-2 给 `maos/core/control_plane.py` 加了 +46/−2（网关码四象限），X-4 给 `maos/tools/sandbox.py` 加了 +111/−13（降级可见化）—— 两者都**不是退款域上线带来的**，却落在同一个区间里 | 论证的**结论没塌**：`maos/contracts/` 仍严格为零，两条 AST/import 守卫（`test_runtime_and_core_do_not_import_refund_domain` / `test_kernel_does_not_know_the_refund_domain`）本轮实跑 2 passed，「内核不认识退款域」这件事仍被机器钉住。塌的是**数字的读法**：表格里 `maos/core/` 那一格从「（空，零改动）」变成非零，读者会以为是退款域把它改的 | 本轮已按真实值刷了数字，并在表格与「不是零 —— 如实说清楚」小节里点明这两笔的出处（整合轮 4 / X-2、X-4）。但更干净的做法是**把论证的区间端点从「当前主干」换成「退款域上线那一刻的 sha」**，让区间只包住退款域，非退款域的改动另起一段说。这要重新选 sha 并重跑全表数字，属文档结构调整，交下一轮 |
| 2026-08-29 | P5 | **派单 §4 第四步写的「`python3 run.py` 重新生成 `evidence/`」与实际不符** —— `run.py` 跑完 `git status --porcelain` 只有 1 行（gen_docs 的产物），`evidence/` 一个文件都没动。真正产证据的是 README ①②③：`scripts/make_evidence.py`（产 scenario-1..7）+ `python3 -m maos.kb.experiment`（产 scenario-R5）+ `scripts/verify.py`（校验） | 照派单字面执行的会话会以为证据束已按新 HEAD 重跑，实际 `evidence/` 里仍是旧代码的产物 —— 正是派单自己要防的「假绿」。本轮已改跑实际生成器，见 DECISIONS `## integrate-round-4` 第 1 条 | 下一轮派单模板里把第四步的命令换成 `make_evidence.py` + `maos.kb.experiment` + `verify.py` 三条。README §3 的 ①②③ 就是正确版本，直接抄 |
| 2026-08-29 | P4 | **`CLAUDE.md:75` 的常用命令注释仍写 `python3 run.py  # 场景 1-6 端到端`**，X-1 合并后实际跑 1-7 | 与 `## task-X1` 第 3 条是同一条（那条由 X-1 记下并建议「编排侧收口时一行改掉」）。`CLAUDE.md` 每个会话自动加载，是所有会话看到的第一份事实；留着它，下一个会话会照「1-6」去复核 `run.py` 输出，把多出来的场景 7 当成异常 | `CLAUDE.md` **不在整合轮派单的可改面内**（派单 §4 第三步只列 README.md 与 docs/\*.md），故本轮未改。README / demo-script / submission-checklist / architecture / domain-portability 里的同类措辞本轮已全部刷成 1-7，只剩 `CLAUDE.md` 这一处。**请人类一行改掉**，或在下一轮派单里把它列进可改面 |

## task-Y1

接通执行路径时撞见、按铁律 4 与派单 §7 边界**不当场处理**的四条
（分支 `task/y1-exec-path`，基线 `42822fc`）。本轨可改面只有
`maos/agents/testing.py` + `maos/flows/common.py` + 本轨新测试，四条全在面外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P6 | **`sandbox_mode="not-run"` 现在承载了两种语义**：`tools/sandbox.py::_tool_error_report` 用它表示「沙箱进去了、但没跑成」（workdir 不存在、junit 没产出、退出码 ≥2），本轨给 `seed_scripted_report` 的预置件也用它表示「压根没调用过沙箱」。`obs/trace.py` 与 `verify.py` 都不区分这两者 | 目前**不会误判**：两者都不进 `degraded` / `unrecorded` 计数，且各自的 `degraded_reason` 写清了是哪一种（预置件那句点名了 `seed_scripted_report`）。但取值语义混着，下一个人加判据时容易按其中一种理解、把另一种一起圈进去 | 两条路：①给预置件一个独立取值（如 `scripted`），同时在 `PYTEST_RUN_PORT.returns_schema` 与 `trace.py` 的取值表里登记；②保持 `not-run`，在 `returns_schema` 的那一行补一句「含未经沙箱的场景预置件」。**建议 ②**，成本只有一句注释，且不给证据里再加一个新词。`maos/tools/sandbox.py`（X-4 已完工面）与 `maos/obs/trace.py` 都不在本轨可改面 |
| 2026-08-29 | P6 | **`test.verify` 的 `output_schema` 与它实际返回的键对不上**：`skills/builtin/test_verify.py:37-44` 只列了六个键，而它直接返回 `sandbox_pytest_run` 的产物，实际带 `summary` / `sandbox_mode` / `degraded_reason` 三个额外键（`PYTEST_RUN_PORT.returns_schema` 已如实登记了这三个） | 契约自述比实际少三个键。本轨的透传是照 ToolPort 的 `returns_schema` 做的，跑得通；但读 skill 契约的人会以为执行路径到不了 Testing Agent 这一层，而它其实一直在 | 一处三行的补齐（`output_schema` 加三个键），顺带把模块 docstring 的「IO 契约（附录 B-3，逐字段）」那段一起刷。`maos/skills/**` 不在本轨可改面。**建议在下一轮随手做掉** —— 契约文档与实际返回分叉，正是 C-7 当初要收敛的那类问题 |
| 2026-08-29 | P6 | **仓库里已入库的 `evidence/` 38 个文件仍是本轨改动前的产物**，每一份 `test_report` 都缺 `sandbox_mode` | 直接跑 `python3 scripts/verify.py`（不带 `--out` 重跑）仍会印出那 4 条「执行路径不可审计」warn。本轨按派单 §5 把证据束落到仓库外（`/tmp/ev-y1`）验证，**没有改写 `evidence/**`**（出处 `## task-W2` 第 6 条） | **整合轮必须按合并后的 HEAD 重跑一次证据束并入库**，否则仓库里的证据与代码对不上，评委看到的仍是「不可审计」。重跑命令见 README §3 的 ①②③（`make_evidence.py` + `python3 -m maos.kb.experiment` + `verify.py`）。与 `## integrate-round-4` 第 2 条是同一笔账 |
| 2026-08-29 | P6 | scenario-3/5 预置件的 `summary` 仍写「沙箱回归：1 过 0 挂 0 错」/「支付回调回归：2 过 0 挂」，读起来像是跑出来的 | 执行路径这一层已经说清了（`sandbox_mode=not-run` + 点名 `seed_scripted_report` 的 reason），但只读 `summary` 那一行的人仍会误读成实跑。措辞归 `flows/scenario_3.py:17` 与 `flows/scenario_5.py:117`，两份都在派单 §7 的禁改面（Y-2 / Y-4 的轨） | 低优先，两处各改一个词（如「场景预置回归（未跑沙箱）：1 过 0 挂 0 错」）。**交给持有 `flows/scenario_*.py` 的那一轨顺手做**，或整合轮收口时一并改。不建议由 `seed_scripted_report` 去改写调用方的 summary —— 那是替别人重写措辞，越界 |

## task-Y2

场景 6 播知识 + 规划期 plan_id 归属时发现、按铁律 4 与派单边界**不当场处理**的四条
（分支 `task/y2-kb-provenance`，基线 `42822fc`）。第 1 条是本轨改动面内的**真缺口**
但超出派单范围，第 2 条是派单点名「撞上就一并定」而实测**不触发**的那一条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **政策投影把 `channel_id` 一律落 NULL，而语料里有 4 条不是通配的规则**。`kb/experiment.py:promote_policy_rule` 的注释写着「语料里这些规则的 `channel_scope` / `sku_scope` 都是通配」——实测**不成立**：`scenarios/refund/policy/policy_rules.json` 里 `AS-004`（两个租户各 v1/v2，共 4 条「经销渠道差异」）的 `channel_scope` 是 `ch-dealer`，投影后在 `kb_doc` 里变成 `channel_id IS NULL` | 「文档侧 NULL = 通配」是阶段一的口径，于是**一条经销商专属政策对任何渠道的查询都是候选**。R5 当前就在踩：它的查询是 `channel_id="ch-online"`，候选集 5 条里就有 `kb-policy-tnt-mfg-a-AS-004-v1`。症状是「召回了一条不该出现在这个渠道的政策」——不报错，且在小库上分数还不低。这是**投影层的口径错误**，不是检索器的问题 | 修法一行：投影时 `channel_id = None if row["channel_scope"] == "*" else row["channel_scope"]`，`sku_scope` 同理。**本轨不改**：超出派单 §4 的两件事，且会把 R5 的 `candidate_count` 从 5 改成 4，动到证据束与 `test_kb_corpus.py` 的漏斗断言——属于要连着证据一起重跑并说明的改动，不该搭在本轨的车上。**建议下一次动 `kb/**` 的轨一并定**，改完把 R5 的候选集变化写进回执 |
| 2026-08-29 | P5 | **`workflow_version` 的类型分叉仍在**（`## task-X3` 第 5 条）：`kb/schema.sql:18` 声明 INTEGER，W-1 语料里是 `"1.0.0"` / `"1.1.0"` 字符串。派单 §4.3 要求「撞上就一并定」 | **本轨实测不触发，故未改**。场景 6 的检索上下文是 `tenant_id / biz_type / channel_id / sku`（+keyword），**不带这一维**；政策投影落的 `workflow_version` 一律是 NULL。阶段一按 `workflow_version = ?` 严格相等比对的那条分支在这条路径上根本走不到，改不改列类型对场景 6 一样 | 留给**第一个真的把 `workflow_version` 放进检索上下文**的那一轨，或第一个投影出非 NULL 值的那一轨。届时统一成字符串、列声明改 `TEXT`（版本号本来就是 `1.0.0` 这种形状，塞不进 INTEGER）。⚠️ `kb/schema.sql` 全是 `CREATE TABLE IF NOT EXISTS`、**没有迁移路径**，改列对已存在的库静默无效（`## task-R1` 第 5 条）。本轨已留守卫：`test_kb_provenance.py::test_scenario_6_retrieval_query_carries_no_workflow_version` 在这一维被加进来或投影出非 NULL 值时会红 |
| 2026-08-29 | P5 | **`## task-W3` 第 5 条的预期值「`candidate_count` 会从 1 变成几十」与实测不符**。接上 W-1 语料后，R5 实测 5、场景 6 实测 3 | 照那条账去核对的人会以为链路仍有问题（「怎么才 3 条」），从而去调检索参数或放宽过滤——而 3 是正确答案：阶段一是**硬约束**，语料 40 条里能进单个租户候选集的本来就只有个位数。把「几十」当判据，等于鼓励下一个人去把过滤放松 | 纯账本措辞。**建议下一轮整合时把 `## task-W3` 第 5 条的「几十」改成实测值并注明口径**（候选集大小取决于查询带了几维，不取决于库存）。本轨不改别人的 BACKLOG 小节 |
| 2026-08-29 | P5 | **派单模板里「`trace-tree` 分母会上涨」这条预期不成立**。该项的分母 = span 树数 × 1 + case 数 × 1（`scripts/verify.py:328-340`：每棵树一次无孤儿无环判定，每个 case 一次「与库重放逐字节一致」），游离事件从来不进分母，只走 `_warn_stray_events` 出 warn | 本轨把 3 条游离事件并进了**已存在**的树，没有新增树，分母如实不动（18/18）。照派单预期去核对的会话会以为接线没生效，进而去改核验器让分母动起来——那正是派单 §2 明令禁止的 | 下一轮刷派单模板时把这条期望改成「`warn:「不在任何一棵树内」3 → 0` + 各 case `trace.json` 的 `summary.stray_event_count` 全 0」。这两个数才是游离事件的直接量度，且都能一条命令数出来 |

## task-Y3

修复复现路径时发现四条，均在本轨白名单（`scripts/make_evidence.py`、`scripts/verify.py`、
`maos/tests/test_repro_path.py`）之外，按铁律 4 记账不当场改。
第 1 条是**本轨改动直接造成的过期事实**，下一轮必刷。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P6 | **R5 并进 `make_evidence.py` 之后，三份文档里写死的「三条命令」与「7 场景落盘」都过期了。** `README.md:89-92` 的 ①②③、`docs/submission-checklist.md:22-24`、`docs/demo-script.md:17-19` 都还把 `python3 -m maos.kb.experiment` 列成必跑的一步；其中 checklist 第 22 行的勾选项写死「□ 7 场景落盘，0 场景缺模块」，而现在这条命令印的是「完成：**8** 场景落盘，0 场景缺模块」 | 命令本身没坏（`maos.kb.experiment` 幂等，重跑一次只是重产 `scenario-R5`），但两处会咬人：①照 checklist 逐条打勾的人会看到 7 与 8 对不上，很容易读成回归；②演示彩排会多跑一条已经不需要的命令，而本轨改这一处的**全部意义**就是把复现路径从三条收敛成两条 —— 文档不刷，收敛就只存在于代码里 | **下一个整合轮刷**，三处一起：README §3 的 ②合进①（保留 R5 单跑作为「只想看对照实验」的旁路，措辞降级成可选）、checklist 的「7 场景」改「8 场景（含 R5）」并去掉 ②那一行的必跑语气、demo-script §开场自检同理。三份都在整合轮的可改面内 |
| 2026-08-29 | P6 | **空转判 SKIP 之后，退出码仍然是 0。** `render()` 的 `return 1 if failed else 0` 只看 FAIL，而 README `:92` 与 `docs/demo-script.md` 教的判读方式恰恰是 `echo "verify exit=$?"` | 屏幕上「只跑了一半」已经看得见（`5/5 PASS, 2 SKIP`），但**脚本化判读看不见**：任何拿 `$?` 当门禁的用法（CI、彩排脚本、评委照 README 敲的那一行）在缺 R5 时仍会拿到 0。本轨没动退出码语义 —— 那会连带改掉「上游能力没落地判 SKIP」这条既有纪律的含义（P5 之前 kb 层不存在时，SKIP + exit 0 是对的），属于超出派单范围的判据改动 | 建议给 `verify.py` 加 `--strict`：让 SKIP 也计非零，README / checklist / demo-script 里那条 `echo "verify exit=$?"` 改成带 `--strict` 的版本。区分「本来就没有这个能力」与「有能力但这一轮没跑」需要一个新维度，`--strict` 是成本最低的那个 |
| 2026-08-29 | P6 | **其余五项没有分母为 0 的守卫，仍可能印 `0/0 PASS`。** 本轨按派单只给第 5、7 项（RAG 两项）加了空转判定。最接近的是第 6 项 `business-outcome`：`maos/tests/test_trace_evidence.py::test_6_non_terminal_plan_is_not_judged` 明确断言「Plan 停在 RUNNING → `PASS` 且 `total == 0`」 | 第 6 项那条断言现在是**对的**（非终态本来就不在判据内，不是没跑），所以不能照搬本轨的修法一刀切。但一份「所有 Plan 都停在非终态」的证据束，第 6 项会印 `0/0 PASS`，与本轨消灭的那个形态一模一样。第 1-4 项同理（一个空库能让 `hash-integrity` 印 `0/0 PASS`） | 不急。真要做，得先把「分母为 0」拆成两种：**没素材**（判 SKIP）与**素材不适用本判据**（当前 PASS 0/0 的合法形态），逐项定性。建议等有第二个真实踩坑案例再动 —— 现在动等于凭想象给五项各造一套语义 |
| 2026-08-29 | P6 | **第 7 项放宽后留下一个张力：整轮只装导入知识的证据束仍判负。** 判定单位取的是整轮合计（见 DECISIONS `## task-Y3` 第 2 条）：有 `history_case` 文档、却一条都没进判据 → 判负 | 当前形态不受影响：`scenario-R5` 的那 1 条 `history_case` 是本库晋升的（`case-r5-hist` → `settled`），A 组实测 `history-case 1/1`。真要出问题得同时满足「把 W-1 那 24 条历史案例投影进证据库」且「整轮没有任何本库晋升的 case」—— 那时守卫会拦下一份其实合法的束 | 等真要做「导入历史案例进证据库」时再议。届时两条路：①保证同一轮里至少有 R5 这种带本库晋升的束（最省，且 R5 本来就一直在）；②给判据加一个「本轮是否存在可晋升的 case」的前置，没有则判 SKIP 而非判负。**建议 ①** |

## task-Z3

换论证区间端点时发现四条，均**不在本轨可改面内**（本轨只许动
`docs/domain-portability.md` 与两份账本），按铁律 4 记账不当场改。

> ✅ 本轨已收口 `## integrate-round-4` 第 1 条（「把论证的区间端点从当前主干换成
> 退款域上线那一刻的 sha」）。区间已换成 A `90251b3..4a70cb0` + B `4a70cb0..42822fc`，
> 全表九面按两个区间各重跑一次，`maos/core/` 在区间 A 下**实测为真零**，
> 该条连同它引出的文件内自相矛盾一并消除，见 DECISIONS `## task-Z3`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`gate.py` 里那条与实际拦点不符的注释仍在**，只是行号从 W-3 记账时的 `363` 漂到了 `454`（第七道闸插在它前面）。注释写「没检索到历史案例 → 计划里漏排财务复核 → 在这里被拦下」，实测漏排时闸没有可判的对象 | 与 `## task-W3` 第 2 条是**同一个坑**，两轮过去没修。本轨在 `domain-portability.md` §5 如实写明它还在、并标了新行号，但注释本身没动 —— `maos/runtime/` 是本轨禁改面 | 修法仍是 W-3 给的两条路（①改注释承认这道闸守的是「带了金额却交不出凭据」；②给闸加 plan 级判据）。**下一次动 `runtime/gate.py` 的一轨顺手改掉**。⚠️ 改注释时注意：闸的 docstring 里写着「不许 import `maos.domain.refund`」那句是 §3 第 2 条守卫踩过的坑（AST 扫而非子串扫），改注释别把这句删了 |
| 2026-08-29 | P7 | **区间 B 的数字与主干 HEAD 绑定，没有任何机器守卫盯着它过期。** `gen_docs.py --check` 只管三份生成文档，`domain-portability.md` 是手写的，数字漂了不会红 | 这是整合轮 4 让 `maos/core/` 那一格变成假话、却拖到本轮才发现的**根因**。本轨的缓解是文末「## 待整合轮 5 回填」列了 12 行逐条复跑命令，但那仍靠人记得去跑 | 两条路：①把区间 B 的 shortstat 也纳入 `gen_docs.py` 生成（数字由脚本算，`--check` 自动守），代价是 `scripts/` 要改且生成器要能跑 git；②退一步，加一条测试断言「`domain-portability.md` 里出现的 sha 必须是 `git log` 里存在的 commit」。①更彻底，②便宜。`scripts/**` 与 `maos/tests/**` 都不在本轨可改面，**交下一轮或编排侧决定** |
| 2026-08-29 | P7 | **`docs/` 下其余手写文档可能还引用旧区间 `90251b3..df96fa8` 或「`core/` 非零」的旧读法。** 本轨只清了自己这一份，未做全库 grep（其余 docs 多为他轨本轮独占面，同时在改） | 若 README / architecture / submission-checklist 里还留着旧区间的数字，会与 `domain-portability.md` 打架 —— 评委对照两份文档会看到两套 `maos/core/` 的数 | **整合轮 5 合并后统一 grep 一次**：`grep -rn 'df96fa8\|90251b3' docs/ README.md`，把仍指旧区间的地方改成引用本文件的 §2.1／§2.3。本轮 Z-1/Z-2/Z-4/Z-5 正在各自改 `ppt-outline` / `demo-script` / `submission-checklist` / `README`，此刻 grep 出来的结果会立刻过期，故不在本轨做 |
| 2026-08-29 | P7 | **区间 A 的左端点 `90251b3` 是「P2 四轨收口」，不是「退款域第一个 commit 的父提交」**，两者之间可能夹着少量非退款域的 P2 收尾改动 | 本轨沿用派单钉死的端点未做收窄。实测九面数字里没有明显的非退款域残留（`core/` `contracts/` 双零已是最强证据），但严格说区间 A 仍可能宽了一点点 | 优先级低。若要做到极致，可用 `git log --oneline 90251b3..4a70cb0 -- maos/domain/` 找退款域首个 commit 再往前退一格。**收益很小**：`core/`／`contracts/` 已经是零，收窄区间只可能让 `agents/`／`skills/` 的数字略降，不影响任何论断。除非评委追问，否则不建议动 |

## task-Z1

方案 PPT 逐页大纲（`docs/ppt-outline.md`）落地时发现，均**不在本轨可改面内**
（本轨独占面只有 `docs/ppt-outline.md` + 两份账本），按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **「三条护栏」与代码里的 4 个 assert 函数对不上。** `README.md:259`、`maos/kb/guardrails.py` 模块 docstring（`:7` 「拆成三条可执行的断言」）、`check_all` 的 docstring（`:151` 「三条护栏一次跑完」）三处都写「三条」，但 `check_all` 实际调用 4 个：`assert_only_adds` / `assert_no_dependency_removed` / `assert_no_fact_override` / `assert_no_approval_skip` | 口径本身**没有错**——第 4 个 `assert_no_dependency_removed` 是第 1 条「只增不删」的依赖侧半条，拆成两个函数是实现选择。但材料上台后，评委若照着代码数 assert，会数出 4 个而 PPT 和 README 都说 3 条，现场解释成本很高。本轨已在 `docs/ppt-outline.md` P8b 把这层关系写明，但 `README.md` 与代码 docstring 未改（不在本轨面内） | 最小改法：在 `maos/kb/guardrails.py` 的 `check_all` docstring 里补一句「三条护栏、4 个断言函数（第 1 条拆成任务侧 + 依赖侧两半）」，`README.md:259` 同步。**持有 `maos/kb/**` 与 `README.md` 的轨顺手做**，不值得单开一轨 |
| 2026-08-29 | P7 | **`docs/demo-script.md:190`、`:193` 的 verify 输出是旧的**：写着 `business-ref 23/23`、`kb-hit 1/1`，而基线 `42822fc` 的实测值是 `33/33` 与 `4/4`（`README.md:98-106` 已是新值） | 分镜是给主讲人照着念的。台上跑出 33/33 而念词写 23/23，是当场被抓的口径不一致；且这两处正是 `## integrate-round-4` 那轮刷过期事实时漏掉的同一类 | `docs/demo-script.md` 是 **Z-2 的独占面**，Z-2 本轮正在改该文件，**大概率已一并刷掉**。整合轮 5 合并后 grep 一次 `23/23` 与 `1/1` 确认；若仍在，一行改掉 |
| 2026-08-29 | P7 | **`docs/EXECUTION.md:788-802` 的附 C 用的全是已改名的旧编号**：`scenario-R1` / `R2` / `R5`（现为 `scenario-6` / `7` / `R5`）、`Phase 5`（现为 kb 子包）、「退款域 6 Skill」（实为 7 个，见 `docs/skill-catalog.md:15-29`） | 附 C 是 v4 手册**原文保真**，按仓库纪律不该改。但它与 `README.md:249-261` 的 §8 是「同一张表的两个版本」，写材料的人若照附 C 抄落点，会指向不存在的目录。本轨已在 `docs/ppt-outline.md` 表 A 抬头显式写明「以 README §8 为准，附 C 只用来确认十三条一条不漏」 | **不改 `EXECUTION.md`**（原文保真是它存在的理由）。建议在附 C 表格上方加一行注释指向 README §8，由持有 `docs/EXECUTION.md` 的轨或人类顺手做 |
| 2026-08-29 | P7 | **`docs/ppt-outline.md` 前向引用了尚不存在的 `docs/open-questions.md` OQ-1。** 该文件是 **Z-4 的独占面**，在本轨基线 `42822fc` 上还没有 | 四维口径的「待确认」指针会悬空。本轨按派单 §2 红线写死为 `（四维口径待确认，见 docs/open-questions.md OQ-1）`，未编造任何四维名称或权重 | 整合轮 5 合并 Z-4 后，grep 一次 `open-questions.md` 确认文件存在、且四维那条的编号确实是 **OQ-1**；编号若不同，改 `docs/ppt-outline.md` 抬头与文末「待确认」两处 |
| 2026-08-29 | P7 | **P8 拆成 P8a / P8b 后，页锚总数从 14 变成 15。** 派单允许按 `P8a`/`P8b` 拆子页，但 Z-4 的自查单 B 段「PPT 页」列若只按 P1–P14 填，会没有 P8b 这一格 | 要求 8 / 11 / 13 三条落在 P8b 上。Z-4 的表若填不进 P8b，这三条要么错填到 P8a、要么留空——而派单明确要求「一条都不许留空」 | 整合轮 5 合并 Z-4 后核一次：`docs/submission-checklist.md` B 段第 8 / 11 / 13 行的「PPT 页」列应能填 `P8a` / `P8b`。**这是 Z-1 与 Z-4 唯一的接口面**，别漏 |

## task-Z2

Demo 分镜逐镜实跑（基线 `42822fc`）时撞到的四条，全部**不在本轨白名单内**
（本轨只许动 `docs/demo-script.md` 与两份账本），按铁律 4 记账不当场改。
前三条的共同点：**证据/工具的输出去向与人的直觉不符**，会让照直觉操作的人扑空。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`python3 -m maos.kb.experiment` 的三段对照结果不上屏。** `[1/3]` 准备段、`[2/3]` without_kb、`[3/3]` with_kb、`差异：delta_tasks=...`、`检索漏斗：...` 这些 print 全部被 `write_evidence()` 内部重定向进 `evidence/scenario-R5/run.log`；终端上实测只有一个空行加一行「证据束已落盘：<路径>」 | RAG 对照实验是本仓库「历史流程知识改善规划质量」的**唯一**实证，而跑完它的人在屏幕上看不到任何结论，要自己想到去 `cat run.log`。Demo 分镜原本就写着「镜头：这条命令的三段输出」—— 照着录会对着一行落盘提示讲解。本轨已在 `docs/demo-script.md` 该镜加了第二条 `cat` 命令兜住，但那是分镜侧的补丁，工具侧仍然反直觉 | `maos/kb/experiment.py` 出本轨的面。两条路：①**建议**——`write_evidence()` 用 `contextlib.redirect_stdout` 包 tee，既落 `run.log` 又照常上屏，调用方零改动；②`__main__` 末尾在落盘提示后补一行 `提示：完整对照见 evidence/scenario-R5/run.log`，成本最低但仍要人多敲一条命令。**录 Demo 前做掉 ① 最好**，做掉后分镜那一镜可收回一条命令、省 3 秒 |
| 2026-08-29 | P7 | **`python3 -m maos.kb.experiment` 没有 argparse，加任何参数（含 `--help`）都会直接开跑并写盘。** `__main__` 里就是 `write_evidence()` 然后 `sys.exit(0)`，参数一律被忽略 | 想查用法的人敲 `--help`，得到的不是用法而是一次真实的证据束重写 —— `evidence/scenario-R5/` 的 7 个文件全部变 M（首行 `# generated at ... from <sha>` 每跑必变）。在录制前置阶段手滑敲一次，工作区就脏了，而最后一镜要打 `git diff --stat`。本轨已在分镜的录制前置块写了红字警告 | 同上，出本轨的面。加一个最小 argparse：`--help` 打用法、无参照常跑。与上一条一起做掉最省事。**建议在录 Demo 前做掉**，它是「照直觉操作反而弄脏工作区」的一类坑，评委自己复现时同样会踩 |
| 2026-08-29 | P7 | **`evidence/scenario-*/result.json` 里不含任何业务裁定内容**，每个任务只有 `task_id` / `role` / `title` / `state` / `attempt` / `risk_level` / `effect_risk` 七个字段的骨架。实测 `evidence/scenario-7/result.json` 里 `AS-01` 出现 **0 次**，政策裁定产物只在同目录的 `business-objects.json` 里 | 文件名叫 `result.json`，直觉上是「这一跑的结果」，但真正的业务结果在隔壁文件。Demo 分镜原本让主讲人打开 `result.json` 讲「规则编号 + 版本」，屏幕上根本没有 —— 本轨已把该镜改成只开 `business-objects.json` 并加了红字。评委自己翻证据束时会同样扑空，且 `INDEX.json` 没有解释两个文件的分工 | 低优先，且**不建议改文件结构**（`result.json` 的骨架形态是 `verify.py` 与 `make_evidence.py` 双方约定的，动它要连带改核验器）。建议改文档：在 `evidence/INDEX.json` 的说明或 README 证据索引段加一句「`result.json` = 计划与任务骨架；业务裁定产物见 `business-objects.json`」。归 Ω 面 |
| 2026-08-29 | P7 | **`scripts/verify.py` 的 17 条 `· warn:` 会把 `RESULT: 7/7 PASS` 顶出一屏。** 实测 `trace-tree` 下 13 条、`business-outcome` 下 4 条，且每条 warn 都带一整段解释性长文（含 BACKLOG 出处），单条最长超过 100 字 | 结论行是这条命令最该被看到的一行，却在最下面。Demo 分镜原本写「镜头：七行 PASS」，实际布景对不上（本轨已加红字要求滚到 `RESULT` 行再停）。评委自己跑时第一眼看到的是一屏 warn，容易读成「这么多问题」，而实际七项全 PASS、退出码 0 | 低优先，且 Y-1 轨正在收口 `business-outcome` 那 4 条。若 Y-1 合并后 `trace-tree` 下 13 条仍在，建议给 `verify.py` 加个 `--quiet`（只打七行 + RESULT）或把 warn 明细挪到结论行之后打印。**不要为了好看去删 warn** —— warn 的内容是真的，删了才是造假 |

## task-Z4

写自查单时撞见、按铁律 4 与派单边界**不当场处理**的四条
（分支 `task/z4-checklist-seal`，基线 `42822fc`）。本轨只写 `docs/*.md`，四条全在可改面之外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **证据束的三条自查判据在当前双命令架构下无法同时满足。** ①每个文件首行 sha 不带 `-dirty`；②`INDEX.json` 的 `git_sha` == HEAD；③全量证据同一个 sha。根因：`make_evidence.py`（产 scenario-1..7）跑完**工作区就脏了**，紧接着的 `python3 -m maos.kb.experiment`（产 scenario-R5）读到脏工作区，R5 的 7 个文件首行全变 `<sha>-dirty` | 实测三种组合：正序 → R5 的 **7 个**带 dirty；反序 → **43 个**带 dirty（更糟）；「跑一条 commit 一条」→ 两批 sha 不同、`INDEX.json` 又对不上 HEAD。自查单只能认下「1..7 干净 + R5 带 dirty」并把偏差写成已知缺口 —— 但那意味着 A-2 有一条判据永远是「例外通过」 | **Y-3 的「一条命令复现全量证据」直接根治**：一次跑完则全量同一个 sha、都不带 dirty，三条判据同时成立。合并后删掉自查单 A-2 的「已知缺口」整节，第 2 条判据改回「每个文件」。若 Y-3 的收敛方式是「`make_evidence.py` 把 R5 纳入」（`## task-W5` 第 2 条也建议过），顺带解决 exit=2 那个坑 |
| 2026-08-29 | P7 | **`python3 -m maos.kb.experiment` 没有 argparse**，`--help` 不打印用法而是**直接开跑并落盘**（实测 exit=0，输出「证据束已落盘」） | 评委或新会话按惯例先敲 `--help` 探一下，会在毫无提示的情况下改写 `evidence/scenario-R5/` 七个文件、把工作区弄脏。此时若他刚跑完 `make_evidence.py` 并 commit，会莫名多出一批 dirty 证据 | 低优先但便宜：加个 `argparse` 空壳（只有 `-h`）即可，或在 module docstring 顶部写一行「本模块无参数，任何调用都会立即生成证据」。`maos/kb/**` 不在本轨可改面。**与上一条一起在 Y-3 收口时做掉最省事** |
| 2026-08-29 | P7 | **`verify.py` 的 warn 没有汇总行。** 7/7 PASS 之后直接铺 17 行 `· warn:`，既不分类也不计数，`RESULT: 7/7 PASS` 又印在最下面 | 第一次跑的人（评委、新克隆冒烟的人）看到满屏 warn，第一反应是「这东西没跑过」。自查单只能用一张手写对照表兜住（A-2 的 17 行 / 4 类表），而**手写表会随 Y-1 补洞立刻过期** —— 本轨已把它列进「待整合轮 5 回填」第 1 条 | 建议在 `verify.py` 结尾加一行汇总，形如 `WARN: 17 行 / 4 类（已知缺口，见 docs/BACKLOG.md task-X4）`，并把 warn 按类折叠。`scripts/` 归 Y 轮 / Ω。**Y-1 补完洞后若 warn 归零，这条自然消失**；若还剩，就值得做 |
| 2026-08-29 | P7 | 自查单 A-2 第 3 条要求「`INDEX.json` 里的 `git_sha` 与提交的 commit 一致」，但**它记的是「跑证据链那一刻」的 HEAD**。于是 D-0 选甲（`git add evidence/ && git commit`）之后 HEAD 前进一格，该判据必红 | 不是 bug，是这两条判据的语义天然差一个 commit。人类照自查单走「跑证据链 → commit → 核对齐」，必然在最后一步撞红一次 | 本轨的处置是在 D-0 甲选项下写明副作用与两种收敛办法（再跑一次再 commit / 接受落后一格且材料不引用它）。**更干净的修法**是让 `verify.py` 的对齐检查允许「`git_sha` == HEAD 或 HEAD 的父」，或让 `make_evidence.py` 支持 `--expect-sha`。属脚本面，交 Y 轮 / Ω |
| 2026-08-29 | P7 | **仓库里已 commit 的 `evidence/` 落后 HEAD 3 个 commit。** 实测 `42822fc`：`INDEX.json` 的 `git_sha` = `df96fa8`，而 HEAD = `42822fc`。中间三个 commit（`8c2d598` / `002e4af` / `42822fc`）**全是文档改动**，证据内容并没过期 | 更普遍的问题：**任何一次纯文档 commit 都会让证据束的 sha 对齐失效**，而为了刷一行 README 就重跑一遍证据链并不现实。于是「`git_sha` == HEAD」这条判据在日常开发中长期是红的，人会习惯性忽略它 —— 等真的因为代码变更而失效时也就看不见了 | 两条路：①**提交前重跑一次**收口（本轨已写进自查单 D-4 的 🔴 提示，是提交前必做项）；②更根本的，让对齐判据只关心**代码面**的 sha —— 比如 `git rev-parse HEAD -- maos/ scripts/` 或证据生成时记录 `git log -1 --format=%H -- maos/`，这样纯文档 commit 不再触发红灯。**建议 ②**，属脚本面（`make_evidence.py` / `verify.py`），交 Y 轮 / Ω |

## task-Z5

新克隆冒烟（分支 `task/z5-clone-smoke`，基线 `42822fc`）时撞见、**README 救不了**的两条。
本轨可改面只有 `README.md` 与本轨四份文档，`scripts/**` 与 `maos/**` 全在禁改面内。

已有账不重开：**「只跑 ①③ 会在 `缺数据库: scenario-R5/maos.db` 上原地打转」这条属
`## task-W5` 第 2 条**，本轨在全新克隆上又复现一次（照提示重跑 `make_evidence.py`
第二次、第三次，报错一字不差），结论与出路与该条完全一致，故不另开条目 —— 只补一条
实测读数供那条参考：**照提示重跑一次后工作区脏行从 43 涨到 50 再到 43+**，因为
第二次生成时工作区已不干净，证据首行的 sha 变成 `-dirty`，把原本字节稳定的
`scenario-1..4` 也一并改脏，观感上像「越修越坏」。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`evidence/*.json` 入 git 而 `evidence/*/maos.db` 不入 git，两者会失同步；一旦评委用 `git checkout -- evidence/` 把跑脏的工作区「收拾干净」，`verify.py` 立刻从 7/7 掉到 `RESULT: 3/7 PASS`。** 本轨在标准路径上实测：clone → ①②③ → 7/7 PASS（脏 50 行）→ `git checkout -- evidence/`（脏 0 行）→ 再 `verify.py` → `[FAIL] hash-integrity 4/74`、`[FAIL] business-ref 0/33`、`[FAIL] business-outcome 0/10`、`RESULT: 3/7 PASS`、exit=1。根因是 checkout 只还原了入库的 json 快照，现跑出来的新库还在原地，核验器拿新库校验旧快照 | **这是本轨发现的最坏一条，比 W-5 第 2 条更伤**：W-5 那条的表现是 `exit=2` 加一句「缺数据库」，评委知道自己少跑了一步；这一条的表现是**七项里三项 FAIL、hash 完整性 4/74**，屏幕上写着「证据被篡改或事后手写」（README §3 的失败释义表原话）。一个刚跑出 7/7、顺手 `git checkout` 收拾了一下、又复核了一遍的评委，拿到的结论是**这个项目的证据束是伪造的**。而整件事只是两边不同步 | 三条路，建议 ①+③：①`verify.py` 在 `hash-integrity` 大面积失配时**先比对库与 json 的生成时间戳**，不一致就报「库与快照不同步，请重跑 ①② 或删库还原」而不是直接判 FAIL——它出现在评委正看着的那一屏上，与 `## task-W5` 第 2 条建议 ① 是同一类修法，宜一并做；②把 `evidence/*.json` 也移出 git（只留 `INDEX.json` 与目录骨架），让「证据只能现跑」变成结构上的事实，但这会动 `.gitignore` 与仓库形态，且 W-5 建立的「证据入库可被 diff 审计」这条好处会没掉，**不建议**；③`make_evidence.py` 跑完打一行提示，说明工作区会脏、以及不要单独 `git checkout`。**本轨已在 `README.md` §3 写了警告并给出两条实测过的出路（重跑 ①② / `find evidence -name 'maos.db' -delete && git checkout -- evidence/`），但那只在读了 README 的人身上生效** |
| 2026-08-29 | P7 | **`python3 -m maos.kb.experiment` 没有 argparse，任何参数都被无视并直接开跑**：`--help` 不打用法、不退出，而是跑完对照实验、把 `evidence/scenario-R5/` 7 个文件落盘，工作区脏 7 行，`exit=0`。同一屏上并列的 `python3 scripts/make_evidence.py --help` 则是正规 argparse（`usage: make_evidence [-h] [--out OUT] ...`），打完用法即退、不写任何文件 | README 抬头与 §3 把这两条命令并列成 ①②，评委很自然会对两条都敲一次 `--help` 看有什么开关。结果是：一条给用法，另一条**默不作声地改了他的工作区**。踩到的人不会意识到是自己触发的，只会看到 `git status` 又多了 7 行 —— 与上一条叠加时尤其糟，因为他此刻正在琢磨「工作区怎么又脏了」。这也是「一条命令复现全量证据」这个卖点上最后一处不齐的接缝 | 与 `## task-W5` 第 2 条、本文件 `## task-X4` 段落里那条 `--out` 的账**同源**，一次做完：给 `maos/kb/experiment.py` 的 `__main__` 补一个真 argparse（`--out`，缺省 `evidence/`，带 `-h`）。`maos/kb/**` 不在本轨可改面内。**Y-3 若把 ①② 合并成一条命令，本条自动消解**（届时 `kb.experiment` 不再是评委直接敲的入口），故优先级跟着 Y-3 走 |

## task-Y4

把手册 R2 的「换渠道重试」演进场景 7（分支 `task/y4-gateway-demo`，基线 `42822fc`）。
本轨撞上的两条硬冲突**已由人类授权改四处白名单外文件解掉**，过程与理由记在
`docs/DECISIONS.md` 的 `## task-Y4`。以下两条是留给后来者的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **「让付款先撞一次可重试码」这件事有两个绕不开的副作用，任何后续场景想复用这条演法都会再撞一次。** 四象限里唯一允许 replan 换渠道的那一格是 `retriable=True + outcome=failed`，而它①severity 恒为 blocker（`gate.py:563`）→ **必然**产生一次 `AWAITING_REVIEW -> REWORK`；②在 `MockGateway` 里直接返终态 failed（`gateway.py:270-272`）→ `payment.observe` **必然**落一行 `payment_observation`（`payment_observe.py:123-128`）。这两个「必然」在 Y-4 之前不存在，因为没有场景撞过这一格 | 本轮为此调了三处断言的形状（`test_replan_gateway.py` 的 REWORK 断言、`test_refund_failure.py` 的空表断言、`compensate._last_observed_state` 的查询口径）。三处原本都假设「一个案子只有一笔请求、付款只过一轮闸」—— 那个假设在换渠道之后不再成立。**再有场景走这条演法，先对照这三处** | 无需处理，记录性质。若日后码表新增 `retriable=True + outcome=failed` 的码，四象限断言 `test_gateway_demo.py::test_四象限每格都被真码覆盖到` 会自动覆盖到 |
| 2026-08-29 | P4 | `CLAUDE.md:59` 的 `python3 run.py  # 场景 1-6 端到端` 仍是旧值，实际跑 1-7 | 与 `## task-X1` 第 3 条、`## integrate-round-4` 第 3 条是同一条，整合轮 4 漏刷。每个会话自动加载，是所有会话看到的第一份事实 | 已由派单点名「不要顺手改」（`CLAUDE.md` 不在任何一轨白名单）。**请人类一行改掉** |

## integrate-round-5

Y-4 补合进整合轮 5（合并提交 `783d9dd`，证据束重跑 `9964f17`）后的整体验收发现三条，
均**不在整合轮可改面内**（派单 §3：整合轮只做合并 + 验证，问题交下一轮），
按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`verify.py` 的 warn 从 10 行 / 2 类变成 11 行 / 3 类** —— `authoritative-fact` 项下新增一条 `scenario-7 case=case-s7-0001: 有回执但 biz_status 不是 settled`。这是 Y-4 让场景 7 先撞一次 `40005` 换渠道带来的：主渠道那笔**真收到过网关回执**，而全案最终落人工审批、`biz_status=compensated`，从未进入 `settled`。七项判定不受影响，`RESULT: 7/7 PASS` 照旧 | `docs/submission-checklist.md` A-2 本轮刚立的判据是「跑出来的 warn **就是这 10 行 / 2 类**，多出来的才要查」。照它执行的人会把这条**预期内**的 warn 判成回归，而它恰恰是场景 7 演对了的证明 —— 有回执不等于结算，正是权威事实边界那条铁律要守的东西 | 下一轮把 A-2 的 warn 表改成 **3 类 11 行**，给 `authoritative-fact` 这条写明「场景 7 专有，失败路径的正确表现，不是缺口」。与 A 类（`provenance=unknown`）、D 类（外部判据来源未审计）的性质不同，不要并进 `## task-X4` 那笔账 |
| 2026-08-29 | P4 | **五份收口文档都是在 Y-4 并入之前写的**（`33924d1` / `5ea6890` / `fb8a10e` / `f853063` 四个提交），其中四份带「Y-4 尚未合并」的明确断言与「待整合轮 6 回填」清单：`ppt-outline.md` 的两条（P10 换渠道画面、P5「机制在但演示里没有场景走这条路」）、`submission-checklist.md` A-4 的 replan 禁语与表 A 第 10 条、`demo-script.md` 镜 5 的 A/B 版选择与「总长 4:25 → 4:47」、`domain-portability.md` §2.3 那句「两处都是软件交付域/通用侧」与文末四条复跑清单 | Y-4 已于本轮并入，这些断言**当场失真**。最伤的是 PPT：台上照 `ppt-outline.md` P5 讲「机制已落地但演示里看不到」，而屏幕上演的就是自动换渠道重试 —— 当场被打脸。`domain-portability.md` §2.3 的失真方向相反：Y-4 往 `maos/agents/refund/` 里加了行，区间 B 的 `agents/` 从此**既有通用侧也有退款域侧**，那句话不改就是错的 | 各文档自己定的回填纪律是「先实跑拿到新输出再改文案」，其中 `demo-script.md` 镜 5 要**重录重掐表**（B 版 +22s，其后镜 6/7/8 顺延）、`domain-portability.md` §2.3/§2.4 两表要按新 HEAD 重跑 `git diff --shortstat`。分属 Z-1 / Z-2 / Z-3 / Z-4 各自的面，交下一轮，整合轮不代改 |
| 2026-08-29 | P7 | **README 两处数字本轮未重测，仍是 Y-4 之前的值**：§3 失败释义段的 `hash-integrity 4/74`（分母应随 `81/81` 走）、§4 的「全部跑完约 **18 秒**，最短路径约 **5 秒**」 | 前者是「跑完 `git status` 脏了 → 用 `git checkout -- evidence/` 收拾 → 再 `verify.py`」那条坏路径的实测读数，分母 74 与现在的 81 对不上；后者是 Z-5 全新克隆的掐表值，而 Y-4 带进 25 条测试与场景 7 的一次额外 replan，实际会略长。两处都不改判定，但细看的评委会发现对不上 | 两处都**只能实跑才能填**：前者要复现一次 checkout 坏路径，后者要重做一次全新克隆冒烟。都超出整合轮「只做合并 + 验证」的边界，也不该靠推算。交下一轮，与 `docs/clone-smoke-report.md` 的第四遍冒烟一起做 |
## task-C1

自建 Synapse + Element 房间地基期间发现三条，均**不在本轨可改面内**（派单 §4 独占文件只有
`deploy/synapse/**` + `docs/hiclaw-probe.md` + 两份账本的尾部追加），按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **本机 docker daemon 没有可用的境外出口。** 宿主机 shell 走 `HTTP_PROXY=http://127.0.0.1:7897`，而 `docker info` 显示 daemon 走 Docker Desktop 内置的 `http.docker.internal:3128` —— 两套代理，daemon 那套出不了境。三个交叉验证：容器内 `urlopen('https://registry-1.docker.io/v2/')` 超时、容器内 `urlopen('https://www.baidu.com')` 返回 200、宿主机同一个请求走 7897 只报 `SSL: CERTIFICATE_VERIFY_FAILED`（握手阶段才失败 = 链路是通的） | 这台机器上**任何 `docker pull` 境外镜像都会失败**，不止 Synapse。本轨绕开了（改用 `ghcr.nju.edu.cn`），但下一个要拉境外镜像的轨会再撞一次，而症状（10 分钟零进展、Images 体积不涨）看起来像网速慢，不像配置问题，很费时间 | 根治要**在 Docker Desktop 的 Settings → Resources → Proxies 里把代理指到 `http://host.docker.internal:7897`**（或等价配置），属 CLAUDE.md「必须问人类」的第 3 类（改 Docker），本轨没动。**请人类配一次**，配完 `deploy/synapse/up.sh` 不用改 —— 覆盖 `MAOS_SYNAPSE_IMAGE` / `MAOS_ELEMENT_IMAGE` 换回 `ghcr.io/element-hq/*` 即可 |
| 2026-08-29 | P5 | **`deploy/docker-compose.yml` 末尾那段 Synapse 注释与本轨实做对不上**，四处：①镜像写 `matrixdotorg/synapse:latest`，实做是 `ghcr.nju.edu.cn/element-hq/synapse:latest`；②`generate` 与 `register_new_matrix_user` 都写了 `-it`，非 tty 会话下会失败；③注册用 `-a`（管理员），实做一律 `--no-admin`；④四键示例里 `MATRIX_HOMESERVER=http://host.docker.internal:8008` 是容器口径，而 C 轮四轨都在宿主机跑 python，该用 `http://localhost:8008` | 那段注释是下一个人接手 Matrix 时最先读到的东西，照它跑会连撞三个坑（`-it` 失败、拉不到镜像、口径写错后静默降级 log-only）。注释末尾还写着「上面这一整段没做也不影响任何一条验收命令」—— C 轮之后这句也不再成立 | `deploy/docker-compose.yml` 是 **Ω 的面**，派单 §4 明确列为禁改并交代「对不上就记账不改」。建议由持有该文件的轨（或编排侧收口时）把那段注释换成一行指路：「Synapse 起停见 `deploy/synapse/README.md`」，细节不必在 compose 里重复维护 |
| 2026-08-29 | P5 | **镜像用的是 `latest` 浮动 tag，且经第三方镜像站缓存。** 实测南大站的 `latest` 与 ghcr.io 官方 `latest` digest 不相等（`18db676d…` vs `20ac3981…`），说明缓存有延迟；本轨拉到的是 Synapse v1.159.0 / Element v1.12.26 | 复现性风险：南大站某天刷新缓存后，同一条 `up.sh` 拉到的可能是另一个版本。对复赛演示来说影响有限（房间协议面很稳），但「同一脚本两次跑出不同版本」这件事本身违反证据可复现的口径 | 建议把 `up.sh` 的默认镜像从 `:latest` 钉到实测过的具体版本 tag（`ghcr.nju.edu.cn/element-hq/synapse:v1.159.0` 与 `element-web:v1.12.26`），复赛前定版时一并做。本轨没直接钉，是因为还没验证南大站是否缓存了这两个具体 tag（只验过 `latest` 拉得动），而验证要再拉一次 500MB，属派单范围外的动作 |
## task-C2

`_NioChannel` 真房间实测（分支 `task/c2-nio-live`，基线 `f42ea83`）中发现、按铁律 4
与派单 §4 边界不当场处理的四条。第 1 条**时间敏感**：C-4 一旦在导了真键的机器上跑
pytest 就会撞上。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **测试不是 env-hermetic 的，且在「键是真的且连得通」时会变红并往真房间发消息。** `maos/tests/test_registry_autodiscovery.py:286` 的 `build({}, matrix=True)` 经 `maos/flows/common.py::_wrap_matrix` 走 `MatrixBusConfig.from_env()`，读的是进程真环境 | 实跑复现（四键指向一个连得通的 stub homeserver）：全量 **`1 failed, 537 passed`**，红在 `assert bus.config.log_only is True`；且 `build({}, matrix=True)` 的订阅接线**真往房间发了 2 条消息**。C-4 截图时机器上一定 export 了四个真键 —— 那时一次 pytest 既会报一条假回归，又会把测试消息灌进演示房间。编排侧此前观察到的「指向连不通的地址仍全绿、只慢 10 秒」是同一个根因的温和形态 | **建议人类现在就加**，一个新文件 `maos/tests/conftest.py`，六行：<br>`import pytest`<br>`@pytest.fixture(autouse=True)`<br>`def _no_ambient_matrix_env(monkeypatch):`<br>`    for k in ("MATRIX_HOMESERVER","MATRIX_USER","MATRIX_TOKEN","MATRIX_ROOM_ID","MAOS_APPROVERS"):`<br>`        monkeypatch.delenv(k, raising=False)`<br>本轨没动它：`conftest.py` 在派单 §4 的独占文件之外，且它是**全仓测试共用面**，C-3/C-4 也在跑测试，无归属地新建会撞车。加完后 `test_matrix_bus.py` 现有断言全部不受影响（它们本就用 `from_env({})` 或 monkeypatch） |
| 2026-08-29 | P5 | **真 Synapse / Element 侧仍未验。** 本轨三条假设是拿真 matrix-nio 0.26.0 客户端栈打真 HTTP 到本地 stub homeserver 撞出来的，C-1 收工时仍是 `PENDING` | stub 能证明「给定这样的状态码与响应体，nio 会解析成什么、`_NioChannel` 会怎么判」，**证不了** Synapse 究竟回什么。具体三条待验：①Synapse 对未加密房的 `m.room.encryption` 是否确实回 **404 + `M_NOT_FOUND`**（判据的 clear 一侧全押在这上面）；②真加密房是否确实回 **200 + `algorithm`**；③Element 里人类发的消息，`event.sender` / `event.body` 原文形态，以及 bot 自己发的 `m.notice` 会不会进 `RoomMessageText` 回调（离线实测显示不会 —— `m.notice` 解析成 `RoomMessageNotice`，所以回声过滤对镜像流量其实是冗余的，但对将来改用 `m.text` 的回话不冗余） | C-1 的 `~/.maos-matrix/STATUS` 变 `READY` 后，`. ~/.maos-matrix/room.env` 再跑 `~/.maos-matrix/venv/bin/python scripts/matrix_probe.py`。探针已写好并自测过（缺 env exit=2、连不通 exit=3、都验到才 exit=0），三条假设各打「判据原文 / 实际请求 / 实际响应」三行。**另需 C-1 或人类再建一个默认加密的房间**，把 id 放进 `MATRIX_ROOM_ID_ENCRYPTED` —— 只验未加密那一侧等于没验 |
| 2026-08-29 | P5 | **房间监听仍没有接进任何运行路径**（`## task-E` 第 5 条的延续，本轨未消除） | `run.py --matrix` 只装镜像，不起监听；`MatrixEventBus.channel` 已按 C-3 的需要放出来，但谁在什么时候调 `channel.listen()`、场景 3 怎么阻塞等人类回话，仍无归属。本轨只负责让 `listen()` 本身是对的 | C-3 的 `hiclaw/room_demo.py`。本轨已把它依赖的三处形状冻住：`MatrixEventBus.channel` 只读属性、`MirrorChannel.listen(on_message: Callable[[str, str], None]) -> None`、`RoomApprovalBridge.handle_message(sender: str, body: str) -> str` |
| 2026-08-29 | P5 | `scripts/matrix_probe.py` 会往房间发一条 `[matrix_probe] 探针连通性自检 <时间>` 的 `m.notice`，用来验 `room_send` 这条路 | 演示房间里会多出探针消息。C-4 截图前若刚跑过探针，截图里可能带上它 | 不改（不发就验不了 `room_send`）。C-4 截图前若介意，人类在 Element 里删掉那几条即可；或跑探针时把 `MATRIX_ROOM_ID` 指向另一个测试房 |
## task-C3

落状态迁移镜像与房间审批入口时发现、按铁律 4 与派单边界不当场处理的五条
（分支 `task/c3-room-wiring`，基线 `f42ea83`）。第 1 条是**下一轮直接可用的施工草案**，
不是问题单。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **把房间决策口搬进 scenario_6 / scenario_7 的最小 diff 草案**（派单第 8 步 §5 要求「只写不改」）。现状：`scenario_6.py:260`、`scenario_7.py:318`、`scenario_7.py:351` 三处是硬编码的 `hq.decide(...)` 一行，没有可注入的决策口，房间里的人插不进去 | 没有这个口，「在 Element 里 `/approve` 放行正式场景」这条链路只能靠 `hiclaw/room_demo.py` 另起一个 plan 演示，演的不是场景 6/7 本身。房间演示与正式场景因此是两条路 | **下一轮，由 Y-2（`scenario_6.py`）与 Y-4（`scenario_7.py`）落**。草案见本条下方代码块，共 4 处 + `common.py` 1 处，逐字可用 |
| 2026-08-29 | P5 | **派单里 scenario_6/7 的行号已过期**：派单写 `scenario_6.py:295/310`、`scenario_7.py:391/408/445`，基线 `f42ea83` 上实际是 `scenario_6.py:245`（构造）/`:260`（decide）、`scenario_7.py:301`（构造）/`:318`、`:351`（decide，两处不是三处）。构造与调用的**形状完全一致**，只有行号漂了 | 照派单行号去核对会看到不相干的代码，容易被当成「口径对不上」而误判成回归 | 下一轮刷派单模板时改成按 `grep -n "HumanApprovalQueue\|\.decide("` 定位，不写死行号 |
| 2026-08-29 | P5 | `matrix_bus.summarize()` 对 event_log 行的 `attempt=` 只能 best-effort。它是给 Envelope 写的、硬编码 `attempt={env.attempt}`，而 event_log **没有 attempt 这一列**（`_transit` 把 attempt 当任务字段更新，没写进 detail） | 镜像出来的迁移摘要行里，`attempt=` 读的是**轮询那一刻**任务的当前值，不是迁移发生那一刻的值。返工多轮时这个数可能偏大。折叠 JSON 里是 event_log 原样，不受影响 | 优先级低（房间里没人按 attempt 做判断）。真要修有两条路：①`_transit` 把 attempt 写进 detail（要动 `control_plane.py`，Y-2 持有）；②`summarize` 对非 Envelope 来源省掉 attempt 段（要动 `matrix_bus.py`，C-2 持有）。两条都跨轨，故本轮只在 `render_transition` 的 docstring 里标注 |
| 2026-08-29 | P5 | `matrix_bus.MirrorChannel` Protocol 只声明了 `send` / `close`，**没有 `listen`**，而真通道 `_NioChannel` 有 `listen(on_message)`，房间审批链路完全依赖它 | 类型上「能监听的通道」无处可表达，调用方只能 `getattr(channel, "listen", None)` 探测（`room_demo.py` 现在就是这么写的）。少了这层声明，某天有人给 Protocol 加实现却忘了 `listen`，症状是「房间里发命令没反应」——最难查的那种 | 归 C-2（`matrix_bus.py` 所有者）。见本轨回执「需要 C-2 改的东西」一节。不是现存故障：`_NioChannel` 与 `room_demo.StdoutChannel` 三方法签名本轮已实测逐字对齐 |
| 2026-08-29 | P5 | **真房间未验**：本轨交付时 `~/.maos-matrix/STATUS` 仍是 `PENDING —— C-1 尚未交付房间凭证`，`_NioChannel` 那条活路径（`sync_forever` + `add_event_callback` + `room_send`）一次都没在真 Synapse 上跑过 | 与 `## task-E` 第 2 条同源。本轨全部判据建立在注入 fake channel 上，可复现、不依赖 Synapse；但「接上就能镜像 / 接上就能审批」仍是**推断而非观察** | C-1 交付房间后跑派单第 9 步末尾那两条真房间命令（`--case approve` / `--case reject`）。三处要重点看：①`listen` 回调拿到的 `(sender, body)` 是不是就是 Element 里打的那行；②`sender` 的形态与 `MAOS_APPROVERS` 里写的是否**逐字**一致（不一致的症状是「命令发了没反应」）；③折叠 JSON 在 Element 里是否真的折叠 |

**最小 diff 草案（下一轮直接用，本轮一个字未改）**

`maos/flows/common.py`（Y-1）——新增一个缺省决策口，3 行：

```python
def default_decider(hq, task, *, approved: bool, operator: str, note: str = "") -> None:
    """缺省决策口：直接落 hq.decide。房间接线时换成等房间回话的那个。"""
    hq.decide(task["task_id"], approved=approved, operator=operator, note=note)
```

`maos/flows/scenario_6.py`（Y-2）——2 处：

```diff
-def run(*, matrix: bool = False) -> int:
+def run(*, matrix: bool = False, decider=None) -> int:
+    decide = decider or default_decider
@@
-    hq.decide(blocked["task_id"], approved=True, operator=APPROVER, note="已核对金额与政策版本")
+    decide(hq, blocked, approved=True, operator=APPROVER, note="已核对金额与政策版本")
```

`maos/flows/scenario_7.py`（Y-4）——3 处（决策口要穿过 `drive()`，`run()` 只透传）：

```diff
-def drive(*, matrix: bool = False) -> dict:
+def drive(*, matrix: bool = False, decider=None) -> dict:
+    decide = decider or default_decider
@@
-    hq.decide(finance_task["task_id"], approved=True, operator=APPROVER,
-              note="已核对金额与政策版本")
+    decide(hq, finance_task, approved=True, operator=APPROVER,
+           note="已核对金额与政策版本")
@@
-    hq.decide(payment_task["task_id"], approved=False, operator=APPROVER, note=REJECT_REASON)
+    decide(hq, payment_task, approved=False, operator=APPROVER, note=REJECT_REASON)
@@
-def run(*, matrix: bool = False) -> int:
-    out = drive(matrix=matrix)
+def run(*, matrix: bool = False, decider=None) -> int:
+    out = drive(matrix=matrix, decider=decider)
```

三点注意：

1. **缺省行为一字不变**（`decider=None` 时走 `default_decider`，即现在这一行），
   所以 `run.py` 与全部存量测试不受影响 —— 这是这份草案能安全落地的全部前提。
2. 决策口收的是 **task dict 而不是 task_id**：房间侧要拿 `title` / `effect_risk`
   渲染审批卡，只给 id 就得再查一次库，而那次查询在超时路径上可能查到已变的状态。
3. 房间侧的决策口由 `hiclaw` 提供（本轮 `room_demo.py` 里那套 listen -> bridge ->
   `decided.wait(timeout)` 可原样搬），`flows/**` **不 import hiclaw** ——
   由 `maos/main.py` 或入口层在 `--matrix` 时注入，保持 flows 对 hiclaw 零依赖。
## task-C4

房间演示 runbook + `evidence/room/` 证据束时发现，均**不在本轨可改面内**
（分支 `task/c4-matrix-evidence`，基线 `f42ea83`），按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`docs/EXECUTION.md:499` 与 `:502` 写的截图落点会把 `scripts/verify.py` 整个打死。** 原文两句是「`# Element 里看到全过程；发 /approve → DONE。截图存 evidence/scenario-R1/`」与「`# 网关失败 → replan → 达上限 → /reject → 补偿。截图存 evidence/scenario-R2/`」。根因是 `scripts/verify.py::load_cases`（`verify.py:542`）的发现规则：`evidence/` 下**任何** `scenario-` 开头的目录都被当成一个证据束，逐个要求 `maos.db` + `trace.json` + `result.json`。截图目录满足不了这三样 | **不是某一项 FAIL，是整个 `verify.py` 进不去核验。** 编排侧实测（造了个只放一张假图的 `scenario-R1/` 目录）：<br>`[FAIL] 无法开始核验：缺数据库: <…>/scenario-R1/maos.db（先跑 python3 scripts/make_evidence.py）`<br>`exit=2`<br>「7/7 PASS」这条头号卖点当场没了。而照手册字面执行的人**不会预料到**这个后果 —— 手册里那两句读起来只是在指定一个存放位置 | **下一轮改这两行**，把 `evidence/scenario-R1/` 与 `evidence/scenario-R2/` 都换成 `evidence/room/`，并在原地补一句为什么不能是 `scenario-R*`（附上面那段实测输出），否则下一个读手册的人会把它改回去。本轨已按 `evidence/room/` 落地并实跑验证：`python3 scripts/verify.py 2>&1 \| tail -2` 的报错原文只出现 `scenario-1`，不含 `scenario-R1` / `scenario-R2` / `room`。`docs/EXECUTION.md` 是事实源且不在本轨可改面内，故只记账不改。理由见 DECISIONS `## task-C4` 第 1 条 |
| 2026-08-29 | P4 | `docs/EXECUTION.md:790` 的证据映射表里，「用一条脱敏真实退款需求完成可执行纵向切片」一行的证据列写的是 `` `evidence/scenario-R1,R2/` ``；`:793`「返工 / HITL Trace」一行写的是 `` `evidence/scenario-R2/trace.json` `` | 与上一条同根，但**性质不同**：这两处指的是**数据**证据，而数据证据的实际落点是 `evidence/scenario-6/` 与 `evidence/scenario-7/`（R1 = `--scenario 6` 顺利路径，R2 = `--scenario 7` 失败路径），两个目录都真实存在且带出处头。所以这两行不是「会打死 verify」，而是**指向了两个不存在的目录** —— 评委按图索骥会以为证据缺失 | 与上一条同批改：`:790` 改成 `` `evidence/scenario-6,7/` ``，`:793` 改成 `` `evidence/scenario-7/trace.json` ``。**注意别一刀切换成 `evidence/room/`** —— 这两行要的是机器侧数据证据，`room/` 装的是人机交互证据，两者互补不可替代。对照关系已写进 `evidence/room/README.md` 的「命名对照」一节 |
| 2026-08-29 | P5 | **`scripts/make_evidence.py::scan_for_secrets` 只扫文本，扫不到 PNG。** 本轨是全轮唯一往仓库里放二进制的一轨，而截图恰恰是最容易夹带 access token 的载体（终端 scrollback 里的 `Bearer <token>`、Element 的账号设置页） | 现有密钥守卫在本轨**完全不设防**，且是**静默**的 —— 扫过了、没报错，读起来像「已检查通过」。图一旦进 git 历史就取不出来，事后补救只能重写历史 | 两条路。①**成本最低**：`scan_for_secrets` 遇到非文本文件时不要静默跳过，输出一行「跳过 N 个二进制文件，未扫描」，让「没扫」和「扫了没问题」在输出里能分开 —— 一行改动，建议先做这条。②真要扫图得上 OCR，超出本仓库范围，不建议。本轨的对策是把脱敏前移到**按快门那一刻**（`docs/matrix-room-runbook.md` §7 逐条列出），并要求 `transcript.md` 作为可 grep 的文本镜像 —— 但那是流程约束，没有机器守卫。`scripts/make_evidence.py` 是 Y-3 的面，本轨只读 |
| 2026-08-29 | P5 | `evidence/INDEX.json` 由 `make_evidence.py` 生成，只登记 `scenario-*` 系列（当前 7 条），**不认识 `evidence/room/`**；`evidence/scenario-R5/` 也同样不在 `INDEX.json` 里 | 评委若把 `INDEX.json` 当作证据总目录，会漏看 `room/` 与 `scenario-R5/` 两个目录。当前无功能影响（`verify.py` 不读 `INDEX.json`，走的是目录发现） | 下一轮由持有 `make_evidence.py` 的一轨决定：要么让 `INDEX.json` 登记全部证据目录（含非 `scenario-*` 的），要么在 `README.md` 的证据一节明写「`INDEX.json` 只覆盖 `scenario-1..7`，另有 `scenario-R5/` 与 `room/`」。**建议后者**，成本一行，且不用改生成器的语义 |
| 2026-08-29 | P5 | runbook §1 让人跑一条 `curl` 探房间是否加密（`.../state/m.room.encryption`，期望 `M_NOT_FOUND`），该命令**必然**把 `Bearer <token>` 留在终端 scrollback 里 | 这是本轨脱敏规程里最容易漏的一步：人跑完探测、确认房间没加密、心情很好，直接开始截图 —— token 就在上面几行。runbook 已在该命令下方红字要求「跑完立刻 `clear` 再截图」，但那仍是**靠人记得** | C-1 落 `deploy/synapse/` 时，把这条探测包成一个不回显 token 的小脚本（token 从 `room.env` 读进变量，只打印判定结果 `encrypted: yes/no`），runbook 改为引用它。这样脱敏是**结构性**的而不是靠纪律。`deploy/**` 不在本轨可改面内 |

## task-E1

修 `settled` 回执内容不校验（分支 `task/e1-receipt-guard`，基线 `c1049c2`）时，
撞到两处白名单外的文件需要跟着改。按铁律 4 记账，不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`docs/authoritative-facts.md` §2 的「四道拦截」表已经不全，且四个行号全部漂了。** 本轮在 `update_biz_status()` 里加了第五道（回执内容判据，`guard.py:215` 起）：③ 只保证「有一张回执」，不保证那张回执说到账了。表里四行的行号也全变了 —— 唯一写入路径 `guard.py:125 -> :144`，① `:150 -> :169`，② `:160 -> :179`，③ 迁移 `:172 -> :191`，④ 缺字段 `:179 -> :198`，同事务那段 `:190 -> :239` | 这份文档是「权威事实边界」这条主线论证的落点，README §3 与首页都指着它。表里少一道闸，等于把本轮补上的那道防线从对外叙述里抹掉了 —— 而它恰恰是「系统持有的是**网关说到账了**，不只是**有一张回执**」这句话的唯一代码依据。行号对不上则是评委按图索骥时第一眼就会撞到的 | 文档不在本轨白名单（派单 §3 只列了 guard.py / verify.py / 两个测试 / BACKLOG / DECISIONS）。建议下一轮连同 README:269 那行「必须同事务附回执」一起补成「附一张**说到账了**的回执」 |
| 2026-08-29 | P7 | **`payment_observe.py:123` 的 `if status == "failed"` 分支从「唯一防线」降级成了「第一道」，但它仍然必要，不要当冗余删掉。** guard 现在会拦住任何非 `settled` 回执，而这个分支做的是另一件事：走 `_record_failure` 落观察行 + 返回 `needs_compensation=True`，把案子交给失败路径场景 | 后来者看到 guard 已经兜底，可能顺手把这个分支简化掉 —— 那会让「网关明确失败」这条观察不再留痕，且场景 7 的补偿路径拿不到 `needs_compensation` | 无需处理，记录性质。真要动的话，`test_refund_failure.py` 会先红 |

---

## task-E2

E-2 轨（交付面文档失真）执行中发现，**均不在本轨白名单可改面内**，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`docs/clone-smoke-report.md` §5 结论段的 🔴 已失真**：仍写着「Y-3 合并后，本轨的冒烟结论必须重跑一遍……**在 Y-3 合并前，不要把本报告当作最终版引用**」。但 Y-3 早已合入，同文件 §2「第三遍（整合轮 5，基线 `5ea6890`）—— Y-3 收敛成两条命令之后」就是那次重跑的记录 | 一份交付面报告在正文里演示了重跑结果、却在结论里拦着读者「别把我当最终版」，自相矛盾。该文件「待整合轮 5 回填」表第 2 行本来就写明「§5 结论段的『Y-3 合并后必须重跑』一句届时删除」，这条**回填动作漏做了** | 派单 §5.2 只列了三处（本机路径 / 时效声明 / 第一遍 ❌ 交代），这条不在其中，本轨不擅自扩面。交整合轮 6，与该文件的条数、耗时读数一起收口：把 🔴 改成「Y-3 已合入，重跑见 §2 第三遍」，并把「待整合轮 5 回填」表第 2 行标为已解 |

## integrate-round-7

C 轮四轨（C-1…C-4）并入 `integrate/round-7` 时，编排侧**实跑核验**发现四条。
均**不在整合轮可改面内**（整合轮只做合并 + 验证，问题交下一轮），按铁律 4 记账不当场改。
基线 `c1049c2`，合并后 HEAD `802d49d`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **`scripts/matrix_probe.py` 的 ②a「首次 sync 灌不灌历史」是假阴性。** 它在跑 ②a 之前**没有**把 `client.next_batch` 清空，而同一个 client 已被假设 ① / ③ 用过，`next_batch` 非空 —— 于是 `client.sync(timeout=0)` 实际带着 `since` 发出去，做的是**增量**同步，当然一条历史都收不到，恒判 `skip("房间无历史消息，观察不到")`。两个交叉证据：①编排侧往房间灌了 3 条历史后重跑，②a 仍报「房间可能是空的」，而**同一次运行**的 ②c 明明白白收下了那 3 条；②两次相隔 20 分钟的运行印出的 `next_batch` 逐字节相同（`s16_5_0_1_1_1_1_9_0_1_1_1_1_1`），而中间房间新增了 3 条消息 | 探针最后一行印「已验 8 条，未验 1 条」并 exit=3，看起来像「真房间还有一条没验通」，实际那条**已经被 ②c 观察到了**，只是记在了别的条目下。照这个退出码去判断的人会以为 `_NioChannel` 还有未验风险 | 一行的事：②a 之前补一句 `client.next_batch = ""`（②b 在 `scripts/matrix_probe.py:158` 已经这么做了，照抄即可）。**下一个动 `scripts/matrix_probe.py` 的轨顺手做**，做完 exit 应变 0 |
| 2026-08-29 | P5 | **`scripts/matrix_probe.py` 的 ②c 回声丢弃计数恒 0。** 印的是「收下 7 条、按回声/异房丢弃 **0** 条」，但实测 bot 自己在监听窗口内发的那条**确实没进** `on_message` | 过滤器本身是对的（见影响栏的实测），错的只是计数。但「丢弃 0 条」这句话会让人以为回声过滤**没有被触发过**，从而以为这条判据没真验上 —— 与上一条叠加，等于两条都在自我怀疑 | 实测依据：编排侧在 20s 窗口里按 4 秒一条发了 8 条，其中 #3（t=12s）与 #6（t=24s）由 **bot 自己**发出，其余由 boss 发出，8 条全部 `HTTP 200`。窗口内收下的是历史 3 条 + #1/#2/#4/#5，**#3 不在收下列表里**，而时间上夹着它的 #2（t=8s）与 #4（t=16s）都在 —— 即回声过滤真的把 bot 自己那条挡掉了，计数器没跟着加。与上一条同批修 |
| 2026-08-29 | P5 | **`evidence/room/` 一张截图都没有，且目录里那段「卡在 C-1」的实测依据已经过期。** C-4 收工时 C-1 尚未交付房间，`evidence/room/README.md` 如实记了当时的实测（`cat ~/.maos-matrix/STATUS` → `PENDING`、`ls deploy/` 无 `synapse/`、`hiclaw.room_demo` 找不到）。**现在这三条全都不成立了**：`STATUS` = `READY 2026-08-29T05:52:11Z`，`maos-synapse` / `maos-element` 两个容器 `Up (healthy)`，`python3 -m hiclaw.room_demo --help` 正常 | C-4 拒绝拿降级模式的终端输出冒充房间截图，这个判断是对的（也是派单明令要求的），**不要因为这条账去指责 C-4**。但结果是 `docs/EXECUTION.md` Phase 4 验收里「Element 全过程 + `/approve` → DONE / `/reject` → 补偿，截图落盘」这一条**仍未达成**，而它是「人在环」这条卖点唯一还没落实的一块 | 截图这一步**现在就能做**，且只差人：`docs/matrix-room-runbook.md` 已经写全了步骤，房间与容器都在跑。需要真人在 Element 里打 `/approve` 与 `/reject`。**建议单开一轨（或人类自己走一遍 runbook）**，同时把 `evidence/room/README.md` 里那段过期的「卡在 C-1」实测依据一并刷掉 —— 留着它比没有更坏，读的人会以为房间到现在还没起来 |
| 2026-08-29 | P5 | **演示房间曾被整合轮 7 的验证脚手架污染，已换新房处理。** 为把 C-2 探针缺的三条前置补上，编排侧往当时的主房间 `!xfRqhNYVNyuOMitWVs:maos.local` 灌了 **11 条测试消息**（3 条历史 + 8 条 drip 探测），另建了一个**加密房** `!feRLkOSGGtRtZtKVbj:maos.local`（探针 ① 的另一侧要它），并往 `~/.maos-matrix/room.env` 追加了 `MATRIX_ROOM_ID_ENCRYPTED` 一行 | 那 11 条会出现在演示截图里。**已于同日处理**：不走 `down.sh --purge`（要删数据卷、账号与签名密钥全没、四键全变，代价远大于收益，且本机权限层也拦破坏性操作），改为**建一个全新的空房**并把 `room.env` 的 `MATRIX_ROOM_ID` 改指过去。现役演示房 = `!qcaXWSgkmosmxdYgpD:maos.local`，只读核对过：成员 bot/boss/intern 三人全 joined、`m.room.encryption` 返回 `M_NOT_FOUND`（未加密）、历史消息 **0 条** | **无需再处理**。两个旧房都**保留未删**（随时可回看验证过程），`MATRIX_ROOM_ID_ENCRYPTED` 仍指那个加密房、探针照常能用。唯一要注意的是：**截图前不要再往现役房发任何测试消息** —— 包括 `deploy/synapse/smoke_send.py` 与 `scripts/matrix_probe.py`（它的 ③c 会 `room_send` 一条进去）。要冒烟就把 `MATRIX_ROOM_ID` 临时指回旧房 |

## task-G2

G-2 轨（verify 第 6 项：外部判据只验列表非空）执行中发现，**均不在本轨白名单可改面内**，
按铁律 4 记账不当场改。本轨只做了派单点名的那一件事：`external_evidence` 里的每一条
必须在库里回查得到。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **FAILED 的 plan 可以自称 `status: "succeeded"` 而第 6 项照过。** `check_business_outcome` 对终态的判据是：`state not in ("DONE","FAILED")` 就跳过，否则只要 `business_outcome` 是个 dict 且 `status` 非空就 `chk.ok()` —— 只有 `state == "DONE"` 的分支才继续查判据。于是库里 FAILED、result.json 也老实记 FAILED（躲开了 state 比对）、`business_outcome.status` 却写 `succeeded`，第 6 项一声不吭 | 与本轨修的是**同一个模式**：只验字段在不在，不验说的是不是真的。危害比本轨那条小一档（要骗过的是「读 json 的人」而不是「跑核验器的人」，且 `basis`/`plan_state` 两个字段会自相矛盾），但它就在同一个函数里，隔着五行 | 建议下一轮顺手做，判据是现成的：FAILED 分支加一句 `status` 必须是 `failed`（生成侧 `derive_business_outcome` 就是这么写死的，`plan_state == "FAILED"` -> `status, basis = "failed", "plan_failed"`），一行的事。本轨不擅自扩面 |
| 2026-08-29 | P7 | **`business_outcome` 的 `basis` / `plan_state` / `source` / `unaudited_evidence_count` 四个字段仍是「写什么就是什么」。** 本轨回查的是 `external_evidence` 里**指得到的东西**（产物、回执），这四个字段本身没有任何一层校验 | `unaudited_evidence_count` 尤其值得点名：把它改成 0 就能让第 6 项那条 warn 凭空消失，而那条 warn 是评委判断「这份报告是不是脚手架」的唯一线索。warn 不判负，所以这不是「伪造成功」，是**伪造干净** —— 一屏没有 warn 的 7/7，比有 warn 的 7/7 更容易被当成没问题 | 不急，但修法很便宜：`unaudited_evidence_count` 应当等于列表里 `provenance == "unknown"` 的条数，`plan_state` 应当等于库里的 `state`，`basis` 与 `status` 的对应关系在 `derive_business_outcome` 里是死的。四条都能在同一个 for 循环里就地比对，不需要新查库 |

## task-D1

rework 第三出口（分支 `task/d1-human-exit`，基线 `956e6af`）。设计与取舍记在
`docs/DECISIONS.md` 的 `## task-D1`；以下是本轨**按铁律 4 不当场改**的四条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **既有的 `replan_limit_exceeded` 有和第三出口一模一样的洞**（派单 §5.1 设计点 3 要求顺带核）：`control_plane.py` 那条分支对**任何** `effect_risk` 的任务都落 `AWAITING_REVIEW -> BLOCKED`，而改造前的 `HumanApprovalQueue.pending()` 只捞 `effect_risk == HIGH` —— 非 H 任务重规划撞上限后会停在 BLOCKED 且没有任何人捞得到。X-2 当时的两条链路（`test_replan_gateway.py` 与场景 5/7）用的都是 H 任务，所以一直没露出来 | 本轨选了设计点 3 的方案 (a)，`pending()` 改成「H **或** `detail["await"] == "human_decision"`」，而 `replan_limit_exceeded` 写的正是 `await: human_decision` —— 所以**这个洞被顺带覆盖了**。但覆盖不等于修：控制面那条分支一行没动，能不能被捞到仍然取决于 `pending()` 这一个消费方。若日后另有代码按 `effect_risk == HIGH` 自己过滤 BLOCKED 任务，同一个洞会在那里重新长出来 | 派单明确「记 BACKLOG，别顺手修」（X-2 的既有语义，改它超出本单范围）。建议下一轮把「BLOCKED 的任务由谁捞」收成一处判据（`pending()` 是唯一入口），而不是让每个消费方各写一遍过滤条件 |
| 2026-08-29 | P4 | **证据束七项里四项的数字变了，README / 自查单 / PPT 里写死的是旧值**。本轨实测（`python3 scripts/make_evidence.py && python3 scripts/verify.py`，`RESULT: 7/7 PASS`，8 个来源不变）：`hash-integrity` **81 → 90**、`business-ref` **33 → 38**、`trace-tree` **18 → 19**、`business-outcome` **9 → 10**；`authoritative-fact 3/3`、`kb-hit 7/7`、`history-case 1/1` 三项未变 | 场景 7 的 `result.json` 从 1 个 plan 变 2 个（第二段另起了一个 plan），四项分母跟着涨。`README.md:104-109` 的读数块、`docs/submission-checklist.md`、`docs/ppt-outline.md` 里凡写死这四个数的地方都对不上了 | 派单 §5.2 明确「记 BACKLOG 交整合轮，别去改」（那三份都不是本轨的面）。刷数时注意 `README.md:133` 那个 `hash-integrity 4/74` 是**坏路径**的读数，分母另算，与 `## integrate-round-5` 第 3 条是同一笔账 |
| 2026-08-29 | P4 | **`verify.py` 的 warn 从 11 行变 12 行，仍是 3 类** —— `authoritative-fact` 项下新增一条 `scenario-7 case=case-s7-0002: 有回执但 biz_status 不是 settled` | 与 `## integrate-round-5` 第 1 条同源、同性质：第二笔**真收到过网关回执**（`ACQ.TRADE_NOT_EXIST`），而全案落人工驳回、从未进入 `settled` —— 这条 warn 恰恰是权威事实边界守住了的证明，不是缺口。`docs/submission-checklist.md` A-2 若已按整合轮 5 的建议改成「3 类 11 行」，本轮又要改成 **3 类 12 行** | 与 `## integrate-round-5` 第 1 条**合并一次做**，别分两轮改两遍。建议 A-2 那一格不再写死行数，改成「`authoritative-fact` 每个未 settled 的退款 case 一行，场景 7 现有 2 个 case」——行数会跟着场景走，写死一个数就是每加一笔演示都要回来改一次 |
| 2026-08-29 | P4 | **`_gate_gateway` 的 `severity` 与第三出口的判据在 `GW_QUERY_OR_HUMAN` 这一格不同源**：同一个 disposition 有两种严重度 —— 未知码走 `gate.py` 的 `except KeyError` 分支给 `blocker`，而**已知**的 `retriable=False + outcome=unknown` 码（如 `ACQ.DISCORDANT_REPEAT_REQUEST`）走正常分支，按 `outcome != failed` 给 `info` | 后者单独出现时 `_review` 判 `pass`，走不到 rework 分支，也就走不到第三出口 —— 它落回 `effect_risk=H` 的人工审批入口（有 H 的话），非 H 任务则直接 DONE。这一格是四象限里官方称「最危险的一档」，却是唯一一个「已知码比未知码更容易被放行」的组合。本轨不改：severity 的判据在 `gate.py:563`，那是 D-2 的面，且改它会动场景 7 第一段现在走的路径 | 交 D-2 或下一轮一并想：要么让这一格的已知码也给 blocker（与未知码同源），要么明确写下「已知的 unknown 由高风险审批兜、未知的 unknown 由第三出口兜」这个分工。**两种都行，但不能像现在这样没人写下来** —— 本轨的 `test_terminal_gateway_codes_route_to_human` 已经把「路由侧对两格一视同仁」钉住了，缺的是产出侧的口径 |

## task-D2

第六道闸补 plan 级判据（分支 `task/d2-plan-gate`，基线 `956e6af`）时发现四条，
均**不在本轨白名单内**，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`refund.intake` 在返工下不幂等**。`maos/domain/refund/guard.py:115` 的 `create_case` 是裸 `INSERT`（无 upsert、无 `ON CONFLICT`），而 `refund_case` 的主键是 `(tenant_id, case_id)`。任何一道闸在受理任务上判出 rework，重跑就抛 `IntegrityError: UNIQUE constraint failed: refund_case.tenant_id, refund_case.case_id`，任务耗尽 3 次 attempt 后 FAILED | **先于本次改动就在的坑**，只是过去没有触发路径（受理任务一直没被判过 rework）。本轨补上 plan 级判据后它被走到了：R5 without_kb 段的实测拦点因此是这条 UNIQUE 报错，而不是那条 plan 级 finding 的文案。D-1 的第三出口合并后 plan 级 blocker 直接转人工、不返工，这条路径会重新变成不可达 —— 但**坑还在**，换任何一道闸在受理上判 rework 都会重现 | `guard.py` / `skills/builtin/refund/intake.py` 都不在本轨白名单。建议与 D-1 合并后一并处理：或让 `create_case` 在同 `(tenant, case)` 且同 `plan_id` 时幂等返回既有行，或让受理 skill 先查后建。**不建议**改成 `INSERT OR REPLACE` —— 那会让重跑悄悄覆盖已经推进过的 `biz_status`，比抛异常坏得多 |
| 2026-08-29 | P7 | **`verify.py` 的 `business-ref` 从 33/33 变成 30/30**。R5 without_kb 段的 plan 现在死在受理那一步，裁定 / 付款 / 通知三步都没跑，少落三条业务引用 | 不影响判定（`RESULT: 7/7 PASS`，30/30 全部指得到、版本对得上），但**分子分母同时变小**这件事会让照着旧数字对的人以为丢了引用。`docs/submission-checklist.md` 与 README 里凡是写死 `business-ref 33/33` 的地方都会对不上 | 这个数字**还会再变一次** —— D-1 的第三出口合并后 without_kb 段变成「受理 BLOCKED 等人决策」，跑到哪一步又不一样。所以现在不值得刷任何文档里的数字，等整合轮把 D-1/D-2 合起来重跑证据束之后一次刷到位。本轨已按派单 §8「不要改 README / 自查单 / PPT 里的数字」留给整合轮 |
| 2026-08-29 | P7 | **`maos/kb/guardrails.py:204-219` 的 `_shared_inputs` 只扫顶层 `inputs`**，取不到嵌在 `case_seed` 里的 `amount_claimed`。它的 docstring 明写「`amount_claimed` 取自当前计划已有的任务 …… 抄错一位数就是把闸绕过去」 | 当前无症状：with_kb 段的 baseline 里 finance 那一步带着顶层 `amount_claimed`，拿得到。但**漏排财务核算的 baseline 拿不到** —— 知识建议若在那种 baseline 上补步骤，补出来的任务会缺申报金额，第六道闸对它恒不触发。这与本轨修的是同一类坑（触发面只看顶层），只是在检索侧 | `kb/guardrails.py` 不在本轨白名单。本轨已把「按字段名任意深度扫」抽成 `gate.py` 的 `_claimed_amounts`，检索侧若要修可以照同一口径走，但**不要跨轨共用实现** —— 内核与知识层之间不该新增依赖方向。交后续轨 |
| 2026-08-29 | P7 | **`docs/domain-portability.md` §2 的行数与 diff 统计没跟着本轨刷**（§2.2 表里 `maos/runtime/` 的 `+126 / −4`、正文 `:116` 与 `:251` 的「+126」、§2.3 的 `+150 / −6`） | 本轨给 `gate.py` 加了约 +200 行（plan 级判据 + 两个模块级辅助函数 + docstring），这几处数字全部偏小。不影响论证方向（「这些行领域无关」照旧由两条 AST 守卫钉着），只是数字不准 | 那几个数字按定义是 `git diff --shortstat` 的区间统计，**必须实跑才能填**，且区间端点会随整合轮的合并提交变。本轨只刷了 §5 与收口台账里**与本轨改动直接相关**的行号（`gate.py:454 -> :522`，新增 `:578`），没有代刷区间统计。交整合轮 |

## task-F1

口径统一轨（分支 `task/f1-role-count`，基线 `c1049c2`）：只改两处措辞
（`maos/agents/manager.py` 的场景集合、`docs/agentteams-mapping.md:21` 的角色数），
零行为变更。以下三条是本轨看见但**按铁律 4 不当场改**的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`maos/kb/experiment.py:678` 的 `write_evidence()` docstring 写「与场景 1-6 走同一套落盘与脱敏口径」，应为 1-7**。本轨实测判定：**确认该改**。依据是这句话自称的复用关系确实覆盖到了场景 7 —— `write_bundle` 全仓唯一实现在 `scripts/make_evidence.py:409`，`:481` 在 `for n in wanted` 循环里对每个场景调它，而 `wanted` 缺省取 `maos/main.py:26` 的 `ALL_SCENARIOS = (1,2,3,4,5,6,7)`；R5 自己在 `experiment.py:711` 调的是同一个 `write_bundle`。所以场景 7 与 R5 同源这件事成立，只是数字没跟着 Y-4 走 | 不改判定、不改行为，纯文档失真。但它恰好是在解释「为什么不另立第二份落盘口径」，把 7 漏在外面会让读者以为场景 7 走的是别的路径 —— 而场景 7 正是唯一走失败路径的那个，最容易被当成特例 | **`maos/kb/experiment.py` 是 D-2 全文件独占，本轨一个字节没碰。** 交 D-2 顺手改，或 D-2 合并后另开一单 |
| 2026-08-29 | P7 | **`docs/EXECUTION.md:710` 说 `agent-identity.md` 是「十角色清单（软件域 6 + 退款域 4）」，与 `AGENT_POOL` 的 9 个对不上**，是 `docs/BACKLOG.md:304` 那条账的第三处表述。本轨判定：**建议不改** | 严格说这句没错 —— 它描述的是 `agent-identity.md` 这份生成物的**内容清单**（确实列了 10 个 Identity），不是在描述可派单数；且生成物自己 `:7` 已如实印出「10 个 / 9 个 / 1 个」并解释差在哪，顺着链接就能数平。真正会误导的是把 10 直接挂在 `AGENT_POOL` 后面那种写法，那处已由本轨在 `agentteams-mapping.md:21` 修掉 | **手册是事实源，改它历来要人类当场授权（先例 `docs/DECISIONS.md:322`），本轨不动。** 若人类仍想把三处表述统一，最小改法是在该行末尾追加「其中 9 个可被派单」—— 那是措辞增强，不是纠错，可与 `## task-X4` 那批文档一起做 |
| 2026-08-29 | P7 | **`mgr.plan()` 还有一个「场景」以外的调用点：`maos/kb/experiment.py:344`（R5 RAG 对照实验，传 context）**。本轨把 `_user_message` 的注释改成「走 ManagerAgent 规划的场景（1 / 2 / 5 / 6 / 7）全部改判」，措辞限定在**场景**，未提这一处 | 「用户请求」前缀一旦动，R5 实验同样改判，而 R5 不在 `ALL_SCENARIOS` 里、也不在场景编号体系内，照注释复核的人可能漏掉它。影响只在「改这个前缀之前要复核哪些出口」这一件事上 | 与上面第 1 条同属 `maos/kb/experiment.py` 面。若 D-2 处理那条时顺手，可在 `experiment.py:338-344` 附近加一句「本处与场景共用 `_user_message` 的『用户请求』前缀」的反向指路；不做也不影响任何判定 |

## task-F2

派单模板轨（分支 `task/f2-dispatch-template`，基线 `c1049c2`）。本轨一行代码、一个测试都不改。
以下三条都在白名单外，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **仓库 `CLAUDE.md` 的「开工自检」只认下了一条预期拦截，没有第二条。** 它写的是「让 Claude 读一次守卫脚本，**被拦 = hook 正常**」—— 只覆盖那一次 Read。但守卫按路径与裸文件名拦截的是**一整类**只读引用（`git log -- <受保护路径>`、`grep <裸文件名> …`、`wc -l <受保护路径>`，本轨探针实测均 exit=2），而每份派单同时写着「被拦不要绕，停下来报告」 | 子会话想查清自己的禁改面、或看某个受保护文件最近改过没，都会撞上一次拦截，然后**按铁律停手叫人**。这是纯假警报：守卫是对的，命令也是无害的只读。它每轮、每轨都会重演一次，而且踩到的人无从判断这次拦截是不是该报告的那种 | 本轨的修法落在派单侧（模板 §3 已写死判据与五条换写法）。**但 `CLAUDE.md` 是所有会话自动加载的第一份事实**，只在派单里写会漏掉不走派单的会话。建议人类在「开工自检」那条后面补一句指向：「其它只读命令被守卫拦下也多半是预期，判据与换写法见 `review/DISPATCH-TEMPLATE.md` §3」。`CLAUDE.md` 不在任何一轨白名单，**请人类一行改掉** |
| 2026-08-29 | P7 | **`review/` 由 `.git/info/exclude:7` 排除、不入库，于是「每轮一份的 paste」与「跨轮复用的资产」被同一条规则一起挡在版本库外。** paste 文件不入库是对的（一轮一份、靠粘贴交付，看板 §7 08-28 那行与 §8 抬头都记了这条惯例）；但派单模板与 `review/tools/` 下的排障脚本不是一轮一份 | 不处理的话，这类资产只活在某个 worktree 里，worktree 一清就蒸发；下一轮编排又从零抽一次共性。本轨已用 `git add -f` 逐文件点名把两个新文件入库绕过（记 `docs/DECISIONS.md` `## task-F2`），但那是**每次新增文件都要记得加 `-f`** 的手工绕法，下一个人不会知道 | `.git/info/exclude` 是本机、非共享文件，且在任何一轨白名单外。两条路：①在 exclude 里给这两条路径加放行（`!review/DISPATCH-TEMPLATE.md`、`!review/tools/`）—— 但 exclude 不随仓库分发，换台机器又是老样子；②**建议这条**：把跨轮复用的东西挪出 `review/`，模板归 `docs/ops/`、探针归 `scripts/` 或 `tools/`，让「`review/` = 一轮一份的草稿」这条规则重新自洽。①②都要动白名单外的文件，交人类定 |
| 2026-08-29 | P7 | **本轨核对过全局 `~/.claude/CLAUDE.md`「多轨并行派单的交付形式」一节与本模板，未发现互相矛盾之处**（模板已把该节的「`cd` 与 `claude` 同一行」「派单会过期，粘之前 grep 一遍旧 sha / 旧条数」两条逐条吸收）。唯一的缺口是**该节没有「守卫预期拦截」这一条** | 全局 CLAUDE.md 管的是所有项目，而「守卫按路径字面量拦只读命令」这件事只要项目装了同型 hook 就会重演。缺这一条意味着换个仓库、换套派单，同一个假警报还会再发一次 | 优先级低于上面第 1 条（那条影响的是本仓库每个会话）。建议等模板在下一轮实际用过一次、§3 的措辞被验证过之后，再把判据压成一两句放进全局 CLAUDE.md 的那一节。**本轨不动全局 CLAUDE.md**，它不在任何一轨白名单 |

## integrate-round-6

本轮并入**四轨**，基线 `c1049c2`：D-1（rework 第三出口）、D-2（第六道闸 plan 级判据）、
F-1（角色数口径：Manager 两处注释改成实测场景集合）、F-2（派单模板落盘）。
前两轨是派单 INT-6 的原定范围，F-1/F-2 是并轨协调方备料、经沈总批准后本轮增收的。
以下是本轮**按铁律 4 不当场改**的账。

F 轨两支的账各自记在 `## task-F1` / `## task-F2`，本节不重复；本节只记整合侧的发现。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`make_evidence.py` 同一次运行内部的 dirty 传染**：`scenario-R5` 的 7 个文件首行出处 sha 仍带 `-dirty`。sha 本身正确（本轮实测 `9c7483b`），dirty 来自它自己 —— 场景 1..7 先落盘就把工作区弄脏了，排在最后的 R5 取 sha 时读到的自然是脏状态 | 与 `## task-Z5`、自查单 A-2「已知缺口」是同一笔账，本轮**实测复现**：干净工作区下重跑，`INDEX.json` 与场景 1-7 全部干净，**只有 R5 那 7 个带 `-dirty`**。不影响判定（`RESULT: 7/7 PASS`），但自查单 §D 那条「evidence 首行 sha 不带 `-dirty`」的判据在 R5 上恒红 | 属 `scripts/make_evidence.py` 的面，不在本单范围。**最小修法**：进程启动时取一次 sha 存起来，八个场景共用，而不是每个场景各取一次 —— 这样「跑之前工作区干净不干净」才是它真正想记的那件事。自查单 A-2 已按「当前应只列出 scenario-R5 的 7 个」写死了预期，修好后那条也要跟着改 |
| 2026-08-29 | P7 | **`docs/clone-smoke-report.md` 的旧读数本轮一个字没改**（`521 passed` / `571 passed` / `4/74` / `0/33` / 前三遍逐步耗时） | 该文件是**某次冒烟的实测记录**，带明确的基线 sha 标注与时效声明，里面每个数字都是「那一次跑出来的事实」。照当前值改写它，等于伪造那次冒烟的结果 —— 比留着旧数字坏得多 | **已处理，不是欠账**：本轮按「历史快照，只增不改」的口径重跑了一遍全新克隆冒烟，另起 §2「第四遍（整合轮 6，基线 `e6075e5`）」，§1 克隆命令表补了一行，文末加了收口节。前三遍与 §3/§4/§5 全部原样保留。**剩下的真欠账只有一条**：§5 早就建议给自查单 A-1 补一句「且全程零非零退出、不需要跨节拼路径」（四遍秒数 6.57/6.44/5.4/6.89 几乎无差，而第一遍 6 处卡点、第四遍零卡点，掐表这个判据没有区分力），这条至今没落到 `docs/submission-checklist.md` |
| 2026-08-29 | P7 | **`docs/ppt-outline.md` 数字口径行末尾的两组 diff 统计（`+62−4` / `+273−7`）本轮没重算** | 这两个数按定义是 `git diff --shortstat` 的区间统计。本轮实测同区间已变成 `core/ +162 / −5`、`runtime/ +470 / −9`（见 `docs/domain-portability.md` 的整合轮 6 台账），所以口径行里那两组**确实偏小**。已在该行下加了一行 ⚠ 注明「本轮没重算」，没有写「已刷」 | 下一轮连同 `domain-portability.md` 的区间表一起刷 —— 两处是同一笔账，分开刷必然又对不上。或者更省事：口径行不再复述这两个数，改成一句「diff 统计以 `domain-portability.md` §2.4 为准」，单点维护 |
| 2026-08-29 | P7 | **`grep -c 'def _gate_'` 这个数闸法本轮开始失准**：在 `2474c56` 上数出 **9**，而闸仍是**七道** —— D-2 把 `_gate_finance` 拆成了「分发 + `_gate_finance_task` + `_gate_finance_plan`」三个函数 | `domain-portability.md` §1 的注脚块用这条命令逐端点实测闸数，是「两道新闸都不 import 业务域」那句话的实测支撑。本轮已在该块里如实写明 9 与七道闸的差别并给出正确数法（数 `_review` 的判据表），但**命令本身仍会数出 9** | 低优先，且**不建议为此改代码**（拆三段是 D-2 有理由的设计，见 `## task-D2` 的 DECISIONS）。若下一轮想让这条命令重新可用，改成数 `_review` 判据表的条目数即可；在那之前，谁引用这个数都要连注脚一起引 |
| 2026-08-29 | P7 | **`## task-D1` 记的 `_gate_gateway` severity 与第三出口在 `GW_QUERY_OR_HUMAN` 一格不同源，D-2 没有接** | D-1 当时写的是「交 D-2 或下一轮一并想」，而 D-2 本轮做的是第六道闸的 plan 级判据，没有碰 `_gate_gateway` 的 severity。所以这条**仍然悬着**：同一个 disposition 下，未知码给 `blocker`、已知的 `retriable=False + outcome=unknown` 码给 `info`，后者走不到第三出口 | 原样保留在 `## task-D1` 里，本轮只是确认它没被接走、不要以为 D 轮合完就消解了。判据落在 `gate.py`，与 D-2 本轮改的是同一个文件，下一轮动 `gate.py` 时一并处理最省事 |

## integrate-round-8

`integrate/round-ce`（C1-C4 / E1 / E2 / G1）、`integrate/round-6`（D1 / D2 / F1 / F2）与
`task/g2` 三条线收敛时，编排侧**实跑核验**发现下面几条。均**不在整合轮可改面内**
（整合轮只做合并 + 验证 + 本轮数字回填，问题交下一轮），按铁律 4 记账不当场改。
基线 `c1049c2`，收敛提交 `4d691fc`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`docs/clone-smoke-report.md` 的读数本轮仍一个字没改**，占位由 `PENDING-R6` 改挂 `PENDING-R9`。原因不是漏做：该报告的每一个数都归因于「仓库外全新 clone 的冒烟实跑」，而整合轮 8 没有做第四遍冒烟 | 拿仓库内的 `703 passed` 去填，等于伪造一次没做过的克隆冒烟 —— 正是该报告 §5 与 `DECISIONS.md` 反复立规矩禁的那件事。宁可留占位 | 需**单独一轨**：仓库外全新 clone、无任何 API key、掐表跑第四遍，把 pytest 条数 / verify 七项 / 各步耗时一起回填，并与 README §3 的读数对齐 |
| 2026-08-29 | P7 | **`grep -c 'def _gate_'` 这个数闸法已经失准**：本轮在 `maos/runtime/gate.py` 上实测数出 **9**，而闸仍是**七道**。`## integrate-round-6` 已记过一次，本轮复现确认 | 自查单里还在用这个命令数闸。一个印 9、判据写七道的自查条，评委一跑就对不上 | 换成不靠 `grep` 计数的判据（例如闸的注册表长度）。归下一轮代码面 |
| 2026-08-29 | P7 | **`docs/ppt-outline.md` 数字口径行末尾的两组 diff 统计（`+62−4` / `+273−7`）本轮仍没重算**。`## integrate-round-6` 已记，本轮没接 | 该行其余数字本轮全部刷成了 `4d691fc` 的实测值，唯独这两个仍是旧值，同一行里新旧混排 | 与下一轮材料面一起重算 |
| 2026-08-29 | P7 | **`scenario-R5` 的 7 个文件首行出处 sha 仍带 `-dirty`**（`4d691fc-dirty`）。`## integrate-round-6` 已记，本轮按同一路径复现 | 不影响 verify（`verify.py` 显式容忍 R5 的这个后缀），但每轮都要向读者解释一次 | 修 `make_evidence.py` 的落盘顺序（先全算后全写），归下一轮 |
| 2026-08-29 | P7 | **`docs/submission-checklist.md` 的「待整合轮 6 回填」一节标题已过期**，且其中第 2 条「`integrate/round-5` 并回 `goai-restructure`」早已完成（主干 `c1049c2` 已含） | 一节挂着「待整合轮 6」的清单出现在整合轮 8 的交付里，读者会以为轮 6 没收口 | 下一轮顺手改标题并清掉已完成项。本轮不擅自扩面 |

## task-H4

本轨只做一件事：让 `guardrails._shared_inputs` 取得到嵌在 `case_seed` 之类载荷里的
共享参数（基线 `1131795`）。下面几条是查这件事时**实测撞到、按铁律 4 不当场改**的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`suggested_tasks_from_docs` 会把历史文档里的 `amount_claimed` 原样抄到建议任务上**：它先抄历史 step 的 inputs，再用 `_shared_inputs` 的结果覆盖；而 `ORDER_FACT_FIELDS` 不含 `amount_claimed`（申报金额是客户诉求、不是订单事实，这个归类本身是对的），所以历史那份不会被丢弃。**本轨修的是「取得到就覆盖」**，`baseline` 顶层与嵌套**一份都没有**时，历史金额仍会原样留在建议任务上 | 第六道闸会按**历史那一单**的钱数判当前这一单 —— 正是 `_shared_inputs` 自己 docstring 里警告的「抄错一位数就是把闸绕过去」。现有测试 `test_kb_retriever.py::test_apply_suggestions_adds_step_without_carrying_facts` 的 baseline 恰好就是这个形状（历史 `9999.0` 被抄进建议任务），但那条测试没有断言金额，所以一直是绿的 | 改法有两条，都不在本轨白名单内：① 覆盖不到就**显式删掉** `amount_claimed`（宁可让闸的 plan 级判据接住，也不用别人的钱数放行）；② 把「知识层不许携带的触发量」单独立一份清单，与 `ORDER_FACT_FIELDS` 并列。①更省事但会改闸的触发面，需要与持 `gate.py` 的轨一起定 |
| 2026-08-29 | P7 | **R5 对照实验的两个金额是同一个常量**（`kb.experiment.AMOUNT = 6800.00`，历史 case 与当前 case 共用），于是上面那条症状在证据束里**完全没有表象** | 本轨修前修后，R5 的 `dag-diff.json` 逐项相同（`finance_gate` 仍是 blocker/pass、`finance_entries` 仍是 0/1）——「建议任务的金额来自历史文档」这件事，靠 R5 一个字都看不出来。要靠回归测试把两个数拉开才显形（`test_kb_nested_inputs.py`：历史 3200 阈下 / 当前 9000 阈上） | 属 `maos/kb/experiment.py`（本轨只读）。建议把历史 case 的金额与当前 case **拉开**，让证据束自己就能证明「补出来的财务任务用的是这一单的钱」。改动会动 R5 证据束的读数，须与证据面一轨一起做 |
| 2026-08-29 | P7 | **深度上限 `SHARED_SCAN_MAX_DEPTH = 4` 与 `runtime.gate.FINANCE_SCAN_MAX_DEPTH` 是两份各自写死的常量**，靠注释互相指认，没有任何机器守卫 | 两处扫的是同一片 `inputs` 树。哪天有人只改一边，症状是「闸看得见的金额，规划期取不到」—— 正是本轨修的这个 bug 的形状，只是换个深度重现一次，且不会有任何测试变红 | 加一条守卫测试断言两个常量相等即可（一行），但断言要落在哪个文件里、由谁持有，得等 `gate.py` 那轨收工后定。本轨不跨面加测试 |
