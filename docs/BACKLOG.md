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
| 2026-08-29 | P5 | **政策投影把 `channel_id` 一律落 NULL，而语料里有 4 条不是通配的规则**。`kb/experiment.py:promote_policy_rule` 的注释写着「语料里这些规则的 `channel_scope` / `sku_scope` 都是通配」——实测**不成立**：`scenarios/refund/policy/policy_rules.json` 里 `AS-004`（两个租户各 v1/v2，共 4 条「经销渠道差异」）的 `channel_scope` 是 `ch-dealer`，投影后在 `kb_doc` 里变成 `channel_id IS NULL` | 「文档侧 NULL = 通配」是阶段一的口径，于是**一条经销商专属政策对任何渠道的查询都是候选**。R5 当前就在踩：它的查询是 `channel_id="ch-online"`，候选集 5 条里就有 `kb-policy-tnt-mfg-a-AS-004-v1`。症状是「召回了一条不该出现在这个渠道的政策」——不报错，且在小库上分数还不低。这是**投影层的口径错误**，不是检索器的问题 | 修法一行：投影时 `channel_id = None if row["channel_scope"] == "*" else row["channel_scope"]`，`sku_scope` 同理。**本轨不改**：超出派单 §4 的两件事，且会把 R5 的 `candidate_count` 从 5 改成 4，动到证据束与 `test_kb_corpus.py` 的漏斗断言——属于要连着证据一起重跑并说明的改动，不该搭在本轨的车上。**建议下一次动 `kb/**` 的轨一并定**，改完把 R5 的候选集变化写进回执。**✅ 已于 2026-09-10 由 T118 处理**：`promote_policy_rule` 改从 `channel_scope` / `sku_scope` 取值（`*` 才落 NULL），AS-004 在自营渠道不再是候选。R5 的候选集因此少 2 条（同时因装入九类流程知识而净增），实测漏斗 40/21/7 -> 133/49/27。`test_kb_corpus.py` 新增 `test_channel_scoped_policy_is_not_a_candidate_off_its_channel` 钉住，原先那条「两侧候选集完全相同」的断言收窄成「通配的那几条两侧都在」——它原来的绿正是靠这个 bug。见 DECISIONS `## task-t118` |
| 2026-08-29 | P5 | **`workflow_version` 的类型分叉仍在**（`## task-X3` 第 5 条）：`kb/schema.sql:18` 声明 INTEGER，W-1 语料里是 `"1.0.0"` / `"1.1.0"` 字符串。派单 §4.3 要求「撞上就一并定」 | **本轨实测不触发，故未改**。场景 6 的检索上下文是 `tenant_id / biz_type / channel_id / sku`（+keyword），**不带这一维**；政策投影落的 `workflow_version` 一律是 NULL。阶段一按 `workflow_version = ?` 严格相等比对的那条分支在这条路径上根本走不到，改不改列类型对场景 6 一样 | 留给**第一个真的把 `workflow_version` 放进检索上下文**的那一轨，或第一个投影出非 NULL 值的那一轨。届时统一成字符串、列声明改 `TEXT`（版本号本来就是 `1.0.0` 这种形状，塞不进 INTEGER）。⚠️ `kb/schema.sql` 全是 `CREATE TABLE IF NOT EXISTS`、**没有迁移路径**，改列对已存在的库静默无效（`## task-R1` 第 5 条）。本轨已留守卫：`test_kb_provenance.py::test_scenario_6_retrieval_query_carries_no_workflow_version` 在这一维被加进来或投影出非 NULL 值时会红。**✅ 已于 2026-09-10 由 T118 处理，但方向与本条建议相反**：本条建议「统一成字符串、列声明改 TEXT」，T118 的派单要求「语料侧整数化、列不动」，按派单执行 —— 映射 `1.0.0 -> 1`（基线流程）、`1.1.0 -> 2`（带补偿分支）。选整数的实质理由是 `schema.sql` 全是 `IF NOT EXISTS`、改列对已存在的库静默无效（正是本条自己点出的那一条），而改语料没有这个问题。守卫 `test_kb_corpus.py::test_history_corpus_workflow_version_is_an_integer`。T118 的新语料也一律落整数，`task_pattern` 是第一类投影出非 NULL 值的知识 |
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

## task-H1

verify 第 6 项的自述层收口（FAILED 自称成功 + 四个元数据字段无校验）。
本轨白名单只有 `scripts/verify.py` / `maos/tests/test_verify_receipt.py` 与两份账本，
下面三条都在白名单外或超出派单 §5.2 的「就地比对」口径，按铁律 4 记账不当场改。
基线 `1131795`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`maos/tests/test_sandbox_degradation_visible.py::test_unaudited_warn_no_longer_calls_a_real_report_fake` 的 stub 与生成侧真产出不一致**：它手搭的 `business_outcome` 只有 `status` / `external_evidence` / `unaudited_evidence_count` 三个键，缺 `plan_state` / `basis` / `source`；且那条判据条目**不带 `provenance`**，却记 `unaudited_evidence_count: 1`。生成侧 `derive_business_outcome` 六个键恒全、每条判据必带 `provenance` | 本轨把这四个字段做进判据后，这条测试红（`StopIteration` + `chk.status == PASS` 两条断言都不成立）。它钉的是**warn 的措辞**，与自述层判据正交 | **已停手问人、经沈总批准后补齐**（补 `plan_state` / `basis` / `source` 三个键，判据条目补 `provenance="unknown"`，四处新增，断言与测试意图一个字未改）。**这是本轨唯一动过的白名单外文件，合并时留意**：它不在派单 §4 的独占表里，若同期有别的轨也改了 `maos/tests/test_sandbox_degradation_visible.py`，这一处会冲突。留这条不是欠账，是跨轨提示 |
| 2026-08-29 | P7 | **`provenance` 这个值本身仍然没有任何回查**。本轨新加的 `outcome_selfclaim` 只保证「自述的 `unaudited_evidence_count`」等于「列表里 `provenance == "unknown"` 数得出来的条数」，两边都取自 `result.json` | 攻击者把每条判据的 `provenance` 从 `unknown` 改成 `task_result`、同时把 `unaudited_evidence_count` 改成 0，两边**自洽**，于是不判负、warn 也照样消失 —— §5.2 那个「伪造干净」的洞换个改法还在。实测口径：干净束 warn 12 行 3 类，这一手能打到 11 行 | 要堵得重算入库路径（从 `event_log` 回溯每份产物的产出事件，与生成侧 `provenance` 字典同源），属**新查库**，超出派单 §5.2 明写的「四条就地比对，不需要新查库」。归下一轮：与第 4 项 trace-tree 的产物来源判定合并做最省事，两处推的是同一件事 |
| 2026-08-29 | P7 | **`git checkout -- evidence/` 还原出来的证据束内部不自洽**：`*.db` 按 `.gitignore:40` 不入库，还原只回滚了 json，库仍停在上一次 `make_evidence.py` 的产物上。两边 `plan_id` 随机且不同，于是 verify 报 `RESULT: 3/7 PASS`、exit=1 | 派单 §6 硬判据 1 写的复现收尾动作就是它。本轨第一次跑攻击复现时照做，四条判据全部报出**假红**，差点被当成回归。它只够让 `git status` 干净，不够让 verify 复绿 | 下一轮改派单模板：攻击复现的还原动作写成「重跑 `make_evidence.py`」或「先备份 `result.json` 再逐份复制回去」，`git checkout -- evidence/` 只留作 commit 前的清场步骤。本轨这次用的是备份复制法，实测还原后 `7/7 PASS`、exit=0、warn 12 行 |

## task-H2

第三出口的同款洞 + 网关闸 severity 与 disposition 不同源（分支 `task/h2-replan-exit`，
基线 `1131795`）。设计与取舍记在 `docs/DECISIONS.md` 的 `## task-H2`；
以下是本轨**按铁律 4 不当场改**的四条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`await == "human_decision"` 这个标记还有两处消费方各自硬编码字面量**：`maos/runtime/gate.py` 的 `HumanApprovalQueue.pending()`（`e["detail"].get("await") == "human_decision"`）与 `maos/kb/experiment.py:415`（`approved = await_kind != "human_decision"`）。本轨只把**产出侧**的两条分支收成了一处（`control_plane.AWAIT_HUMAN_DECISION`），消费侧一行没动 | 同一个约定现在有 3 份实现（1 产 + 2 消费）。产出侧改名不会报错，只会让 `pending()` 静默漏捞、让 `experiment.py` 把「等人裁决」误算成「已放行」—— 与 `## task-D1` 第 1 条是同一类失效形态 | 本轨白名单里 `maos/runtime/gate.py` 只许改 `_gateway_finding` 的 severity，`maos/kb/experiment.py` 根本不在面内，所以没动。下一轮动这两个文件时，让它们 import `AWAIT_HUMAN_DECISION`（方向 gate/kb -> control_plane，不成环）即可收口 |
| 2026-08-29 | P4 | **`## task-D1` 第 1 条里有两处与本轮实测不符**：(a)「非 H 任务撞上限后没有任何人捞得到」在基线 `1131795` 上**已不成立** —— 探针实测 `pending()` 捞得到；(b)「`test_replan_gateway.py` 与场景 5/7 用的都是 H 任务，所以一直没露出来」不成立 —— 该文件 `_make_task` 写死的就是 `effect_risk: "L"` | 下一轮的人照抄这条会去修一个已经不存在的洞，或按「用的都是 H 任务」这个错前提去设计复现，两次都会白跑一遍 | 建议下一轮把该条改写成它**现在**的形态：「洞已被 `pending()` 顺带覆盖，本轨补了回归钉住；剩下的是消费侧字面量三份实现」（即上面第 1 条）。本轨不改别人的账本条目 |
| 2026-08-29 | P4 | **`## task-D1` 第 4 条与 `## task-D2` 那条（`_gate_gateway` severity 不同源）已由本轨接走**，两处仍原样挂着 | 账本里三处（D1 第 4 条、D2 唯一一条、本节）说的是同一件事，其中两处的结论已经过时 | 下一轮收账时标记闭环，并把口径落到一句话：`GW_QUERY_OR_HUMAN` 已知码与未知码同为 blocker，两者都由第三出口兜 —— D-1 当时说「两种都行，但不能像现在这样没人写下来」，现在写下来了 |
| 2026-08-29 | P4 | **`ACQ.DISCORDANT_REPEAT_REQUEST` 从本轮起会挡闸**（severity `info -> blocker`）。当前 `maos/flows/` 的场景 1-7 无一注入它，`run.py` 输出实测逐行无差异 | 但 `scenarios/refund/history/history_cases.json` 的语料里有这条码（`kb-rc-0021`），`maos/tools/gateway.py:256` 也会在同幂等键参数不一致时真的返回它。哪天有场景走到那条路径，任务会停在 BLOCKED 等人，而不是像改前那样直接 DONE | 这是**有意为之**的收严（见 `docs/DECISIONS.md` 的 `## task-H2` 第 2 行），不是回归。记在这里是为了让下一轮加演示场景的人知道这一格的出口变了；若届时演示需要它放行，那要改的是场景设计，不是把这一格再降回 info |

## task-H3

受理幂等轨（分支 `task/h3-intake-idempotent`，基线 `1131795`）：把 `guard.create_case`
从裸 `INSERT` 改成幂等 upsert，只动 `maos/domain/refund/guard.py` 一个代码文件。
`## task-D2` 第 1 条点名的坑本轨已修；按惯例不回改别人的既有条目，处置理由见
`docs/DECISIONS.md` 的 `## task-H3`。以下三条是本轨看见但**在白名单外、按铁律 4
不当场改**的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`maos/skills/builtin/refund/intake.py:79` 那条注释的前提已被本轨推翻**。原文「纯规则 + 一次库写入。重试会撞 refund_case 主键，没有可重试的失败形态」，而 `create_case` 现在幂等，重试不再撞主键 | 注释给的理由已不成立，这是纠错；但同处的 `max_retries=0` + `failure_policy="escalate"` 还对不对是**策略判断**，两件事要分开：受理的库写入之外全是入参校验，确实没有别的可重试失败形态，保持 0 也说得通 | `intake.py` 不在本轨白名单（派单 §4 只给了 `guard.py`）。建议下一轮连注释带 `max_retries` 一并定：要么只改注释，要么把「重跑安全」这条新事实兑现成允许一次重试 |
| 2026-08-29 | P7 | **`maos/kb/experiment.py:41` 的模块 docstring 把 R5 without_kb 段那句 IntegrityError 归因于「受理 skill 在返工下不幂等」，本轨之后这个归因不再成立**。同段自己已声明是过渡态（「D-1 合并后这一段会变成『受理 BLOCKED，等人决策』」），而 D-1 早已并进主干 `1131795` | 纯文档失真，不改行为。本轨实跑 `make_evidence.py` + `verify.py` 仍 `RESULT: 7/7 PASS`、`business-ref 35/35`，与基线逐项相同 —— 恰好反证那条返工路径已不可达，这句归因描述的是一个现在跑不出来的现象 | `maos/kb/experiment.py` 是 D-2 的独占面，本轨一个字节没碰。`## task-F1` 第 1 条也在等同一个文件的另一处改动，建议一并处理 |
| 2026-08-29 | P7 | **退款域内出现了两种「重跑安全」口径**：`refund_case` 走本轨这条「同则幂等、异则报错」，而 `business_ref` 与 `customer_evidence` 走 `INSERT OR REPLACE` 静默覆盖（`objects.attach_business_ref`、`intake.py:137`） | 当前无症状，且对那两张表说得通 —— 它们存的是引用与证据指针，重跑覆盖成同一份值本就幂等，没有「被推进过的状态」会被盖掉。问题在于同一个域里两种口径并存而没有一处说明，下一个人照着哪张表抄都不知道自己抄的对不对 | **不建议为统一而统一**：`refund_case` 那条口径的理由是 `biz_status` 会被推进、`amount_claimed` 是财务闸的量，那两张表两条都不占。建议在 `docs/` 补一句「按表说明为什么口径不同」，归下一轮文档面 |

## task-H4

本轨只做一件事：让 `guardrails._shared_inputs` 取得到嵌在 `case_seed` 之类载荷里的
共享参数（基线 `1131795`）。下面几条是查这件事时**实测撞到、按铁律 4 不当场改**的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`suggested_tasks_from_docs` 会把历史文档里的 `amount_claimed` 原样抄到建议任务上**：它先抄历史 step 的 inputs，再用 `_shared_inputs` 的结果覆盖；而 `ORDER_FACT_FIELDS` 不含 `amount_claimed`（申报金额是客户诉求、不是订单事实，这个归类本身是对的），所以历史那份不会被丢弃。**本轨修的是「取得到就覆盖」**，`baseline` 顶层与嵌套**一份都没有**时，历史金额仍会原样留在建议任务上 | 第六道闸会按**历史那一单**的钱数判当前这一单 —— 正是 `_shared_inputs` 自己 docstring 里警告的「抄错一位数就是把闸绕过去」。现有测试 `test_kb_retriever.py::test_apply_suggestions_adds_step_without_carrying_facts` 的 baseline 恰好就是这个形状（历史 `9999.0` 被抄进建议任务），但那条测试没有断言金额，所以一直是绿的 | 改法有两条，都不在本轨白名单内：① 覆盖不到就**显式删掉** `amount_claimed`（宁可让闸的 plan 级判据接住，也不用别人的钱数放行）；② 把「知识层不许携带的触发量」单独立一份清单，与 `ORDER_FACT_FIELDS` 并列。①更省事但会改闸的触发面，需要与持 `gate.py` 的轨一起定 |
| 2026-08-29 | P7 | **R5 对照实验的两个金额是同一个常量**（`kb.experiment.AMOUNT = 6800.00`，历史 case 与当前 case 共用），于是上面那条症状在证据束里**完全没有表象** | 本轨修前修后，R5 的 `dag-diff.json` 逐项相同（`finance_gate` 仍是 blocker/pass、`finance_entries` 仍是 0/1）——「建议任务的金额来自历史文档」这件事，靠 R5 一个字都看不出来。要靠回归测试把两个数拉开才显形（`test_kb_nested_inputs.py`：历史 3200 阈下 / 当前 9000 阈上） | 属 `maos/kb/experiment.py`（本轨只读）。建议把历史 case 的金额与当前 case **拉开**，让证据束自己就能证明「补出来的财务任务用的是这一单的钱」。改动会动 R5 证据束的读数，须与证据面一轨一起做 |
| 2026-08-29 | P7 | **深度上限 `SHARED_SCAN_MAX_DEPTH = 4` 与 `runtime.gate.FINANCE_SCAN_MAX_DEPTH` 是两份各自写死的常量**，靠注释互相指认，没有任何机器守卫 | 两处扫的是同一片 `inputs` 树。哪天有人只改一边，症状是「闸看得见的金额，规划期取不到」—— 正是本轨修的这个 bug 的形状，只是换个深度重现一次，且不会有任何测试变红 | 加一条守卫测试断言两个常量相等即可（一行），但断言要落在哪个文件里、由谁持有，得等 `gate.py` 那轨收工后定。本轨不跨面加测试 |

## task-H5

H-5 轨（`scripts/matrix_probe.py` 的 ②a / ②c 两个假阴性，分支 `task/h5-probe-falsenegative`，
基线 `1131795`）。本轨只改探针那两段判据；以下是**按铁律 4 不当场改**的账。
取舍记在 `docs/DECISIONS.md` 的 `## task-H5`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **`should_deliver` 的回声否决分支（`sender == self_user_id`）在生产里是死代码。** `_NioChannel._send`（`hiclaw/matrix_bus.py:353-357`）发的是 **`m.notice`**（有注释写明理由：不触发人类推送、不和别的 bot 接龙），而 `listen()`（`:383`）把回调绑在 **`RoomMessageText`** 上。nio 的类型分发按 `isinstance` 过滤，`RoomMessageNotice` 与 `RoomMessageText` 是 `RoomMessageFormatted` 下的**兄弟类**，互不为实例 —— 于是 bot 自己的回声被挡在**比 `should_deliver` 更早的一层**，那条否决永远走不到 | 实测证据（H-5，只读 sync 旧房 `!xfRqhNYVNyuOMitWVs`）：绑 `RoomMessageText` 收到 7 条、全是 `@boss`；绑全量 `Event` 收到 10 条，多出的 3 条全是 `RoomMessageNotice` 且 `sender=@maos-bot`，**含编排轮 7 drip 的 #3/#6「发送方 bot」**。当前无症状（回声本来就该被丢），但**保护是两层里靠前那层给的，而判据写在靠后那层**：哪天 `_send` 改回 `m.text`（或新增一条走 `m.text` 的通知），回声过滤就成了唯一防线，而它从没被执行过 —— 也就从没被验证过 | `hiclaw/matrix_bus.py` **不在本轨白名单**，未动一字。建议下一轮把两处收成同源：要么 `listen()` 绑 `(RoomMessageText, RoomMessageNotice)`（回声真的流经 `should_deliver`，判据变活），要么在 `should_deliver` 的 docstring 里写死「回声分支是**冗余**防线，主防线是 msgtype 过滤」。**两种都行，但不能像现在这样没人写下来** |
| 2026-08-29 | P5 | **`## integrate-round-7` 第 1 / 2 两条的归因经实测均不成立**（本轨按派单要求复跑确认，未改动那两条原文 —— 不属本轨条目）。第 1 条说 ②a「没清 `next_batch`，而同一个 client 已被 ① / ③ 用过，`next_batch` 非空 …… 实际做的是增量同步」：实测 `whoami` / `room_get_state_event` / `room_send` **都不写 `next_batch`**（nio 只在 sync 响应处理里赋值，`async_client.py:709`），而 `AsyncClient.__init__` 把 `next_batch` 与 `loaded_sync_token` 双双初始化成 `""`（`base_client.py:238-239`）—— 所以 ②a 那次**本来就是**冷启动，补 `client.next_batch = ""` 是**空操作**。第 2 条说 ②c「过滤器本身是对的，错的只是计数」：实测回声压根没进回调，`should_deliver` **一次也没被调用**，不存在「过滤器挡住了」 | 两条的**现象**都真（②a 恒 skip、②c 恒印 0），**修法**却都会落空：照第 1 条改完 exit 不会变 0；照第 2 条只修计数会修不动，因为计数所在的回调根本收不到事件。本轨按实测重定根因后改的（见 `## task-H5` 决策），不是照抄 | **无需再处理**，此条只为存证。留着原文不改是有意的：那两条如实记录了当时观察到的现象与推断，事后证明推断错了 —— 按本仓惯例（`## task-F1` 第 2 条同款处置）不追改历史条目，在新节里写清推翻依据即可 |
| 2026-08-29 | P5 | **现役演示房 `!qcaXWSgkmosmxdYgpD:maos.local` 已积了 14 条 `m.notice`**，全是探针 ③c 的 `[matrix_probe] 探针连通性自检`（每跑一次探针 +1）。`m.text` 仍是 0 条 | `## integrate-round-7` 第 4 条已立规矩「截图前不要再往现役房发任何测试消息，包括 `scripts/matrix_probe.py`（它的 ③c 会 `room_send` 一条进去）」—— 本条是那条规矩的**实测欠账数**：换新房时房间是干净的，之后至少跑过 14 次探针。这 14 条会出现在演示截图里 | 与「补 `evidence/room/` 截图」那一轨**一并处理**：要么截图前再换一次空房，要么给探针加一个「只读模式」开关（跳过 ③c 的 `room_send`）。本轨已把 ②a 的 `/messages` 回溯做成**只读**、不新增任何发送，但没动 ③c —— 那是 §5 范围外 |

## task-H6

本轨买的是「Matrix 那一组环境变量的起跑线」（`maos/tests/conftest.py`）。落地后顺手
**逐个实测**了生产代码里其余的 `os.environ` 读取点，确认同一个根因还有哪几扇门开着。
下面几条**都不在本单白名单内，一个字没改**，按铁律 4 记账。基线 `1131795`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`MAOS_STORE_BACKEND` 没有起跑线**：机器上 export 了它，全仓测试跟着换后端。本轮实测 `MAOS_STORE_BACKEND=postgres python3 -m pytest maos/tests -q` → **683 passed, 20 errors**（干净环境 703 passed），报 `NotImplementedError`，集中在 `maos/tests/test_store_port.py` | 与本轨修的 Matrix 洞**同一个根因**：`maos/store/__init__.py:60` 读进程真环境决定后端，而测试默认按 sqlite 写。谁的机器上留着这个变量，一次 pytest 就凭空多 20 个 error，且报错指向 store 实现而不是指向环境 —— 又一次「把机器状态念成代码缺陷」 | 归下一轮测试面。**最小修法**与本轨同形：在 `maos/tests/conftest.py` 的 `MATRIX_ENV_VARS` 之外再加一组 `MAOS_STORE_BACKEND` / `MAOS_PG_DSN` 的 autouse delenv。本轮没做，是因为它超出本单白名单语义，且要先确认 `test_store_port.py` 里有没有用例**故意**依赖这个变量选后端（本轮只实测了现象，没查这一层） |
| 2026-08-29 | P7 | **`MAOS_MAX_REPLAN` 没有起跑线**：本轮实测 `MAOS_MAX_REPLAN=0 python3 -m pytest maos/tests -q` → **12 failed, 690 passed, 1 error**，散落在 `test_refund_failure.py`、`test_replan_gateway.py`、`test_kb_provenance.py` 三个文件 | 出处 `maos/core/control_plane.py:524`。重规划上限被外部拧到 0，多条场景用例的收敛路径当场变形。12 条红分布在三个互不相干的文件上，第一眼完全看不出是同一个原因 | 同上，归下一轮测试面一并处理 |
| 2026-08-29 | P7 | **本轮实测排除的三组**（记在这里是为了下一轮不必重查）：`MAOS_LLM_API_KEY`/`MAOS_LLM_BASE_URL`/`MAOS_LLM_MODEL` 齐全 → **703 passed**；`MAOS_SANDBOX_WORKDIR`/`MAOS_SANDBOX_TIMEOUT`/`MAOS_SANDBOX_FORCE_SUBPROCESS` 齐全 → **703 passed**；单独 `MAOS_FINANCE_THRESHOLD=0` → **703 passed** | 无影响，**不是欠账**。尤其 `MAOS_LLM_*` 这组值得写明：带真 key 的机器跑 pytest **不会**去调真模型、不会出网花钱 —— 这是本轨最担心的一种后果，实测排除了 | 无需处理。下一轮若给 conftest 扩面，这三组**不要**顺手加进去：删掉不影响结果的变量只会增加维护面 |
| 2026-08-29 | P7 | **`## task-C2` 第 1 条记的「真往房间发了 2 条消息」与当前实测不符**：本轮在 `1131795` 上按同一条件（四键齐全 + 一台连得通的 homeserver）复现，一次全量 pytest 是**连 homeserver 4 次、发 22 条**，其中 6 条带真实 task_id 的 TaskAssignment / TaskResult / ReviewVerdict | 差别不在结论（洞是真的、C-2 的修法也对），在**量级**：2 条像是「漏了个边角」，22 条 + 4 次连接是「一次 pytest 把演示房间灌了一屏」。C-2 当时大概只数了单文件那一处 `build(matrix=True)`；实际还有 `hiclaw/room_demo.py:209` 经 `test_room_wiring.py` 触发的 3 次 | **不改 `## task-C2` 那条**（不是本轨的账目，且它的结论没错）。记在这里供下一轮引用时以本条为准。复现手法：假 `nio` 模块经 `PYTHONPATH` 注入模拟一台连得通的 homeserver —— 本机系统 python3 未装 matrix-nio，直接 export 四个真键**复现不出来**，`open_channel` 会因 ImportError 立刻降级 |

## task-H7

证据束卫生三件（分支 `task/h7-evidence-hygiene`，基线 `1131795`）时，撞到的白名单外文件。
按铁律 4 记账不当场改。**第 3、4 条是本轨改动的直接连锁反应，下一轮必须接**——
本轨把 `scenario-R5` 的 `-dirty` 修掉了，而多处文档把「R5 恒带 `-dirty`」写成了判据。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`## task-C4` 第 3 条（本文件 `:551`）的归因不成立**。原文记「`scripts/make_evidence.py::scan_for_secrets` 只扫文本，扫不到 PNG」。本轨在 `1131795` 上实读确认：该函数**本来就按字节读**（`open(path, "rb")`），docstring 还专门写了为什么（sqlite 库就在同一目录，按文本读会解码失败而跳过）。既有测试 `test_scan_finds_sentinel_even_inside_a_binary_file` 一直守着这条 | C-4 那条**结论对、原因错**：现有守卫确实对截图完全不设防且是静默的，但**不是因为它扫文本**。真实原因是**截图里的 token 是像素不是字节** —— PNG 把文字压成图像数据，`Bearer <token>` 在文件里根本不以该字节序列存在，扫字节扫不到、扫文本一样扫不到。照错误归因去修（改成"扫得更狠"）会得到一个假装解决了的洞，比不解决更危险 | **本轨已按真实原因修**（见 DECISIONS `## task-H7`）：遇到图像/版式扩展名时显式报「无法核验」并拒收，而不是静默通过。此条只为改正记账，无需再动代码 |
| 2026-08-29 | P7 | **`## task-C4` 第 3 条给的建议①也随归因一起失效**。原文建议「遇到非文本文件时输出一行『跳过 N 个二进制文件，未扫描』」 | 这条建议隐含「二进制 = 没扫」，而实际上 sqlite 库这类二进制**扫得到也必须扫**（哨兵反查的主力对象就是它）。照做会把「扫过且干净」的库误报成「未扫描」，同时真正扫不了的图像反而混在同一句里 | 已作废，不要照做。正确的切分是按**扩展名**分「可扫」与「无法核验」两类，见 `scripts/make_evidence.py::_UNVERIFIABLE_EXT` |
| 2026-08-29 | P7 | **`docs/submission-checklist.md` 有四处把「`scenario-R5` 恒带 `-dirty`」写成了判据，本轨修复后全部过期，其中 `:99` 会直接反转**。四处是：`:55`（判据缩到 `scenario-1..7`）、`:87-98`（整个「已知缺口」小节讲根因）、`:99`（**「认下当前口径：`scenario-1..7` 干净、`scenario-R5` 的 7 个带 `-dirty`，其余组合都算异常」**）、`:257-258`（② 的注释「当前应只列出 `scenario-R5` 的 7 个」） | **`:99` 是硬伤**：本轨修复后 8 个场景全部干净，按这条判据反而**算异常** —— 下一个照单子勾的人会把一次成功的修复判成回归。`:87-98` 那节自己写了「若要全量干净，得让 `make_evidence.py` 在开跑时**一次性取定 sha**、全程复用」，本轨走的正是这条路，所以整节的「已知缺口」前提已消失 | **下一轮优先处理**：`:55` 把范围从 `scenario-1..7` 扩到 `evidence/` 全部；`:87-99` 整节删掉或改写成「已于 `task-H7` 消除」；`:257-258` 的注释改成「当前应为空」。`docs/submission-checklist.md` 不在本轨可改面内，故只记账不改 |
| 2026-08-29 | P7 | **`scripts/verify.py:80-83` 的注释过期**。原文：「`scenario-R5` 恒带这个后缀：它由 `build_r5()` 在场景 1-7 已经把 `evidence/` 改脏之后才自算 sha（其余场景共用主流程开头那一次干净的取值）」 | 注释描述的根因**本轨已消除**，但 `_SHA_DIRTY_SUFFIX` 的剥离逻辑**仍然正确且必须保留**（工作区真脏时照样带后缀）。所以这是纯注释过期，不是代码 bug —— 核验行为不变，本轨实跑 `verify.py` 仍 `RESULT: 7/7 PASS`、`exit=0`、warn 12 行 3 类 | 下一轮由持有 `scripts/verify.py` 的一轨（本轮 H-1）改这段注释即可。**不要动 `_SHA_DIRTY_SUFFIX` 本身** |
| 2026-08-29 | P7 | **`## task-C4` 第 4 条（本文件 `:552`）里「`evidence/scenario-R5/` 也同样不在 `INDEX.json` 里」这半句已过期**。本轨实测：`INDEX.json` 的 `produced` 当前含全部 8 条（`scenario-1..7` + `scenario-R5`），R5 是有登记的 | 该条前半句（不认识 `evidence/room/`）属实且本轨已修；后半句会让读者以为 R5 也漏登记，去修一个不存在的问题 | 无需处理，此条仅为改正记账。C-4 建议的「**建议后者**（在 README 里加一句说明，不改生成器语义）」本轨**没有采纳** —— 理由见 DECISIONS `## task-H7` |
| 2026-08-29 | P7 | `docs/DECISIONS.md:596` 记的那条决策（「三条判据在双命令架构下无法同时满足、只能三选二」，认下「1..7 干净 + R5 带 dirty」）其前提已被本轨消除 | 按本仓库惯例，`DECISIONS.md` 是**历史台账、只追加不改**，那一行记的是当时的真实判断，不该改写 | **不改那一行**。本轨在 `## task-H7` 追加新行说明现状已变即可。此条只为让下一轮读到 `:596` 的人知道去看后面 |
| 2026-08-29 | P7 | **`docs/submission-checklist.md:260-261` 的第 ③ 条判据「evidence 里出现过几个不同的 sha？（**应当只有一个**，忽略 `-dirty` 后缀）」在基线上就已经对不上**，与本轨改动无关。实测该命令输出**两行**：本次生成的 8 束一个 sha，`evidence/room/` 的两个文件一个 sha（`f42ea83`，C-4 轨落盘时的出处） | 判据写「应当只有一个」，而 `room/` 的出处**本来就该与 8 束不同** —— 它不由 `scripts/make_evidence.py` 产，每轮重跑证据束时不会跟着更新，保留原始出处才是正确的。所以这是判据本身没算上 `room/`，不是证据有问题。本轨已复核 `room/` 与基线 `1131795` 逐字节一致（`git diff 1131795 -- evidence/room/` 为空） | 下一轮把 ③ 的期望改成「`scenario-*` 系列只有一个 sha；`room/` 另有自己的出处，属正常」，或把命令的扫描范围限到 `evidence/scenario-*`。`docs/submission-checklist.md` 不在本轨可改面内 |

## task-H8

`docs/EXECUTION.md` 三处失真修订轨（分支 `task/h8-execution-doc`，基线 `1131795`）：
只改派单点名的三处（`:499`/`:502` 截图落点、`:710` 角色数、`:790`/`:793` 证据列），零代码改动。
以下是本轨看见但**按铁律 4 不当场改**的账。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`docs/EXECUTION.md:669`（本轨改后行号）「场景 R1/R2 留 `SCREENSHOT-HERE.md` 提示放 Element 截图」与本轨修掉的 `:499`/`:502` 同源**，但不在派单点名的三处内，故未改。两个问题：①「场景 R1/R2 的目录」在 D-05 编号映射下应是 `evidence/scenario-6,7/`，可这句读起来仍像要另建 R1/R2 目录，下一个人照做就会建出 `evidence/scenario-R1/`，把 `verify.py` 再打死一次（死法见本轨在 `:509-522` 补的实测块）；②`SCREENSHOT-HERE.md` **全仓没有任何实现**——本轨实测 `grep -rn 'SCREENSHOT-HERE' --include='*.py' .` 零命中、`find evidence -name 'SCREENSHOT-HERE*'` 无结果，`scripts/make_evidence.py` 不产它 | 手册要求的一个产物根本不存在，且指路方式会诱发 `verify.py` 的整体失败。当前无实际损害（没人真去建过），但它是 `:499`/`:502` 那个雷的**同一个雷的第二处引信** | 下一轮改这一行：落点写 `evidence/room/`（人机交互证据）并去掉或补实 `SCREENSHOT-HERE.md`。本轨已在 `:509-522` 写清「为什么不能放 `scenario-*`」，改这行时直接引它即可 |
| 2026-08-29 | P5 | **`docs/phases/phase-5.md:13` 是同一条未实现约定的第二处表述**，且场景号与 `EXECUTION.md` 互相打架：这里写「场景 3 目录留 SCREENSHOT-HERE.md」，`EXECUTION.md` 写「场景 R1/R2」 | 两份手册对同一个产物给出两个不同落点，且该产物两处都没实现。照哪份做都做不出来 | 与上一条同批处理，两处口径统一。`docs/phases/**` 不在本轨可改面内 |
| 2026-08-29 | P4 | **`evidence/room/README.md:110` 的补图自检命令在干净检出上「空过」**：`python3 scripts/verify.py 2>&1 \| tail -2`，判据是「报错原文不许出现 scenario-R1 / scenario-R2 / room」。但干净检出没有 `*.db`（`.gitignore:40`），verify 在第一个证据束 `scenario-1` 就 `exit=2` 退出。本轨实测原文：`[FAIL] 无法开始核验：缺数据库: <…>/evidence/scenario-1/maos.db`、`exit=2` | 报错里当然不会出现那三个名字，于是这条判据**永远通过、却什么都没验到**。补完图的人跑一遍看着绿，实际上 verify 根本没进核验 —— 恰恰漏掉了这条自检唯一要防的那件事 | 一行改动：在该命令前补 `python3 scripts/make_evidence.py`（有库之后 `tail -2` 打的是 `RESULT: 7/7 PASS` + 证据来源行，判据才真正生效）。`evidence/room/**` 是 C-4 的面，本轨只读 |

**本轨对既有账目的处置**（不改别人的条目，只在此登记结论）：

- `## task-C4` 第 1 条（`:499`/`:502` 打死 verify）：**实测复现，已修**。见 `docs/DECISIONS.md ## task-H8` 第 1 行。
- `## task-C4` 第 2 条（`:790`/`:793` 证据列指错目录）：**实测仍成立，已按其建议修**（`:790` → `evidence/scenario-6,7/`、`:793` → `evidence/scenario-7/trace.json`）。
- `## task-F1` 第 2 条（`:710` 十角色与 `AGENT_POOL` 9 个对不上）：**本轨实测判定该条记账不成立** —— `docs/EXECUTION.md:710` 描述的是生成物 `docs/agent-identity.md` 的内容清单，那份生成物确实是 10 个 Identity、软件交付域确实 6 个（含 `manager`）、退款域 4 个，两个数都对。F-1 当时的判断（「严格说这句没错，建议不改」）是对的。本轨按派单授权只做了**措辞增强**：在该行下补出「10 = 带 Identity 的角色总数，9 = 可派单数，`manager` 有 Identity 不进池」，不是纠错。

## integrate-round-9

H 轮八轨（H-1…H-8）合入时，编排侧**实跑核验**发现下面几条。均**不在整合轮可改面内**
（整合轮只做合并 + 验证 + 本轮数字回填），按铁律 4 记账不当场改。
基线 `1131795`，合并后 HEAD `627cce6`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`scripts/verify.py` 里 `_SHA_DIRTY_SUFFIX` 上方的注释已过期**：仍写着「`scenario-R5` **恒带**这个后缀」，而 H-7 修好落盘顺序后实测八个场景 sha 全干净。H-7 已记过一条（它当时量到的行号是 `:80-83`，合入 H-1 对同文件的改动后现在是 `:94-97`） | 一条写着「恒带」的注释旁边跟着一个再也不会命中的分支。`_SHA_DIRTY_SUFFIX` 常量**本身仍要保留**（工作区真脏时仍会写 `-dirty`），过期的只是「R5 恒带」这半句 | 下一轮持有 `verify.py` 的那一轨顺手改注释。**不要删 `_SHA_DIRTY_SUFFIX`** |
| 2026-08-29 | P7 | **`evidence/scenario-R1/` 那颗地雷只拆了手册一侧，`verify.py` 一侧还在**。H-8 把 `docs/EXECUTION.md:499/502` 的截图落点从 `evidence/scenario-R1/` 改到了 `evidence/room/`，但 `verify.py` 仍按 `d.startswith("scenario-")` 收目录 —— 编排侧本轮当场复现：`mkdir evidence/scenario-R1 && python3 scripts/verify.py` → **exit=2**，七项一项都跑不到 | 手册不再教人踩，但任何人手工建一个 `scenario-*` 目录仍会把核验器打死，且报错说的是「缺数据库」，指不到真正的原因 | 下一轮给 `verify.py` 加一条：遇到 `scenario-*` 目录但里面没有证据束文件时，报「这不是证据束目录」而不是「缺数据库」。归 `verify.py` 持有轨 |
| 2026-08-29 | P7 | **`docs/clone-smoke-report.md` 的读数连续两轮没改**，占位仍挂 `PENDING-R9`。理由同 `## integrate-round-8`：它每个数都归因于「仓库外全新 clone 的冒烟实跑」，整合轮 9 同样没做第四遍冒烟 | 报告里的 `521 passed` / `4/74` 与当前的 `749 passed` 差了两百多条，而它是 A-1「新克隆冒烟 ≤ 15 分钟」那条的执行记录 | 仍需**单独一轨**做第四遍冒烟。这是目前唯一一条跨两轮没动的待办 |
| 2026-08-29 | P7 | **`docs/submission-checklist.md` 的「待整合轮 6 回填」一节标题仍然过期**。`## integrate-round-8` 已记过一次，本轮没接 | 一节挂着「待整合轮 6」的清单出现在整合轮 9 的交付里 | 下一轮顺手改标题并清掉已完成项 |

## integrate-round-10

T 轮六轨（T-1…T-6）合入时，编排侧**实跑核验**发现下面几条。均**不在整合轮可改面内**
（整合轮只做合并 + 验证 + 本轮数字回填），按铁律 4 记账不当场改。
基线 `27c9e18`，合并后 HEAD `16563ef`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`_advance` 新增的「无活任务 → FAILED」分支，对已经是 FAILED 的计划再调一次就抛 `IllegalTransition: FAILED -> FAILED`**。本轮实跑复现：空规格重规划让计划收敛为 FAILED 之后，再手工调一次 `cp._advance(plan_id)` 当场抛。修复前这条路径是静默 no-op（`if tasks and all(...)` 的短路） | **今天够不到**：`_advance` 的两个调用点（`on_task_result` / `human_decision`）都要先走一次合法的任务迁移，而全冻结后的任务停在 PENDING+frozen，迟到的 TaskResult 会先在状态机上被拦掉。但它把一个「重复调用无害」的方法变成了「重复调用抛异常」，下一个接线的人踩得到 | 下一轮持有 `control_plane.py` 的那一轨。收法与 T-2 自己记的那条同源（把收敛判定并进 `start_plan` 的返回路径），顺带让 `_fail_plan` 对已是终态的计划短路 |
| 2026-08-29 | P7 | **`docs/domain-portability.md:23` 的锚 `maos/core/control_plane.py:380` 在基线上就是错的**，不是本轮改坏的。正文说的是「判据 `_should_replan`」，而 `27c9e18` 的 `:380` 指向 `_attach_compensation` 尾部的 `CompensationAttached` 日志行；该函数现在在 `:533` | 评委照锚点跳过去看到的是另一件事。同类锚本轮已修两处（`ppt-outline.md` / `matrix-room-runbook.md`，那两处是本轮 T-2 加行改坏的，属数字回填），这一处属**既有缺陷**，按整合轮口径只记不改 | 下一轮任何持有 `docs/domain-portability.md` 的轨顺手改。真要根治得给行号锚做一次全仓校验器 —— `gen_docs.py` 只管它自己生成的那 3 份 |
| 2026-08-29 | P7 | **`scripts/gen_docs.py --check` 在合入 T-1 后由绿转红**（`docs/skill-catalog.md`、`docs/toolport-contract.md` 落后于代码）。原因是这两份文档带 `.py:<行号>` 锚，而 T-1 往 `sandbox.py` / `code_repo_patch.py` 各插了几十行 | 本轮已跑 `python3 scripts/gen_docs.py` 重生成，改动是纯行号（4 处）。记下来是因为**它不在任何一轨的验收项里**：六轨各自的派单都没有这条，只有整合轮会撞上 | 流程项，不是缺陷：下一轮派单模板里，凡改 `maos/**` 源码的轨都该把 `gen_docs.py --check` 写进 §6 |

## task-T1

修 P0-1（C-quoted 路径绕过）时实测撞到的，均**不在本轨白名单内或不属派单范围**，按铁律 4 记账不当场改。
基线 `27c9e18`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`maos/tools/sandbox.py::_diff_targets` 对含空格的路径切分不正确**：`diff --git` 行用的是 `line.split()`，而 git **不**给含空格（但无特殊字节）的路径加引号。本轨实测 git 产出 `diff --git a/tests/my file.py b/tests/my file.py`，`parts[2:4]` 切成 `['a/tests/my', 'file.py']`，`_diff_targets` 返回 `['tests/my', 'file.py', 'tests/my file.py', 'tests/my file.py']` | **不是绕过，是错报**。本轨逐条实跑确认拦截仍然成立 —— 碎片保留了原路径的 `/` 分段，`tests` 照样命中 `PROTECTED_SEGMENTS`（含空格的 rename in / rename out 两种形态都实测被拦、文件未变）。真正的损害在结构化错误的 `path` 字段：实测报 `tests/my` 而不是 `tests/my file.py`，Gate 拿它转 findings 会把一个**不存在的路径**喂回 Coding，返工时改不到正确的文件 | 下一轮持有 `sandbox.py` 的那一轨改。`diff --git` 行的路径切分本身在 git 里就是有歧义的（`a/x b/y` 与 `a/x b` + `/y` 无法区分），可行的收敛是优先信 `---`/`+++` 行与本轨新增的 `_numstat_targets`（后者走 `-z`，路径不 quote 也不按空格切，实测报的是完整的 `tests/my file.py`），把 `diff --git` 行降级成兜底来源 |
| 2026-08-29 | P7 | **本轨给 `sandbox_git_apply` 加的同源校验让每次 apply 多跑一次 git 子进程**（`--numstat -z` 一次 + 真正 apply 一次），`_GIT_TIMEOUT` 也因此变成两段各 120s | 场景 1-7 端到端实测无感（`python3 run.py` 仍 `exit=0`，全量 `pytest` 从 749 条 10.24s 到 770 条 12.70s，增量主要是新增的 21 条用例自己建靶场），但它确实把这个热路径的子进程数翻了倍 | 只在有人量到 apply 变慢时再处理。真要省，可让 `--numstat` 的结果复用到 `--check` 那一路（两者都不落盘），但那会把「干跑」和「取路径清单」两件事耦合起来，收益不值 |
| 2026-08-29 | P7 | **`_path_segments` 的解码结果与「反斜杠 → 斜杠」那一步在语义上有残余冲突**：一个真实文件名里就带反斜杠的路径（`"a\\b.py"`，git 编码成 `"a\\\\b.py"`），解码后是 `a\b.py`，下一步会被切成 `a` / `b.py` 两段 | 方向是**保守**的（更容易命中受保护段，不会更松），且本轨已在 `unquote_c_style` 的 docstring 里写明取舍，故未改。但它意味着「解码后的段」与「git 眼里的真实段」在这一种输入上仍不完全相等 | 优先级低。真要修，得让 `_path_segments` 区分「解码前的转义反斜杠」和「解码后的真实反斜杠」，代价是签名要从 `str` 变成带标记的结构。POSIX 靶场上文件名带反斜杠本就罕见，等真出现再说 |

## task-T2

修 P1-2 / P1-3 / P1-4 / P1-5 / P2-9 时在**白名单外**撞见的三条，按铁律 4 记账不当场改。
基线 `27c9e18`，三条都在本轨实跑复现过，不是读代码推的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **一次 `TaskResult` 交回多份 `patch_set` 时，补偿指向哪一份是未定义的**。`on_task_result` 对 `p["artifacts"]` 里的**每一份** `patch_set` 各调一次 `_attach_compensation`，它们的 `patch_ref` 只写 `(task_id, kind, attempt)` —— 同一次结果里的多份补丁 attempt 相同，引用彼此无法区分；解析侧 `resolve_patch_ref` 又是「取第一条 `kind==patch_set` 且 `version==attempt` 的」，取到哪一条同样没有排序依据 | 与 P2-9 同源但**不是同一条**：P2-9 是「多条 compensation 选哪条」（本轨已在 `control_plane.py` 侧修死），这条是「一条 compensation 指向的 patch_set 有多份」。真出现时的后果一样 —— 反向应用了错误的那份补丁。目前所有场景每次结果只交一份 patch_set，所以没暴露 | 解析口径在 `maos/artifacts.py`（冻结面，可读不可写），修它要人类先定「一次结果多份补丁该不该各附一条补偿」这个语义问题。**归整合轮，本轨不碰** |
| 2026-08-29 | P7 | **零任务计划 / 全冻结计划在 `start_plan` 之后不会自行收敛**。本轨实测：`create_plan(tasks=[])` + `start_plan()` → 计划停在 `RUNNING`；再手工调一次 `_advance()` 才落 `FAILED`（本轨新加的收敛判定） | 理论边界，**当前没有调用点**会这么用：唯一产生冻结任务的路径 `_replan` 已由本轨在出口处兜住。留着是因为 `start_plan` 是公开方法，下一个接线的人可能踩 | 下一轮持有 `control_plane.py` 的那一轨，把收敛判定并进 `start_plan` 的返回路径（`dispatch_ready == 0` 时顺带问一次）。本轨不做 —— 派单范围是那五条，改 `start_plan` 会波及所有场景的启动路径 |
| 2026-08-29 | P7 | **`human_decision` 重复投递抛 `IllegalTransition` 而不是短路返回**，这是本轨**有意保留**的既有行为（见 `docs/DECISIONS.md` 的 `## task-T2`），但它对 UI 不友好 | 接上真实审批界面后，操作员双击「驳回」会拿到一个异常而不是一次无害的重复提交。副作用已经挡住了（补偿只跑一次），剩下的纯粹是交互口径问题 | 等真做审批 UI 时由人类定：要么在调用侧吞掉这个异常，要么把控制面改成短路返回。现在改属于「为假想的调用方放宽守卫」，不做 |

## task-T3

T3 轨修 `maos/core/store.py` 三处（P2-6 / P2-7 / P2-8）时发现，两条都**不在本轨白名单内**，
按铁律 4 记账不当场改。基线 `27c9e18`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **派单 §2/§4 的前提「改 `update_task` 签名会牵动 `maos/store/port.py` 与 `pg_store.py`」实测不成立**。`StorePort` 协议一共只有 5 个方法（`execute` / `query` / `fts_search` / `vector_search` / `dialect`），本轨实测 `grep -c "update_task" maos/store/port.py` → **0**。`update_task` 只存在于 `maos/core/store.py` 的 `Store` ABC 与 `SqliteStore` 上 | 收窄 `update_task(**fields)` 为显式 `fields: dict` 的成本比派单假设的低得多：只牵动 `Store` ABC 一处声明 + 4 个调用点（`maos/core/control_plane.py:177`、`:619`、`:646`，`maos/tests/test_trace_evidence.py:126`），完全不碰 `maos/store/**`。开放 kwargs 是这个注入面的**根因**，白名单只是把它堵住 | 本轨仍按派单红线**没改签名**。下一轮若要根治，按上面这个实际影响面重新评估，别再按「会牵动 PG 适配」估成本 |
| 2026-08-29 | P7 | **两套 store 实现的注入防护此前不对等**。`maos/store/sqlite_store.py:71-80` 早有 `_IDENT` 正则卡标识符形状，注释明写「这里不卡形状就等于开了一条注入路径」；而同仓 `maos/core/store.py` 唯一一处拼标识符的地方（`update_task`）此前一道防护都没有 | 同一个仓库两条 SQL 出口，一条守得很紧、另一条完全敞开，敞开的那条还是主链路（`control_plane` 走的是 `core/store.py`）。本轨已给它补上白名单（严于正则），但**没有任何机器提醒**要求新增的拼标识符代码也这么做 | 下一轮考虑加一条守卫测试：扫 `maos/**` 里所有把变量拼进 SQL 的 f-string，要求每处都有配套的形状校验或白名单。本轨只修了已知的这一处 |

## task-T4

T 轮真房间取证（基线 `27c9e18`）。照 `docs/matrix-room-runbook.md` 在**真 Matrix 房间**
跑通 approve / reject / 越权三条路径时发现下面这些。均**不在本轨可改面内**
（本轨一行 `maos/**` 与 `hiclaw/**` 源码都不改），按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P4 | **`房间回话失败（）` 是虚警，且报错原文里没有任何原因**。`_NioChannel._await()` 用 `run_coroutine_threadsafe(...).result(self._timeout)`，`_timeout` 缺省 **10.0s**（`hiclaw/matrix_bus.py:272`）。Synapse 默认 `rc_message` 限流会让一次 send 被 nio 退避拖过 10s（实测一轮 approve 打出 4 条 `Got 429 response (ratelimited), sleeping for ~4.5s`），此时抛的是 `concurrent.futures.TimeoutError` —— 而**它的 `str()` 恰好是空字符串**，于是 `log.warning("房间回话失败（%s）", exc)` 打出来就是一对空括号 | 消息其实**送达了**（协程还在私有循环上跑完），可操作者看到的是一条没有原因的失败告警。T 轮 R1 打了 3 条这个警告，房间里 23 条消息一条不少。演示当天看到它的人会以为房间挂了，去重跑 —— 而重跑只会再撞一次限流 | 归 `hiclaw/matrix_bus.py` 持有轨。两件事分开做：①`except` 里把空 `str(exc)` 兜成 `type(exc).__name__`，别让告警说不出原因；②`_timeout` 对 send 放宽（或按 429 的 `retry_after_ms` 自适应）。**不要**简单调大到掩盖问题 —— 限流本身也该在 runbook 里可见 |
| 2026-08-29 | P4 | **驳回路径的补偿结果无人可见**。`human_decision(approved=False)` 确实调了 `_execute_compensation`（`maos/core/control_plane.py:657`）并往 `event_log` 写 `CompensationExecuted`，但：成功路径**一行日志都不打**（只有 `sandbox_unavailable` 那条老分支 warn）；`CompensationExecuted` 走 `append_event_log`、**从不 publish**，因此永远不进房间镜像；`ok=true` 与 `ok=false` 的**退出码都是 0** | 「补偿到底成没成功」在 `room_demo` 这条路径上**没有任何外化形态**。演示当天说「驳回触发了补偿」是站不住的 —— 台下要一个证据，台上拿不出来 | 归 `maos/core/control_plane.py` 与 `hiclaw/` 两轨协商。最小改动是补偿完落一条 INFO（带 `ok` 与 `stage`）；更彻底的是让 `CompensationExecuted` 也进房间镜像 —— 但那要动镜像的事件白名单，得先定「房间里该不该出现非迁移类事件」这个口径 |
| 2026-08-29 | P4 | **照 runbook §2 建的空 workdir 会让补偿恒 `ok=false`**。`room_demo` 用 `seed_scripted_report` 预置报告，**从不**调 `verify_patch_in_sandbox`，所以正向补丁从没被打进 `MAOS_SANDBOX_WORKDIR`；驳回时 `git apply -R` 在一个空目录上必然失败。实测 `{"stage":"apply","path":"auth/session.py","message":"error: auth/session.py: No such file or directory"}`。对照：台架里先 `prepare_sandbox_workdir()` + 打正向补丁，同一条路径就是 `ok=true` | 与上一条叠加，后果放大：不但看不见结果，**结果本身就是失败的**，而且这个失败是「照手册做」必然踩到的。runbook §2 已按实测改写并写明，但那只是把坑标出来，坑还在 | 归 `hiclaw/room_demo.py` 持有轨。建议 `--case reject` 启动时不只检查 workdir 存在，而是**自己把靶场基线备好并打上正向补丁**（`prepare_sandbox_workdir` + `sandbox_git_apply` 两行），让补偿演的是真回滚。**别**改成「补偿失败就不报」——那是反方向 |
| 2026-08-29 | P4 | **`evidence/room/04-reject-compensation.png` 名不副实**。文件名承诺「补偿」，但房间里拍不到补偿（原因见上两条）。这张图实际证明的是「驳回生效 + Plan 落 FAILED」 | 评委按文件名找补偿证据会扑空，而这恰恰是「口径说过头」的典型形态 —— 名字本身就是一句没有证据的断言 | 改名要一起动三处：本文件名、`docs/EXECUTION.md:499/502`、`evidence/room/README.md` 的清单。后两处 T 轮有一处不在可改面内，故**只改了说明没改名**。建议下一轮统一改成 `04-reject-effect.png` |
| 2026-08-29 | P4 | **`docs/submission-checklist.md` §A-4「Matrix 房间」那一行已过期**。仍写着「只能这么说：镜像层已实现，降级路径实测等价，**真房间待接通**」，复核列写「✅ `hiclaw/matrix_bus.py` 在，真房间未接通」 | 该行现在是**把做到的说少了**，方向与它自己要防的相反。照它写材料会主动放弃一块已经拿得出证据的分 | 归整合轮。改法：「只能这么说」列换成「镜像层已实现，降级路径实测等价，真房间三条路径实测通过，截图与逐字副本在 `evidence/room/`」；「不许这么说」列保留并补一条「退款全过程在 Element 里跑通」（房间里跑的是软件域 `room_demo`，不是场景 6/7） |
| 2026-08-29 | P4 | **`room_demo` 退出时刷一屏 asyncio 报错**：`RuntimeError: Event loop is closed`、`Task was destroyed but it is pending!`、`Exception ignored in: <coroutine object AsyncClient.sync_forever …>`。收口时私有事件循环先关、`sync_forever` 常驻协程后死 | 发生在终态之后，不影响判定也不影响退出码（两轮都是 exit=0）。但演示当天终端停在这一屏上，观众看不出它无害 | 归 `hiclaw/matrix_bus.py` 的 `_NioChannel.close()`：关循环前先 `cancel()` 掉 `_sync_task` 并 await 它结束。优先级低于前三条 |
| 2026-08-29 | P4 | **房间里留着 14 条上一轮中断运行的遗留消息**（审批卡发出后无人审批，进程被结束）。T 轮没有删，只在 `transcript.md` 与 README 里记了采集窗口边界 `$KI0ij47tDAxI68MYTdosnSmyygg1jjOEq2eiXLflyxg` | 演示当天从头滚房间会先看到一段没有结局的旧运行，需要临场解释一句。删了则要解释「为什么房间历史被修剪过」，那更糟 | 演示前若要一间干净房：新建一个非加密房、更新 `room.env` 的 `MATRIX_ROOM_ID` 即可，代码一行不用改。**别删旧消息**。归演示前的准备一轨 |
| 2026-08-29 | P4 | **`transcript.md` 的生成器没有落进仓库**。它是从 client-server API 拉房间历史直接落盘的脚本，本轮跑在 scratchpad 里；`scripts/` 不在本轨可改面内 | 下一个人想重新生成逐字副本，得照 `transcript.md` 抬头那条 API 命令自己重写一遍脚本。出处命令是写全了的，但「照着重写」比「跑一个脚本」更容易走样 | 归 `scripts/` 持有轨：把它收成 `scripts/gen_room_transcript.py`，保留内置的脱敏自检（扫到 token 真值就拒绝落盘） |
| 2026-08-29 | P7 | **`evidence/INDEX.json` 的 `aux_bundles` 对 `evidence/room` 记的是 `file_count: 2`，实际已是 7**（5 张 PNG + 2 份 md）。本轨跑 `make_evidence.py` 时它被正确重算过（且给 PNG 标了 `secret_scan: "无法核验（图像，密钥是像素不是字节）"`，口径很好），但 `INDEX.json` 不在本轨可改面内，按派单 §6 判据「`git status` 只应出现 §4 表里的文件」**已还原** | 索引与目录实际内容对不上，直到下一次有人跑 `make_evidence.py`。自愈成本为零（整合轮必跑），但在那之前照 `INDEX.json` 清点房间证据会少数 5 张图 | 整合轮重跑 `make_evidence.py` 时自动消失，**不需要专门处理**。记在这里只是为了让下一个人看到 `file_count: 2` 时不去查「图是不是没提交」 |

### 已处理（2026-08-31，基线 `926aa7b`）

上表第 1 条（空括号虚警）与第 6 条（退出时 asyncio 报错）**已修**，连同 runbook 抬头
那条「用系统 `python3` 跑」和 §7.4 的「bot 不听自己回声」一起做的 —— 四条的共同形态
都是「安静地什么都没发生」，分开修会各自留一半。

| 原条目 | 按建议做了什么 | 落点 |
|---|---|---|
| 空括号虚警 | 建议的两件事都做了：① 所有异常日志过 `describe_exc()`，类名一律带上、空消息补占位、超时类追一句「不要重跑」；② send 单列 `DEFAULT_SEND_TIMEOUT = 30s`，与构造期的 10s 分开。**没有**用「调大超时」去掩盖限流：nio 的 `Got 429 response` 照旧打出来。另加一条建议里没写、但比那两条更要紧的：超时抛 `RoomSendTimeout`，`_mirror` 里**不计入** `MAX_MIRROR_FAILURES` —— 原来撞一次限流就够 3 次、直接永久降级，虚警被自己做实成真故障 | `hiclaw/matrix_bus.py`、`hiclaw/transition_mirror.py` |
| 退出时 asyncio 报错 | 照建议改 `_NioChannel.close()`：`cancel()` 掉 `_sync_task` 并等它落地。另补了建议里没提的三步 —— `shutdown_asyncgens()`、`join()` 线程、`loop.close()`，否则 aiohttp 连接池仍会在 GC 时去碰死循环 | `hiclaw/matrix_bus.py` |

判据：`maos/tests/test_matrix_bus.py` 第 8 节、`test_room_wiring.py` 第 4 节，共 25 条。
四条修复逐个撤掉做过变异检验，每条都能让对应用例变红。

上表其余条目**未动**（补偿结果无人可见、空 workdir 恒 `ok=false`、`04` 文件名、
`submission-checklist` 过期、房间遗留消息、`transcript.md` 生成器、`INDEX.json` 条数），
它们不属于「安静地什么都没发生」这一类，归属轨也不同。

## task-T5

T5 轨把 `docs/ppt-outline.md` 渲染成 `artifacts/` 下的演示稿（HTML 正本 + PDF）。
下面几条是渲染时**逐条打开核对**撞见的，均在本轨白名单外（`docs/` 其余文件与 `evidence/` 都归别的轨），
按铁律 4 记账不当场改。基线 `27c9e18`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`docs/architecture.md:59` 的内核 diff 数字已偏小**：写着 `maos/core/ +46/−2`、`maos/runtime/ +273/−7`，本轨实测 `git diff --shortstat 90251b3 27c9e18` 是 **`core/ +194/−10`、`runtime/ +497/−9`**。`## integrate-round-6` 记的是 `ppt-outline.md` 末尾那两个数，**这一处是另一个落点**，且它是 README §2 架构图的图源 | 分层图的「读法」段落是「换域代价」论证的门面，数字比实际小 3–4 倍。评委若自己跑 `git diff` 会对不上，而这一段恰恰是让人去跑的 | 下一轮持有 `docs/architecture.md` 的那一轨顺手重算。演示稿 P12 已按实测值写，两边届时要对齐 |
| 2026-08-29 | P7 | **`docs/domain-portability.md` 的区间 B 端点仍钉 `2474c56`**（`:64`、`:72`），那是整合轮 6 的 HEAD；主干已到 `27c9e18`，其后 H 轮八轨的内核增量**不在任何区间里** | 「两个区间分开算」这套口径是该文件最有价值的部分，但区间 B 现在只覆盖到一半，读者会把 H 轮的增量误算进区间 A 或漏掉 | 与上一条同批做：重算时把区间 B 端点推到当轮 HEAD，或新开区间 C |
| 2026-08-29 | P7 | **`docs/open-questions.md:15` 的 OQ-1 仍标「未答」**，但 T 轮派单 §0.2 已给出人类从复赛规则原文转述的四维权重（场景价值与复用性 20% / 多 Agent 协同 25% / Skill 工程体系 20% / 工程落地与安全审计 30% / 开源贡献 5%）。本轨已在 `docs/ppt-outline.md` 抬头与「表 C」按新口径写，但**没有动 `open-questions.md` 本身**（白名单外） | 一份标着「未答」的待答清单，和一份已按答案组织的大纲同时存在。下一个读 OQ-1 的人会以为口径还没定，可能重新拒绝填四维 | 整合轮把 OQ-1 标为「已答」并写明出处是人类转述的官方通知；`docs/submission-checklist.md` §B 对照表的「四维」列同批填上 |
| 2026-08-29 | P7 | **`docs/clone-smoke-report.md` 的 `521 passed` 与演示稿 P14 的 `749 passed` 直接矛盾**（`:78`、`:94`）。`## integrate-round-8`／`## integrate-round-9` 已记过「该报告读数连续两轮没改」，本轨补的是**新出现的交付面后果**：这两份材料都会交到评委手上 | 评委同时打开 PPT 与冒烟报告，会看到同一条命令的两个结果差 228 条。这比单纯「文档过期」更伤 —— 它长得像证据造假 | 仍需**单独一轨**做第四遍仓库外全新冒烟。在那之前，若要先交材料，至少把报告里的读数标注为「基线 `<旧 sha>` 的历史记录」 |

## task-T6

本轨（Demo 录制就绪）只持有 `docs/demo-script.md`、`scripts/demo_preflight.sh`（新建）、
`docs/open-questions.md` 的 OQ-1/OQ-2 两节，外加两份账本。下面都是**白名单外**发现的，
按铁律 4 记账不当场改。基线 `27c9e18`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`docs/submission-checklist.md:205` 的 Demo 前置条目已过期**：写着「录制前跑完该文件的**五条命令**，全绿」。T 轮把那 5 条收敛成了一条 `bash scripts/demo_preflight.sh`（逐条断言、退出码=出错步号），`docs/demo-script.md` 里已经没有「五条命令」这个东西了 | 照自查单勾的人会去 `docs/demo-script.md` 找五条命令，找不到；而真正该跑的那一条脚本自查单里一个字没提 | 下一轮持有 `submission-checklist.md` 的那一轨（整合轮）改成「跑 `bash scripts/demo_preflight.sh`，exit=0」。**顺带**：`:206` 的「三件事（Element 是否接通 / replan 是否合并 / 审批由谁驱动）」括号内也过期了 —— replan 早已合并，第 1 件事现在是 A/B 双分支 |
| 2026-08-29 | P7 | **`docs/submission-checklist.md:208` 的时长判据与新分镜对不上**：写「时长 3–5 分钟（手册口径）」，而 T 轮补完必含的 Skill 镜后总长是 **5:26**，超了 26 秒 | 按自查单勾会判红，但那不是回归 —— 是「补必含项」与「手册建议时长」两个口径撞了。`docs/open-questions.md` **OQ-2** 转录的官方上限是 ≤ 8 分钟（🟡 待核实），5:26 在其内 | 与上一条同批处理。改法取决于 OQ-1/OQ-2 核实结果：官方上限 ≥ 5:30 就把判据改成官方上限；若官方上限是 5 分钟，则回 `demo-script.md` 砍 26s（从镜 3／镜 5 的富余里借，已在分镜里写好怎么借） |
| 2026-08-29 | P7 | **`evidence/room/transcript.md:14` 断言 `deploy/synapse/` 与 `hiclaw/room_demo.py` 均未交付，实测两者都已存在**：`hiclaw/room_demo.py` 15532 字节、`deploy/synapse/` 下有 `down.sh` / `element-config.json` / `README.md` | 那句话是「房间为什么没证据」的归因，归因已经不成立了。真正卡住的只剩「截图没采 + 本机 `import nio` 仍 `ModuleNotFoundError`」两条，写成「三轨都没交付」会让下一个人去重做已经做完的事 | 归 T4 轨（`evidence/room/**` 是它的面）。本轨在 `docs/demo-script.md` 的 A/B 双分支里已按**实测**写了正确的前置条件，没有沿用这句过期归因 |
| 2026-08-29 | P7 | **`docs/agentteams-mapping.md:52-54`「当前真实状态（不吹）」的基线是 `df96fa8`，比主干旧很多**，其结论「Synapse 账号需要人类手工注册、该任务仍未开工」与上一条同源 | 这份文档是 AgentTeams 必含项的口径来源，被 `demo-script.md` 引。基线过期会让引用它的人得出过时结论 | 归 T4 轨。本轨引用它时只引**五项映射表**（那部分实测仍成立），没有引「当前真实状态」一节的结论 |
| 2026-08-29 | P7 | **手册 `docs/EXECUTION.md:744` 起的原分镜 8 镜里没有任何 Skill 镜头**，而规则把「Skill 调用过程」列为 Demo 视频必含项（🟡 转录，见 OQ-1） | 照手册那 8 镜录，成片会缺一个必含要素。T 轮已在 `docs/demo-script.md` 补了镜 7，但**手册本身没改**，下一个照手册排分镜的人会再踩一次 | 归持有 `docs/EXECUTION.md` 的轨。改法：在 `:744` 那份清单里补一条 Skill 镜，或加一句「本清单早于规则必含项，以 `docs/demo-script.md` 为准」。**等 OQ-1 核实后再动**，别拿转录件改手册 |
| 2026-08-29 | P7 | **`scripts/verify.py` 的 `business-outcome` warn 单行过长**（实测一条 200+ 字，含完整归因段落），录制镜 8 时在 100 列窗口里要折成 3 行，4 条就吃掉十几行画面 | 镜 8 的镜头要往下滚到 `RESULT: 7/7 PASS` 才停，warn 越长滚得越久，占的是念词时间。不影响判定，纯粹是**上镜体验** | 归 `verify.py` 持有轨。可选改法：warn 正文只留一句，长归因收进 `--verbose`。**不急** —— 分镜里已写明「往下滚到 RESULT 行再停住」，当前富余 +3.1s 够用 |
| 2026-08-29 | P7 | **`scripts/make_evidence.py` 产出的 `business-objects.json` 里，两个 plan 的对象块先后顺序不稳定**。实测连跑三次，场景 7 的 `amount_paid: 6800.0`（`task-s7-*` 组）落在**第 41 → 152 → 152 行**，`task-s7-*` 与 `task-s7b-*` 两组整体换位；git 里 `27c9e18` 提交的那份是 s7 组在前 | ①**任何引用该文件行号的文档都会周期性失效**，而录制前置必然重跑一次证据束 —— 分镜镜 2 原来写的「第 4–22 行附近」就是这么烂掉的（T 轮已改成按 `task_id` 现场 grep，不再依赖行号）；②证据束的**逐字节可复现性**因此不成立：同一份代码同一份数据，两次跑出来的 `business-objects.json` 不是同一个字节序列，只是内容等价。这不影响 `verify.py`（它按对象读，不按行读），但会让「重跑证据束 → 只有出处头变」这个直觉失效 | 归 `make_evidence.py` 持有轨。改法应该很小：落盘前按稳定键排序（如 `(plan_id, task_id, object_type, object_id)`）。**做之前先确认 `verify.py` 的哈希校验不依赖当前顺序** —— 第 1 项 `hash-integrity` 校的是 `event_log` 里的 digest，与本文件的排布无关，但值得跑一遍确认 |
| 2026-08-29 | P7 | **`maos/kb/experiment.py` 的一条 `logging` 输出打到 stderr，让 `python3 -m maos.kb.experiment` 的屏幕输出从 2 行变 3 行**（多出 `[task-nokb-intake] plan_defect —— 机器返工修不好，一次转人工，不再重发`） | 不是 bug（那行本身是真实且有信息量的），但上一版分镜写死了「屏幕上只打两行」，实测已不符 —— T 轮已在镜 3 改正并标注「别把它当成报错」。留档是因为**下一个改这个模块的人可能又把行数改掉** | 不建议改代码。若将来要让这一镜更干净，可考虑把该日志降级到 DEBUG；改之前先看 `docs/demo-script.md` 镜 3 —— 那里写死了 3 行 |

## task-T7

本轨（三组对照 case 接线）持有 `maos/flows/contrast.py`、`maos/domain/refund/fixtures.py`、
`maos/agents/refund/channel_agent.py`（人类当场授权，见 `docs/DECISIONS.md ## task-T7` 第 1 行）、
`run.py`、`scripts/make_evidence.py` 的 `--contrast` 分支、`maos/tests/test_contrast_cases.py`、
`scenarios/refund/README.md`，外加两份账本。下面都是**白名单外**发现的，按铁律 4 记账不当场改。
基线 `4cfef38`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | **`maos/skills/builtin/refund/policy.py` 的 `policy.match` 没有时限窗口判定器**：它的 approve/reject 只判「有没有命中 `AS-` 前缀的规则」，不评估规则参数里的 `no_reason_days`。`scenarios/refund/README.md` 与 `## task-W1` 已记过这条，本轨补的是**新后果：它现在有消费方了** | 对照组 R3 的「同一条 AS-001，租户 A 窗口 30 天通过、租户 B 窗口 7 天驳回」无法由 skill 给出，本轨只能把窗口判定补在流程层（`contrast.evaluate_eligibility`）。代价是**同一个 case 上会出现两个裁定**：产物里 `policy.match` 说 approve（它只看命中），流程层说 reject（补了窗口）。两个都如实出现在 `contrast.json` 的 `policy_baseline` / `decision` 两个键里，但读者不看注释会以为是矛盾 | 归持有 `maos/skills/**` 的轨（当前 T11）。改法很小：`policy.match` 出参已经带着 `matched_rules[].params`，把 `contrast.evaluate_eligibility` 那 20 行搬进去即可，判定逻辑一行不用重写。**搬的时候连 `applies_when.reason_code` 一起搬** —— 少了它，R4 的 `quality_defect` 会被 AS-001 的无理由退货窗口误判 |
| 2026-08-29 | P7 | **五份对照 case 的 `requested_at`（本次退款诉求的时刻）只存在于 `_expected` 块里，`case` 块里没有这个字段**。而 `_expected` 按派单定义是**判据**，不是输入 | 流程层要算「第几天申请」就必须从判据块里取输入，判据与输入混在同一个键下。目前的处理是：只取 `requested_at` 当输入，天数由它与库里的 `paid_at` 现算，再拿算出来的天数去和 `_expected.elapsed_days` 比 —— 所以还没有变成自证。但这个边界很薄，下一个人顺手把 `elapsed_days` 也直接读走，判据就自证了 | 归持有 `scenarios/refund/cases/**` 的轨。改法：把 `requested_at` 提到 `case` 块（或新开一个 `_input` 块），`_expected` 里那份保留不动。**数据是上一轮的成果，本轨按派单不改 json** |
| 2026-08-29 | P7 | **`case_r4a.json` 的 `_expected.approver_role` 写的是中文说明「常规主管（无 AS-004）」，不是机器可读的 role 名**；同组 `case_r4b.json` 写的是 `region_manager`（机器可读） | 判据比对只能降级：`contrast.check_case` 对中文说明改判「审批人仍是缺省的 `supervisor`」，而不是等值比对。降级本身有注释、有测试守着，但**同一个键在同一组里有两种取值域**，是判据表里最容易被下一个人误读的形状 | 与上一条同批。改法：`"approver_role": "supervisor"` + 另开一个 `"_why": "无 AS-004，走常规主管"`。改完 `contrast._ROLE_TOKEN` 那条降级分支即可删 |
| 2026-08-29 | P7 | **`docs/agent-identity.md` 的「10 个 Identity / 9 个注册」因本轨投放 `refund_channel` 变成 11 / 10**，该文件是 `scripts/gen_docs.py` 的生成物，本轨未重跑生成器 | 生成物过期。**没有测试守着生成物与代码一致**（全仓 grep `gen_docs` 在 `maos/tests/` 下零命中），所以不会变红 —— 也就不会有人发现。`docs/EXECUTION.md:726`、`docs/agentteams-mapping.md:21` 两处「10 个 / 9 个」的措辞同步过期 | 归整合轮：重跑 `python3 scripts/gen_docs.py` 并同步那两处措辞。**顺带**：`## task-F1`／`## task-X4` 反复在修「十角色」这三处表述，每加一个业务角色就要再修一次 —— 值得考虑给生成物加一条「重跑生成器，输出与 git 中的文件逐字节相同」的测试，把它变成机器守卫 |
| 2026-08-29 | P7 | **`maos/model/client.py::ScriptedModelClient.__init__` 写的是 `self.script = script or {}`，空 dict 是 falsy，于是它会另造一个新 dict** | 调用方想「先给空表、跑起来之后再填应答」时，填进去的内容根本到不了客户端手里 —— 而它**不报错**：`complete()` 查不到关键字就返回 `"{}"`，`ManagerAgent.plan` 于是拿到一个没有 `tasks` 键的 dict，规划出**零个任务**，Plan 停在 RUNNING。本轨第一次跑对照时就是这个症状，从「Plan 为什么是 RUNNING」查到这一行花了两轮 | 归持有 `maos/model/**` 的轨。改法一行：`script if script is not None else {}`。**改之前确认没有调用方依赖「传 `None` 与传 `{}` 行为相同」** —— 当前 `select_model_client(None)` 是唯一传 None 的路径，改完行为不变。本轨的规避写法是给占位键（`{"用户请求": "{}"}`），已在 `contrast.run_case` 里写明理由 |
| 2026-08-29 | P7 | **`scripts/demo_preflight.sh:35` 的 `EXPECT_TESTS` 与实际条数对不上**：本轨新增 15 条，802 → 817 | **这是预期，不是回归**（派单 §0.2 契约 2 写死了：T10/T11/T12 也都在加测试，四轨改同一行 = 四方冲突）。本轨自测一律用 `MAOS_EXPECT_TESTS=817 bash scripts/demo_preflight.sh`，**文件一个字节没改** | 整合轮统一改一次，改成四轨合并后的最终条数 |
| 2026-08-29 | P7 | **`--contrast` 不重写 `evidence/INDEX.json`**，于是对照束要等到下一次缺省全量跑才被登记进 `aux_bundles` | 只跑 `make_evidence.py --contrast` 的人，会得到三个索引里查不到的目录。这是刻意取舍（在 `--contrast` 里重写索引会把上一次全量跑的 `produced` 清单抹成三条，那份索引就开始说谎了，见 `docs/DECISIONS.md ## task-T7`），但代价确实存在 | 归 `make_evidence.py` 持有轨。若要消除：让 `--contrast` **就地更新** `INDEX.json` 的 `aux_bundles` 一节而不动 `produced`（读回旧索引 → 只换 aux → 写回）。**注意首行出处注释**：读回时要走 `load_evidence_json`，写回要走 `write_json`，两边都别自己拼 |
| 2026-08-29 | P7 | **`maos/agents/refund/__init__.py` 的 `REFUND_ROLES` 里没有 `refund_channel`**，而 `refund_channel` 确实是退款域的角色、也确实注册进了 `AGENT_POOL` | 本轨刻意不加：`maos/tests/test_refund_flow.py:147` 是**等值断言** `set(REFUND_ROLES) == {四个角色}`，加进去就红，而改那条测试是白名单外的文件。所以现在「退款域有几个角色」有两个答案：`REFUND_ROLES` 说 4，`AGENT_POOL` 里带 `refund_` 前缀的有 5 | 归持有 `maos/tests/test_refund_flow.py` 的轨。改法与 `## task-R2` 当年那条同源：把等值断言换成**子集口径**（主干四个角色一个不许少），再把 `refund_channel` 加进 `REFUND_ROLES`。「恰好四个」守的不是注册口径，是「没人加过角色」这件事 —— 而政策驱动的可选分支正是设计上允许加的东西 |

## task-T8

本轨（交付门面：当前基线冒烟 + 提交压缩包 + 开源三件套）只持有
`docs/clone-smoke-report.md`（末尾追加）、`scripts/make_release.sh`（新建）、
`CONTRIBUTING.md`、`SECURITY.md`、`.github/**`（新建）、
`docs/submission-checklist.md` 的两处可勾项，外加两份账本。
**一行 `maos/**` 源码都没改。** 下面都是白名单外发现的，按铁律 4 记账不当场改。基线 `4cfef38`。

> ✅ **2026-08-30 注记（整合轮 13 回填）**：本节里「仓库默认分支是 `main`」那一条**已转绿** ——
> 默认分支已改成 `goai-restructure`，`git ls-remote --symref origin HEAD` 可自证。
> **本节其余行按惯例保留原样，是当时的实录**；别再拿它当当前状态引用。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P7 | 🔴 **仓库的 GitHub 默认分支是 `main`，而 `main` 停在 `3f2d5d1`，是已封存的 TypeScript 骨架**（44 个入库文件：`package.json` / `pnpm-lock.yaml` / `src/*.ts` / `tsconfig.json` / 一个早期 `python/` 目录），**没有 `maos/` 包、没有 `scripts/`、没有 `evidence/`、没有 `run.py`**。工作分支 `goai-restructure` 才是 `4cfef38`。实测：`git ls-remote --symref <url> HEAD` → `ref: refs/heads/main` | **评委裸 `git clone <地址>` 拿到的是那份 TS 骨架，README 里此后每一条命令都跑不了。** 而 `git clone` 会安安静静地成功，没有任何输出提示他走错了分支 —— 他会打开 README，发现里面讲的东西一个都不在，然后合理地推断「这份 README 描述的不是这个仓库」。这比 `docs/clone-smoke-report.md` 前四遍记的六个卡点都靠前、都致命：它发生在他还没跑过一条命令的时候。前四遍冒烟每次都自己带了 `-b`，所以四遍都没看见 | **提交前必须处理，两条路建议都做**。甲（最省事、且评委裸 clone 就对）：在 GitHub 仓库设置里把默认分支改成 `goai-restructure` —— **这要人类手动做，任何单轨都做不了**。乙：`README.md` §4 第 169 行那条 clone 命令写死 `-b goai-restructure`，归整合轮（README 是整合轮的面）。本轨已在 `SECURITY.md` 先补了一段显式警告，并在 `docs/clone-smoke-report.md` 第五遍一节记成「卡点 7」 |
| 2026-08-29 | P7 | **`README.md` 的两处秒数过期**：`:16` 写「clone + 这两条共约 **5 秒**」，`:179` 写「以上全部跑完约 **18 秒**，其中最短路径约 **5 秒**」。实测（`4cfef38`，全新克隆）：最短路径 **6.97s**（本地源）/ **8.69s**（远端源），全序列 **23.9s** / **26.3s** | 不是回归，是数字过期：那两个读数是整合轮 5（`571 passed`）留下的，`pytest` 涨到 802 条后自然变长（9.17s → 13.7s，占了涨幅的绝大部分）。但它与 `docs/clone-smoke-report.md` 第五遍一节的实测直接矛盾，两份材料都会交到评委手上 | 归整合轮（`README.md` 是整合轮的面）。刷数时连带 §4 那段「全新克隆 + 无任何 API key 实测」的措辞一起对齐第五遍 |
| 2026-08-29 | P7 | **`docs/submission-checklist.md` D-4 ③ 的判据已不成立**：写着「`evidence` 里出现过几个不同的 sha？**应当只有一个**，且等于 `git rev-parse HEAD`」。实测当前仓库是**两个** —— 50 个文件是 `17f51a9`（`make_evidence.py` 产），2 个是 `27c9e18`（`evidence/room/transcript.md` 与 `README.md`，由 T4 轨的真房间采集产，**不由 `make_evidence.py` 产**，脚本自己把它们标为 `[AUX] 仅登记`） | 照这条判据勾会判红，但那不是回归 —— 是 `evidence/room/` 这个新面进来之后，「证据束只有一个出处」这个前提本身变了。而且它连「等于 HEAD」也不成立（HEAD 是 `4cfef38`，证据是 `17f51a9`），不过那一条 D-4 自己已经解释过是结构性的不动点问题 | 归整合轮（本轨对 `submission-checklist.md` 的授权只到「补压缩包与本次冒烟两处可勾项」）。改法：③ 改成「`make_evidence.py` 产的那 50 个只有一个 sha；`evidence/room/**` 另算，它记的是采集当时的 sha」 |
| 2026-08-29 | P7 | **`README.md` §10 的目录结构没有 `scripts/make_release.sh`**（`:352` 那行现在写的是 `gen_docs.py / make_evidence.py / verify.py / guard_bash.py`）。这个脚本是本轨新建的 | 提交物「压缩包」的产出方式在 README 里找不到入口。评委看 README §10 会以为 `scripts/` 只有那四个 | 归整合轮，与上面 README 刷数同批。同时值得在 README 里加一句「提交前跑 `bash scripts/make_release.sh` 现打压缩包」 |
| 2026-08-29 | P7 | **A-1 的冒烟判据仍然量不到卡点 7**。`docs/clone-smoke-report.md` §5 早就建议「把『≤ 15 分钟』补一句『且全程零非零退出、不需要跨节拼路径』」，本轨再添一条证据：卡点 7 让评委根本跑不到第一条命令，**而它对掐表读数的影响是 0** —— 掐表是从「clone 对了分支」之后才开始的 | 五遍冒烟的秒数分别是 6.57 / 6.44 / 5.4 / 6.89 / 6.97·8.69，看不出任何差别，而第一遍有 6 个卡点、第五遍有一个更致命的卡点 7。这个指标已经连续五轮没有区分力了 | 归整合轮。建议 A-1 判据补成三句：「≤ 15 分钟」+「全程零非零退出、不需要跨节拼路径」+「**用评委最可能敲的那条 clone 命令**」。本轨已把第三句做成 A-1 下的一条可勾项，但**判据正文没动** |
| 2026-08-29 | P7 | **`review/` 下有 2 个入库文件**（`DISPATCH-TEMPLATE.md`、`tools/guard_probe.py`），而 `review/` 这个目录本身在 `.git/info/exclude` 里被排除 —— 也就是说派单正文未跟踪、这两个却入库了。它们会跟着进提交压缩包 | 影响很小（两个内部协作工件，无密钥、无客户数据），但它是「交付物边界」上的一处不自洽：包是给评委的，里面躺着派单模板和守卫探针。本轨**没有剔除它们** —— 剔掉会让包内 `git status` 带两行 `D`，进而让证据首行 sha 带 `-dirty`，把 A-2 那条判据搞红，代价大于收益 | 归整合轮决定：要么接受（透明度也算一种可信度），要么 `git rm --cached` 移出版本库。**不要在打包脚本里剔** —— 那条路已经实测过，会把 sha 弄脏 |
| 2026-08-29 | P7 | **`.gitignore:7` 的 `dist/` 是 TypeScript 时代留下的**（当时挡的是 `tsc` 的构建输出），注释里没写它现在还兼职挡着 `scripts/make_release.sh` 产出的提交压缩包 | 纯可读性问题，行为是对的（压缩包确实不该入库：sha 一变就要重打，二进制进 git 会让每轮整合多一坨没法 review 的 diff）。但下一个清理 TS 遗留的人可能顺手把这行删掉，然后某次 `git add` 就把一个 10MB 的 zip 提进去了 | 归整合轮（`.gitignore` 是整合轮的面）。改法只是补一行注释，说明这行现在挡两样东西 |

## task-T9

本轨（容器内真跑通）只持有 `deploy/docker-compose.yml`、`deploy/sandbox.Dockerfile`、
新建的 `deploy/README.md`，外加两份账本，**一行 `maos/**` 源码都没改**。
下面都是把「一键起」第一次真的 `up` 起来时撞见的，均在本轨白名单外，
按铁律 4 记账不当场改。基线 `4cfef38`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P6 | **`docker compose up` 的退出码不是判据，而手册三处都拿它当判据**：`docs/EXECUTION.md:705`、`docs/phases/phase-5.md:29`、`docs/ops/ORCHESTRATION.md:150` 都写着裸 `docker compose -f deploy/docker-compose.yml up`。实测同一次崩溃：`up --exit-code-from maos -> exit=1`，裸 `up -> exit=0` | 这是**假绿**，而且是最坏的那种：本轨接手前，那条命令在容器里从第一行 import 就炸（见下一条），可任何照手册跑 `up; echo $?` 的人拿到的都是 0。「可运行环境」这条复赛硬指标，就是被这个退出码掩护着一直没被发现 | 归持有 `docs/EXECUTION.md` / `docs/phases/**` / `docs/ops/**` 的轨。三处统一补 `--exit-code-from maos`。`deploy/docker-compose.yml` 抬头与 `deploy/README.md` 已按实测写死这个开关 |
| 2026-08-29 | P6 | **「核心零依赖」这个说法对容器不成立**：`README.md:163`、`pyproject.toml:8`、`docs/ppt-outline.md:526` 都写「核心零依赖」。`dependencies = []` 说的是 **Python 包**依赖，而 MAOS 有两个**外部命令**依赖 —— `git`（`maos/tools/sandbox.py::prepare_sandbox_workdir()` 的 `git init` / `git apply` / `git apply -R`，补丁的打与回滚全靠它）和 `pytest`（`sandbox_pytest_run()` 走 `python -m pytest --junitxml=…`）。`python:3.11-slim` 里两个都没有，实测连炸两次：先 `FileNotFoundError: ... 'git'`（模块导入期），补上 git 后变成「pytest 没有产出 junit 报告」→ Gate 两轮打回 → `scenario_1.py:143` 断言 DONE 处炸 | 开发机上 git 与 pytest 天然都在，所以这个说法在宿主机上从没被证伪过。但它是**对外材料里的一句话**（README 与 PPT 都在说），碰上容器、CI 最小镜像、评委的干净环境就会当场穿帮 | 归持有 `README.md` / `pyproject.toml` / `docs/ppt-outline.md` 的轨（整合轮）。改法是加个限定词而不是删：「**Python 包**零依赖，只额外要 `git` 与 `pytest` 两个外部命令」。`deploy/docker-compose.yml` 已就地把这两个装进镜像并写清了原委 |
| 2026-08-29 | P6 | **`scenarios/fixture-repo/tests/test_isolation_probe.py` 的容器判据在「MAOS 自己跑在容器里」时失真**。它拿 `IN_CONTAINER = pathlib.Path("/.dockerenv").exists()` 判「我是不是在沙箱容器里」，在则断言连外网必抛 `OSError`，不在则 skip。这条判据默认**MAOS 跑在宿主机上、只有沙箱才是容器** —— 而容器化跑 MAOS 时 `/.dockerenv` 照样存在，探针于是对着**降级路径的裸子进程**要求断网，报 `FAILED ... test_no_network - Failed: DID NOT RAISE OSError` | 后果不是一条测试红，是**整条场景红**：回归一挂 Gate 两轮打回，plan 终态 FAILED，`scenario_1.py:143` 断言炸。也就是说这条探针的自证方式，恰好在「容器内跑通」这个交付面上把自己反噬了 | 归持有 `scenarios/fixture-repo/**` 的轨。判据要能区分「我在沙箱容器里」与「我碰巧在某个容器里」—— 沙箱容器是 `deploy/sandbox.Dockerfile` 建的、`WORKDIR /w`、`USER runner(1000)`，这三者都比 `/.dockerenv` 精确。**本轨没有改它**，改的是让断言成真：compose 给两个服务加了 `network_mode: none`，子进程继承容器的网络命名空间，探针要的「沙箱没有网」这句话于是真的成立 |
| 2026-08-29 | P6 | **compose 的 `name: maos` 让多 worktree 并行时容器落进同一个 project**。本轨验收当天实测：`maos-pgvector-1` 是 **T10 轨** 从 `/Users/…/.worktrees/task-t10/deploy` 起的（`docker inspect` 的 `com.docker.compose.project.working_dir` 标签为证），却和本轨的 `maos-maos-1` 同属 project `maos` | 任何一轨敲 `docker compose … down`（哪怕**不加** `--remove-orphans`），都会把别轨正在用的容器一并停掉、删掉。派单 §5.3 原本就让本轨跑 `--profile pg down` —— 照做的话会当场拔掉 T10 正在写 `pg_store.py` 用的库。本轨因此**没有执行那条命令**，改用独立 project + 换端口验证。**这不是假想：同一轮里它反方向真的发生了一次** —— 验收跑到一半时 `maos-pgvector-1` 与本轨的 `maos-maos-1` 双双消失（本轨 compose 日志里从头到尾没出现过 pgvector，`maos_pgdata` 卷完好，且本轨这次 `up` 打的是 `Container maos-maos-1 Creating` 而不是上一次的 `Recreate`），即 T10 那边跑了一次 `down`，把本轨的容器一并删了。本轨的容器是一次性的、删了无所谓，但换成 T10 的库就是丢工作 | 归整合轮定口径。两条路：①保留 `name: maos`，在 `deploy/README.md` 写死「多会话并行时一律带 `-p <自己的 project>`」（本轨已按这条写了）；②把 project name 改成可覆盖 `name: ${COMPOSE_PROJECT_NAME:-maos}`。②更彻底但会改变现有容器命名，得先确认没有别的脚本按 `maos-*` 认容器 |
| 2026-08-29 | P6 | **`deploy/.env.example` 仍然写不进去**（本轨复现：与根目录 `.env.example` 同一条权限 deny 规则）。键名清单仍只存在于 `docs/BACKLOG.md ## task-omega` | 评委手上没有可配置项的全量样例，只能从 compose 注释和 BACKLOG 里拼。这是**材料完整度**的缺口，不是功能缺口 | 归人类：那条 deny 规则是本机权限配置，不是仓库里的东西，只有人类能放行。放行后把 `## task-omega` 的键名清单原样落成文件即可。**本轨与前一轨一样，没有绕道换文件名** |

## task-T10

本轨（`pg_store` 填实 + PolarDB 迁移说明）只持有 `maos/store/pg_store.py`、
`maos/store/pg_schema.sql`（新建）、`maos/tests/test_pg_store_live.py`（新建）、
`deploy/polardb.md`（新建），外加两份账本。下面都是**白名单外**发现的，
按铁律 4 记账不当场改。基线 `4cfef38`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **`maos/store/__init__.py` 的 `create_store()` 仍无条件拒绝 postgres**（`:69-76` 抛 `NotImplementedError`，理由写的是「P5 才填」）。本轨已把 `PgStorePort` 填实并在真库上实测跑通，但工厂没跟着开口 —— 拿 PG 后端只能绕过工厂直接 `PgStorePort(dsn)` 构造（工厂自己的 docstring 正是这么指引的） | 「后端可插拔」目前只在类一级成立，**公共入口一级不成立**。评委若照 `MAOS_STORE_BACKEND=postgres` 试，拿到的仍是一句「P5 才填」，与 `deploy/polardb.md` 里「代码一行都不用改」的说法对不上 | 归持有 `maos/store/__init__.py` 的轨。⚠️ **改之前必须先解决一个冲突**：`maos/tests/test_store_port.py:199` 的 `test_postgres_backend_raises_and_never_falls_back` 断言工厂在 `MAOS_STORE_BACKEND=postgres` 时抛 `NotImplementedError`，而那 28 条是冻结的。可行解：工厂返回 `PgStorePort()`，让它在 DSN 未配 / 连不上时抛 `PgBackendUnavailable`（本轨已确保它是 `NotImplementedError` 的子类）—— 但**环境里恰好配了可用 DSN 时那条测试仍会红**，所以要连测试一起重新设计，得人类拍板 |
| 2026-08-29 | P5 | **`docs/submission-checklist.md:129` 的 §A-4 口径已可改写**：现写「有地基、未接线；PG 后端是空壳且拒绝回落」「✅ 五个方法全 `raise NotImplementedError`」。本轨之后五个方法已是真实现，在本机 Docker PG 16.15 + pgvector 0.8.6 上 22 条 live 测试全绿 | 保守口径本身不会被问穿，但**证据栏那句「五个方法全 `raise NotImplementedError`」现在是错的**（实测只剩 `PgBackendUnavailable` 这一条不可用路径会抛）。一个能被 `grep` 当场证伪的自查单条目，比口径保守更伤 | 归 T8／整合轮（该文件是它们的面）。建议措辞：「PG 后端已在本机 Docker Postgres 16 + pgvector 0.8.6 上实测跑通（`maos/tests/test_pg_store_live.py` 22 条，无库自动 skip），全文走 `to_tsvector`/`ts_rank`、向量走 pgvector `<=>`；**PolarDB 实例本身未连过**，兼容性是推断，见 `deploy/polardb.md` 的「未实测」栏；工厂 `create_store()` 仍拒绝 postgres（见上一条）」。**不要**改成「后端已可插拔切 PolarDB」—— 那正是该表点名的会被问穿的说法 |
| 2026-08-29 | P5 | **`pyproject.toml` 没有 PG 驱动的 optional extras**。核心零依赖（`dependencies = []`）是对的、必须保持，但 `hiclaw` / `obs` / `dev` 三组 extras 里没有 `pg`，装驱动只能手工 `pip install "psycopg[binary]"` | 迁移文档里多一步手工命令，且没有任何机器可校验的地方写着「PG 后端要什么驱动、什么版本」。版本漂了只会在运行期报错 | 归人类拍板（派单 §0.2 契约乙划为「要改先停下来问」）。建议只加 `pg = ["psycopg[binary]>=3.1"]` 到 `[project.optional-dependencies]`，**绝不动 `dependencies`**。⚠️ 动之前先确认对 T9 容器无影响：`deploy/docker-compose.yml` 的 maos 服务不 build 镜像、只挂源码，extras 装不进容器，所以容器里跑 PG 后端仍需另想办法。**✅ 已于 2026-08-29 收尾解**：人类拍板，照建议加 `pg = ["psycopg[binary]>=3.1"]`，`dependencies` 仍为 `[]`（`tomllib` 复核过）。⚠️ 那条前提**已被 T9 改掉**：T9 这轮把 maos 服务改成了 build 镜像（`image: maos-runtime:local` + `dockerfile_inline`），「不 build 镜像」不再成立。但结论没变、理由换了 —— 那份 inline Dockerfile **一个文件都不 COPY**（上下文取 `deploy/` 只为走过场），没有 `pyproject.toml` 可装，`pip install` 那行也只装 `pytest`。容器里跑 PG 后端现在有了正规入口：在那行后面接 `psycopg[binary]`，或 COPY 一份 pyproject 再 `pip install -e '.[pg]'`。**归 T9 合入后的整合轮**（build 段是 T9 的面，本轨不越界改，且改完要重跑一遍容器内核验才算数）|
| 2026-08-29 | P5 | **`deploy/docker-compose.yml` 的 pgvector 服务没有把 `CREATE EXTENSION vector` 做成初始化脚本**。镜像自带扩展文件，但每个新建的数据卷都要人工再执行一次建扩展 | 换台机器 / 删了 `maos_pgdata` 卷之后，第一条向量查询报的是 `operator does not exist: vector <=> vector` —— 看起来像 SQL 写错，跟「扩展没建」是两个印象。本轨已在 `pg_store.py` 里把它翻成点名 `CREATE EXTENSION` 的 `LookupError`，但那是事后补救 | 归 T9（契约 1：compose 是它的面，本轨只读不改）。改法很小：挂一个只含 `CREATE EXTENSION IF NOT EXISTS vector;` 的文件到 `/docker-entrypoint-initdb.d/`，postgres 镜像会在初始化新卷时自动执行 |
| 2026-08-29 | P5 | **`ts_rank` 与 `bm25` 不是同一把尺子**：`ts_rank` 缺省不做文档长度归一。实测同一条查询 `timeout`，PG 侧 `d1`/`d2` **同分 0.06079271**（并列后按 id 升序），SQLite 侧 `-bm25` 给 `d2` 严格高于 `d1` | 两边都满足 F-2 的「越大越相关、降序、同分按 id 升序」，但**具体名次不同**。混合召回里全文通道的名次会随后端变，而 `retriever._rank_normalize` 正是按名次归一的 —— 换后端可能改变最终排序，且不报错 | 属检索调优，不在本轨（派单 §4 划死 `maos/kb/**` 只读）。要对齐就给 `ts_rank` 传 normalization 参数（如 `|2` 除以文档长度），但那会改变现有 PG 侧排序，需要有人先决定以哪边为准。**注意本轨的中文口径下 PG 全文通道在中文语料上基本不会被用到**（见 `deploy/polardb.md` 局限第 1 条），所以这条的实际影响面比看上去小 |
| 2026-08-29 | P5 | **没有任何文档把 `deploy/polardb.md` 挂进去**。全仓引用它的只有 `docs/EXECUTION.md:574`/`:696`（手册自己的计划条目）和本轨新增的源码注释；`README.md`、`docs/submission-checklist.md`、`deploy/` 下都没有入口 | 一份评委多半找不到的迁移说明。而它恰恰是「部署仅换连接串」这句断言的唯一实证载体 | 归整合轮。挂一行到 `README.md` 的部署/文档索引处即可。⚠️ `deploy/README.md` 当前**不存在**（本轨实测），派单 §4 把它列为 T9 的面 —— 若 T9 这轮新建了它，那里是更自然的落点 |

## task-T11

本轨（Skill 多版本发布 / 回滚实证）只持有 `maos/skills/builtin/refund/policy_v1_1.py`、
`maos/skills/version_demo.py`、`maos/tests/test_skill_versioning.py`（三个都是新建），
外加两份账本。下面都是**白名单外**发现的，按铁律 4 记账不当场改。基线 `4cfef38`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P1 | **`registry.get(name)` 缺省取最高版本 = 发布即全量切流，没有灰度档位**（`maos/skills/registry.py:66`）。退款域两个调用点都不钉版本（`maos/agents/refund/policy_agent.py:40`、`finance_agent.py:48`），所以任何新版本一进 import 路径就立刻接管**全部**存量调用 | 这是本轨最实际的一处阻力：`policy.match` 的 v1.1.0 只要进 `builtin/refund/__init__.py` 的清单，`evidence/scenario-*/trace.json` 里那几十处 `"version": "1.0.0"` 会全部变成 `1.1.0`（落库的是 `cls.contract.version`，`maos/skills/invoker.py:111`），跨轨契约 1 当场破；`test_refund_flow.py:127` 也会同时变红（**已实测**：本轨第一版把 import 写在测试文件顶层，collection 阶段就注册了 1.1.0，那条测试当场 FAILED） | 归持有 `maos/agents/refund/**` 的轨 + 整合轮。两条路：①**调用点显式钉版本**（`invoke(..., version="1.0.0")`，`SkillInvoker` 早就支持这个入参，无需改 registry），改完重跑证据束，此后新版本可以自由进 import 路径；②给 registry 加「默认版本钉选」表，让新版注册但不抢默认。推荐 ①，改动小且语义明确 |
| 2026-08-29 | P1 | **`docs/skill-catalog.md:263`「当前在册的 13 个 skill 中，有多版本的：一个都没有」这句仍然是这句**。已核实它**不是**模板写死的 —— `scripts/gen_docs.py:407-411` 按 `sibling_versions` 动态算，注册表里出现多版本它会自动改写 | 本轨的 v1.1.0 刻意不进正常 import 路径（见上一条），`gen_docs.collect_skills()` 只 `import maos.skills.builtin`，于是扫不到它，那句话没有触发条件。**演示与测试里回滚路径是真跑过的**（`python3 -m maos.skills.version_demo`），但这份文档还没法据此改口 | 与上一条同批：调用点钉完版本、v1.1.0 进 `builtin/refund/__init__.py` 之后，重跑 `python3 scripts/gen_docs.py`，那句会自动变成「有多版本的：`policy.match`」，一览表里 1.0.0 会自动标「（旧版）」。**顺带**：那时 `docs/` 里的「13 个 skill / 13 个版本条目」会变成「13 个 skill / 14 个版本条目」——skill **名字**数不变，README / PPT 里的「13 个 skill」**不受影响**，不必回填 |
| 2026-08-29 | P1 | **`kb.retrieve` 是原地升到 1.1.0 的，1.0.0 的实现没留**（`maos/skills/builtin/kb_retrieve.py:77`，升版发生在 `d79d815`）。所以 `registry.get("kb.retrieve", "1.0.0")` 返回 `None` | 「旧版本从不被覆盖」这条对它不成立 —— 它是全仓唯一一个**回滚路径已经消失**的 skill。`docs/skill-catalog.md` 的回滚一节写着「`get(name, "1.0.0")` 永远拿得到当年那一个」，对 `kb.retrieve` 是句空话。不影响任何现有链路（没人按版本取它），但评委若当场试这一手会翻车 | 不建议追造一个 1.0.0（那是伪造历史）。归整合轮：要么在 catalog 的回滚一节加一句「前提是升版时新建文件而非原地改写，`kb.retrieve` 是反例」，要么在 `CONTRIBUTING.md` 里把「升版必须新建文件」写成规矩。本轨的 `policy_v1_1.py` 是正面样板 |
| 2026-08-29 | P1 | **`maos/tests/test_refund_flow.py:127` 把「退款域每个 skill 按名取到的都是 1.0.0」写死了**（`assert cls.contract.version == "1.0.0"`，对 `registry.get(name)` 不带版本） | 退款域**任何** skill 将来升版都会撞这条 —— 而它的本意是「六个 skill 都注册上了」，版本号只是顺手断言的。本轨绕开的办法是 module 级 fixture 用完即摘（`test_skill_versioning.py` 的 `published`），但那是本轨自己的自律，挡不住下一个人 | 归持有 `test_refund_flow.py` 的轨。改法：把版本断言换成 `cls.contract.version in registry.versions(name)`，或直接删掉版本那半句 —— 「注册上了」这件事由 `cls is not None` 已经断言过了 |

## task-T12

本轨（`verify.py` 12 行 warn 收口）只持有 `maos/flows/common.py`、
`maos/flows/scenario_{1,2,3,5}.py`、`maos/agents/testing.py`、`maos/obs/trace.py`、
`scripts/verify.py`、`maos/tests/test_verify_warn.py`（新建）、
`docs/submission-checklist.md` 的 §A-2 一段，外加两份账本。基线 `4cfef38`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P6 | **第三条旁路 `maos/agents/reviewer.py::review_after_gate` 仍不自报来源**：它直接 `store.insert_artifact` 落 review_note（scenario-1/2/6/7 各一份），是三条绕开 `on_task_result` 的入库路径里唯一没补 `ArtifactSeeded` 的一条。本轨补掉的是另外两条（`flows/common.py::patch_verifier`、`agents/testing.py::seed_scripted_report`） | A 类 warn 因此从 6 行只降到 4 行，剩下的 4 行全是同一条根因。审计链上这 4 份 review_note 仍然指不到是哪一步产的 | **归持有 `maos/agents/reviewer.py` 的轨**（该文件在 T12 白名单外，本轨按铁律 4 不当场改）。改法已经现成：调 `maos.agents.testing.record_seeded_artifact`，与另两条旁路同一个写法，四行代码。做完之后 A 类归零，同步改 `docs/submission-checklist.md` §A-2 与 `maos/tests/test_verify_warn.py::WARN_BASELINE`。**✅ 已于 2026-08-29 收尾解**：人类当场授权改白名单外的 `maos/agents/reviewer.py`，照现成写法调 `record_seeded_artifact`（不带 `sandbox_mode` / `scripted` —— 那两个键是 test_report 的分水岭，review_note 不经沙箱，编一个值进去就是往审计链里塞假事实）。实测 warn 5 行 → **1 行**，`RESULT: 7/7 PASS` 不变，全量 819 passed。§A-2 那张表、`WARN_BASELINE`、`maos/obs/trace.py` 的旁路清单三处已同步；A 类同时进 `RETIRED_WARN_MARKERS`，再出现就是回归 |
| 2026-08-29 | P6 | **`docs/submission-checklist.md:24` 的 `802 passed` 已过期**：本轨新增 17 条（`test_verify_warn.py`）后是 819，而 T 轮其余五轨也各自在加测试 | 照自查单跑第 ① 条会对不上数，被当成回归报 | **归整合轮统一刷**，与 `scripts/demo_preflight.sh` 的 `EXPECT_TESTS`（同样写死 802）同批改 —— 那两处必须同时改，否则前置脚本与自查单互相矛盾。本轨自测一律用 `MAOS_EXPECT_TESTS=819` 覆盖，没有动这两个文件 |
| 2026-08-29 | P6 | **`scripts/verify.py --json` 的 stdout 不是纯 JSON**：`render()` 打完 JSON 之后照旧会追加人类可读的 `RESULT: …` 与「证据来源：…」两行（`--json` 分支只管前半段） | 任何用 `json.loads(stdout)` 读它的下游都会 `JSONDecodeError: Extra data`。本轨的 `test_verify_warn.py` 已改用 `raw_decode` 只取前缀绕过，但下一个写 CI/脚本的人会再踩一次 | 归 `verify.py` 持有轨。改法二选一：`--json` 时不打那两行，或把它们打到 stderr。**不急**（当前没有别的下游），但值得在改 `render()` 时顺手做掉 |

## task-T13

知识层接上 StorePort 时发现、按铁律 4 不当场改的四条（分支 `task/t13-kb-pg-channel`，
基线 `4cfef38`）。本轨已解掉 `## task-X3` 第 1、2 条的**协议面**：`kb_doc` / `kb_doc_fts`
补了 F-2 口径的 `id` 列，`kb.port_of()` 让知识层认 StorePort，检索器实测两条通道
`port_channel_state == {"fts_search": True, "vector_search": True}`。但那两条**要等
T10 的 `pg_store.py` 填实、整合轮跑过真 PG 才算销账** —— 本轨验的是协议面，不是真链路。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **端口与本地实现的全文查询语义本来就不同，且端口那一侧更窄。** 端口 `fts_search(table, field, q, limit)` 把 `q` 按空白切词后**词间做 AND**，且只查 `field` 指定的那**一列**（`sqlite_store.py` 原话：「要算子就另开方法，别动 F-2 那五个签名」）；检索器的本地实现是 `"t1" OR "t2" ...` 且**跨列**（title + body）。实测：语料「锈蚀 与 退款 都 在 正文」「只 有 退款 两 个 字」，查 `锈蚀退款` 时本地召回两条、端口只召回一条 | 换到端口通道之后，**标题命中的知识召不回来**（`_fts_scores` 传的 `field` 是 `"body"`），而且只命中部分词的文档也召不回来。两者都不报错，症状是「换了后端之后 RAG 好像笨了一点」。R5 的对照实验若哪天在端口上重跑，`with_kb` 那一跑的命中数会低于本地实现那一跑，而两跑都是绿的 | 三条路，都出本轨的面：①给 F-2 加一个可选的「列清单 / 算子」入参 —— **动五个签名，冻结面，要 T10 / T13 / T14 三轨一起改**；②检索器对 title、body 各发一次 `fts_search` 再合并 —— 单侧可做，但每次检索多一个来回，PolarDB 上是真的往返，且合并时 bm25 的量纲要重新对；③认下这条差异，把它当成「后端各有各的召回口径」，只保 F-2 附则那两条（分数方向、次序）。**本轨按 ③ 处置并已钉成断言**（`test_kb_pg_channel.py::test_port_and_local_fts_semantics_differ_by_design`）。要改成 ① 或 ② 的话，先把那条测试改判，别删 |
| 2026-08-29 | P5 | **`SqliteStorePort` 不是一个完整的 store：它没有 `append_event_log`。** 所以 `retriever.retrieve()` 能吃端口，`retrieve_and_log()` / `emit_kb_retrieved()` 不能 —— 后者直接调 `store.append_event_log(...)`，那是核心 Store 的方法，不在 F-2 五个签名里 | 主链路（`skills/builtin/kb_retrieve.py` 走的是 `retrieve_and_log`）**现在还不能整体换成端口对象**，只能换检索这一段。本轨因此只验到 `retrieve()`。不点破的话，下一个人把 `ctx.store` 换成 `SqliteStorePort` 会在落 `KbRetrieved` 那一步撞 `AttributeError`，而且撞在检索**之后**——检索看起来是通的 | 归 T10 / T14 或整合轮。两条修法：①让 `SqliteStorePort` 也暴露事件日志入口（**要加第六个方法 = 动 F-2，冻结面**）；②`emit_kb_retrieved` 收一个独立的 `event_sink` 参数，检索用端口、落事件用核心 Store —— 单侧可做且不动契约，**推荐这条**。做之前先确认 `kb_retrieve` 技能与 `flows/**` 那几处调用点谁来传这个 sink |
| 2026-08-29 | P5 | **`kb/schema.sql` 没有迁移路径，本轨新加的 `id` 列对已存在的库静默无效**（`## task-R1` 第 5 条的老问题，本轨踩到它的第一批具体后果）。全是 `CREATE TABLE IF NOT EXISTS` / `CREATE VIRTUAL TABLE IF NOT EXISTS`，表在就整段跳过 | 演示期的库都是 `:memory:` 或每次新建，所以现在不咬人。但 `evidence/*/maos.db` 是**落盘且被 gitignore** 的：谁手上留着一份 T13 之前生成的 `maos.db`，在它上面跑检索会得到「端口通道恒退化」——`no such column: id`，只告警一次，然后一切正常。这正是最难被发现的那种失效 | 归 store 层持有轨。真要做迁移得引入版本表 + `ALTER TABLE`，那是比本轨大得多的一件事。**过渡期最省事的办法是删掉旧的 `evidence/*/maos.db` 重跑证据束**（`make_evidence.py` 会重建）。本轨已在 `schema.sql` 的注释里把这条写在 `id` 列旁边 |
| 2026-08-29 | P5 | `kb.ensure_schema()` 在端口路径上没有 `executescript` 可用（F-2 五个签名里没有它，`## task-W2` 第 3 条记着），本轨的 `_schema_statements()` 是**按 `;` 硬切**的 | 现在对：`schema.sql` 里没有任何含分号的字符串字面量。但哪天有人往 `CHECK` 或 `DEFAULT` 里写一个带分号的字面量，切法会把一条语句劈成两半，报的是语法错——**这次会当场抛，不是静默失效**，所以危害有限，但报错信息会指向一个看不懂的半截 SQL | 归 store 层 / 知识层。两条：①`schema.sql` 里永远不写含分号的字面量（本轨已在 `_schema_statements` 的 docstring 里写死这条约束）；②真要放宽就换一个 sqlite 的语句分割器（`sqlite3.complete_statement` 可以逐段判完整性）。**②的成本不高，撞上第一个含分号的字面量时再做** |

## task-T14

本轨（真连一次 PolarDB）只持有 `scripts/polardb_smoke.py`（新建）、`deploy/polardb-live.md`（新建）
外加两份账本，**零 `maos/**` 源码改动**。下面两条都是**白名单外**发现的，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-29 | P5 | **`deploy/docker-compose.yml` 的 pgvector 在多轨并行下是共享单例**：固定 project name（`name: maos`）+ 固定容器名 + 写死 `${POSTGRES_PORT:-5432}` + 共享 `pgdata` 卷。任一轨跑 `docker compose --profile pg down` 都会掐掉别轨正在用的库 | **本轮双向都真实发生了**：①开工后发现 T10 已把它起在 5432 上，照派单字面跑 `down` 就会打断 T10（本轨因此改为复用，见 `docs/DECISIONS.md ## task-T14`）；②随后 T10 自己跑完 `down`，把本轨正在用的容器**整个删掉**（`docker ps -a` 里 `maos-pgvector-1` 连 Exited 记录都不剩），本轨的本机对照组当场失去库 —— 所幸数据已经拿到。只要有两轨的派单都写了这条 up/down，就是一次静默互踩，**而两边各自的验收都是绿的**，谁都不会发现 | 归 T9（`docker-compose.yml` 持有轨）。可选改法：允许覆盖 project name（`docker compose -p maos-t14 ...`），或在 `deploy/README.md` 写明「多轨并行时只复用、不 down」。**不急** —— 眼下靠派单里的口头约定也能绕开，但下一轮多轨还会踩 |
| 2026-08-29 | P5 | **派单模板 §1 的 DSN 自检只验「非空」**（`print("已配置" if os.environ.get("MAOS_PG_DSN") else "未配置")`）。把 §1 自己给出的占位符模板原样 export 进来时，它照样报「已配置」 | 本轮真实发生：开场自检报「已配置」，实际是 `postgresql://<user>:<pass>@<host>:<port>/<db>` 一字未改，直到第一次连接失败才暴露。开场自检的作用是**在开工前**把前置状态钉死，这一条钉反了方向 —— 它给出的是一个假的通过信号 | 归后续写派单的人（编排侧）。改法一行：判据从「非空」改成「非空且不含 `<` 尖括号占位符」。本轨已把等价检测内建进 `scripts/polardb_smoke.py`，脚本这一侧已经堵上；漏的是**派单自检那一行** |

## integrate-round-11

T7–T14 八轨并入 + 活数字回填 + 证据束全量重跑。基线 `4cfef38`，合并后 HEAD `38fbdad`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **`a9d0d93` 的标题写着 `merge:`，但它不是 merge 提交**：修 t10 归属错位时那次 `git commit --amend` 没有生效（原因未查清，同一批里 t9 的 `8450d97` 与 t11 的 `a5b33ea` 都 amend 成功了），于是变成挂在真 merge `4c6fc17` 之上的单父普通提交，标题却原样保留 | `git log --oneline` 上会看到**两条一模一样的 `merge: t10-pgstore-polardb`**，读历史的人会以为 t10 被并了两次。机器判据：`git log -1 --format=%p a9d0d93` 只有一个父，`4c6fc17` 才是两个父 | **不修，记账即可**。修它要 rebase，而 `evidence/` 42 份出处头钉死 `3d504b1`（整合轮 11 活数字回填提交），重写历史会让这些 sha 全部指向不存在的提交 —— A-2 那条「出处头必须是干净且真实存在的 sha」当场失效。代价对比：一个误导的标题 vs 整束证据的可追溯性。下一轮若要清理，只能在**证据束重跑之前**做 |
| 2026-08-30 | P7 | **`review/DISPATCH-TEMPLATE.md` 的改动被卷进了 `a9d0d93`**：它本该是一个独立的编排侧提交（给 §1 补「环境变量自检判据 = 非空**且不含 `<` 占位符**」，堵 T14 踩的那个假通过信号），却因为并轨期间它一直躺在主干工作区未提交，在 merge 状态下 `git commit` 带上了整个索引 | 内容完好、判据实测过四种情形（未设 / 占位符原文 / 真串 / 只替换一半），只是从提交历史上看不出这条改动的存在 —— 按标题找它会找不到 | **不单独修**（同上，要 rebase）。下次并轨前先把主干工作区清空：并轨期间主干**不该有**任何未提交改动，否则它会随机粘到某个 merge 提交上 |

## task-T15

本轨（工厂放行 PG 后端）只持有 `maos/store/__init__.py`、`maos/tests/test_store_port.py`
外加两份账本。下面四条都是**白名单外**发现的，按铁律 4 记账不当场改。前三条是同一件事的
三处下游：工厂放行之后，仓库里所有「工厂仍拒绝 postgres」的说法同时过期。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`maos/tests/conftest.py` 仍没有 `MAOS_STORE_BACKEND` / `MAOS_PG_DSN` 的 autouse delenv 起跑线**（`## task-T7` 那条第 749 行已记过），而工厂放行之后这条欠账**换了一种更坏的形态**。本轨实测：脏环境但**没配 DSN** 时形态没变，`MAOS_STORE_BACKEND=postgres python3 -m pytest maos/tests -q` → `845 passed, 22 skipped, 20 errors`，与旧账同形（旧账是 683 passed / 20 errors） | 新形态是**同时 export 了 `MAOS_STORE_BACKEND=postgres` 和一个可用 DSN** 的机器：`test_store_port.py` 的 `port` fixture 走 `create_store()`，放行前拿到的是 error，放行后拿到的是一个**真连上的 PG 后端**，接着 fixture 里的 `CREATE TABLE t` / `CREATE VIRTUAL TABLE ... fts5` / `INSERT` 会打到那个真库上。机制已实测（connect 成功时工厂确实返回 `PgStorePort`，见本轨回执方向二），**后果是推断** —— 本机没起库，这条没真跑过。最可能的症状是测试红在 `?` 占位符上（sqlite 方言），指向 store 实现而不是指向环境，与旧账同一种误导 | 归测试面那一轨，**优先级比放行前高**：起了库的机器（如 T18 那类）现在多了一条往真库写表的路。修法与第 749 行那条同形：`maos/tests/conftest.py` 里加一组 autouse delenv。该文件在本轨白名单外，没当场改 |
| 2026-08-30 | P5 | **`docs/submission-checklist.md:162` §A-4 那行、以及 `## task-T10` 第 2 条（第 956 行）给它拟的替换措辞，本轨之后一起过期**。现行文写「有地基、未接线；PG 后端是空壳且拒绝回落」，复核栏写「五个方法全 `raise NotImplementedError`」（T10 填实之后已错，956 记过）；而 956 建议的新措辞里那句「工厂 `create_store()` 仍拒绝 postgres（见上一条）」，本轨之后同样是错的 | T20／整合轮若照 956 的建议措辞抄，会往自查单里写进一句**能被一条命令当场证伪**的话 —— 而 §A-4 恰恰是「最容易被问穿的地方」那张表。一个自证过期的自查单，比口径保守伤得多 | 归 T20／整合轮（该文件是它们的面）。可用口径：「后端可插拔已在**公共入口一级**成立 —— `MAOS_STORE_BACKEND=postgres` + `MAOS_PG_DSN=...` 即得 PG 后端；没驱动 / 没配 DSN / 连不上一律抛 `PgBackendUnavailable`（`NotImplementedError` 子类），绝不回落 sqlite；**PolarDB 实例本身未连过**，兼容性是推断，见 `deploy/polardb.md` 的「未实测」栏」。**仍然不许**写成「已切 PolarDB」 |
| 2026-08-30 | P5 | **`deploy/polardb.md` 第 3 步「换连接串」只写了 `export MAOS_PG_DSN=...`，缺 `export MAOS_STORE_BACKEND=postgres` 这一行**。`docs/DECISIONS.md` 第 1033 行记着这个空缺的成因：当时工厂拒绝 postgres，那条路径根本写不进去 | 这一页是全部 PolarDB 材料的立论根基，「代码一行都不用改」本轨之后**真的成立了**，但页面上给的两行仍差一行 —— 读者只 export DSN 不 export backend，缺省仍走 sqlite，会以为在跑 PG 而其实一行 PG 代码都没执行，正是该页自己点名的那种错误。另：该页「已实测」表里的全量条数仍是 `802 passed, 22 skipped`（无库）/ `824 passed`（有库），本轨之后无库全量是 `865 passed, 22 skipped` | 归持有 `deploy/polardb.md` 的轨（本轨白名单外，没动）。补那一行，并把条数与整合轮一起刷。`docs/EXECUTION.md:638` 的 `MAOS_STORE_BACKEND=postgres MAOS_PG_DSN=... python run.py` 一直是对的，可作对照 |
| 2026-08-30 | P5 | **`create_store(store, backend="postgres")` 会静默忽略传进来的 `store`**。第一个位置参数只对 sqlite 分支有意义（适配器包住它），postgres 分支不包任何既有连接。本轨只在 docstring 写明了这件事，**没改行为** —— 改成抛 `TypeError` 是手册范围外的行为变更 | 接线那天（第 200 行记的 `build()` 接线）若有人写 `create_store(flow_store, backend=cfg.backend)`，配置一切到 postgres，那个精心传进去的 store 就悄悄没人用了。又是一个没有症状的错误：链路照跑，数据落在另一个库里 | 归接线那一轨。届时二选一：要么 `store is not None and name == POSTGRES` 时显式抛，要么把「切 PG 前先把 store 从调用里摘掉」写进接线处的注释。**不要**在没有调用方的时候提前改签名 |

## task-T16

本轨（知识层整条链路走端口）持有 `maos/kb/retriever.py`、`maos/skills/builtin/kb_retrieve.py`、
新建 `maos/tests/test_kb_port_link.py`，外加 `test_kb_pg_channel.py` 里两条**受本轮改动直接
影响**的断言改判，与两份账本。销掉了 `## task-T13` 第 1、2 条（跨列召回按解法 ②、
落事件按解法 ②）。下面四条都是白名单外发现的，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **主链路 skill 仍然不能整体吃 StorePort，卡点已经从通路二挪到通路一。** 本轨把通路二（`kb_doc` 两阶段检索 + 落 `KbRetrieved`）整条打通了，但 `KbRetrieveSkill.run()` 的第一步是通路一 `ctx.store.list_knowledge(...)` —— 那是核心 Store 的具名方法，不在 F-2 五个签名里，也不在本轨白名单 | 把 `ctx.store` 换成 `SqliteStorePort` 仍然会炸，只是**炸的位置变了**：从「检索之后落事件那一步」提前到「检索之前取 knowledge 那一步」，而且这一次**不在** `_kb_docs` 的 `except Exception` 兜底范围内，会直接冒给 invoker。现状比改造前好在它当场炸、不再是「检索看起来通了而 docs 恒空」 | 归 skills 层持有轨。两条路：①`knowledge` 表也走 `kb.port_of()` 那套通用访问器（它已经能吃端口，`list_knowledge` 只是没走）；②给 skill 也开一个 `extras` 里的核心 Store 入口，与本轨的 `event_sink` 同构。**②与本轨口径一致、成本更低**，但两条都要先确认 `flows/**` 那几处调用点谁来传 |
| 2026-08-30 | P5 | **端口全文通道走不通时的「本地退化路径」在 PG 后端上是死路。** `_local_fts_rows()` 发的是 `SELECT doc_id, bm25(kb_doc_fts) ... WHERE kb_doc_fts MATCH ?` —— `bm25()` 与影子表 `kb_doc_fts` 都是 SQLite FTS5 专有的，PG 上两样都没有 | 在 SQLite 上不咬人（影子表就在那儿），**PG 上是真问题**：端口通道一旦退化，本地这条也抛，被 `except` 吞掉记 0 分，于是 FTS 通道整条静默失效，检索只剩另外三个通道，不报错、召回悄悄变少。更难受的是这条 `log.warning` **每次检索都发一遍**（不像 `_port_search` 的探测只告警一次），PG 上退化之后日志会被刷满 | 归 T18 / `pg_store.py` 持有轨或整合轮。两条：①PG 侧把 `fts_search` 填实，让退化路径压根用不上（本来就该这样）；②给 `_local_fts_rows` 也加一次性的失败记账，别每次检索都刷一条。**①是正解，②是止血** |
| 2026-08-30 | P5 | **`retriever.PORT_FTS_FIELDS` 与 `kb/schema.sql` 里影子表实际索引的列是两处口径，没有机器校验。** 本轨写死 `("title", "body")`，恰好等于影子表当前索引的两列（`id`/`doc_id`/`tenant_id` 都是 UNINDEXED） | 哪天有人给影子表加一个参与索引的列（比如 `tags`），**本地那条 MATCH 会自动跨到新列，端口这条不会** —— 跨列差异原样回归，而且两边都不报错，症状与本轨修掉的那个一模一样。这次的断言（`test_kb_port_link.py::test_both_columns_are_asked_and_the_count_is_a_constant`）守的是「问的列 == `PORT_FTS_FIELDS`」，守不住「`PORT_FTS_FIELDS` == 影子表的索引列」 | 归 T17（`schema.sql` 持有轨）或整合轮。改法：加一条测试，从 `PRAGMA table_info(kb_doc_fts)` 或建表语句里取出参与索引的列，与 `PORT_FTS_FIELDS` 对齐。成本一条测试，不动任何生产代码 |
| 2026-08-30 | P5 | **`_kb_docs()` 的 `except Exception` 会把本轨新加的「sink 没接」`TypeError` 也吞成 `docs: []` + 一行 warning。** 那层兜底是有意的（「检索不阻塞」），本轨没去动它 | 配置错误（忘了传 sink）与业务空结果（真没检到）在 skill 出参上**长得一模一样**，都是 `docs: []`。本轨已把报错信息写得能指路，但它只出现在 warning 日志里，不出现在返回值里 | 归 skills 层持有轨。可选改法：兜底里对 `TypeError`（接线错误）与其余异常（运行期故障）分开处置 —— 前者是「这套系统装错了」，本就不该被「空结果不阻塞」这条覆盖。**不急**，眼下有报错文案兜着 |

## task-T17

本轨（kb schema 补迁移路径）只持有 `maos/kb/schema.sql`、`maos/kb/__init__.py`、
`maos/tests/test_kb_schema_migration.py`（新建）外加两份账本，基线 `f15e5dd`。
本轨已解掉 `## task-T13` 第 3 条：`kb` 层有了版本表 + 迁移，T13 之前建的老库
现在能自己升到 `id` 列那一版（实测老库上 `SELECT id FROM kb_doc` 从
`no such column: id` 变成可用，两条端口通道从恒退化变成 `{True, True}`）。
那一条给的过渡办法「删掉旧的 `evidence/*/maos.db` 重跑」因此不再是唯一出路。

下面三条都在白名单外，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`retriever._PORT_STATE` 是按 store 对象记的粘性判定，就地升级不会自愈**：它是「通道探过没有、通不通」的一次性结论（`_port_search` 的「只告警一次」就靠它），一旦在老库上探出 `False` 就一直是 `False` | 长跑进程里对一个已经退化过的 store **就地**跑 `ensure_schema()` 升级成功之后，检索仍旧走本地实现 —— 库已经好了，进程还认为它是坏的，而且不会再告警。必须重开进程或换一个端口对象才恢复。本轨的 `test_migration_restores_both_port_channels` 因此刻意新造端口对象，并在 docstring 里点破了这一点 | 归 `maos/kb/retriever.py` 持有轨（本轮是 T16）。改法二选一：①给 `port_channel_state` 加一个「忘掉这个 store 的判定」的入口，`ensure_schema()` 真跑了迁移之后调一次；②判定改成带 schema 版本号的键（`(store, applied_schema_version)`），版本一变自动重探。**②更省事且不需要调用方记得**。眼下不急 —— 现实里 `ensure_schema()` 总是在第一次检索之前跑 |
| 2026-08-30 | P5 | **退款域的 18 张表有一模一样的坑**：`maos/domain/refund/schema.sql` 全是 `CREATE TABLE IF NOT EXISTS`（18 条 `CREATE`、18 条 `IF NOT EXISTS`），而 `objects.ensure_schema()` 只是 `executescript` 一把，没有版本表、没有迁移 | 退款域是**业务对象层**，它的列比 kb 层更可能要加（铁律 9 明说业务状态是业务对象自己的字段）。今天往那 18 张表里任何一张加一列，对已存在的库同样静默无效，症状同样是「跑起来一切正常，直到某条 SELECT 报 no such column」。PolarDB 上线后这是必然会撞的 | 归 `maos/domain/refund/objects.py` 持有轨。本轨在 `maos/kb/__init__.py` 里的做法可以整份照搬（版本表 + `_MIGRATIONS` + 每步自带探针 + `_atomic` 的显式 SAVEPOINT），四十来行。**建议在往退款域加第一列之前做**，不然那次加列会先静默失效一轮 |
| 2026-08-30 | P5 | **迁移只管「补列」，不管「影子表与源表失步」**：一个只有 `kb_doc` 而没有 `kb_doc_fts` 的库（部分建表、或影子表被人手工删过），`ensure_schema()` 会把影子表按目标形状建成**空表**，随后 v1 的探针看到 `id` 列已经在，于是不重灌 | BM25 通道恒空，且不报错、不告警 —— 与本轨要买掉的那类失效同型，只是触发条件更窄（正常写入口不会造出这个状态） | 归知识层。改法：给 v1 之后新增一步 v2，判据是 `COUNT(*)` 两边对不上就整表重灌；或把「影子表重灌」抽成独立函数、由一条 `kb doctor` 式的自检命令调用。**不急**：本轨已确认正常路径（`upsert_doc` 是唯一写入口）造不出这个状态，它只来自手工改库 |

## task-T18

本轨（PG 侧全文排序与本地同尺）只持有 `maos/store/pg_store.py`、`maos/tests/test_pg_rank_parity.py`（新建）
外加两份账本。下面四条都是**白名单外**发现的，按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`deploy/polardb.md` 的「已知差异」第 3 节已被本轨证伪**：那节写着「PG（`ts_rank`）`d1` 与 `d2` 同分 0.06079271」「要长度归一就给 `ts_rank` 传 normalization 参数，那会改变现有排序，属于检索调优，不在本轨范围」。本轨已经传了（`FTS_RANK_NORMALIZATION = 2`），实测同一条查询同一份语料，PG 侧现在排 `['d2', 'd1']`，与本地 `-bm25` 一致 | 那节的表格与结论现在都是错的，而它正是「换后端要注意什么」这页里最容易被照着做决定的一节 —— 读的人会以为两边名次仍然不同，从而在混合召回上做多余的兜底。**分数不可跨后端比较仍然成立**（PG 侧 1e-2 量级、本地侧 1e-6 量级），要改的只是「名次也不同」那半句 | 归 T9／T10／整合轮（`deploy/**` 是它们的面，派单 §4 划死只读）。建议措辞：「`ts_rank` 缺省不做长度归一，本层因此传了 normalization=2（见 `maos/store/pg_store.py` 的 `FTS_RANK_NORMALIZATION`），**名次两边一致**；但分数量纲差着四个数量级，**仍然不可跨后端比较绝对值**。名次一致性由 `maos/tests/test_pg_rank_parity.py` 守着」 |
| 2026-08-30 | P5 | **`docs/BACKLOG.md` 的 `## task-T10` 第 5 条（本文件第 959 行）已由本轨解决**：那条写着「要对齐就给 `ts_rank` 传 normalization 参数……但那会改变现有 PG 侧排序，需要有人先决定以哪边为准」 | 条目本身没错，只是已经不是待办了。留着不标注的话，下一个人会重做一遍本轨做过的事 | 整合轮在那条尾部补一句「**已由 T18 解决**，口径以本地 `-bm25` 为准，见 `docs/DECISIONS.md`」即可。本轨不动别轨的记账行（改了就是合并冲突） |
| 2026-08-30 | P5 | **`deploy/docker-compose.yml` 没把 `CREATE EXTENSION vector` 做成 initdb 脚本**（派单 §0.2 点名要记的那条）：起库之后必须手工 `psql -c "CREATE EXTENSION IF NOT EXISTS vector;"` 才有向量类型 | 每个起库的人都要多记一条命令；忘了的话第一条建表就报 `type "vector" does not exist`，而报错指向的是建表语句、不是缺扩展，排查方向容易带偏。`maos/tests/test_pg_store_live.py` 的 fixture 自己补了 `CREATE EXTENSION IF NOT EXISTS vector`，所以测试碰不到这个坑，**只有手工连库的人会踩** | 归 T9（`deploy/**` 是它的面）。改法是在 pgvector 服务上挂一个 `/docker-entrypoint-initdb.d/*.sql`，内容一行 `CREATE EXTENSION IF NOT EXISTS vector;` |
| 2026-08-30 | P7 | **`scripts/demo_preflight.sh` 的 `EXPECT_TESTS` 在「配了 `MAOS_PG_DSN` 的环境」下必然对不上**：无库时本轨实测 `860 passed, 29 skipped`（条数没变，只是 skipped 从 22 涨到 29），有库时是 `889 passed, 0 skipped`。差的 29 条 = T10 的 22 条 live + 本轨的 7 条 parity | 同一份代码、同一条命令，条数取决于跑的时候环境里有没有 DSN —— 整合轮刷这个数字时，刷成哪个值全看当时库起没起，而脚本比的是精确相等。这不是本轨引入的（T10 那 22 条起就这样），但本轨把差额从 22 扩到了 29 | 整合轮刷条数时**在无库环境下刷**（即 `860`，本轨没有改变这个数，不需要为 T18 刷）。更彻底的修法是让脚本比「passed + skipped」或允许 `>=`，但那是 `scripts/demo_preflight.sh` 的面，契约 2 划死了谁都不许改 |

## task-T19

裸 clone 可用性（默认分支卡点 + README 活数字）。基线 `f15e5dd`。
三条都是**单轨做不了、必须人类动手**的，且前两条都卡在提交之前。

> ✅ **2026-08-30 注记（整合轮 13 回填）**：本节里「仓库默认分支是 `main`」那一条**已转绿** ——
> 默认分支已改成 `goai-restructure`，`git ls-remote --symref origin HEAD` 可自证。
> **本节其余行按惯例保留原样，是当时的实录**；别再拿它当当前状态引用。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | 🔴 **卡点 8（本轨新发现）：远端 `goai-restructure` 停在 `4cfef38`，落后本地 `f15e5dd` 21 个 commit** —— 整合轮 11 连同 T7–T14 八轨从未推送。机器判据：`git ls-remote --heads origin` 给 `4cfef38…`，`git rev-parse goai-restructure` 给 `f15e5dd…` | **评委分支敲对了也拿不到当前代码**：实测远端 clone 跑出 `802 passed`（README 写 `860 passed`，差 58 条）、`trace-tree 19/19`（README §3 贴 `29/29`）。**而七项全 PASS、退出码全 0，没有任何一条命令报错** —— 卡点 7 会让人立刻发现不对，这条不会，评委只会看到「跑得通但和 README 对不上数」，最自然的解释是**「这份 README 的数字是编的」**，恰好打在铁律 3 上。逐条读数见 `docs/clone-smoke-report.md` 第六遍「卡点 8」 | 🔴 **提交之前必须做，且必须在卡点 7 之前想到**：人类把本地 `goai-restructure` push 到远端。铁律 5 禁止任何单轨 push，本轨做不了。**不做的话卡点 7 修好了也没用** |
| 2026-08-30 | P7 | 🔴 **卡点 7 的甲路仍未闭合：GitHub 默认分支仍是 `main`**（`git ls-remote --symref origin HEAD` → `ref: refs/heads/main`）。更要命的是实测 `main` 的 `README.md` 里 **`goai-restructure` 零命中、`branch`/`分支` 零命中** —— 它自带一份也叫 “MAOS Runtime” 的 README，看起来完全像对的 | 本轨改的是乙路（`README.md` 的 clone 命令补 `-b` + 显式警告），**但乙路只在「读者已经到了正确分支」时才起作用**。裸 `git clone` 的人、以及在 GitHub 网页上打开仓库的人，读到的都是 `main` 那份 README —— 本轨写的警告他们一个字都看不到，而那份 README 不会告诉他们该切到哪儿去 | 🔴 **提交之前**：人类在 GitHub 设置里把默认分支改成 `goai-restructure`。这是唯一能覆盖「冷启动落地」的修法；乙路是兜底，不是替代 |
| 2026-08-30 | P7 | `docs/submission-checklist.md` A-1 的冒烟判据量不到卡点 7 与卡点 8：它从「clone 对了分支之后」开始掐表，于是这两条对读数的影响都是 **0** | 判据全绿，但评委实际连第一条命令都跑不到（卡点 7），或跑得到却拿到旧代码（卡点 8）。A-1 现在只能证明「机器够快」，证明不了「评委拿得到这份东西」 | 归 **T20**（`submission-checklist.md` 持有轨，本轨按契约 1 不改该文件）。建议 A-1 补两句：**「且用评委最可能敲的那条命令 clone」**（第五遍已提，仍未补）与**「且远端 `goai-restructure` 与本地同 sha」** |

## task-T20

生成物加机器守卫 + 残余过期口径收口。基线 `f15e5dd`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **`docs/EXECUTION.md:730` 仍写「生成物自己 `docs/agent-identity.md:7` 会如实印出「10 个 / 9 个 / 1 个」」**，而生成物实际已是 **11 / 10 / 1**（整合轮 11 投放 `refund_channel` 后重跑过）。同段 `:724`–`:727` 已刷成「11 个角色…其中 **10 个注册进 `AGENT_POOL`**」，唯独这一句括号里的三个数没跟上 | 这句的职责恰恰是**教人怎么引用这组数**，自己却引了过期的。读者照它去核对 `agent-identity.md:7`，看到的是 11 / 10 / 1，会反过来以为生成物错了。本轨新增的 `maos/tests/test_generated_docs.py` **守不到它** —— 守卫保证的是「生成物 == 代码的投影」，而 `EXECUTION.md` 是人写的散文，不在 `gen_docs.py` 的 `TARGETS` 里，任何谈论这组数的散文都在守卫射程之外 | 归持有 `docs/EXECUTION.md` 的轨 / 整合轮。改法只有括号里三个数：「10 个 / 9 个 / 1 个」→「11 个 / 10 个 / 1 个」。~~**本轨白名单外，未改**（铁律 4）~~ ✅ **已闭环**（T31 于 2026-08-31 复核）：该句现在写的是「会如实印出「**11 个 / 10 个 / 1 个**」」（`grep -n '会如实印出' docs/EXECUTION.md` 实测命中一行）。⚠️ **但下面 `## task-T22` 那条（末句语义反了）仍未闭环** —— 同一句的结尾还写着「引用时别把 **10** 直接挂在 `AGENT_POOL` 后面」，而回填后 10 恰恰就是 `AGENT_POOL` 的数，该提醒的对象已变成 11。T31 持有该文件但那一处不在派单范围内，按铁律 4 未改 |
| 2026-08-30 | P7 | **`docs/ppt-outline.md:136` 引的是 A-4 的旧口径**：「PG 后端是空壳且拒绝回落」，并注出处 `docs/submission-checklist.md:60`。本轨已按 `## task-T10` 的建议把 A-4 那格改写成「PG 后端已在本机 Docker PostgreSQL 16.15 + pgvector 0.8.6 实测跑通（22 条 live 测试）；PolarDB 实例未连过」 | PPT 大纲与自查单从这一刻起说的是两件事，而**PPT 是对外的那一份**。「空壳」这个词现在能被 `grep maos/store/pg_store.py` 当场证伪（五个方法全是真实现，只剩 `PgBackendUnavailable` 一条不可用路径会抛）。`## task-T10` 记的那条账说得对：一个能被 grep 证伪的自查单条目，比口径保守更伤 —— 这条同样适用于 PPT。**顺带**：它引的行号 `:60` 也早已不对，A-4 那格实际在 `:162` | 归持有 `docs/ppt-outline.md` 的轨。改法：引文换成 A-4 的新措辞，行号改 `:162`；或索性只引小节名「§A-4」—— 行号漂移是这类跨文件交叉引用的通病，本轮已在两处踩到 |
| 2026-08-30 | P7 | **新增的生成物守卫只守一个方向**：它断言「`docs/` 三份 == 现在重跑生成器的输出」，但**没有任何东西守着生成器本身扫得对**。`scripts/gen_docs.py` 的 `collect_agents()` 若漏扫一类 Agent（比如将来有人不从 `BaseAgent` 继承），文档与生成器会**一致地错**，守卫照样全绿 | 这是「投影式文档」的固有上限，不是本轨实现的缺陷 —— 但值得写下来，免得下一个人把「守卫绿」读成「文档一定对」。缓解事实：当前扫描口径（`BaseAgent.__subclasses__()` 递归 + 有 `identity` + 非抽象）与 `AGENT_POOL` 的注册口径是两套独立机制，生成物把两个数都印出来，本身就是一次交叉验证，真漏扫会让「11 / 10」这组数当场不自洽 | **不急**，观察项。若要补，最小改法是给 `gen_docs.py` 加一条「扫到的角色数 ≥ `len(AGENT_POOL)`」的自洽断言 —— 那要动生成器，本轨白名单外 |

## integrate-round-12

T15–T20 六轨并入。基线 `f15e5dd`（整合轮 11 合并态），六轨基线一致、代码文件零重叠，
冲突只出在两本只追加账本。下面三条都是并轨期发现、**本轮不改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`kb.port_of()` 会把 `PgStorePort` 判成「走老路径」，于是 PG 后端上 `kb.ensure_schema()` 撞 `AttributeError: 'Connection' object has no attribute 'executescript'`**。判据是「暴露了 `_conn` 的对象一律走老路径」，本意是认出核心 `SqliteStore` 的 `_conn()` **方法**；而 `PgStorePort` 恰好也有一个叫 `_conn` 的**实例属性**（存 psycopg 连接），同名不同物，一样命中 | 报错含糊、不带修法，与本仓库「报错要说清怎么修」的通例不符。**这是既有缺陷，六轨一条都没引入它** —— 但 T15 把工厂对 postgres 的放行做掉之后，这条路径从「够不着」变成「两行环境变量就够得着」，所以本轮才显形。实际影响有限：PG 侧的表本来就由 `maos/store/pg_schema.sql` 建，`kb/schema.sql` 是纯 SQLite 方言（FTS5 虚表），本就不该在 PG 上跑 | **不当场改**（铁律 4）。要改得动 `port_of` 的判据，而它是 T13/T16/T17 三轨共用的分叉点，改判据的影响面远大于这条报错本身的收益。下一轮若要动：把判据从「有没有 `_conn`」换成「`_conn` 是不是可调用」最省，或让 `PgStorePort` 把连接改名为 `_pg_conn` |
| 2026-08-30 | P7 | 🔴 **远端 `goai-restructure` 落后本地 27 个 commit**（远端 `4cfef38` / 本地并完六轨后）。T19 轨在第六遍冒烟时发现并记在 `## task-T19`，当时是 21 个，本轮六个 merge 又拉开 6 个 | 拿 README 的 `git clone -b goai-restructure` 照做的人，clone 到的是整合轮 10 的代码：跑 `pytest` 得 `802 passed`，而 README 现在写 `903 passed` —— 差 101 条，且**两边都不报错**，只表现为「照着 README 做，数字对不上」 | **本轮不动**（铁律 5：禁止 push，推送由人类手动做）。**需要人工 `git push` 一次**，否则 README 的 clone 路径对外是失效的 |
| 2026-08-30 | P7 | **生成物 `docs/skill-catalog.md` 写死了源码行号，任何在被引用符号之前插行的改动都会让它过期**。本轮 T16 在 `kb_retrieve.py` 里插了 15 行 `_event_sink()`，`KbRetrieveSkill` 由 `:73` 移到 `:88` | 单轨开发期发现不了：T16 的 worktree 里还没有 T20 的守卫，T20 的 worktree 里没有 T16 的改动，**两轨各自全绿、合起来才红**。本轮由 T20 的守卫当场抓到，跑一次 `gen_docs.py` 即修 | **不改设计**。行号是这份文档的价值所在（点到实现位置），换成「只写符号名」会丢信息。既然守卫已经把「过期」变成红灯，过期本身就不再是无症状故障 —— 这正是 T20 那三份守卫要买的东西 |

## polardb-live

真连一台阿里云 PolarDB PostgreSQL 版实例补跑 `deploy/polardb-live.md` 时发现的，
**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30<br>→ 08-31 部分闭环 | P7 | ~~🔴 该实例不支持 SSL，公网链路是明文~~ → **该实例支持 SSL，2026-08-31 已在控制台开启**。开启前的三处互相印证（`sslmode=require` 被拒、`prefer` 连上但 SSL 未生效、`SHOW ssl` = `off`）是**开启前**的状态，实录保留在 §3.5 | ✅ 加密已生效：两个 env 的 DSN 都带 `?sslmode=require`，实连 `ssl_in_use = True`。🔴 **但口令未轮换** —— 开 SSL 不能追溯地保护开之前的流量，那段明文期用过的口令仍应视为已在公网暴露。这是**已接受的残留风险**，不是已解决 | ✅ 已做：控制台开 SSL、两个 DSN 加 `?sslmode=require`（`pg-admin.env` 那条 08-31 补 —— 原派单只写了改 `pg.env`，漏了高权限账号，导致「实例侧已开、admin 仍走 `prefer`」的半吊子状态）。❌ 口令轮换**决定不做**，见 `docs/DECISIONS.md`。口径已刷进 `polardb-live.md` §3.5 / §3.4 / §五 与 `polardb.md` |
| 2026-08-31 | P7 | **零只读节点时 `rwlb` 集群地址的行为未核实**。阿里云官方文档没有正面写「集群中不存在只读节点时集群地址如何表现」；删只读节点那篇只说「该节点上的连接会发生闪断，其他节点不受影响」，没有任何「集群地址会失效」的警告 | 若将来删只读节点省钱，这是**唯一可能导致连不上**的风险点。推断是集群地址仍可用、读写都落主节点（依据：集群地址的节点集合始终含主节点），但**这是推断，不是文档原文** | 真要删只读节点时，**同时把连接串切到主地址** —— 主地址语义文档明写死（永远指向主节点、读写都行），不依赖任何推断。别拿生产连接去赌灰区行为 |
| 2026-08-31 | P7 | **存储买多了：100 GB 实用 4.03 GB**。ESSD AutoPL **按预购容量计费，不是按实际使用量**（官方原话：「按照您预购的 ESSD 云盘的存储空间进行收费」），那 96 GB 空盘是实收的钱 | 存储占月费的 **81.9%**（¥149.76 / ¥182.88）。100 → 40 GB 可省 **¥89.86/月**，是删只读节点（¥16.56/月，占 9.1%）的 **5.4 倍**。省钱的大头在盘上，不在节点上 | 第一步只需去控制台变配页看滑杆能否下拉到 40 GB（标准版 + 按量付费是否开放缩容，PG 侧文档没正面写），一分钟有答案。约束：AutoPL 地板 **40 GB**（降到 20 GB 做不到）；缩容后总空间需比已用至少多 20 GB 且已用不超其 80%（4.03 GB 下 40 GB 两条都满足）；缩容有 ~30s 闪断且只读节点会自动重启；AutoPL **不支持转换为其他存储类型** |
| 2026-08-30 | P7 | **`deploy/polardb.md` 的一条推断被实测推翻**：原文写「`zhparser` / `pg_jieba` 这类中文分词扩展**大概率装不了**（托管实例通常只允许白名单内的扩展）」。实测该实例共 **189** 个可用扩展，`zhparser 2.2`、`pg_jieba 1.1.2`、`pg_bigm 1.2`、`pgroonga 4.0.5` **四个都在可用列表里** | 这条推断此前是「中文全文在 PG 上无解」的最后一环。`polardb.md` 记着：`_port_search` 是「探一次记一次」，一次 CJK 查询抛 `LookupError` 就把该 store 的全文通道**永久**标记不可用，此后连英文查询也走本地实现 —— 在本仓库这种中文语料上，等于 PG 全文通道基本不会被用上。现在这个死结**有解的可能** | 单独一轨做，因为它是三步不是一步：`CREATE EXTENSION zhparser` 能不能成 → 建文本检索配置与配套 GIN 索引 → `MAOS_PG_FTS_CONFIG` 切过去后实测中文召回质量。本轮只验到「在可用列表里」，**装都没装**，三件事别混说。推断已在 `polardb.md` 与 `polardb-live.md` §3.1 两处点名改掉 |
| 2026-08-30 | P7 | **白名单不放行时的症状是 TCP 静默超时，不是拒绝**。`dig` 能解析出公网 IP，`connect()` 挂满 8 秒超时，看起来跟「网络不通 / 实例没起来」完全一样 | 排障时极易误判方向。`polardb_smoke.py` 第 1 步刻意只报驱动异常类名不报 message（防 host 泄漏），所以从脚本输出上更看不出是白名单 | 若以后要省这一轮往返：给 `polardb_smoke.py` 第 1 步加一条判据 —— **DNS 解析成功 + TCP 超时 → 提示「大概率是白名单没放行本机出口 IP」**，并打印 `dig +short myip.opendns.com @resolver1.opendns.com` 拿到的出口 IP（这条不泄漏目标 host）。本轮不改脚本 |
| 2026-08-30 | P7 | `scripts/demo_preflight.sh` 的 `EXPECT_TESTS` 在配了 `MAOS_PG_DSN` 的环境下对不上 —— 这条 T18 已记在 `## task-T18`，当时**预测**该值是 932 | **本轮实测确认：932**（`903 passed, 29 skipped` 无库 → `932 passed, 0 skipped` 有库；29 条 skip 全是 DSN 门控，22 条在 `test_pg_store_live.py`、7 条在 `test_pg_rank_parity.py`）。即预测值正确，不是新问题 | 处理时机仍归 `## task-T18` 那条。真要修，最省的改法是让 `demo_preflight.sh` 检测到 `MAOS_PG_DSN` 非空时把期望值切到 932 |

## polardb-live-r2

第二轮（HNSW 性能 + zhparser）发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **`maos/tests/test_pg_store_live.py::test_chinese_query_raises_instead_of_silently_missing` 在 `MAOS_PG_FTS_CONFIG` 指向非内置配置时必红**。它断言 CJK 查询必抛 `LookupError`，写死了「当前配置一定是 PG 内置的」这个前提；装了 zhparser 并切到 `zhcfg` 之后本层正确地不再抛，于是 `DID NOT RAISE` | 只在配了中文分词的部署上出现（本轮实测：`39 passed, 1 failed`）。**库代码没有任何问题** —— 这条红恰恰是「升级路径是一个环境变量、不用改代码」成立的证据。但它会让一个正确配置的环境跑出红灯，读起来像回归 | 修法很短：按 `port.fts_config()` 是否落在 `_PG_BUILTIN_FTS_CONFIGS` 里分支 —— 内置就断言抛错（现状），非内置就断言**真能查出结果**（正好把 §1.4 的中文召回也钉成守卫）。属于改守卫判据，该由一轨专门做 |
| 2026-08-30 | P7 | **本层从不设置 `hnsw.ef_search`，完全依赖服务端缺省值**。实测该实例缺省是 40，召回 99.3%；调到 10 则掉到 85%–90% | 换一台实例、换一个 pgvector 版本，或有人在实例参数里改了这个值，**向量召回会静默变化**：不报错、不变慢，只是结果悄悄变差。这正是本仓库反复防的那类「无症状故障」 | 若 PG 向量通道要上生产：在 `PgStorePort.connect()` 里显式 `SET hnsw.ef_search`（值随召回要求定，实测 40 够用），把它从「环境的缺省」变成「代码的选择」。本轮不改，属于实现面 |
| 2026-08-30 | P7 | 补跑在云库里留下两张表：`kb_doc_pg` 有 24 条**真语料**（`scenarios/refund/history/` 灌入，带真实 embedding），`t_hnsw_perf` 有 20 万条**合成数据**（堆表 123 MB + HNSW 索引 109 MB，合计 238 MB） | 都不影响测试（live 测试用的是自己的 `t10_live` / `t10_live_cmp` 靶表，跑完即删）。但 `t_hnsw_perf` 是纯合成数据，留着会让后来的人误以为库里有业务数据；238 MB 的存储也在按量计费 | **本轮已执行**：`DROP TABLE t_hnsw_perf`（2026-08-30），库体积 247 MB → 8545 kB，`public` 里只剩 `kb_doc_pg` 一张表、24 行。要复跑 §1.5 的规模测试得重建，一次约 200 秒公网 COPY。`kb_doc_pg` 那 24 条按建议留着，它是 §1.4 中文召回结论的现场 |
## task-T21

提交清单的冷启动判据（A-1 ⑧⑨）与 A-4 口径转实测。基线 `129e71d`。
本轨只改 `docs/` 下三个文件，不碰任何 `.py`。下面三条都在本轨白名单外，**本轮不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | ✅ **卡点 7 已转绿，但四处旧口径还写着「默认分支是 `main`」**。转绿发生在本轨执行途中：开工自检时 `git ls-remote --symref origin HEAD` 还给 `ref: refs/heads/main`（HEAD `3f2d5d1`），二十分钟后复跑已是 `ref: refs/heads/goai-restructure`（HEAD `8492c56`），三处交叉印证（本地 `origin` 别名／裸 clone 出来那份自己的 `origin`／直连 URL 不经别名）。裸 clone 落地实测：检出 `goai-restructure`、`maos/` 在、顶层 19 项齐、README 里 `goai-restructure` 命中 4 次。四处过期口径：`README.md:165`、`docs/clone-smoke-report.md:466`、本文件 `## task-T8` 第 1 条、本文件 `## task-T19` 第 2 条 | `README.md:165` 那条最要紧，它写的是「**`-b goai-restructure` 一个字都不能省。** 本仓库的默认分支是 `main`」—— **结论仍可留，理由已作废**。留着不改，读者照着读会得出「远端设置还是坏的」这个与实测相反的印象；而真去核对又会发现 README 与仓库当前状态对不上，正好踩在铁律 3 的观感上。`clone-smoke-report.md:466` 同理，它是六遍冒烟的实录文档 | 归整合轮（`README.md` 与 `clone-smoke-report.md` 都是整合轮的面，本轨白名单只有 `docs/submission-checklist.md` 与两份账本）。**建议改法**：`-b` 保留不动，把理由从「默认分支是 `main`」改成「写死分支是为了不依赖远端设置 —— 该设置在网页上改回去不留 git 痕迹」；`clone-smoke-report.md` 与本文件 `## task-T8`／`## task-T19` 两节属历史实录，按惯例**不改历史行**，只在节内加一行「已于 2026-08-30 转绿」的注记 |
| 2026-08-30 | P7 | 🔴 **卡点 8 仍红**：远端 `goai-restructure` = `8492c56`，本地 = `129e71d`，A-1 ⑨ 的自判命令实测打 `DIFF`。本地领先一个 commit —— 就是 `129e71d` 那条「PolarDB 真连补跑」还没推上去 | 评委分支敲对了（现在裸 clone 就对）也拿不到这一个 commit。这一个 commit 恰好是把两份 PolarDB 文档从「未实测」转实测的那次，而 A-4 的新口径正引用它 —— **清单说「已连通实测」，评委 clone 到的那份文档却还写着「未连过」**。比 `## integrate-round-12` 记的那次（差 27 个）影响面小得多，但方向更难堪 | 🔴 **提交之前**：人类 `git push` 一次。铁律 5 禁止任何单轨 push，本轨做不了。注意 `## task-T19` 记的「21 个」与 `## integrate-round-12` 记的「27 个」都已作废（两条都把远端记作 `4cfef38`，实测远端已是 `8492c56`），别拿旧数字去核 |
| 2026-08-30 | P7 | **判据里写死「落后 N 个 commit」这个模式本身有问题**：`## task-T19` 与 `## integrate-round-12` 各写死过一个 N（21／27），人类一次 push 就让两个数同时作废，而账本里没有任何机制会提醒它们过期 | 后续轨照着旧 N 去核，会把「已经推过一次」误读成回归，或反过来以为还差 27 个而不敢动。本轮派单也是先踩了这个坑才在正文里点名两个数都不对 | 流程项，不是缺陷。**本轨已在 A-1 ⑨ 落地了替代写法**：判据写成「远端与本地同 sha」并给一行自判命令，数字由命令自己算，不需要维护。建议后续凡涉及「两个 ref 的相对位置」的判据一律照此写，不再写死 N |
## task-T22

整合轮 13 前的口径回填轨（`docs/EXECUTION.md` 三个数 + `docs/ppt-outline.md` 交叉引用）。
下面五条都是本轨改动范围**之外**发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | 🔴 **`docs/EXECUTION.md:730` 末句在三个数回填后语义反了**。原文是「…会如实印出「10 个 / 9 个 / 1 个」并解释差在哪，**引用时别把 10 直接挂在 `AGENT_POOL` 后面**」。旧口径下 10 = Identity 总数，所以提醒「别把 10 挂在 `AGENT_POOL` 后面」成立；回填成 11/10/1 之后，**10 恰恰就是 `AGENT_POOL` 的数**，该提醒的对象变成了 11 | 这句话现在会把读者往反方向带：照它做反而不敢写「10 个注册进 `AGENT_POOL`」——而那正是同段 `:724`–`:727` 已经写对的说法。派单 §5.1 明令「末尾那句一个字不动」，故本轮只回填三个数 | 下一轮把末句改成「引用时别把 **11** 直接挂在 `AGENT_POOL` 后面」。一处一行，改完与同段 `:724`–`:727` 自洽 |
| 2026-08-30 | P7 | **`docs/ppt-outline.md:726` 的「活数字复跑」表仍是旧口径**：`\| Agent 数 \| `docs/agent-identity.md:9` \| **10** Identity，**9** 个可派单 \| ✅ 一致 \|`。与本轨 §5.1 在 EXECUTION 修掉的是同一处失真，且锚点行号也漂了（生成物现在是 `:7`，不是 `:9`） | 末列印着「✅ 一致」，而它实际与生成物**不一致** —— 这比单纯写错更坏：一张自称复跑过的表给出假的通过信号。生成物实测第 7 行是「**11 个** / **10 个** / **1 个**」 | 派单白名单只点名了「不许说的话」两行与 checklist 交叉引用，这一行两样都不是，故不当场改。下一轮与上一条合并做：锚点改 `:7`、数字改 11/10，末列重跑后再写「✅ 一致」 |
| 2026-08-30 | P7 | **`docs/ppt-outline.md:268` / `:473` / `:505` 三处仍禁两代前的口径「后端已可插拔切 PolarDB」**，而它们的出处本轮已按硬判据 3 一并改成 `§A-4` | T21 把冻结契约 A 落进 `docs/submission-checklist.md` 的 A-4 表之后，A-4 里**不再有这句话**（换成了「PolarDB 上生产可用」与「支持中文分词检索」两条），于是这三处成了**悬空引用**：指了小节，小节里没有被引的那句。P4 页（本轨已改）与 P8a/P12/P13 三页因此口径分叉 | 下一轮把这三处按冻结契约 A 的「不许这么说」格统一，与本轨 P4 页写法一致。P13（`:505`）那处是「A-4 七行总表」的枚举，改法是把枚举项换成新的两条 |
| 2026-08-30 | P7 | **`docs/ppt-outline.md:706`–`:707` 的历史台账记的 checklist 行号本轮实测已再次漂移**：台账记「实测 `:183-190` = 评委三段反馈的诊断与回应落点」「实测 `:119-137` = A-4 口径一致性七行表」，而当下 `:183-190` 落在页锚说明块、`:119-137` 落在 A-2 的「已知缺口 ✅ 已解决」块，两处都指错 | 这正是本轨把交叉引用改成小节名的直接证据 —— 同一批锚点在一轮之内漂了第二次。台账本身是**历史读数**（记的是 `147df03` → `27c9e18` 那一轮的当场实测值），改了就是篡改证据（铁律 3） | **不改**。若要补，只能在台账下方另起一行注明「这两行的实测值已于 2026-08-30 再次失效，故改引小节名」，不动原行 |
| 2026-08-30 | P7 | **`docs/ppt-outline.md` 里还有 62 处非 checklist 的「文件:行号」式交叉引用**（`docs/architecture.md:12-56`、`README.md:*`、`docs/agentteams-mapping.md:*`、`docs/domain-portability.md:*`、`maos/**:*` 等） | 与本轨清掉的 14 处 checklist 引用是同一种漂移风险，只是暂时还没被踩到。本轮已在两处实测踩到（上一条 + `:60` 指向 TypeScript 骨架行） | 本轨只被授权改「引 checklist 的」那一类，故其余 62 处一处未动。下一轮若要收口，建议按「同一文件有稳定小节号的（`README.md` §N、`docs/domain-portability.md`）优先改小节名；指向 `maos/**` 源码行的保留行号」分两类处理 —— 源码行号是这类引用的价值所在，不宜一刀切 |
## task-T23

给 `scripts/gen_docs.py` 加自洽断言、给 `scripts/demo_preflight.sh` 加双档期望值时发现的，
**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **期望条数从一个写死的数变成了两个**：`EXPECT_TESTS_NOPG=903` 与 `EXPECT_TESTS_PG=932`。本轨修的是「脚本认不认环境」，没修「数是写死的」 | 整合轮回填活数字时要**同时改两处**，而漏改有库那处**不会在无库机器上显形** —— 绝大多数回填恰恰发生在无库机器上，于是漏改要等到下一次真起库才炸。两数之间的差值 29（22 条 live + 7 条 parity）反倒是稳定的 | 下一轮可考虑只写死无库档，有库档写成「无库档 + 29」，把「两个各自漂的数」压成「一个数 + 一个差值」。本轮不做：差值本身也会随 live／parity 用例增删而变，换写法只是把维护点从一处挪到另一处，收益不明确，先观察一轮 |
| 2026-08-30 | P7 | **本轨的自洽断言只守住三份生成物里的一份**。`_assert_scan_covers_pool()` 拿 `AGENT_POOL` 反查 `collect_agents()` 的扫描面；`collect_skills()`（skill-catalog）与 ToolPort 那份**没有等价的第二套注册口径可反查**，本轨也没去找 | `## task-T20` 第 3 条点名的就是 agents，本轨照做属范围内。但「文档与生成器一致地错」这个失效形态对另外两份同样成立，那两份目前无人看守 | 下一轮若要补：先确认 skill／tool 两侧存不存在与扫描面**互相独立**的第二套口径 —— agents 这侧能做，正是因为「扫 `BaseAgent` 子类」与「注册进 `AGENT_POOL`」是两套独立机制。找不到独立口径就别硬造：拿同一个来源自己验自己是个假守卫，比没有守卫更坏 |
| 2026-08-30 | P7 | **判据同源，实现是两份**：`demo_preflight.sh` 的 `pg_reachable()` 内联一段 python 拿 `PgStorePort.connect()` 探测，`maos/tests/test_pg_store_live.py` 的 `_live_dsn()` 做的是同一件事。两边走同一条码路，但代码是各写各的 | 今天两边一致，无痛。将来若测试那侧收紧判据（例如再验一条「`vector` 扩展装了没」），preflight 不会跟着变严，于是出现「测试那 29 条 skip 了、preflight 仍按有库档期望 932」的误红 —— 与本轨刚治好的病同形，只是换了触发条件 | 要收敛就把探测提成 `maos/store/pg_store.py` 里一个公开小函数（如 `dsn_reachable()`），preflight 与测试都调它。本轨白名单外（`maos/store/**` 不归本轨），且当前无症状，只记账不改 |
## task-T24

修 `kb.port_of()` 的判据（`## integrate-round-12` 第 1 条）时，在判据周边看到的三条，
**本轮都不改**（铁律 4）。三条同源：判据只能靠「形状」区分两种连接，而形状是驱动给的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`kb._conn()` 里还留着同一类风险的下半截**：它只判 `_conn is None`（是就抛 TypeError），**不判「这是不是一条 sqlite 连接」**。所以一个 `_conn` 非 None、不可调用、又**没有** `execute` + `query` 的对象，`port_of()` 会返回 None，`_conn()` 会把这条非 sqlite 连接原样交出去，下一步照样撞 `executescript` | **今天够不着**：真链路上没有这个形状 —— `PgStorePort` 有 `execute` + `query`，会被 `port_of()` 先认走。够得着的前提是「有人写了个持有 psycopg 连接、却没实现 F-2 两方法的 store」，那本身就不是合法 store | 与下一条一起收口更划算：把两处都换成 `isinstance(conn, sqlite3.Connection)`，语义直接对上「老路径只会 sqlite」。本轮不做的理由是它比修 bug 本身的面大 —— 会改掉 `port_of()` 对**鸭子类型 sqlite 连接**的接受度，而测试里有替身在用这条 |
| 2026-08-30 | P5 | **新判据的第一道防线压在 `sqlite3.Connection.__call__` 这个 CPython 老接口上**（建预编译语句用，非文档主推）。本机 3.11.7 实测：`sqlite3.Connection` 自带 `__call__` → 可调用；`psycopg.Connection` 整条 MRO 都没有 → 不可调用。判据正是靠这一条分开两者 | 若将来某个 Python 版本摘掉 `sqlite3.Connection.__call__`，第一道防线塌。**但不会静默失效**：核心 `SqliteStore` 没有 `execute` / `query` 两个方法，仍旧落回 None，第二道防线接住；且 `maos/tests/test_kb_port.py` 第 4 节直接钉住了这两条驱动事实，会先红 | 等 `test_kb_port.py::test_sqlite_connection_is_callable_and_pg_connection_is_not` 真红那天再动，换成上一条说的 `isinstance` 判据。**在那之前不要预先改** —— 现在换的收益只是「理由更好听」，代价是动一个三轨共用的分叉点 |
| 2026-08-30 | P5 | **`## integrate-round-12` 第 1 条对成因的描述有一处事实错误**，本轮实测推翻：原文写判据「本意是认出核心 `SqliteStore` 的 `_conn()` **方法**」。实测 `maos/core/store.py:98` 是 `self._conn = sqlite3.connect(...)` —— 它是**存连接的实例属性**，和 `PgStorePort._conn` 是同一类东西，不是「方法 vs 属性」之别 | 影响的是**下一个人的修法选择**：照原文读会以为「两者本质不同、判 callable 天经地义」，实际两者本质相同，判 callable 之所以成立纯粹因为 `sqlite3.Connection` 恰好带 `__call__`（见上一条）。这个差别决定了要不要给判据留第二道防线 | **已在本轮就地更正**：`maos/kb/__init__.py` 的模块头与 `port_of()` docstring 都按实测重写了。历史条目（`## integrate-round-12` 第 1 条）**一字不动**——铁律 3，那是当时的读数；本条即为更正记录 |
## task-T25

检索层三条卫生（粘性判定自愈 / 影子表口径机器校验 / 退化路径记账）落地时发现的，
**本轮都不改**（铁律 4）。基线 `129e71d`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **两条端口通道都走不通时，`retrieve()` 静默返回空列表** —— 调用方分不清「库里确实没有相关知识」和「所有通道都死了」。本轨的假 PG 复现里 5 次检索全部 0 命中：全文与语义两条通道退化后，剩下的 `rule_no` / `gateway_code` 是精确通道，查询不带这两个字段就一个信号都没有，`score <= 0` 全被滤掉 | 返回值 `[]` 与「真的没命中」逐字节相同，而后者是冷启动的常态、前者是故障。日志里有退化告警，但那是**按 store 只刷一次**的（正是本轨买的东西），第二次检索起就完全无声。铁律 8 的口径是 MAOS 只持观察与推断 —— 这里连「我没看清」这个观察都没往上报 | 归 skills 层那一轨。最省的止血是让 `retrieve_and_log` 落 `KbRetrieved` 时把 `port_channel_state()` 一起带上，事件里就能分辨；动 `retrieve()` 的返回形状（比如改成 `(hits, degraded)`）影响面大得多。两条都在本轨白名单外 |
| 2026-08-30 | P5 | **退化告警把异常对象当日志参数传**（`log.warning("…（%s）…", exc)`），于是任何「验对象被回收」的测试在 pytest 下恒红：`caplog` 把 `LogRecord` 攥到测试结束，record → args → exc → traceback → 抛出它的栈帧 → `self`（端口对象）→ store，`WeakKeyDictionary` 里的条目整场掉不下去 | 生产上无害（handler 格式化完就丢），**只坑测试**，而且坑得很像真泄漏。本轨判据 3 第一次就撞上：同一段代码单机脚本跑是 `5 -> 1 -> 0` 干净回收，pytest 下是 `20 -> 20`，看上去像弱引用写丢了 | 本轨已在测试侧绕过（`_warnings_of` 在作用域内掐掉 propagate，并当场把记录转成字符串）。真要根治是把传参从 `exc` 换成 `str(exc)`，一处一行 —— 但它动的是既有告警路径，在本轨白名单内却**不在任务定义里**，铁律 4 不当场改 |
| 2026-08-30 | P5 | **`_schema_stamp()` 读不到版本一律记 `-1`，是个两态判据**：版本查询若因连接抖动间歇性失败，戳会在 `-1` 与真实版本之间来回跳，每跳一次判定就整张作废、下次调用重探、失败再刷一条告警 | 「按 store 只告警一次」在后端半死不活时退化成「每两次抖动刷一条」—— 而那正是日志最不该被刷屏的时候。稳态下不出现（读得到就是读得到），所以本轮的测试全绿也说明不了什么 | 最省的改法是把「读不到」与「读到了但还没记账」分开：读不到时**不动**已有的戳（保持判定不变），只有读到一个**不同的真实版本**才作废。本轮没做 —— 它要给戳加一个「缺失」的第三态，而三态会让 `_port_state()` 的作废条件从一行变成三行，收益却要等真出现抖动才兑现 |
## task-T26

退款域迁移路径 + 测试环境隔离起跑线（T26）执行期间发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | 🔴 **冻结契约 B 写死的「有库 932」已经不成立，实测 944**。契约 B 同时要求两件互相冲突的事：T26 要新增 `test_refund_migration*.py`（派单 §5.2），而任何新增测试都会把有库全量从 932 抬高。本轮新增 12 条，于是 903+12=915（无库）、932+12=944（有库） | `scripts/demo_preflight.sh` 的 `EXPECT_TESTS` 有库分支若按契约 B 写死 932，会当场对不上并被当成回归报假警 —— 而契约 B 明文要求 T23「不要因为自己跑出别的数就去改这个契约，对不上就停手报告」。**T23 那侧照做就一定卡住**，这是契约本身的缺陷不是任一轨的执行问题 | 由人类裁决改哪一侧。建议把契约 B 的判据从写死的数字改成对新增测试免疫的**不变量**：`有库 passed == 无库 passed + 29 且 有库 skipped == 0`。这条表述钉的正是契约 B 真正要买的东西（22 条 live + 7 条 parity 一条都不许被饿死），而且以后谁加测试都不用回来改契约。本轮实测：915 + 29 = 944 ✅ 　**✅ 已闭环**（T31 于 2026-08-31 复核）：建议被采纳了 —— `scripts/demo_preflight.sh:48-50` 现在是 `PG_GATED_TESTS=29` / `EXPECT_TESTS_NOPG=1069` / `EXPECT_TESTS_PG=$((EXPECT_TESTS_NOPG + PG_GATED_TESTS))`，写死的数字换成了对新增测试免疫的算式，「有库 932」这个判据不存在了 |
| 2026-08-30 | P5 | `maos/tests/conftest.py` 新加的 `MAOS_PG_DSN` delenv 之所以没饿死 live 测试，靠的是 collection 期时序（模块级 `pytestmark` + `_live_dsn()` 的 `lru_cache` 在 import 那一刻就求了值），**而这条时序没有任何断言钉着** | 把 live 那两个模块的 `pytestmark` 换成用例内的 `pytest.skip()`、或摘掉 `_live_dsn()` 的 `lru_cache`，判定就挪进用例执行期，那时 `os.environ` 已被 delenv 清空 → 29 条当场全 skip。**无库环境跑不出这个差别**（无库时它们本来就 skip，读数一模一样），唯一的哨兵是有库全量 | 需要新起一个测试文件（如 `test_conftest_env_baseline.py`）钉住它，超出 T26 白名单所以本轮没做。做的时候连上一条一起做：那条不变量正好也是这个文件该守的 |
| 2026-08-30 | P5 | **`## task-T17` 第 2 条写的「退款域 18 张表」是错的，实测 14 张**（`grep -c 'CREATE TABLE' maos/domain/refund/schema.sql` 在 T26 之前 = 14）。T26 加了 1 张迁移记账表之后该 grep = 15，业务表仍是 14 | 数字本身不影响代码，但它是 BACKLOG 里被下一轨直接引用的事实。派单 T26 已当场纠正过一次；再有人照抄 `## task-T17` 原文仍会拿到 18 | 顺手改 `## task-T17` 第 2 条那个数字时一并说明「T26 后 grep 得 15，业务表 14」。本轮不改别人的节（六轨共享账本，只许尾部追加） |
| 2026-08-30 | P5 | `maos/domain/refund/objects.py` 的 `_atomic()` **只有底层连接一条路径，没有 StorePort 分支**。照搬源 `maos/kb/__init__.py` 的 `_atomic()` 是先试 `port_of(store)` 走端口的 `transaction()`，端口不存在才落到 `_conn()` | 退款域换到 PG 后端时，迁移拿不到事务 —— 而 `_conn()` 那条路径本身就取 `SqliteStore` 的私有属性，PG 上直接 `TypeError`。当前无影响：退款域整层都还锁在 SQLite 上（见下一条） | 与下一条「`_conn()` 取私有属性」是同一件事的两面，一起做：给退款域接 StorePort 时把 `_atomic()` 的端口分支一并补上 |
| 2026-08-30 | P5 | （承接 `## task-R1` 第 3 条，派单 §0.2 明示本轮留账不改）`objects._conn()` 取 `SqliteStore` 的私有属性 `_conn`，退款域整层因此绑死 SQLite | 退款域上不了 PG／PolarDB。P5 已经把工厂放行到 PG，这条是退款域**没跟上**的那一截 | 归 StorePort 接线那一轨，本轮无人持有 |
| 2026-08-30 | P5 | （承接 `## task-T15` 第 4 条，派单 §0.2 明示不归本轨）`create_store(store, backend='postgres')` 静默忽略传入的 store | 调用方以为自己传的 store 被用上了，实际没有 | 归 `maos/store/factory` 那一面，本轮无人持有 |

## integrate-round-13

T21–T26 六轨并入。基线 `129e71d`，并轨时主干已由另一会话推进到 `6d195ec`
（`072f4b2` PolarDB 二轮 + `6d195ec` 删合成表）。六轨各 1 个 commit、工作区全干净。
本轮实测：**935 passed / 29 skipped（无库）**、**964 passed / 0 skipped（有库）**，
935 + 29 = 964；`run.py` exit=0。有库那档是本轮自起 pgvector 容器
（`-p integrate13-pg`，端口 55433，跑完 `down -v` 无残留）实跑出来的，不是抄的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **`deploy/polardb.md` 的读数表已过期**：写着「932 passed, 0 skipped（有库）／903 passed, 29 skipped（无库）」，整合轮 13 后是 **964 / 935** | 该表是 PolarDB 部署手册里的「本机对照组」，评委照它核会对不上。但两份 `deploy/polardb*.md` 属另一会话（`9d1e0d57`）的面，且 SSL 加固与口令轮换落地后还要再改一轮 | ✅ **已闭环（2026-08-31）**：起 Docker + `docker build -t maos-sandbox` 后重跑，拿到全绿读数 **`1098 passed, 10 skipped`（有库）/ `1069 passed, 39 skipped`（无库）**，已刷进 `deploy/polardb.md`。964 也早已过期（整合轮 14 后又涨了一截），**没有沿用任何预测值**。同时把「跑全量前先起 Docker」写进了那张表下面 —— 不起 Docker 会红两条，且降级产出的证据在隔离性这一维是空的 |
| 2026-08-30 | P7 | （承接 `## task-T26` 第 3 条）**`## task-T17` 第 2 条写的「退款域 18 张表」仍是错的**，实测业务表 14 张、加 T26 的迁移记账表共 15 张 | 它是被后轨直接引用的事实，照抄会拿到 18 | 本轮未改：`## task-T17` 是历史实录节，按惯例不改历史行。下次有人正当持有该节时顺手更正 |
| 2026-08-30 | P7 | **`maos/tests/conftest.py` 那条 delenv 的时序仍没有断言钉着**（`## task-T26` 第 2 条原文） | 本轮把「有库必须 0 skipped」钉进了 `demo_preflight.sh` 第 1 步，比只查条数强：29 条被饿死时 passed 会正好等于无库那档，只查条数看不出来。**但这仍是环境哨兵不是单元断言** —— 无库机器上跑不出这个差别 | 仍需一个 `test_conftest_env_baseline.py` 把时序钉死。归下一轨，与 `## task-T26` 第 2 条合并处理 |
| 2026-08-30 | P7 | **`docs/clone-smoke-report.md` 的六遍冒烟没有第七遍**：卡点 7 已转绿、卡点 8 本轮推送后也闭合，但报告里没有一遍是在「两条都绿」之后跑的 | 报告的结论行仍写着「裸 clone 不成立」，与仓库当前状态相反。本轮已在卡点 7 一节顶上加了转绿注记，并**已补跑第七遍**（裸 clone，五步全 0 退出，见该文件末节） | ✅ 本轮已做：第 3 条结论六遍以来第一次转为**无条件成立**。仍未做的是**物理断网**下的零出网复核（七遍都没做过） |

## task-T27

RocketMQ 真后端（整合轮 14 · 丙类）。基线 `d98b9d1`。本轨白名单外、按铁律 4 记账不改的六条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5/P6 | 🔴 **`scripts/demo_preflight.sh:47` 的 `EXPECT_TESTS_NOPG=935` 被本轨打破**：本轨新增 9 条无 broker 也跑的测试，无库全量从 935 变成 **944**。实跑 `bash scripts/demo_preflight.sh` → `[FAIL] 第 1 步不符期望：测试条数与期望不符 / 实际：944 passed / 期望：935 passed`，exit=1 | 录制前置第 1 步当场红。有库那档是 `EXPECT_TESTS_PG=$((EXPECT_TESTS_NOPG + PG_GATED_TESTS))` 的算式，改一处即可两档一起对 | 归 **T23**（`scripts/demo_preflight.sh` 是那一轨的面，§4.2 明确禁碰）。整合时把 `EXPECT_TESTS_NOPG` 改成 944。**注意本轨的 8 条 broker 门控测试不进这个数**——它们只在装了客户端且 broker 可连时才跑，与 PG 那 29 条门控是同一种结构，但**没有**纳入 `PG_GATED_TESTS` 那个差值。**✅ 已闭环**（T31 于 2026-08-31 复核）：该行现在是 `scripts/demo_preflight.sh:49` 的 `EXPECT_TESTS_NOPG=1069`，与本机实测 `1069 passed, 39 skipped` 对得上（944 这个中间值也早已被后续几轨越过） |
| 2026-08-30 | P5/P6 | 另有五处文档写死了 `935 passed`：`README.md:180`、`docs/submission-checklist.md:24`、`docs/ppt-outline.md:520` 与 `:724`、`docs/clone-smoke-report.md:716` | 与上一条同源。评委照 README 跑会得到 944，与文档对不上；症状是「照着做数字不符」，两边都不报错 | 与上一条一起改。五处都在本轨白名单外 |
| 2026-08-30 | P5/P6 | **`README.md` §9 文档索引里没有 `deploy/rocketmq.md` / `deploy/rocketmq-live.md` 两行**，而 PolarDB 那一对是有的，且 §9 有一段红字专门讲「这两份别混着读」 | 新加的两份文档没有入口，评委不会知道它们存在 —— 而「哪些跑通了、哪些没有」正是这两份的价值 | README 在本轨白名单外。整合时照 PolarDB 那两行的写法补上，红字段落也照抄一段 |
| 2026-08-30 | P5/P6 | **`create_event_bus()` 没有接进 `maos/flows/`**：`maos/flows/common.py:90` 仍是写死的 `InMemoryEventBus()`。七个场景因此**仍然只跑内存版** | 本轨证明的是「总线层可替换 + 两个后端语义等价」，**不是**「场景已经跑在 RocketMQ 上」。文档里已经点明这一点（live §2.3），别把前者当后者 | `maos/flows/**` 是「谁都不许动」的面（场景收口断言）。真要接，得单独一轨，并且要先接受「每轮 drain 的地板成本是 `2 × 5s × topic 数`」这个代价 |
| 2026-08-30 | P5/P6 | **`PushConsumer` 没有实现**。`maos/flows/common.py:104` 写着「换 RocketMQ 后这个循环消失（消费者常驻）」，而本轨只做了 `SimpleConsumer` + `drain` 语义等价 | 那句注释描述的能力**尚未兑现**。当前措辞是「换了之后会怎样」的设计描述，够不上「声称支持但无法实现」的红线，但它确实还没被证过 | 归后续轨。做之前先读 live §3.6：drain 的 5 秒地板正是「常驻消费者」要解决的问题，两件事是同一个动机 |
| 2026-08-30 | P5/P6 | **`maos/tests/conftest.py` 没有清 `MAOS_EVENTBUS_BACKEND`**（派单 §5.4 要求：该文件是 T26 的面，本轨不许动，需要配合就记账） | 谁在 shell 里 export 过 `MAOS_EVENTBUS_BACKEND=rocketmq`，再跑全量时那条「未设环境变量必须拿到内存版」的红线守卫会红 —— 而它是本轨最该守的一条。本轨自己的测试用 `monkeypatch.delenv` 兜住了，兜不住的是**别的**测试里间接走 `create_event_bus()` 的路径（目前没有，所以当前无实害） | 归 **T26**（`conftest.py` 那一面）。与 `## task-T26` 第 2 条「delenv 时序没有断言钉着」是同一类问题，一起做：那条要的正是一个把环境变量基线钉死的测试文件 |

## task-T28

Nacos 配置治理（T28）执行期间发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | 🔴 **MAOS 看不见「配置中心挂了」**。`NacosConfigSource.degraded` 只在**构造时**连不上才置位；连上之后掉线由 SDK 内部重连兜着，它不通知上层。实测（`deploy/nacos-live.md` §1.6）：`docker stop maos-nacos` 之后 `degraded` 仍是 `False`、`explain()` 仍说 `nacos` | 「沿用最后一份配置」（last-known-good）这个行为本身是对的 —— 反过来等于「配置中心一抖动所有人都批不了钱」。问题是**它在 MAOS 这侧没有任何症状**：你以为在读 Nacos，其实读的是几小时前的快照。与「静默降级比不接更坏」是同一类洞，只是这次在 SDK 那侧 | 补法是起一个低频心跳调 `NacosConfigService.server_health()`，把结果并进 `degraded` 并在翻转时落一条日志。本轮没做：派单 §5.1 的降级三态说的是**取值来源**，加探活线程是新增一条后台线程，超出范围。**归下一轮配置面轨** |
| 2026-08-30 | P5 | **「谁改的」只在走控制台 / OpenAPI 时追得到**。实测同一个 dataId 的历史：OpenAPI（带 accessToken）写入 `srcUser='nacos'`，SDK 的 gRPC `publish_config` 写入 `srcUser=''` | 这是 Nacos + SDK 的行为，MAOS 补不了。当前口径是「查到就记、查不到就留空并在 `actor_source` 写明原因」，不编 actor。**但演示时若说成「所有配置变更都能追溯到人」就是说过头** | 无需代码改动，是**口径**要守住：`deploy/nacos.md` §6 与 `nacos-live.md` §1.5 都写了准确说法。做 PPT / 答辩稿的那一轨照抄那两处，别自己简化 |
| 2026-08-30 | P5 | **`MAOS_APPROVERS` 改动没有二次确认，也没有生效前的预演**。Nacos 控制台上一次手滑（比如把名单写成空串）会让所有人当场批不动 —— 空名单在 `_effective_approvers()` 里会回落到构造时的快照，但那份快照在真部署里同样可能是空的 | 审批权限名单是本轨点名的安全面，而现在它的写入侧一道校验都没有。落审计只解决「事后查得到」，不解决「当场就错了」 | 建议在 `NacosConfigSource._apply` 里加一条**只针对 `MAOS_APPROVERS`** 的合理性检查：新名单为空时拒绝采用、沿用旧名单并落一条告警级审计。本轮没做：那是给配置面加策略，派单没列，且「拒绝采用」本身是个需要人类拍板的口径（拒绝 vs 采用并告警） |
| 2026-08-30 | P5 | **`maos/tests/conftest.py` 没有剥 `MAOS_CONFIG_SOURCE` 与 `MAOS_NACOS_*`**。本轨在自己的测试文件里用 autouse fixture 补了（派单 §5.5 要求不动 conftest），但**其余 933 条测试没有这层保护** | 一台 export 了 `MAOS_CONFIG_SOURCE=nacos` 的机器上跑全量，`_finance_threshold()` / `_max_replan()` / `sandbox_timeout()` 会去连 Nacos。连不上时降级 env、结果不变（本轨已验），**但每次调用都会走一遍连接超时** —— 症状是「测试变得很慢」而不是红灯。与 `## task-C2` 第 1 条（Matrix 那组）、`## task-T15` 第 1 条（存储那组）是同一类洞的第三例 | 归 conftest 的正当持有轨：`STORE_ENV_VARS` 旁边再加一组 `CONFIG_ENV_VARS = ("MAOS_CONFIG_SOURCE", "MAOS_NACOS_SERVER", "MAOS_NACOS_NAMESPACE", "MAOS_NACOS_GROUP", "MAOS_NACOS_DATA_ID", "MAOS_NACOS_USERNAME", "MAOS_NACOS_PASSWORD", "MAOS_NACOS_TIMEOUT_MS")`，同款 autouse delenv。**注意别顺手把本轨那四个旋钮也加进去** —— `test_governance.py` 等用例靠 `monkeypatch.setenv` 验它们，删掉不影响结果但会白白扩大维护面（`## task-H6` 末条「下一轮若给 conftest 扩面，这三组不要顺手加进去」是同款告诫） |
| 2026-08-30 | P5 | **生效延迟 ~5s 是 SDK 的轮询节拍，改不动**。`v2/nacos/config/remote/config_grpc_client_proxy.py:195` 是 `asyncio.wait_for(queue.get(), timeout=5)`，服务端的变更通知不进那个队列 | 「灰度 / 动态治理」这条能说到「秒级生效」，**不能说「实时生效」**。区间是 0~5s | 无需处理，记在这里是为了别人不要去查「为什么不是毫秒级」。真要更快只能改 SDK 或自己走 OpenAPI 长轮询，都不值当 |
| 2026-08-30 | P5 | **`deploy/nacos/docker-compose.yml` 没有 `env_file`，也没有对应的 `.env.example`**，与 `## task-omega` 记的 `deploy/.env.example` 撞的是同一条 deny 规则 | 照抄 §3 三条 `export` 就能起，但评委拿不到一份可 `cp` 的样例。需要的键：`NACOS_AUTH_TOKEN` / `NACOS_AUTH_IDENTITY_KEY` / `NACOS_AUTH_IDENTITY_VALUE`（起容器用），`MAOS_CONFIG_SOURCE` / `MAOS_NACOS_SERVER` / `MAOS_NACOS_NAMESPACE` / `MAOS_NACOS_GROUP` / `MAOS_NACOS_DATA_ID` / `MAOS_NACOS_USERNAME` / `MAOS_NACOS_PASSWORD` / `MAOS_NACOS_TIMEOUT_MS`（接 MAOS 用） | 与本文件 `## task-omega` 那条 `deploy/.env.example` 一并处理（人类放开 deny 规则之后）。键名清单在此列全了，届时照抄即可，值一律占位符 |
| 2026-08-30 | P5 | **Nacos 2.4.3 的 admin 初始化接口会在响应里回显口令**（`POST /nacos/v1/auth/users/admin` 返回 `{"username":...,"password":...}`） | 任何把这条命令的输出重定向进文件或贴进 evidence 的做法都会当场违反铁律 6。本轮两份文档都刻意只写「200」不贴正文 | 无需代码改动。若以后有人写 Nacos 的一键起脚本，那个脚本**必须**把这个接口的响应丢掉（`> /dev/null`），不能顺手 `tee` 到日志里 |

## task-T29

成本与 Metrics 挂 `trace_id`。基线 `d98b9d1`。本轮实测：**970 passed / 29 skipped**
（935 + 本轨新增 35），`verify.py` **8/8 PASS**、exit=0。

以下七条都是**在本轨白名单外**发现的，一条都没有当场改（铁律 4）。
前四条共同的后果是一样的：成本数字偏低或归不上账，而屏幕上看不出来 ——
所以每一条都在 `trace.json` 或 `verify.py` 第 8 项里留了显式出口，不是静默缺失。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P5 | **`maos/flows/**` 多处构造 Agent 不传 `store=`**：`scenario_1.py:121`／`scenario_2.py:119`／`scenario_5.py:166`／`scenario_7.py:475,490` 的 `ManagerAgent(model)`、`ReviewerAgent(model)`。`BaseAgent.__init__` 的 store 缺省 None，此时 `record_model_usage` 直接跳过 | **scenario-7 整场一条用量都没记到**，`cost.calls=0`。「真的没调」与「调了没记」在这个数字上分不开 —— 已由 `obs/trace.py::ZERO_CALLS_NOTE` 在 `trace.json` 里写明，但那只是让洞看得见，不是把洞补上 | 归持有 `maos/flows/**` 的那一轨（本轨明令不许动）。改法是构造时一律带 `store=store`，与 `scenario_6.py:285` 已有的写法一致 |
| 2026-08-30 | P5 | **`ManagerAgent.plan()` 结构上拿不到 `trace_id`**：它跑在 `cp.create_plan()` **之前**（`mgr.plan(GOAL)` 是 `create_plan` 的入参），那一刻还没有 plan 行 | 本轮实测 **4 条用量归属不上任何 Run id**（scenario-6 一条、scenario-R5 三条），落空 `trace_id` 后由 `unattributed_usage` 逐条点名。规则要的「Metrics 关联到同一个 Run id」在这几条上没兑现 | 与上一条同轨。改法是让 flows 先生成 `plan_id`／`trace_id` 再规划并放进 `context`（`scenario_6.py` 的 `_kb_prefetch` 路径已经这么做了一半），本轨不许扩 `plan()` 签名 |
| 2026-08-30 | P5 | **`flows/scenario_2.py:98` 的 `FlakyModel` 直接 `ModelResponse(text=...)`，不填 `model=`** | 落库那行 `model` 为空，`verify.py` 第 8 项判据 c（`estimated` 与 `model` 两个独立来源交叉印证）在这 2 条上**印证不了**，只能记 info。方向仍安全（都已标 `estimated=1`），但交叉印证少了一半 | 与上一条同轨。一行的事：`ModelResponse(..., model=f"scripted-{tier}")` |
| 2026-08-30 | P5 | **`model_usage.latency_ms` 是整毫秒**，Scripted 调用一律取整成 0 | 「快到测不出」与「没测」在这一列上分不开 —— 本轮八个场景全是 0。真模型接上后自然有值，但在 Scripted 缺省路径上这一列不承载信息 | 要么换成微秒列（改表结构，只能新增列或新表），要么在成本视图里显式标「本束全 Scripted，latency 不承载信息」。不急，接真网关那一轨顺手定 |
| 2026-08-30 | P7 | **`maos/tests/test_verify_warn.py:155` 写死 `len(checks) == 7`**，与派单 §5.4 要求新增第 8 项直接冲突 | 该文件不在本轨白名单内。本轨已实测：除这一行外全绿（969 passed），warn 基线 `{"authoritative-fact": 1}` 一行不变（第 8 项只出 info 不出 warn） | **已停手问人类**，等授权后改这一行（`== 7` → `== 8`）并把函数名一并改掉。不许为了让它绿而不注册第 8 项 |
| 2026-08-30 | P7 | **`docs/agentteams-mapping.md:21` 的两处行号已过期**：`maos/agents/base.py:101`（`AGENT_POOL`）、`:104`（`@register`），本轨改动后应为 `:140` / `:143` | 该文件是**手写**的，不在 `gen_docs.py` 的 TARGETS 里，`--check` 发现不了 —— 这类漂移没有任何机器守卫 | 归 T21／T22 文档轨。更根本的是：手写文档里嵌行号本身没有守卫，值得让 `gen_docs --check` 覆盖到它 |
| 2026-08-30 | P7 | **「七项 / 7/7」的口径散在四份文档里**：`README.md`（§3 那段实跑输出、共 8 处）、`docs/EXECUTION.md`、`docs/ppt-outline.md`、`docs/architecture.md:153` | 第 8 项落地后这四处全部过期，而它们正是评委会读的那几份。`README.md` §3 那段贴的是逐项 PASS 实跑输出，需要整段重贴 | 归 T21／T22／Y 轨。本轮刻意没动：四份都在别轨的面上，且要等第 8 项注册这件事定下来才谈得上改 |

## task-T30

推荐工具链口径文档（`docs/gateway-rationale.md`）撰写期间发现的，**本轮都不改**（铁律 4）。
本轨白名单只有 `docs/gateway-rationale.md` / `maos/model/client.py` 的一处 /
`maos/tests/test_model_client_hardening.py` / 两份账本尾部，下面每一条都在白名单外。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | 🔴 **`README.md` §8 的「数据口径」与 `docs/agentteams-mapping.md` 的「当前真实状态」互相矛盾**：前者写「**Matrix 真房间未接通**：镜像层已实现、降级路径实测等价，真房间截图待补」，后者写「**真房间已接通**」并有 `evidence/room/` 五图 + `transcript.md` 41 条逐字副本兜底 | 两份都是评委会读的口径文件，而且 §8 那句还**主动往低了说** —— 把已经兑现的证据说成没兑现，等于白扔一个必须项的分。`docs/gateway-rationale.md` 第 1 节按 `agentteams-mapping.md` 写（它是那一节的口径上位法且有证据），并在文末附注标明了这处矛盾 | 归整合轮统一改 `README.md`（派单 §5.4 明示 README 归整合轮）。改法是把 §8 数据口径第三条换成 `agentteams-mapping.md` 末尾那句已经拟好的口径：「镜像层已实现，降级路径实测等价，真房间三条路径实测通过、截图与逐字副本在 `evidence/room/`」 |
| 2026-08-30 | P7 | **`docs/agentteams-mapping.md` 里指向 `hiclaw/matrix_bus.py` 的行号整体过期**，11 处全错，偏移约 +6 到 +137 行。实测对照：`MatrixBusConfig` 写 70 实为 76、`from_env` 写 85 实为 91、`REQUIRED_ENV` 写 55 实为 60、`summarize` 写 132 实为 138、`render_mirror` 写 147 实为 153、`_NioChannel` 写 178 实为 258、`publish` 写 321 实为 458、`_mirror` 写 348 实为 485、`parse_approval_command` 写 420 实为 557、`RoomApprovalBridge` 写 435 实为 572、`handle_message` 写 452 实为 589 | 那张五项映射表是「AgentTeams 事件链」这一维的**主证据表**，每格都写着代码位置让评委去核。照着翻会翻到不相干的行，比不给行号更伤 —— 给了行号就是在邀请人核。同文件里 `maos/agents/base.py:101/104` 与 `maos/runtime/worker.py:34` 三处**实测是对的**，所以漂的只有 `matrix_bus.py` 那一个文件 | 归持有 `docs/agentteams-mapping.md` 的那一轨。**根治不是再刷一遍数字** —— 它已经漂过一次，还会再漂。要么把行号换成符号名（`hiclaw/matrix_bus.py::publish`），要么加一条测试／`gen_docs` 校验把「文档里点的每个行号确实落在那个符号上」钉死。本轮只读该文件，不改 |
| 2026-08-30 | P7 | **`README.md` §9 的文档索引里没有 `docs/gateway-rationale.md` 这一行**（`grep -c gateway-rationale README.md` → 0） | 新文档补的是「推荐工具链未使用需说明理由」那一维的判分条件本身，进不了文档索引就等于评委找不到它 | 归整合轮，与上面第 1 条一起改 README。建议插在 `docs/toolport-contract.md` 那一行之后，来源栏写「人写」 |
| 2026-08-30 | P7 | **本 BACKLOG `2026-08-28` 那条（第 40 行）对自己的描述不够准**：它写「key 会进 repr / pytest 对象打印 / traceback」，但实测**改之前 `repr()` 里也查不到 key** —— 默认的 `object.__repr__` 只打类名和内存地址，属性一个都不打（实测输出 `<maos.model.client.HigressModelClient object at 0x102dab710>`） | 真正的泄漏面是两条：① 公开属性 `api_key` 本身（`vars()` / `__dict__` / 任何遍历属性的序列化都带出值）；② 「将来」—— 谁给这个类加一个 `@dataclass` 或自己的 `__repr__`，key 当场进 repr。所以那条的**结论（对齐 `GatewayModelClient`）是对的，理由写偏了**。本轮已按结论执行并补了 6 条回归（`maos/tests/test_model_client_hardening.py`，10 个用例），其中一条专门钉「repr 必须是显式格式」—— 因为「repr 里没有 key」在没写 `__repr__` 时也恒真，单靠它防线被删掉了也照样绿 | **本轮不改那一行**：它是 2026-08-28 的历史实录行，按仓库惯例不改历史条目（同 `## integrate-round-13` 对 `## task-T17` 的处理）。下次有人正当持有该节时顺手把理由更正为「公开属性 + 将来加 `__repr__` 的风险」 |
| 2026-08-30 | P7 | **`docs/BACKLOG.md` 第 39 行那条 301/302/303 仍然定不了**（同 origin 的 301/302/303 被 urllib 默认 handler 把 POST 静默改写成 GET） | 该条原文写着候选修法「同 origin 也只放行 307/308」会缩小兼容面，**需要拿真 Higress 的行为定**；而本轮编排侧已定**不接 Higress**（派单 §0.2 三条实测理由）。判据来源没有，所以这一轮仍然不动 —— 在没有判据的情况下拍板比留着这条账更糟 | 与 Higress 接入同轮做（Track B）。已在 `docs/gateway-rationale.md` §5⑤ 把「接之前必须先定这一条」写进迁移路径，接的人不会漏掉 |

## integrate-round-14

T27–T30 并轨发现的，**本轮都不改**（铁律 4）。合并态实测：`1069 passed / 39 skipped`
（935 + T27 9 + T28 80 + T29 35 + T30 10），`run.py` exit=0，`verify.py` **8/8 PASS**
exit=0，`gen_docs.py --check` exit=0，`demo_preflight.sh` 5 步全过 exit=0。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-30 | P7 | **有库那档（`EXPECT_TESTS_PG=1098`）本轮没有实测**：本机无 PG，`demo_preflight.sh` 走的是无库档。1098 = 1069 + 29 由算式得出，新判据「SKIPPED 行里不许出现 `test_pg_`」也只在有库时才启用 | 配了 `MAOS_PG_DSN` 的机器上第 1 步是否真绿，本轮给不出实测背书。方向安全（算式与判据都比原来写死的更宽），但没验过就是没验过 | 归下一个有 PG 环境的轨：起一次 pgvector 容器跑 `bash scripts/demo_preflight.sh`，把实测读数补进 `scripts/demo_preflight.sh:45` 的注释 |
| 2026-08-30 | P7 | **`RoomApprovalBridge._effective_approvers()` 有一处行为变化没被测试钉住**：它返回 `current_approvers() or self.config.approvers`，即**进程环境的名单优先于构造时传入的那份**。显式 `MatrixBusConfig.from_env({...})` / `replace(config, approvers=...)` 给的名单，在进程环境也配了 `MAOS_APPROVERS` 时会被盖掉 | 现有三条路都不触发：`room_demo.py:230` 的 replace 有 `not config.approvers` 守卫、测试有 conftest 的 `delenv`、真部署里两者本来同源。T28 的 docstring 已如实写明这一点，属于已知取舍不是缺陷 | 归下一轮配置面轨：补一条测试钉住「环境有名单 + 显式 config 有另一份」时取哪个，把口径从 docstring 升成断言。现在改判据反而会动到已绿的三条路 |
| 2026-08-30 | P7 | **`maos/tools/sandbox.py:46` 的 import 被拆成两段**（`from maos.config import get_config_source` 与 `from maos.tools.port import ToolPort` 之间空了一行），isort/ruff 口径下应合并 | 纯风格。仓库没有 lint 门禁，不影响任何判据 | 谁下次动这个文件时顺手并掉，不值当为它单起一轨 |
| 2026-08-30 | P7 | **派单模板缺一条**：T28 / T29 / T30 三轨都往 `DECISIONS.md` 裸追加表格行、没带 `## task-TNN` 小节标题（三轨的 BACKLOG 侧都带了）。三轨独立犯同一个错，说明不是个人疏忽而是模板没写 | 每次并轨都要人工补标题并把条目从上一节名下移出；漏补一次就永久挂错名，且 Markdown 渲染正常、没有红灯 | 归派单模板轨：`review/DISPATCH-TEMPLATE.md` 的账本一节补一句「往 BACKLOG **和** DECISIONS 追加时都必须先起 `## task-<轨号>` 小节标题 + 表头，不许直接接在上一节的表格后面」，并把「每节恰好一个表头 + 一个分隔行」那个机器判据写进回执格式 |
| 2026-08-30 | P7 | **容器手册与多份文档的 `7/7 PASS` / `935` 读数仍过期**，本轮刻意没刷：`deploy/README.md`（标题 + 4 处）、`docs/matrix-room-runbook.md:483`、`deploy/rocketmq.md:18,120`、`deploy/rocketmq-live.md:389`、`docs/gateway-rationale.md:405`、`docs/EXECUTION.md` 6 处 | 评委若走容器路径或读这几份，看到的仍是 7/7。方向上不致命（实际跑出来是 8/8，比文档写的多一项 PASS），但「照着做数字不符」这个症状本身就是要治的病 | 容器那几处**必须在有 docker 的环境实跑后再刷**，不许照抄本机读数（本轮没跑 docker，按「只认可复现的输出」没替它断言）。`EXECUTION.md` 与 runbook 那几处是叙述面，可随下一轮文档轨一起刷 |

## polardb-hardening（SSL 收尾）

2026-08-31 SSL 收尾轮发现的。**Docker 那条本轮已解决**（经人拍板起 Docker 并 build 镜像），
其余不改（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | **Docker daemon 没起，沙箱容器隔离静默降级**。`test_verify_warn` 的两条断言因此红：warn 基线 `{'authoritative-fact': 1}`，实测 `{'authoritative-fact': 1, 'trace-tree': 2}`。多出的两行正文是「scenario-1 的 2 份 / scenario-2 的 3 份 test_report 是**降级**跑出来的，容器隔离（`--network none` / `--read-only` / `--user 1000:1000`）本次未生效；原因：镜像 `maos-sandbox:latest` 不可用（`Cannot connect to the Docker daemon`）」 | **不是回归**：不带 `MAOS_PG_DSN` 单跑 `test_verify_warn.py` 同样是这 2 条红，与 SSL 改动无关。但它有两层危害——① 全量测试不全绿，掩盖真回归；② `verify.py` 是给评委的答案，降级跑出来的 test_report **看上去和真隔离跑出来的一样**，只在 warn 里留一行。交付前若在无 Docker 的机器上产证据，隔离性这条实际没有被证明 | ✅ **已解决（2026-08-31，经人拍板）**：起 Docker Desktop（Server 29.6.1）→ `docker build -t maos-sandbox -f deploy/sandbox.Dockerfile .` → 重跑 **`1098 passed, 10 skipped`，两条红消失**，证实是环境而非回归。全绿读数已刷进 `deploy/polardb.md`，「跑全量前先起 Docker」也写进了那张表下面。**留作教训的是这条 warn 的价值**：`verify.py` 没有把降级判成失败，而是产出证据 + 留一行 warn —— 判成失败会让无 Docker 的机器根本跑不出证据束，而静默通过则会让隔离性这一维凭空消失。warn 这个中间档正好卡在这两者之间，`test_verify_warn` 把它钉成会红的东西才让它没被忽略 |
| 2026-08-31 | P7 | **`scripts/polardb_smoke.py` 不打印链路是否走 SSL**。这次要证明「开 SSL 没打破任何东西」，只能靠另跑一段 `psycopg` 脚本读 `ssl_in_use`，冒烟脚本自己给不出这个事实 | 五步全绿**不蕴含**「这条连接是加密的」—— `sslmode=prefer` 下五步同样全绿而链路是明文，§3.5 记的正是这个坑。也就是说冒烟报告作为证据，在加密这一维上是哑的 | 给第 1 步的输出加一行 `SSL: on/off`（读 `c.pgconn.ssl_in_use`），不改任何断言、不改退出码语义。**本轮没做**：铁律 4，且它改的是已跑绿的脚本，值得单独一轮 |

## code-review-2026-08-31（scripts/ 面，high 档）

`git diff main...HEAD -- scripts/`（7 文件 / ~2971 新增行 / 5 个全新脚本）的审核结果。
**15 条 confirmed，每条都带实测复现**；另有 3 条候选被证伪后丢弃。
**本轮都不改**（铁律 4），归 T36（verify 面）与 T34（冒烟 / preflight 面）。

主线是同一类问题：**证据可以被伪造 / 被删 / 被误判，而 `verify.py` 照样打印 8/8 PASS。**
`verify.py` 是给评委的答案，它说 8/8 的时候必须真的是 8/8。

### 发现清单（15 条，每条都实测复现过）

| 位置 | 问题 | 复现 | 归属 |
| :-- | :-- | :-- | :-- |
| `scripts/verify.py:1110` | **只有 `trace.json` / `result.json` 过 provenance 校验**，`run.log` / `kb-hits.json` / `kb-dump.json` / `dag-diff.json` 可以手写。铁律 3 的「每个 evidence 文件首行必须是 generated at」在**交付束上没有被强制** | 把 `kb-hits.json` 首行改成任意散文、`run.log` 重写成伪造的 `from deadbeef…` → 仍 `RESULT: 8/8 PASS`, exit 0。测试读起来像全束锚定，实则 `test_verify_receipt.py` 的伪造头测试**恰好只打那两个被检查的文件**，`test_trace_evidence.py` 那条跑在 tmp 新产物上、钉的是生成器不是交付束 | T36 |
| `scripts/verify.py:1096` | **整束删掉不可见**：`load_cases` 只按 `os.listdir` 建集合，`evidence_sha()` 除 `git_sha` 外不读 `INDEX.json` 任何东西 | 把 `evidence/scenario-7`（唯一的失败路径束，带 FAILED plan 与 compensated 业务结局）移走 → `8/8 PASS`, exit 0，而 `INDEX.json` 的 `produced` 里还列着它。表头数字数的是 check 不是 bundle | T36 |
| `scripts/verify.py:373` | `check_business_ref` 只走 DB→JSON 单向，**JSON 里多出来的条目永远不被访问**，文件自己的 resolved/dangling 合计也不重算 | 往 `evidence/scenario-6/business-objects.json` 塞一条伪造 case 并把 `resolved` +1 → `[PASS] business-ref 35/35`。`check_hash_integrity` 有反向那一遍，`check_business_ref` 没有 | T36 |
| `scripts/verify.py:1090` | `resolve_db` 在 `--db` 既不是文件也不是含该场景的目录时**静默回落**到仓库自己的 `evidence/<scenario>/maos.db` | `--db /tmp/does-not-exist.db` → `8/8 PASS`。评委指向解压出来的发布副本却打错路径，拿到的是给在库内那份背书的绿屏，且没有任何警告 | T36 |
| `scripts/verify.py:109` | **provenance 锚定完全自指**：两侧都先剥 `-dirty` 再比；sha 从不与仓库真实 HEAD 比对（`verify.py` 里没有任何 git/subprocess 调用）；`MAOS_EVIDENCE_PINNED_SHA` 原样接受、不校验、且不打 dirty | (a) 把 50 个文件加 `INDEX.json` 全改成 `<sha>-dirty` → `8/8 PASS` 且全输出 `grep -ci dirty` = 0；(b) evidence 自称 `0df1596` 而 HEAD 是 `784aad7`，今天照样 8/8；(c) `MAOS_EVIDENCE_PINNED_SHA=deadbeef-totally-not-a-sha` 会被盖进每个头**和** `INDEX.json`，然后两份伪造互相比对 | T36 |
| `scripts/verify.py:834` | 文件自己的规则「**分母为 0 的项一律不判 PASS**」只在 8 个 check 里实现了 3 个 | 用**未修改的**交付证据即可复现：只放 `scenario-4` → `[PASS] business-outcome 0/0` 计进 `RESULT: 4/4 PASS`；只放 `scenario-7` → `[PASS] authoritative-fact 0/0`。docstring 自己称 0/0 PASS 是「这个核验器能犯的最坏的错」 | T36 |
| `scripts/verify.py:706` | `_test_report_backing` 用**真值判断**测失败，而它审计的生成器用**严格相等** | `verify.py` 是 `if content.get("failed") or content.get("errors") or …`，`make_evidence.py:394` 是 `c.get("failed") == 0 and c.get("errors", 0) == 0`。把 test_report 的 `failed`/`errors` 两个键**删掉**（留 `passed`）→ 三者全 falsy，「report 自己失败了」那支被跳过，`[PASS] business-outcome 1/1`，无 warn。校验器比被校验者更弱 | T36 |
| `scripts/verify.py:1020` | `check_cost_attribution` 判据 (d) —— 文档里写着它是**防止 (a) 空转变绿的把关人** —— 是个**同义反复**，在任何忠实导出的束上都不可能失败 | `named` 读自 trace.json 的 `unattributed_usage`，而 `maos/obs/trace.py:653` 就是 `WHERE trace_id=''`，`store.py:527` 又永远写 `trace_id or ""`（从不 NULL）—— 两者是同一个查询。照 docstring 说它能防的事做（`UPDATE model_usage SET trace_id=''` 全表）→ `[PASS] cost-attribution 19/19`，100% 成本归不上账，只落一行非 warn 的 `info:` | T36 |
| `scripts/verify.py:939` | `check_history_case` 新加的「外部导入知识」豁免**恰好按伪造条目的特征**放行（`source_case_id` 解析不到），KB 投毒判据失去牙齿 | 插一条 `kind='history_case'` / `source_case_id='case-that-never-existed'` → `[PASS] history-case 1/1` + 一行 warn，`8/8 PASS`。这个 diff 之前同一条会打到 `chk.bad("追不到成功收口的真实 case")` 并让 check 7 FAIL。`if chk.total == 0 and seen:` 那道兜底被今天仅有的一条真实本地文档**解除了武装** | T36 |
| `scripts/verify.py:215` | 新的 `INDEX.json` ↔ header sha 交叉校验把所有束绑到同一个 sha，而 `make_evidence.py` **无条件重写 INDEX.json** → **部分重生成会让 verify 完全拒绝启动并指控证据造假** | `make_evidence.py --scenarios 1,2`（模块 docstring 第 5 行自己写的用法）→ `verify.py` 在任何 check 之前死在 `load_cases`：exit 2，`出处对不上的证据不予采信（铁律 3）`。中途中断的跑同理。**在 main 上这个用法是正常的** | T36 |
| `scripts/make_evidence.py:503` | `collect_kb_hits()` 的 `note` 是**写死的**，断言「无 KbRetrieved 事件、无 kb_doc 表、hits 为空数组」，而同一个 dict 的兄弟键是实时算的 —— **交付出去的证据自相矛盾** | `scenario-R5` 与 `scenario-6` 的 `kb-hits.json` 都是 `"hits": [1 条]` + `"has_kb_doc_table": true` 紧挨着那句 note。首行是真命令输出，正文那段散文是假的（铁律 3）。scenario-6 是**在这个分支上新变得矛盾**的：`main` 那版是 0 / false | T36 |
| `scripts/demo_preflight.sh:219` | 脚本**推荐**的方案 B（`git checkout -- evidence/`）把上一次 commit 的 JSON 配上这一次跑出来的库，**必然**让 verify 失败 —— 而脚本还明说「本条已实测」安全 | 复现得 `RESULT: 4/8 PASS`，失败项 `hash-integrity, business-ref, trace-tree, business-outcome`，exit 1。`build_scenario` 是 `rmtree(final); os.replace(tmp, final)`，每次跑都换一个全新 maos.db，所以是**保证**会坏不是可能会坏。「已实测」只对演示第 7 镜的 sqlite3 查询成立，**第 8 镜正是 `verify.py` 期待 8/8，会在镜头前显示 4/8**。`docs/demo-script.md:112/116-121` 重复了同一条建议 | T34 |
| `scripts/demo_preflight.sh:49` | `EXPECT_TESTS_NOPG=1069` 这道门**静默依赖 Docker + 本地 build 的 maos-sandbox 镜像**，而这个依赖在操作者会看的任何地方都没有 —— 与脚本自称的「零出网…评委在没有任何密钥的机器上照样跑得出同一份结果」直接矛盾 | 没 build 镜像 → `_docker_ready()` False → scenario-1/2 的 test_report 变 `sandbox.mode=subprocess` → verify 多 2 行 warn → `test_verify_warn.py` 的 `WARN_BASELINE` 红 → `1067 passed, 2 failed` → preflight 第 1 步死在「存量测试没有全绿」exit 1 → `make_release.sh` 的 `RC=1` →「❌ 有未通过项…不要提交」。**`README.md`、`docs/submission-checklist.md`（写着「□ 1069 passed」）、本脚本三处零个 `docker build` 指令**，它只存在于 `deploy/polardb.md:150` 与 `docs/phases/phase-2.md`。**本仓库 08-31 已经踩过这个事故一次（`docs/BACKLOG.md ## polardb-hardening`），当时修的是机器不是脚本** | T34 |
| `scripts/make_release.sh:216` | 发布门禁的判决**两个方向都错** | **假阳性**：`export POSTGRES_PASSWORD=maos-local-dev`（`deploy/docker-compose.yml:178` 的默认值、`scripts/polardb_smoke.py:73` 里写死的同一个串）→ 名字命中 `*PASSWORD*`、长度 14 ≥ 8、`grep -rqF` 在两个已跟踪文件里命中 → `RC=1` →「❌ 不要提交」，指控 `POSTGRES_PASSWORD` 泄漏，**无白名单无 override**。**假阴性**：`verify.py` 只要没有 FAIL 就返回 0，而 SKIP 不动分子分母 → `RESULT: 4/4 PASS, 4 SKIP` 也给 `VERIFY_RC=0` → `:267` 打印「✅ 打包 + 解压验证 + 密钥自查 全过」，与抬头承诺的「verify.py 到 8/8 PASS」矛盾（`demo_preflight.sh:200` 会 grep 那个字符串，这里不会）。`--no-verify` 同样打印「解压验证…全过」。另外 `EVID_SHA` 在 `:187` 算出、`:189` 打印，**从不与 `SHA_FULL` 比较** —— 它自己 `:184` 的注释承诺的断言不存在 | T36 |
| `scripts/polardb_smoke.py:179` | 🔴 **铁律 6 违规**：`--local` 路径把口令插进 URI 而不做 percent-encoding，libpq 与 `urlsplit` 对 userinfo 边界的判断不一致 → 连错主机**且**脱敏登记错密钥 → **口令明文打印** | `POSTGRES_PASSWORD='p@ss1234' … --local` 拼出 `postgresql://maos:p@ss1234@127.0.0.1:5432/maos`：libpq 切**第一个** `@`、去连 `ss1234@127.0.0.1`；`urlsplit` 切**最后一个** —— `_remember_secrets` 登记的是 libpq 从没用过的 host。换成 `pw/rd`：`urlsplit` 返回 username/password 全 None、hostname=`maos`，于是 `_redact('… password "pw/rd"')` **原样返回口令**，而 `maos` 进了无条件子串表、把 `maos_smoke_fts` 打成 `<redacted>_smoke_fts` —— 正是 `:82-85` 那个 `\b` 词边界要防的误伤 | T34 |

### C 类 —— 已确认但被 15 条上限截掉（下一轮再捞，多数是便宜的修法）

- `scripts/matrix_probe.py`：`report.ok("③b")` 在打印 `两者相等？False` 之后**无条件**触发；`Report` 没有 fail 路径，**被证伪的判据与从未测过的判据打印得一模一样**（`✗ 本条未验证`）；②b 在 0 条消息到达时就记「过滤器生效」（包括 ②a 刚断定房间是空的、以及 sync 抛 SyncError 时）；②b 的 sync 抛异常时 ②c 从摘要里整个消失；`homeserver` / `room_id` 未脱敏打印、`resp.message` 在 `:311`/`:340` 未打码（**铁律 6** —— `evidence/room/*` 是人工手动打的码）
- `scripts/verify.py:236`：「先跑 `python3 -m maos.kb.experiment`」这句提示**忽略 `--evidence`**，会写进仓库自己的 `evidence/` —— 照做既修不好用户的束，也弄脏了仓库。孤儿回执查询的 `GROUP BY` 漏了 `tenant_id` 却按它 join，**静默把两个租户的 case 并成一条 warn**

## task-T34

整合轮 15（PG / PolarDB 上生产收尾）发现的。基线 `784aad7`。
本轨白名单外、或需下一轮处理的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | 🔴 **`zhcfg` 的配套 GIN 索引没有任何建成/被用上的实测记录**。`polardb-live.md` §1.4 只写了「配套的 GIN 索引也由普通账号建」——这是**可行性陈述**，不是实测；`maos/store/pg_schema.sql:55` 那条 `CREATE INDEX idx_kb_doc_pg_fts_zh` 是**注释掉的模板**，真建的只有 `idx_kb_doc_pg_fts_simple` | 症状是**不报错、只是慢**：`pg_schema.sql:44` 自己写着「索引只对建索引时那个配置有效，把 `MAOS_PG_FTS_CONFIG` 换成中文配置之后这条索引就用不上了，退化成顺序扫描」。§1.4 那张召回表在 24 条语料上跑，顺序扫描一样出正确结果，所以**召回质量的结论不受影响，但「索引生效」这一维从来没被证明过** | 归下一个能连上云库的轨：`\d kb_doc_pg` 看 `idx_kb_doc_pg_fts_zh` 在不在，不在就照模板建；然后 `EXPLAIN (ANALYZE)` 一条中文查询，确认走的是 `Bitmap Index Scan` 而不是 `Seq Scan`。**本轨没做**：云库被白名单挡住，全程没连上 |
| 2026-08-31 | P7 | **派单 §5.1 的前提已失真**：称 zhparser 三步「装都没装、三件事别混说」，而 `polardb-live.md` §1.4 显示 2026-08-30 三步已全部做完并有完整实录（`zhparser 2.2` 装成、`zhcfg` 建成、24 条真语料召回对比表） | 照派单重做等于伪造一次「首次装成」。派单 §0.3 的范围裁剪表列了 5 条已闭环项，唯独漏了这条 —— 它引的 `BACKLOG:1131` 与 §1.4 的实录**同为 08-30**，编排时漏看了同日闭环 | 归派单模板／编排侧：派单里写「某项尚未做」时，应对着目标文档的实录再核一遍，而不是只读 BACKLOG 条目。全局约定已有「派单放久了会过期」这一条，本例说明**同日写成的派单也会过期** |
| 2026-08-31 | P7 | **BACKLOG 里三条明确标着「T34」的条目，派单一条都没派**：`demo_preflight.sh:219`（方案 B 必然让 verify 变 4/8）、`demo_preflight.sh:49`（1069 那道门静默依赖 Docker + `maos-sandbox` 镜像）、`polardb_smoke.py:179`（🔴 铁律 6 违规：`--local` 口令未 percent-encode，可致口令明文打印） | 三条**都落在本轨白名单文件内**，却不在派单 §5 的任务定义里。账本与派单对同一轨的认定不一致，漏的那条还是铁律 6 违规 | 归编排侧：派单成文前应 grep 一遍 BACKLOG 里指名本轨号的条目。**本轨没改这三条**（铁律 4：不做手册范围外的改动），已当面报告人类由其决定是否另派 |
| 2026-08-31 | P7 | **`git restore evidence/`（= `demo_preflight.sh:219` 推荐的方案 B）让 verify 从 8/8 掉到 4/8，本轨现场复现**：`RESULT: 4/8 PASS`，失败项 `hash-integrity, business-ref, trace-tree, business-outcome`，exit=1 | 证实 `## code-review-2026-08-31` 那条 T34 条目属实，且给出了此前缺的完整读数。机理：`build_scenario` 每次跑都换一个全新 `maos.db`，而 `.db` 是 gitignore 的、`git restore` 只还原被跟踪的 JSON —— 于是旧 JSON 配新库，**一个 `git status` 完全看不见的不一致** | 归修 `demo_preflight.sh` 的轨。**对本轨交付无影响**：commit 只含 4 个白名单文件，`.db` 不进 git 不会传播；派单 §0.2 本就要求 verify 走 `make_evidence.py --out <tmp>` |
| 2026-08-31 | P7 | **白名单失效的真实成因通常不是「忘了配」，而是出口 IP 变了**。本轨开场即撞上：编排侧 08-31 同日在另一 worktree 连得上，本会话全程 TCP 静默超时 | 白名单按出口 IP 放行，家用宽带／移动网络的出口 IP 会变 —— **上一轮能连上不代表这一轮能连上**。派单 §0 把「实例当前状态」写成既定事实（含「已实测 1098」），据此写的开场自检期望值在换网络后就对不上，容易被当成回归 | 已在 `polardb-live.md` §3.8 记成运维事实，并给 `polardb_smoke.py` 加了自动判据（DNS 通 + TCP 静默超时 → 点名白名单并打印本机出口 IP）。派单模板可考虑把「连库前先跑一次连通性探测」写进开场自检 |
| 2026-08-31 | P7 | **`scripts/polardb_smoke.py` 的诊断分支没有测试覆盖**。新增的 `_ssl_state()` / `_diagnose_unreachable()` / `_egress_ip()` 三个函数，验证方式是本轨手工实跑（SSL 行在无 SSL 容器上显示 `off`、诊断结论在真超时上命中） | 该脚本整体本来就没有 pytest 覆盖（它刻意不 import maos、靠人跑）。新增分支同样只有手工证据，回归风险在于以后有人改动 `Reporter` 时不会有红灯 | 归下一轮：若要覆盖，`_ssl_state` 可用 fake conn 对象测三条路径（psycopg3 属性、psycopg2 回落、两者都无 → `unknown`），`_diagnose_unreachable` 可 monkeypatch `socket` 测四条判据分支。**本轨没加**：`maos/tests/**` 归 T33 持有，不许碰 |
- `scripts/make_release.sh`：detached HEAD 下 `git rev-parse --abbrev-ref HEAD` 返回 `HEAD`，`git clone --branch HEAD` 会失败；多行 env 值会打断 `read -r` 哨兵循环；`VERIFY_DIR` 不在 EXIT trap 里；`:158` 的注释断言解压目录没有 `.git`，与 `:140` 的门禁正好相反
- `scripts/demo_preflight.sh`：`if ! cmd` 里的 `"exit=$?"` 在三处**永远**打印 `实际：exit=0 / 期望：exit=0`；PG 饿死守卫的 `^SKIPPED` 锚点在设了 `FORCE_COLOR`/`PY_COLORS` 时（Claude Code 里就是）**匹配不到任何东西，静默失效**
- `scripts/gen_docs.py`：写死的 `" 两处。"` 紧挨着一个算出来的模块列表（违反文件自己「数量不写死」的规矩，且 `--check` 抓不到）；`collect_tools` 按 `value.name` 去重且无自洽断言；写死的 `"refund" if ".refund." in cls.__module__`；写死的 `test_skills.py:76` 锚点
- `scripts/polardb_smoke.py:151` 回落 psycopg2，而 `pg_store._driver()` **只认 psycopg v3** —— 验收工具可能在一台每次 `PgStorePort` 调用都抛 `PgBackendUnavailable` 的机器上报 5/5 全绿

## task-T31

交付面口径与行号根治（整合轮 15）执行期间发现的，**本轮都不改**（铁律 4）。
基线开跑时是 `784aad7`，中途被外部 `git reset --hard` 换成 `dd3edff`（详见
`docs/DECISIONS.md ## task-T31` 第 6 行），下面的行号与读数都以 `dd3edff` 为准。

本轨白名单是 `README.md` / `docs/EXECUTION.md` / `docs/agentteams-mapping.md` /
`docs/matrix-room-runbook.md` / `docs/gateway-rationale.md` / `docs/ppt-outline.md` /
`deploy/README.md` / `deploy/rocketmq*.md` / `review/DISPATCH-TEMPLATE.md` / 两份账本，
下面每一条要么在白名单外，要么在白名单内但不在派单范围内。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | **`docs/submission-checklist.md` 仍有 14 处 `7/7`**，其中 `:246` 是判分对照表的判据格（`verify.py 7/7`）、`:55` 与 `:377` 是「PPT 与视频里出现的每一个数字都要对得上」那两条自查的锚。`:42`「从零跑到 `verify.py` 7/7」是给评委的复现指引 | 自查单是**用来核对其它材料的那把尺**，尺本身过期，照它核 PPT 会把已经刷成 8/8 的页判成错。且 `:377` 那条自查的对象正是这类数字，它自己不对是最坏的一种 | 白名单外（本轨只读）。归下一轮持有该文件的轨：判分表与复现指引刷成 8/8；带出处的历史实测行（如 `:257` 的 kb-hit 4/4→7/7）按仓库惯例保留不动 |
| 2026-08-31 | P7 | **`docs/ops/ORCHESTRATION.md` 有 10 处 `7/7`** | 那是编排侧自己的作业文件，派单模板与验收命令都从它抄；不刷会让下一轮派单继续写 `7/7` 当期望值，子会话开场自检第一条就对不上，被当成回归报假警 | 白名单外。归编排侧自己，优先级比对外材料低，但**在下一次发派单之前**要刷，否则复发 |
| 2026-08-31 | P7 | **`docs/architecture.md:153` 的 mermaid 节点仍写「`scripts/verify.py`<br/>七项重放校验」** | 架构图是 README §2 直接链过去的第一张图，评委大概率会看。一个字的错，但位置显眼 | 白名单外。一处一词：「七项」→「八项」 |
| 2026-08-31 | P7 | **`deploy/docker-compose.yml:4` 与 `:153` 两处注释写 `RESULT: 7/7 PASS`** | `:4` 就是「评委拿到仓库后的第一条命令」那段抬头注释，与同目录 `deploy/README.md`（本轮已刷成 8/8）当场矛盾 | **归 T34**（`deploy/docker-compose.yml` 是那一轨的面，本轨明令只读）。容器内实跑读数本轮已拿到：`RESULT: 8/8 PASS`，可直接引用不必重跑 |
| 2026-08-31 | P7 | **`docs/clone-smoke-report.md` 标题就是「从零跑到 verify 7/7」**，全文 40 处 `7/7` | 全文是**历史执行记录**（文件自己在第 2 行就有时效声明），正文那 39 处按仓库惯例不该改；但**标题**是当前指引的门面，且 README §9 现在把这份列进了文档索引 | 白名单外。建议只改标题一处，正文历史行原样保留 |
| 2026-08-31 | P7 | **`docs/EXECUTION.md:778` 末句语义仍是反的**（即 `## task-T22` 记的那条，本轮复核仍成立）：「会如实印出「11 个 / 10 个 / 1 个」…**引用时别把 10 直接挂在 `AGENT_POOL` 后面**」—— 三个数已回填成 11/10/1 之后，**10 恰恰就是 `AGENT_POOL` 的数**，该提醒的对象变成了 11 | 这句话现在把读者往反方向带：照它做反而不敢写「10 个注册进 `AGENT_POOL`」，而那正是同段上文已经写对的说法 | **本轨持有 `docs/EXECUTION.md`，但这一处不在派单 §5 的四项任务里，按铁律 4 未改。** 一处一词：末句的 `10` → `11`。下一个正当持有该文件的轨顺手做 |
| 2026-08-31 | P7 | **`deploy/rocketmq.md:156-157` 与 `deploy/rocketmq-live.md:388` 的测试条数实录已过期**：写着「全量（无 broker）**944 passed, 37 skipped**」「全量（有 broker）**951 passed, 30 skipped**」，本机现测是 `1069 passed, 39 skipped` | 这两行是 T27 在「装了 `rocketmq-python-client` + 起了 broker」那台环境上量的，**本轨复现不了那个环境**（本机没装客户端），照实测值改会把「有 broker」那档也一并改错 | 本轨持有这两份文件但**刻意没改**——只认可复现的输出（铁律 3）。归下一个能起 RocketMQ broker 的轨：两档一起重量一次再刷 |
| 2026-08-31 | P7 | **`README.md:193` 的「第六遍冒烟读数，`860 passed` 基线」与同文件 `:183` 的「`1069 passed`」不同源** | 同一份 README 里两个测试条数差 209，读的人分不清哪个是当前值。`860` 那个是有出处的历史冒烟读数（`docs/clone-smoke-report.md`），不算错，但**紧挨着 7 秒那个耗时**放着，容易被读成「现在跑一遍是 860」 | 本轨持有 README，但派单 §5 只给了 §8 / §9 / `7/7` 三件事，测试条数轴不在范围内，按铁律 4 未改。归下一轮：要么补一句「860 是第六遍冒烟时的基线」，要么连耗时一起重新掐表 |
| 2026-08-31 | P7 | **`docs/ppt-outline.md` 里 `935 passed` / `802 passed` 等测试条数散落多处**（`:520`、`:724` 等，与 `## task-T27` 第 2 条同源） | PPT 是对外那一份，条数对不上就是「照着做数字不符」 | 本轨持有该文件但派单只给了「读数一致性（`7/7` 那一轴）」，测试条数轴未改。与上一条合并处理 |
| 2026-08-31 | P7 | **手写文档里的 `file:line` 引用没有任何机器守卫**（`## task-T30` 第 2 条原话，本轮再次踩到）：`docs/agentteams-mapping.md` 16 处行号有 15 处已漂，本轮改成 `file::symbol` 治住了这一份，但 `docs/ppt-outline.md`、`docs/submission-checklist.md`、`docs/BACKLOG.md` 里同类交叉引用仍是行号 | 符号名不随插行漂移，但**没有守卫钉着「文档里点的符号确实存在」** —— 符号被重命名时同样会静默失效，只是比行号慢得多 | 根治要让 `scripts/gen_docs.py --check` 覆盖手写文档的代码引用（派单 §5.5 的 B 方案）。那是 **T33 的面**，本轨按派单要求没动。做的时候按 `::symbol` 校验比按行号校验简单得多 |

## task-T32

整合轮 15 · 成本归因补全 + 总线接线轮发现的。三条都在本轨白名单外，一律不改（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | **`scripts/verify.py` 第 8 项里关于「空 `model` 列」的注释与 info 文案已过期**。本轮补上 `flows/scenario_2.py::FlakyModel` 的 `model=` 之后，那个 `blind` 计数恒为 0，末尾那行「2 条用量的 model 列为空 …… 出处 `flows/scenario_2.py` 的 FlakyModel 直接构造 ModelResponse 不填 model」再也不会打印，判据 c 中间那段注释举的例子也不复存在 | **不影响判定**：三态判据本身是对的，空 `model` 仍是结构上可能出现的第三种取值（任何自定义 `ModelResponse` 都可能不填），该分支该留。过期的只是它拿来举例的那个出处 —— 读代码的人会照着去 `scenario_2.py` 找，而那里已经填上了 | 归 T34（`scripts/**` 持有者）。把注释与 info 里的具体出处改成不指名的说法即可，判据逻辑一行不动。优先级低：读错的代价是白跑一趟，不是判错 |
| 2026-08-31 | P7 | **生成文档 `docs/agent-identity.md` 把源码行号写死**（`maos/agents/manager.py:33` 等），于是**任何在被记录的类声明之上的增删行**都会让 `test_generated_docs.py` 的 2 条断言变红，哪怕改动与该文档的内容毫无关系。本轮加一行 import 就撞上了 | 把「文档是否最新」耦合成了「有没有人在文件上半部分动过行」。红的信息量低（它不指示任何真实不一致），而修法要跨到 `scripts/**` 与 `docs/` 两个别轨的面 —— 于是每一轨都被迫在「绕开行号」和「越界重跑生成器」之间二选一。本轮选了前者（收回单行 import，见 DECISIONS `## task-T32`），代价是留下一处「为了不动行号而不能换行」的隐性约束 | 归 T34 或整合轮。可选做法：`gen_docs.py` 改成只记声明位置的**文件**不记行号，或按符号名生成锚点。不建议现在动 —— 行号对人读代码确实有用，取舍要一起看几轨的实际摩擦再定 |
| 2026-08-31 | P7 | **并行轨的 worktree 会被外部 `git reset --hard` 连未提交的工作一起清掉，且没有任何提示**。本轮九个文件跑绿并 `git add` 之后提交，得到的是 `nothing to commit, working tree clean`；reflog 显示七个检出被统一移到新基线 `dd3edff`。工作区改动与索引一起蒸发，只有 reflog 能看出发生过什么 | 重新钉基线本身是对的（各轨都该跟上主干），但它对**正在干活、尚未提交**的轨是静默破坏性的：改动没有进 stash、没有进 commit，只能靠执行方自己有没有留底重做。本轮因为改动内容都还在会话上下文里，重做代价约十分钟；若发生在更大的一轮上，丢的就是几小时 | 归整合轮/派单编排侧。可选做法：重新钉基线前先扫一遍各 worktree 的 `git status`，非空的轨先落一个 WIP commit 再动；或改用 `git rebase` 让未提交改动自己挡住操作（有未提交改动时 rebase 会拒绝，而 `reset --hard` 不会）。这条不是本轨能改的东西，记在这里是为了让下一次重新钉基线的人看见 |

## task-T35

配置面健壮性与审批安全（整合轮 15）。基线 `784aad7`。本轮实测：
**1069 passed / 39 skipped**（与基线逐条相同），`run.py` exit=0。

以下七条都在**本轨白名单外**或**需要人类动作**，一条都没有当场改（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | 🔴 **`deploy/nacos/.env.example` 建不出来** —— Write 直接被拦：`File is covered by a Read deny rule in your permission settings`。与 `## task-omega` 的 `deploy/.env.example`、`## task-T28` 末条撞的是同一条 deny 规则，这已经是**第五次记账、第五次写不进去** | `deploy/nacos/docker-compose.yml` 的 `env_file` 声明这一半已经加好并实证过（scratchpad 里放一份 `.env` 进去，值真的落进容器环境），但评委仍然拿不到一份可 `cp` 的样例。`.gitignore:18` 的 `!.env.example` 已经放行了这个路径（`git check-ignore` 实测不被忽略），所以**唯一的拦路石就是 deny 规则** | **卡在人类放开 deny 规则**，不是代码问题。放开后照抄下面这份即可（值一律占位符，禁止填真值）：`NACOS_AUTH_TOKEN` / `NACOS_AUTH_IDENTITY_KEY` / `NACOS_AUTH_IDENTITY_VALUE` 三个是**容器侧**、也是唯一该进 `deploy/nacos/.env` 的；`MAOS_CONFIG_SOURCE` / `MAOS_NACOS_SERVER` / `MAOS_NACOS_NAMESPACE` / `MAOS_NACOS_GROUP` / `MAOS_NACOS_DATA_ID` / `MAOS_NACOS_USERNAME` / `MAOS_NACOS_PASSWORD` / `MAOS_NACOS_TIMEOUT_MS` / `MAOS_NACOS_HEALTH_INTERVAL_S` 是**宿主机侧**，走 shell export，**不许进 env_file**（会把口令白白注进 Nacos 容器，多一处 `docker inspect` 读得到的面，铁律 6） |
| 2026-08-31 | P7 | **`MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS` 没进 `GOVERNED_KEYS`**（人类 2026-08-31 拍板：只接读取点）。`maos/tests/test_config_source.py:467` 的 `test_governed_keys_are_exactly_the_four_this_track_owns` 钉着「就是那四个」，而该文件同轮归 T33 | 那两个旋钮现在是**「能治理，变更不落审计」**：`NacosConfigSource._resolve` 读快照不看 `GOVERNED_KEYS`，所以 Nacos 上改了立刻生效（本轮实测 `explain()` 返回 `nacos`）；但推送到达时不会为它们落 `ConfigChanged`。四个安全/成本旋钮的审计不受影响 | 归**持有 `maos/tests/**` 的那一轨**。改动是两处共三行：`maos/config/source.py` 的 `GOVERNED_KEYS` 加两项、那条测试的元组与两条 `not in` 断言一起改。改完 `maos/config/__init__.py` 抬头那张六行表的「进 GOVERNED_KEYS」列也要跟着刷。**已处理（2026-09-11，task-t131）**：两个 key 已进 `GOVERNED_KEYS`（连同 `MAOS_KB_ADVICE` / `MAOS_FORCE_SCRIPTED` 共八个），那条按数目写的测试改名成 `test_governed_keys_are_the_eight_whose_changes_must_land_in_the_audit`，读取点表格也刷了。实证见 `test_a_push_now_audits_the_four_knobs_t131_added` |
| 2026-08-31 | P7 | **`maos/config/source.py:93-95` 的注释口径已过期**：它写着 kb 那两个旋钮「这一轮不在此列：`maos/kb/**` 归 T24 / T25，同轮并行改同一个文件必冲突」，而 T35 已经把两个读取点接上了，不在此列的理由也换成了上一条那个测试断言 | 只是注释，无行为影响。但它正是下一个人来接 `GOVERNED_KEYS` 时会读的那段字，读到一个过期的理由会去查一个已经不存在的冲突 | `maos/config/source.py` **不在 T35 白名单**（§4 只列了 `nacos_source.py` / `__init__.py` / `audit.py`），所以没动。与上一条一并做，一次改完。**已处理（2026-09-11，task-t131）**：整块注释已按补齐后的现况重写（八个 key 的来历、为什么拖了三轮、以及「加进来只动审计面不动取值」这条判据） |
| 2026-08-31 | P7 | 🔴 **探活心跳（本轮新增）在真 SDK + 真 Nacos 上一次都没跑过**。本机没装 `nacos-sdk-python`（63 个包 / 135MB，不在 `pyproject.toml`，装依赖属必须问人类的四类），也没有 Nacos 容器在跑 | `server_health()` 那一支是**用桩验的**：SDK 3.2.0 到底有没有这个方法、是同步还是协程、返回什么，都没验过。探不到时会退到就绪端点兜底（那一支拿真 HTTP server 起停验过三态），所以最坏情况是「实际走的是兜底那条」而不是心跳失效 —— **但这句话本身也没在真 SDK 上验过** | 下一个手上有 Nacos 容器 + SDK venv 的会话，把 `deploy/nacos-live.md` §1.6 照原样重跑一次即可闭合。判据只有一条：`docker stop maos-nacos` 之后 30s 内 `degraded` 翻成 `True`、日志落一行 |
| 2026-08-31 | P7 | **构造时就连不上那一档没有自愈**：心跳只在 `_connect()` 成功之后才起（`_service` / `_loop` 都是 None 时探什么都没意义），且 `_connect()` 失败路径会调 `close()`，而 `close()` 置 `_health_stop` —— 于是这条源**此后永久没有心跳** | 症状是「MAOS 起得比 Nacos 早」这个真实顺序下，Nacos 后来起来了 MAOS 也不会自动接上，得重启 MAOS。本轮补的洞是「接通之后掉线」，这一档是它的镜像 | 需要的是**重连**而不是探活（要重新 `create_config_service` + `add_listener`），比本轮的心跳大一圈，且要处理「重连成功后要不要把快照差异当成一次配置变更」这个语义问题。**归下一轮配置面轨**，别顺手塞进心跳里 |
| 2026-08-31 | P7 | **`maos/tests/conftest.py` 仍没剥 `MAOS_CONFIG_SOURCE` 与 `MAOS_NACOS_*`**（`## task-T28` 第 4 条记过，本轮复核 `conftest.py:65` 仍只有 `STORE_ENV_VARS`），而本轮**又新增了一个 `MAOS_NACOS_HEALTH_INTERVAL_S`**，这个面比 T28 记账时又大了一格 | 一台 export 了 `MAOS_CONFIG_SOURCE=nacos` 的机器上跑全量，症状仍是「测试变得很慢」而不是红灯；新增的那个变量还会让降级后的实例**多起一条线程**（缺省 30s 一次，跑完一轮全量约 40 次探活） | 归 conftest 的正当持有轨。照 `## task-T28` 第 4 条给的 `CONFIG_ENV_VARS` 清单加，**记得把 `MAOS_NACOS_HEALTH_INTERVAL_S` 一并加上**；同样别顺手把四个治理旋钮加进去（`test_governance.py` 靠 `monkeypatch.setenv` 验它们） |
| 2026-08-31 | P7 | **写入侧闸门只管 `MAOS_APPROVERS` 一个键**（派单 §5.2 就是这么划的）。另外五个旋钮的写入侧仍然零校验 | 影响不对称，所以本轮没扩：那四个（replan / 阈值 / 沙箱超时 / kb 权重）的**读取侧本来就有回落 + 告警**（`_max_replan` 非法值回默认、`_finance_threshold` 解析不出回默认且方向收严、`load_weights` 读不懂回默认），配错的最坏结果是「治理没生效但喊出来了」。`MAOS_APPROVERS` 不同 —— 它配错的结果是**所有人当场批不动**，而且回落目标（构造时快照）在真部署里同样可能是空的 | 无需处理，记在这里是为了别人不要把「只校验了一个键」当成漏做。真要扩，先想清楚每个键「配错的最坏结果是什么」，别为了对称而对称 |

## integrate-round-15（四轨并轨收口）

2026-08-31 合并 T31 / T32 / T34 / T35 四轨时发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P7 | **T34 新加的白名单判据方向写反了**。它的判据是「DNS 解析成功 + TCP **超时** → 大概率白名单没放行」，但本机出口 IP 漂移后实测的真实症状是「DNS 成功 + TCP **握手成功** → 连接随即被断，`OperationalError`、**无 SQLSTATE**」，于是脚本打印的是「网络通，问题在 PG 鉴权层（账号 / 库名 / SSL 策略）」 | **在真正的白名单场景下把人指向错误方向** —— 会去查账号口令和 SSL 策略，而实际只需在控制台加一条白名单。这比没有判据更糟：`## polardb-live` 里那条老结论（「白名单不放行的症状是 TCP 静默超时」）应该也只在**某些链路**成立，公网地址前有 SLB 时 TCP 是能握上的 | 判据改成三分支：TCP 超时 → 白名单（链路 A）；**TCP 握手成功但 PG 层无 SQLSTATE 地断开 → 同样优先怀疑白名单**（链路 B，实测形态）；有 SQLSTATE → 才是真的鉴权层。归下一轮 PolarDB 轨。改之前先把两种链路各复现一次，别照抄本条 |
| 2026-08-31 | P7 | **本机出口 IP 会漂**，白名单是一次性配置但需要反复维护。T34 收尾时报的是一个 IP，几小时后并轨复验时已换成另一个 | 有库档全量（1098）与云库冒烟（5/5）**随时可能因为 IP 漂移而跑不出来**，且症状看起来像回归。交付前如果撞上，容易误判成代码问题 | 两条路：① 白名单放宽到运营商网段（安全性下降，需人拍板）；② 在 `polardb_smoke.py` 的诊断里直接打印当前出口 IP（`dig +short myip.opendns.com @resolver1.opendns.com`，**不泄漏目标 host**），让人一眼看出要加哪个。②成本低且无风险，推荐先做 |

## task-T37

保险理赔域纵向切片（新域轮）。基线 `926aa7b`。以下八条都在**本轨白名单外**、或**属于
手册没覆盖而不该当场改**的面，一条都没有顺手修（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P8 | 🔴 **第七道闸 `_gate_gateway` 的码表硬绑在 `maos/tools/gateway_codes.py`（支付宝那张）上**，而它的触发判据是纯数据形状：任一 artifact 的 `content["receipt"]` 是带 `code` 的 dict 就进闸。于是**第二个「有外部回执」的域接不进去** —— X12 的 CARC `96` 送进去只会得到一条「未知错误码」的 blocker | 理赔域只能把回执挂在 `content["payer_receipt"]` 上避开这道闸（`maos/agents/claim/_base.py::RECEIPT_FIELD`，有 `test_claim_scenario.py` 两条回归钉着）。代价是**本域拿不到第七道闸那套四象限处置**，等价能力由 `claim_codes.recourse_of()` 在域内自己实现了一份。`docs/domain-portability.md` §2.3 说第六、第七两道闸「换第三个域一行都不用改」——**第七道闸这半句在理赔域上不成立**，那份文档该补一句 | 归**持有 `maos/runtime/gate.py` 的那一轨**。真要做，方向是把「码 -> disposition」从闸里提出来变成可注册的判据表（按 `receipt["code_system"]` 之类的字段分派），而不是给闸加一个 if。**本轮不动内核**是派单明令 |
| 2026-08-31 | P8 | **`scripts/gen_docs.py` 的 `DOMAIN_NAMES`（:161）只有 `software` / `refund` 两项**，理赔域的四个 Agent 与六个 skill 在生成物里印成裸 key `claim`（「分域：软件交付域 6 个；claim 4 个；制造售后退款域 5 个」） | 只是显示，无行为影响 —— 生成器本来就设计成「标签查不到就直接印字段名，不静默丢弃」，所以这不是 bug 而是**没配标签**。但评委读 `docs/agent-identity.md` 时会看到中英夹杂 | `scripts/**` 不在本轨白名单，且同期 T38 / T39 也在新增域，三轨同改这一个 dict 必冲突。**归整合轮**，一次把三个域的中文名一起加进去 |
| 2026-08-31 | P8 | **场景 8 不在 `maos/main.py::DEFAULT_SCENARIOS`（仍是 1..7）** | `python3 run.py` 跑不到理赔域；本轮它的唯一调用方是 `maos/tests/test_claim_scenario.py` | 派单 §8 明令不改（三轨都新增 flow，谁改谁冲突）。**归整合轮**，连同 `--scenario` 的 argparse choices、`scripts/make_evidence.py` 的场景列表、证据束数量、README 里写死的「场景 1-7」一起改 |
| 2026-08-31 | P8 | 🔴 **房间审批桥只写任务级决定，不写域级审批记录**。`RoomApprovalBridge.handle_message` 走的是 `HumanApprovalQueue.decide()`，它只推 Task 状态；而 `claim.pay` / `payment.execute` 都要求库里先有一行 `approved` 的域审批记录（`claim_approval` / `approval_record`），那一行在两个域里都是由**场景**在 `hq.decide()` 旁边显式落的 | 于是**纯房间驱动的业务流程走不完**：房间里 `/approve` 生效、任务 DONE，下一个任务却因「没有 approved 的审批记录，不许发起赔付」而失败。本轮 §5.4 的真房间实跑撞上过这一条（第一次运行的 Plan 收在 FAILED），补一行 `record_approval` 后 Plan 正常推进 | 归**持有 `hiclaw/**` 的那一轨**。两条路：① 桥上挂一个可注册的「决定落地回调」，由各域自己登记怎么把人的决定写进本域的表；② 或者把域审批记录改成从 `event_log` 的 `human_approve` 迁移事件里现读。①更正、②更省。**别在桥里 import 业务域** —— 那会让 `hiclaw` 认识两个域 |
| 2026-08-31 | P8 | **`hiclaw/matrix_bus.py::RoomApprovalBridge._say` 在 429 限流下把一次成功的回话记成失败**。`_NioChannel.send` 走 `_await(...)`，默认超时 10s；Synapse 限流时房间发送会排到 10s 之后，于是 `_say` 捕获超时打出「房间回话失败（），判定已生效」——**而那条回执随后确实送进了房间**（本轮实测：三条 warning，房间里三条回执一条不少） | 日志里出现「回话失败」而房间里明明有回执，排查时会往通道坏了的方向找。异常对象的 `str()` 还是空的（`concurrent.futures.TimeoutError`），warning 里那对括号是空的，更难认 | 归 `hiclaw/**` 那一轨。最小改法是把 `_NioChannel` 的 `timeout` 提到构造参数（现在写死 10.0）并在 `open_channel` 里给一个对限流友好的值；顺手把 warning 改成打 `type(exc).__name__`，空 `str()` 的异常不至于印成一对空括号 |
| 2026-08-31 | P8 | **`hiclaw/room_demo.py` 的等待循环在 `926aa7b` 上仍是坏的**（派单 §5.4 第 4 条已点名）：只 `decided.wait(timeout)` 等回调置位，而回调里 `decided.set()` 排在「把回执发进房间」之后 | 后果是一次**审批已经生效**的运行报 `exit=2`，并打出「未等到审批 …… 任务仍停在 DONE」这种自相矛盾的话 | 本轨没用它 —— §5.4 自己写了等待循环，判据取**先到者**（回调置位 **或** 库里任务已离开 BLOCKED），实测正是「先到者」那一支救回来的。修复归 `hiclaw/**` 那一轨；照抄本轨那段循环即可 |
| 2026-08-31 | P8 | **`maos/tests/conftest.py` 没剥本轮新增的 `MAOS_CLAIM_APPROVAL_THRESHOLD`**（`## task-T28` 第 4 条、`## task-T35` 第 6 条记的是同一类问题，本轮又长出一个） | 一台 export 了这个变量的机器上跑全量，`test_claim_adjudication.py` 那三条阈值用例的结果会跟着机器走。症状是「某台机器上红、别的机器全绿」 | 归 conftest 的正当持有轨。照既有 `STORE_ENV_VARS` 的写法加一组 `CLAIM_ENV_VARS`；**注意别把它加进那条会饿死 live 测试的路径**（见 conftest 里那段关于时序的长注释） |
| 2026-08-31 | P8 | **`MockPayer` 不建模「金额被调整」**。X12 的 `1` / `2` / `3`（起付线 / 共保 / 自付额）与 `45` / `97`（合同价 / 已并入他项）这五条码的 `effect` 是 `patient_share` / `reduced` —— 真实赔付方回执会**同时**给出一个被削减后的实付金额，而 mock 只在 `detail.adjusted_by` 里留个码，到账金额仍是发出去的那个数 | 当前**无正确性影响**：本域从不从回执读金额，赔款一律取 `adjudication.allowed_amount`（`claim.pay` 里那一行）。所以这是**演示保真度**的缺口，不是 bug —— 但它意味着那五条码在流程上一次都没影响过钱，码表里的 `reduced` / `patient_share` 两档只被单元测试覆盖过 | 真要补，`PayerReceipt` 加一个 `paid_amount` 字段，并让 `claim.observe` 在到账时把「发出去的」与「实际到账的」两个数一起落进观察行 —— 那时才谈得上「对账」。**归下一轮理赔轨**；本轮不做是因为它会牵动权威边界的判据（到账金额与申报金额不符算不算 paid？那是个需要人拍板的业务问题，不是实现问题） |
| 2026-08-31 | P8 | **`maos/domain/claim/objects.py` 与 `maos/domain/refund/objects.py` 结构性重复**（`execute` / `query` / `lock_of` / `_atomic` / `_has_column` / 迁移记账那一整套，逐行同构，只换表名与保存点名）。`guard.py` 的四道闸同理 | 本轮是**有意的重复**：抽一层共用会让两个域焊死，而「换域只新增文件」正是本轮要证的那句话（`test_claim_isolation.py::test_the_two_business_domains_do_not_import_each_other` 钉着它）。代价是同一个 bug 要修两处 —— 比如 `_has_column` 那条「不用 PRAGMA」的坑 | **上第三个域之前**再决定。判据不是「重复了多少行」，而是「这些代码里有没有一行是领域相关的」：如果确实一行都没有，它就该下沉成 `maos/domain/_sql.py` 这样的**基础设施**（不是共用的域），且下沉后那条互不 import 的守卫仍要绿 |
## task-T38（银行差错处理域纵向切片）

以下六条都在**本轨白名单外**或**需要人类动作**，一条都没有当场改（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P8 | 🔴 **三份生成物过期，4 条存量测试变红**：本域新增 4 个角色 / 5 个 skill / 2 个 ToolPort，`docs/agent-identity.md`、`docs/skill-catalog.md`、`docs/toolport-contract.md` 与代码不再逐字节一致，`test_generated_docs.py` 的 4 条用例因此失败 | **派单 §6 与 §4 在这里自相矛盾**：§6 把 `python3 scripts/gen_docs.py --check` exit=0 列为验收项（并注明「新 skill/tool 落进生成物且与代码一致」），而 §4 的白名单里没有这三份文件。更要紧的是 **T37 / T38 / T39 三轨都新增 skill/tool/agent**，三轨都得重跑同一个生成器、改同一份文件 —— 谁先提交谁就给另两轨造一次三方冲突，性质与 §0.1 说明「谁改 `main.py` 谁就和另两轨冲突」完全相同 | **卡在人类裁决**，不是代码问题。两条路：① 三轨都不碰，**整合轮统一跑一次** `python3 scripts/gen_docs.py`（推荐 —— 生成物是机器产的，合并时重跑一次即可，零信息损失，且与 `main.py` 的处理口径一致）；② 授权本轨现在就跑，那么 T37/T39 合入时必然要再跑一次并解冲突。选 ① 的话，建议把这三份文件与 `maos/main.py` 一起写进后续派单的「整合轮统一处理」清单 |
| 2026-08-31 | P8 | **`hiclaw/room_demo.py` 的等待循环在基线 `926aa7b` 上是坏的**（派单 §5.4 第 4 条已点名，本轮实跑复核属实）：它只 `decided.wait(timeout)` 等回调置位，而回调里 `decided.set()` 排在「把回执发进房间」**之后**，429 限流下那一步实测撞满超时 | 一次**审批已经生效**的运行会报 `exit=2`，并打出「未等到审批 …… 任务仍停在 DONE」这种自相矛盾的话。演示当天撞上会被当成「审批没接通」 | `hiclaw/**` 不在本轨白名单，没动。修法很小：等待判据取**先到者** —— 回调置位 **或** 库里那个任务已离开 BLOCKED（后者是权威的）。本轨的房间脚本已经这么写了，可直接照搬（在 scratchpad 里，不入库）。归持有 `hiclaw/**` 的那一轨 |
| 2026-08-31 | P8 | **`maos/main.py` 的 `DEFAULT_SCENARIOS` 不含场景 9**（派单 §0.1 有意如此，记账备查） | `python3 run.py` 跑不到本域这条链路；场景 9 目前只由 `maos/tests/test_investigation_flow.py` 调用。评委按 README 跑 `run.py` 看不到银行域 | 整合轮。接进去要一并动 `--scenario` 的 argparse choices、`scripts/make_evidence.py` 的场景列表、证据束数量、以及 README / 自查单里写死的「场景 1-7」——与 §0.1 说的是同一笔账 |
| 2026-08-31 | P8 | **守卫对含中文引号的 heredoc / 嵌套引号命令解析失败**，报文是「blocked: 该操作触碰受保护面 `<命令无法解析: No closing quotation>`（解析失败）。停止当前工作并向人类报告。」 | **措辞误导**：它说的是「触碰受保护面」并要求停手报告，而实际只是 shell 词法解析失败（我的引号问题），与受保护面无关。本轮撞了 3 次，每次都要先判断是不是真的碰了禁改面。按派单 §3.1 的判据表，这属于「你在动自己白名单内的文件却被拦」= 异常拦截 = 应停手 —— 但实际上换个写法（Write 工具代替 heredoc，见 §3.2 招式 3）就过了 | 守卫是禁改面，没动。建议把解析失败那一档的报文与「命中受保护面」分开，例如「blocked: 命令无法解析（引号不成对），换用 Write/Edit 工具或简化引号后重试」—— 它不需要人停手，只需要换写法。归有守卫授权的那一轨；`## task-p7-docs` 里「引号不成对误触守卫」记的是同一类，本条补上了**报文措辞**这一半 |
| 2026-08-31 | P8 | **`business_ref` 表被退款域独占**：它建在 `maos/domain/refund/schema.sql` 里，由 `refund/objects.py` 的 `attach_business_ref` / `resolve_business_ref` 读写，`_REF_TARGETS` 只认退款域那五种 `object_type` | 本域的 Task 与业务对象之间**没有等价的引用表**，`investigation_case` 挂在哪个 task 上只能靠 `plan_id` 反查。演示与测试都不需要它，所以本轮没造 —— 但「DAG 挂到业务对象」这条能力在本域是缺的，`docs/domain-portability.md` 的对照表里那一行对退款域成立、对本域不成立 | 下一轮做「两个域共用一张 business_ref」时一起：要么把表与访问器上提到一个域无关的位置（`maos/domain/__init__.py` 旁边），要么每个域各建一张。**别顺手在本域复制一份同名表** —— 两个域的 `schema.sql` 都 `CREATE TABLE IF NOT EXISTS business_ref` 会让「谁的形状说了算」变成看谁先建 |
| 2026-08-31 | P8 | **ISO 20022 码表取的是 3Q2022 v2（2022 年发布），已知有更新版但本机取不到** | 本轨用到的 3 个码集经独立第三方镜像交叉验证**零条冲突**，镜像只多出 5 条新码（`AACR` / `DT04` / `DUPL` / `RC03` / `RC04`），所以判据全部可信 —— 但码表不含 2022 年之后的增量。若评委拿最新版对，会发现少几条码 | 沙箱只放行 iso20022.org 的 `/sites/default/files/` 静态路径，站点所有 HTML 路径（含目录页、`robots.txt`、首页）一律 read timeout（实测 3 次），因此无法从目录页读到最新版文件名；按命名规律猜了 108 个候选 URL 全部 404。**需要人类在浏览器里打开目录页、把最新版 xlsx 的直链给回来**，之后重跑 `review/tools/` 那套提取脚本即可（提取脚本在 scratchpad，逻辑是纯 stdlib 读 xlsx，无依赖）|
## task-T39（应付账款域纵向切片）

2026-08-31 上应付账款域时发现的，**本轮都不改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-08-31 | P8 | 🔴 **第七道闸 `_gate_gateway` 的触发面是「产物里有没有 `content["receipt"]` 且带 `code`」，与业务域无关，但它拿到 code 之后去查的是 `maos/tools/gateway_codes.py` 的支付宝码表** —— 任何新域只要用了 `receipt` 这个字段名装自己的外部回执，就会被这道闸对着一张它根本不认识的码表判成「未知码 -> blocker -> 最危险的一档」 | 症状极难反推：新域每一份产物都返工，而报错指向「网关码不在已核对清单内」，读的人会去查支付宝文档。本轨绕过的方式是把回单字段命名成 `bank_advice`（`maos/tools/ap.py` 的 `ADVICE_FIELD`，有两条用例钉着），但那是**约定**不是机制 —— 下一个域仍会踩 | 归内核轨。两条路：① 闸的触发面加一个显式的域标记（如 `content["receipt_codelist"]`），认不出码表就**不判**而不是判 blocker；② 让 `gateway_codes` 支持按码表名注册，闸按产物声明的码表去查。① 成本低、语义更保守，推荐先做。改之前先把本域的 `test_ap_tools.py::test_gateway_gate_would_fire_if_we_used_the_wrong_field_name` 读一遍，那条用例就是这个坑的最小复现 |
| 2026-08-31 | P8 | **`scripts/verify.py` 第 3 项 authoritative-fact 只认退款域**：`AUTHORITATIVE_WRITER = "payment.observe"`（`verify.py:68`）、对账查的是 `refund_case` / `payment_observation` 两张表（`verify.py:424`）。本域的 `ap.observe` / `ap_case` / `ap_payment_observation` 完全不在它的视野里 | 「权威事实边界」这条外部核验对本域**是空的** —— 本域自己的守卫与 116 条用例都绿，但 `verify.py` 那一项不会因为本域被绕过而变红。派单 §0.1 已明说本轮 `verify.py` 不作为验收项，所以不是回归，但它是一个**将来会被误读成「已核验」的空白** | 归证据束轨（下一轮跑 `make_evidence.py` 的那一轨）。第 3 项应当改成按域循环：每个域声明自己的 `(writer, case 表, observation 表, 终态判据)` 四元组，`verify.py` 逐个域跑同一套三头检查。四元组的形状两个域已经一模一样（见 `maos/domain/*/guard.py` 的 `AUTHORITATIVE_*` 常量），抽出来不难 |
| 2026-08-31 | P8 | **`docs/domain-portability.md` 已经不全**：全文按「两个域」写（§1 对照表、§5「两个域，不是 N 个域」），本域落地后是三个。§4 那份「换一个新域要做什么」的清单本轮**逐条照着走通了**，可以回填一次实证 | 那份文件是复赛材料里最核心的论证之一。停在「两个域」而仓库里有三个，评委翻到会当场发现文档落后于代码 —— 而这一轮恰恰是那份清单第一次被**第三方**（不是写它的人）照着执行，是最有说服力的一次验证 | 归文档轨。回填时要**分开算**：本域的 `contracts/` 与 `core/` diff 同样是空的（本轮实测），但 `runtime/` 也是空的 —— 这比退款域那一次更强（那次 `gate.py` +126）。别把两次合并成一个数 |
| 2026-08-31 | P8 | **三份生成物（`docs/agent-identity.md` / `skill-catalog.md` / `toolport-contract.md`）不在本轨 §4 白名单，但 §6 把 `gen_docs.py --check` exit=0 列成验收项** —— 只有重跑生成器才满足得了，本轮据此重跑并记进 DECISIONS | 同期 T37 / T38 / T39 三轨都新增 skill / agent / tool，三轨都会重跑这三份 —— **整合轮必冲突**，且冲突点全在自动生成的表格行里，人工 merge 极易漏行 | 归整合轮。**不要手工 merge 这三份**：任取一方的版本落地，然后在合并态上重跑一次 `python3 scripts/gen_docs.py`，再跑 `--check` 确认 exit=0。生成器的三条自我约束（字段顺序取自 dataclass、数量不写死、不写时间戳）保证了这样做的结果是唯一的 |
| 2026-08-31 | P8 | **`maos/main.py` 的 `DEFAULT_SCENARIOS` 不含场景 10**（派单 §0.1 有意为之：三轨都新增 flow，谁改谁冲突）。本域的两条路径目前只由 `maos/tests/test_ap_flow.py` 调用 | `python3 run.py` 跑不到本域，演示时看不见应付账款这条链路 | 归整合轮。接进去要一并动的东西已经在 `scenario_7.py::drive_human_exit` 的 docstring 里列全了：`--scenario` 的 argparse choices、`scripts/make_evidence.py` 的场景列表、证据束数量、`verify.py` 的来源数，以及 README / 自查单 / PPT 里写死的「场景 1-7」。`test_ap_flow.py::test_scenario_10_is_not_wired_into_default_scenarios` 会在那一刻变红，那正是提醒改这些的时机 |
| 2026-08-31 | P8 | **`hiclaw/room_demo.py` 的等待循环在 `926aa7b` 上是坏的**（派单 §5.4 第 4 条已点名）：它只 `decided.wait(timeout)` 等回调置位，而回调里的 `decided.set()` 排在 `bridge.handle_message()` **之后**，后者会把回执发进房间 —— 429 限流下那一步实测撞满超时 | 一次**审批已经生效**的运行会报 `exit=2`，并打出「未等到审批 …… 任务仍停在 DONE」这种自相矛盾的话。本轨的 §5.4 runner 自己写了「先到者」判据绕过它（回调置位 **或** 库里任务已离开 BLOCKED），但 `room_demo.py` 本身没动（不在白名单） | 归 hiclaw 轨。修法就是本轨 runner 那个：等待条件取先到者。顺带把回调里的 `decided.set()` 提到 `handle_message()` **之前**（判定已经生效了，发回执是旁路） |
| 2026-08-31 | P8 | 🔴 **`Sender` 那类「一条命令一次登陆」的写法在 Synapse 上会卡死，不是报错**。`rc_login` 的限流比消息限流狠得多，第二次 `login()` 就吃 429；nio 于是 sleep 42s 重试，而 429 响应体里没有 `user_id`，nio 的 schema 校验再报一次 `Error validating response: 'user_id' is a required property` —— 表现是进程**静止**。本轨第一版实测 10 分钟没走完第一条命令 | 任何要「模拟多个人在房间里发言」的脚本都会踩（演示彩排、证据采集、集成测试）。它的坏处不在慢，在于**看起来像挂了**，排查方向会指向房间或网络 | 归 hiclaw / 演示轨。口径定成「每个账号只登陆一次、复用同一个 client 与同一个事件循环」，并在两次登陆之间留 3 秒。本轨 runner 的 `Sender` docstring 里写了完整现象，照抄即可 |
| 2026-08-31 | P8 | **`ap.match` 不处理单据级折扣与附加费**（BR-CO-11 / BR-CO-12、PEPPOL-EN16931-R040/R041/R042），于是 BR-CO-13 的算式退化成「不含税总额 = Σ 行净额」、R120 退化成「行净额 = 数量 × 单价」 | 真实发票带整单折扣时会被判成 BR-CO-13 勾稽失败 —— 一条**假的拒付理由**，而它挂着一个真实的规则编号，看起来毫无破绽。当前靶场与用例都不带折扣，所以不咬人 | 归下一轮应付轨。补的时候要一并加 `supplier_invoice_allowance` / `_charge` 两张表与对应的 UNCL5189 / UNCL7161 码表（两张表的出处在 `docs.peppol.eu/poacc/billing/3.0/codelist/`，本轮没抄）。**别只改判据不加码表** —— 折扣理由码同样要可核对 |
| 2026-08-31 | P8 | **本域的五个 artifact kind 没进 `maos/artifacts.py` 的 `ALL_KINDS`**（刻意：那是跨轨冻结面，单轨往里加必撞，口径同 `agents/refund/_base.py`） | Gate 对非代码类产物不查 kind 白名单，所以运行时无影响。但 `maos/artifacts.py` 那份清单已经不是「全部 kind」了，读它的人会以为是 | 归整合轮或冻结面轨。要么把三轨的新 kind 一次性加进去，要么在 `ALL_KINDS` 旁边写明它只覆盖软件交付域 |
| 2026-08-31 | P8 | **`maos/tests/conftest.py` 仍没剥 `MAOS_CONFIG_SOURCE` 与 `MAOS_NACOS_*`**（`## task-T28` 第 4 条、`## task-T35` 复核过两次） | 本轮没有新增 env 旋钮，所以这个面**没有变大**。记在这里只是复核确认它还在 | 无需本轮处理，见 `## task-T35` 那条给的清单 |

## task-T51（cumora 六轨解析整合轮 · T40–T45 附录 A 折账）

T40–T45 六轨各自读了参照实现 cumora 的一个面，产出 `docs/refs/cumora-*.md` 六份文档
（1808 行）。每份末尾都有一节「顺手发现的 MAOS 问题」，六份都写着「本轮不改、留给整合轮
统一折进 BACKLOG」——**那个整合轮一直没发生**（T46 整的是 T37–T39 三个业务域，不是这六轨）。
本节就是那次折账，共 24 条。

🔴 **三条关于本节可信度的说明，读表之前先看：**

1. **六份文档的基线是 `926aa7b`，本节的行号全部在 `4bb6694`（T46 tip）上重取。**
   T37–T39 三个业务域加了 20831 行，行号大面积漂移；逐条重跑之后有 **2 条的读数变了**
   （见第 8、9 条，两条都是「变严重了」），其余 22 条事实不变、只是行号变了。
   **本节的行号是 T46 合并态的，不是主干 `1438ba3` 的**；主干合 T46 之前照着查会对不上。
2. **本轮只折账、不修**（铁律 4），唯一的例外是第 1 条 —— 它是**录制门禁本身**，
   留着不修等于让门禁在录制当天继续报假警。理由记在 `DECISIONS ## task-T51`。
3. **有一条是对六份文档之一的更正**（第 22 条）：`docs/refs/cumora-data-model.md` 说
   「kb 只有 `tenant_id` 一维」，实测 `PREFILTER_FIELDS` 有 7 个字段。结论方向不变，
   但落地口径要改，那条不能按「过滤链上加一档即可」去做。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | ✅ **已修**：`scripts/demo_preflight.sh` 的 `EXPECT_TESTS_NOPG` 停在 1069，而 T46 合并态实测 1370 passed / 39 skipped | 本脚本第 1 步逐条断言测试条数，对不上即 `die` —— T46 tip 上实跑得到「实际 1370 / 期望 1069」、打出「前置未通过，**不要开始录制**」、exit=1，**而当时代码是全绿的**。录制前唯一的机器判据自己变成了假警报，是最坏的一种失效：下一次它真红了，人会先怀疑是这个数又没刷 | **本轮已改成 1370 并实跑五步全过 exit=0**。规程写进了该文件的注释块：**加测试的那一轨改这个数，不要留给录制当天的人**。`PG_GATED_TESTS=29` 不变（三个新域一条门控测试都没加），有库档由算式得 1399，**未实测** |
| 2026-09-01 | P8 | **真模型降级只有一行 `log.info`**。`select_model_client` 三个环境变量缺任一个就回落 `ScriptedModelClient`（`maos/model/client.py:294`） | 「以为在跑真模型、其实在跑假模型」不会有任何显眼提示，而它恰好会让**一切验证结论失真** —— 复赛现场 key 配错，「真模型跑通了」就成了假结论 | 出处 `docs/refs/cumora-runtime.md` §3 #1，判「赛前做，0.5 人天」。cumora 对同类静默降级的处理是打阈值告警**并在告警正文里写明后果**（`http-client.ts:347-357`）。已派 **T47** |
| 2026-09-01 | P8 | **沙箱降级路径没有 fail-closed 档**。`_docker_ready()` 返回 False 就**无条件**降级裸 subprocess（`maos/tools/sandbox.py:591`、`:600`、`:603`），没有任何配置能表达「宁可不跑，也不裸跑」 | 现状已把原因写进 `sandbox_mode` / `degraded_reason` 并进 summary（这比只打日志好很多，`sandbox.py:596-599` 的注释解释了为什么），但**没有人必须同意**。「容器隔离」是提交材料里的一句断言，降级发生时那句断言就不成立 | 出处 `docs/refs/cumora-sandbox.md` §3 #1，对照 `engine.ts:1323` 的 `failIfUnavailable: true`。加一个走 `maos.config` 配置面的开关即可，测试与 CI 的 `MAOS_SANDBOX_FORCE_SUBPROCESS=1` 一个字不用改。已派 **T47** |
| 2026-09-01 | P8 | **第六道闸可被一个合法配置值静默停用**。`MAOS_FINANCE_THRESHOLD` 填 `99999999` 解析得通、不告警、闸判 `pass`（`maos/runtime/gate.py:96`、`:169`、`:182`） | 读 `gate_results` 的人分不出「这道闸没话说」和「这道闸被配置掉了」。解析**失败**的路径反而做对了（回落收严并告警，`gate.py:186-188`）—— 缺的是合法但异常取值这一档 | MAOS 自己已经发明了三态 `pass/noted/fail`，这里没用上。阈值非默认时补一条 `SEVERITY_INFO` finding 即可。「闸怎么证明它真的在判」是演示现场最可能被问的一句。已派 **T48** |
| 2026-09-01 | P8 | **`call_site` 没有登记表**。它是自由字符串（`maos/core/store.py:200`），全仓当前只有 3 个取值（`maos/agents/base.py:24` 的 `CALL_SITE_ASK`、两个 skill 各一个 `CALL_SITE`） | 新增一个模型调用点忘了记账**不会有任何信号**，成本统计静默偏低。T32 那次「六处补 `store=`」正是这类漏接的实证 | 出处 `docs/refs/cumora-cost-obs.md` §3 #1。cumora 把 `purpose` 做成穷举枚举、漏接由 CI 守卫 `guard-llm-tracked.mjs` 报红。抄思想即可：登记表 + 一条「出现未登记值即红」的测试，不改表、不改调用点。已派 **T48** |
| 2026-09-01 | P8 | **`maos/agents/reviewer.py:83` 从 JSON 中间裸切**：`json.dumps(artifacts, ensure_ascii=False, default=str)[:8000]` | Reviewer 模型收到的是语法破损的片段，**且不知道自己被截了**。artifact 一多（或某个 patch_set 的 `files` 字段一大），语义审查就基于残片出意见；模型硬着头皮输出了合法 JSON 的话，这是一个**完全静默的审查盲区**（`_parse` 只在输出不合契约时才兜底） | 出处 `docs/refs/cumora-turn-loop.md` §3 #1，对照 `turn.ts:867` 的自描述截断（返回 `{truncated, originalBytes, head, note}`，注释原话：keep the output self-describing instead of silently slicing JSON）。约 10 行。已派 **T49** |
| 2026-09-01 | P8 | **失败的模型调用完全不落账，延迟同理**。`record_model_usage` 只在成功路径被调（`maos/core/store.py:508`），`latency_ms`（`:205`）随之只覆盖成功路径 | 一次超时或被限流的调用照样烧掉了 input token，但在 `cost_view` 里根本不存在，而且**一句提示都没有** —— 这与 `ZERO_CALLS_NOTE` 想防的「调了没记」是同一类假象，区别是那一类有提示。超时调用往往是最贵的（烧了墙钟没有产出） | 出处 `docs/refs/cumora-cost-obs.md` §3 #2 / 附录 A #1 #5。cumora 把失败调用当一等公民记（`llm-ledger.ts:184-195`，catch 分支照样带 `latencyMs`）。🔴 **要给 `model_usage` 加 `status`/`error` 两列 = 动冻结契约**，判**复赛后**；退路是新开一张附表（铁律 1 允许新增表） |
| 2026-09-01 | P8 | **`ToolPort.rate_limit` 是声明了却零读取的死字段，而且本轮变严重了**。定义在 `maos/tools/port.py:31`，赋值处从六轨解析时的 **4 处涨到 10 处**（`gateway.py` / `sandbox.py` / `claim.py` / `ap.py` / `investigation.py` 各 2 处），全部填 `""`，**全仓没有任何读取方** | 风险不是「现在限流不生效」（现在也不需要），是**将来有人给模型调用加限流时，会以为工具这一层已经有了**。每上一个新域就多两处赋值，等于每上一个域就多两处「看起来配置过了」的假象 | 出处 `docs/refs/cumora-coordination.md` 附录 A #1。处置二选一：删掉字段，或在 ToolPort 注册时对非空值抛 `NotImplementedError`。**别第三次让它跟着新域长** |
| 2026-09-01 | P8 | **`AgentIdentity.max_self_repair` 是死字段，本轮同样变严重了**。定义在 `maos/agents/base.py:69`，赋值处从 **10 处涨到 22 处**（三个新域的 agent 各自赋了值），读取点仍然是 **0**；`scripts/gen_docs.py:158` 还把它当真字段生成进 `docs/agent-identity.md` 的角色表 | **文档在承诺一个运行时不存在的约束**，而 `docs/agent-identity.md` 因为是自动生成的，看上去还很权威。评委真去问「自修复上限怎么生效的」，答不上来 | 出处 `docs/refs/cumora-turn-loop.md` 附录 A #2。要么给它接上执行点（失败重试在 agent 内做几次，那要改 worker 主循环），要么删掉。**同上：别让它跟着第四个域再长一轮** |
| 2026-09-01 | P8 | **`register_skill` 同名同版本静默覆盖**。`maos/skills/registry.py:39` 是 `SKILL_REGISTRY.setdefault(contract.name, {})[contract.version] = cls`，两个模块注册同名同版本的 skill，后 import 的静默赢，不报警 | `registry.py` 的模块注释花了很大篇幅解释「保留历史版本是为了旧 Plan 可复现」，而同版本被悄悄换掉恰恰打破这条承诺。MAOS 的 skill 是 import 注册的、不是运行时装的，撞名概率低，但这是个真实的调试陷阱 | 出处 `docs/refs/cumora-turn-loop.md` §3 #9 / 附录 A #3。一行 `if version in versions: log.warning(...)` 即可。cumora 的同款约束更硬：**拒绝覆盖同名**，要重装得先显式删（`skills.ts:221`） |
| 2026-09-01 | P8 | **`claim` 幂等键无过期、无撤销口，PG store 上存在永久卡死的可能**。`maos/core/control_plane.py:309` 的 docstring 自陈「store 只有 claim/finish，没有撤销口」，键烧在 `:325` | Worker 若在 `_transit(RUNNING)` 之后、`_reply` 之前**硬崩**（进程被杀 / OOM），该 attempt 再也无法被认领，任务永久停在 RUNNING。`SqliteStore(":memory:")` 上库随进程消失所以看不出来；**PG store 上会留下卡死的行** | **当前不是活 bug** —— 单进程同步执行，且 `_invoke` 兜住了所有 Python 异常。出处 `docs/refs/cumora-runtime.md` 附录 A #1；cumora 对同一问题的解法是租约超时接管（`inproc-client.ts:955-968`）。**与 PolarDB 上线绑定** |
| 2026-09-01 | P8 | **`maos/core/store.py:1-5` 的开篇承诺对并发语义不成立**。原话是「换 PolarDB 时只改这一个文件……上层零改动」，但 `SqliteStore` 的隔离全靠单连接 + 进程内 `threading.RLock`（`:110`），PolarStore 上**没有这把锁的等价物** | `claim_idempotency` 的 INSERT/catch 还能用（靠唯一约束），但 `update_task` 的读-改-写、以及退款域「写 settled 与插 observation 同事务」（借的正是这把 RLock）都需要显式事务或行锁。PolarStore 落地那天按字面理解这句话，会漏掉一整类并发缺陷 | 出处 `docs/refs/cumora-data-model.md` 附录 A #1。**最小处置成本极低**：那句 docstring 补一句「**并发语义不随文件迁移**」。cumora 在这个位置的解法是多行加锁按 id 排序 + 授权谓词下沉进 UPDATE 的 WHERE |
| 2026-09-01 | P8 | **kb 的 `_MIGRATIONS` 没有并发保护**。`maos/kb/__init__.py:23-28` 自己说明了「迁移没跑」的后果，但没说**两个进程同时跑迁移**的后果 | 演示期的库都是 `:memory:` 或每次新建，两者的区别看不出来；PolarDB 是持久库就看得出来了 | 出处 `docs/refs/cumora-data-model.md` 附录 A #2。这正是 cumora 付过**两次硬故障**的地方（40P01 死锁让所有 pod 起不来；整批 DDL 一个隐式事务锁住约 30 张表直到提交）。它的解法是 session 级 advisory lock + `lock_timeout`。**与 PolarDB 上线绑定** |
| 2026-09-01 | P8 | **`AgentIdentity` 是 `frozen=True` 的代码常量，不是活行**（`maos/agents/base.py:60`），`check_tool` / `check_risk` / `check_write` 三查都对着这份快照判 | 单进程下 Identity 就是代码里的常量，**不存在「撤权后旧令牌还能用」这个窗口**，所以现在抄没有收益。记在这里是因为它与**铁律 8 是同一条道理的另一个应用面**：权威事实不在自己手里的东西，不许缓存成终态 | 出处 `docs/refs/cumora-sandbox.md` §3 #4。cumora 的判据是「令牌不是权威，活行才是」—— JWT 里的租户只是铸造时的快照，每次鉴权回查 `participants` 活行。**等 Identity 落库、多进程之后这条才成立** |
| 2026-09-01 | P8 | **人工放行不绑定操作人被展示的那一版**。`human_decision` 只认 `task_id`（`maos/core/control_plane.py:702`，幂等键 `human:{task_id}` 在 `:728`）；房间卡片把 `attempt` 渲染进了 Envelope（`hiclaw/room_demo.py:141`），但 `/approve {task_id}`（`:145`）把它丢掉了 | **当前不是活 bug** —— `assert_transition` 让陈旧批准响亮失败而非静默生效，且 BLOCKED 期间产物不会变。异步审批 / 多 worker 之后变成活 bug | 出处 `docs/refs/cumora-coordination.md` §3 #3 / 附录 A #3。cumora 的做法是放行标志绑定服务端展示过的那个状态（令牌存 `seq:<n>`，消费时重查房间有没有往前走，走了就作废并重新 HELD）。加**可选** `seen_attempt` 参数即可，不新增状态、不新增迁移 |
| 2026-09-01 | P8 | **四条止损机制并存，且相对顺序本身是判定的一部分**。`max_attempts`（`control_plane.py:452`）、第三出口 `_human_exit`（`:491`）、`_should_replan`（`:534`）、`_max_replan`（`:577`），而 `:442-443` 的注释自己承认第三出口「必须排在 max_attempts 之前」 | 这不是现在的缺陷，是**再加第五条时会出事的形状** —— 顺序是隐式的、只活在一处注释里 | 出处 `docs/refs/cumora-coordination.md` 附录 A #4。建议在 `on_review_verdict` 头上挂一条回归守卫注释：*四条止损的相对顺序是判定的一部分；加第五条之前，先证明现有四条里是哪一条没抓住。* cumora 的同款形态是 `HARD_LOOP_CAP` 头上那句「这条兜底被以『AI 原生的优雅』为名删过两次，两次都回归了——不要删」。已派 **T50** |
| 2026-09-01 | P8 | **「失败但没说为什么」在类型上合法**。`AgentOutput` 的 `open_questions` 默认空列表、`error` 默认 None（`maos/agents/base.py:94-95`），所以 `AgentOutput(status="failed")` 和 `AgentOutput(status="blocked")` 都是合法构造 | `ReviewerAgent._needs_human` 已经在局部守住了这条（注释：产出空白意见书比没有意见书危险得多），但这是 **agent 自觉，不是运行时强制** | 出处 `docs/refs/cumora-turn-loop.md` §3 #2 / 附录 A #4。cumora 把 `reason` 做成协议**必填**，缺了直接判 invalid 并把「reason is required so the runtime can audit why the turn stopped」写回给模型。一个 if 把它从可能变成不可能。已派 **T49** |
| 2026-09-01 | P8 | **usage 解析只认一家 provider 的口径**。`maos/model/client.py:238-239` 只读 `prompt_tokens` / `completion_tokens` | Anthropic 口径把缓存读写单列为 `cache_read_input_tokens` / `cache_creation_input_tokens`，这些字段在 MAOS 侧会被**整体忽略** —— 接第二家网关时 token 数会**系统性偏低且无声**，而 `_safe_int` 的告警只覆盖「字段不是整数」，覆盖不到「字段压根没被读」 | 出处 `docs/refs/cumora-cost-obs.md` §3 #4 / 附录 A #2。cumora 做了双口径映射：OpenAI 的 `input_tokens` **含**缓存需减、Anthropic 的**已排除**需分别取（`cost.ts:166-193`）。**现在只有一家网关，接第二家之前必须先做这条**；赛前动解析层是无收益的回归风险 |
| 2026-09-01 | P8 | **`GatewayModelClient` 无任何重试**。一次 `urlopen`，`URLError` / `TimeoutError` / `OSError` 直接转 `RuntimeError`（`maos/model/client.py:223-225`） | 演示现场一次网络抖动 = 一个任务 failed，而 MAOS 的失败会一路走到 REWORK 或 FAILED —— 观众看到的是「系统判错了」而不是「网断了」 | 出处 `docs/refs/cumora-runtime.md` §3 #2 / 附录 A #3。cumora 的判断是「连接类失败短重试能救回本回合，且**不该让用户看见失败**」，重试次数与退避写在客户端内部、调用方无感。**判复赛后**：赛前缺省路径是 Scripted，真模型只在演示时开，而重试会让演示时长不可预测——彩排价值高于健壮性 |
| 2026-09-01 | P8 | **`estimated` 一个字段扛了会分叉的语义**。现在它的含义是「token 数是不是 `len(user)//4` 编的」（`maos/obs/trace.py:283-291`） | 接真模型后会出现第三种情况：**真模型、真调用，但网关没回 usage**。届时这一个字段读不出区别，而给它加列会碰冻结面 | 出处 `docs/refs/cumora-cost-obs.md` 附录 A #4。cumora 用两个正交旗子分开这件事：`measured`（provider 报没报）与 `cost_estimated`（价格是不是猜的）。**值得在还没接真模型的现在就想清楚**，因为想清楚是免费的、加列不是 |
| 2026-09-01 | P8 | **`security_boundary` 的措辞容易被读成「降级也隔离了」**。`docs/toolport-contract.md:187` 写的是「降级路径裸 subprocess，env 按白名单重建（只放行 PATH/LANG，HOME 指向一次性空目录）」—— 每个字都对 | **没说的那半句**是：降级路径没有任何文件系统与网络隔离，模型产出的 Python 在 pytest collection 阶段就以宿主 uid 执行，能读该 uid 可读的一切**绝对路径**（换掉 `HOME` 不影响 `~/.ssh` 这种硬编码路径被读到）。这是**文档口径问题，不是代码 bug**，但 `security_boundary` 是「评审会逐条对」的字段 | 出处 `docs/refs/cumora-sandbox.md` 附录 A #2。建议在 ⑦ 里补一句明说降级路径隔离为零。**注意该文件由 `scripts/gen_docs.py` 生成，要改得改代码里的声明**，不能直接改生成物 |
| 2026-09-01 | P8 | 🔴 **对六份文档之一的更正 + kb 缺一个生命期维度**。`docs/refs/cumora-data-model.md` §3 #1 与附录 A #3 都写「kb 只有 `tenant_id` 一维」，**实测不对**：`maos/kb/retriever.py:70-73` 的 `PREFILTER_FIELDS` 有 7 个字段（`tenant_id, biz_type, channel_id, region, sku, policy_version, workflow_version`） | **结论方向仍然成立**（`grep -rn pinned maos/kb/` 零命中，那一档确实没有），但缺的是**生命期维度**而不是「第二个隔离维度」：「这条政策所有单子都适用」和「这条是那一单的经验」在召回时同权。落地**不能**按那份文档说的「过滤链上加一档即可」去做 | 出处 `docs/refs/cumora-data-model.md` §3 #1，原判「赛前做 1.5 人天」，**本轮下调为复赛后**：动手前必须先读完 `retriever.py` 的两阶段检索全流程（那份文档 §5 自己写明这一步没做）。cumora 的 `pinned` 是很便宜的补法（`memory-scope.ts:234`：pinned 无视 scope 永远可见），但那是在它读懂了自己检索链的前提下 |
| 2026-09-01 | P8 | **没有钉死的「上一个已知良好基线」**。同一条 `python3 -m pytest maos/tests -q` 在不同 worktree 给出不同条数，而没有一处记录「哪个 sha 上是哪个数、当时是什么状态」 | 回归时无法执行「对着上一个已知良好基线做 `git log --since` 逐个 commit 读」这套排查。第 1 条那个门禁失效，成因正是这个 —— 条数散在脚本里、没有跟着 sha 走 | 出处 `docs/refs/cumora-coordination.md` 附录 A #5。cumora 把基线钉到**时间戳 + commit sha + 一句「当时是什么状态」**。这是流程问题不是代码问题；`scripts/demo_preflight.sh` 本轮新加的注释块是这条的局部落地，**全局那份还没有** |
| 2026-09-01 | P8 | ✅ **已核实，判「不必改」**：`ENV_PASSTHROUGH` 原样透传宿主 `PATH`（`maos/tools/sandbox.py:80`，用在 `:126`） | **不构成新增风险** —— 降级路径本来就在以宿主 uid 执行模型产出的 Python（pytest collection 阶段就会 import 补丁写进 workdir 的文件），PATH 影子不增加任何新能力 | 出处 `docs/refs/cumora-sandbox.md` §3 #7 / 附录 A #3。**记在这里只为一件事**：让后来的人不必把这条推导重来一遍，也不要因为「cumora 做了 PATH 清洗」就顺手给 MAOS 加一个没有收益的加固。真正该修的是上面第 3 条「要么隔离要么不跑」 |

## task-T47（降级要留痕 —— 模型客户端 + 沙箱）

本轨修的是 `## task-T51` 的第 2、3 两条（真模型静默降级、沙箱没有 fail-closed 档），
两条都已落地并实跑自证，不再重复记账。下面是**动手过程中撞到、但按铁律 4 不当场修**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **`gen_docs --check` 会因为纯行号漂移变红，而它没有「只是行号变了」这一档**。`docs/toolport-contract.md` 把声明位置写成 `maos/tools/sandbox.py:634` 这样的行号；本轨往 `sandbox.py` 里加了代码，两个 ToolPort 的行号 `:634→:723` / `:365→:439` 全漂，`security_boundary` 等**声明内容一字未变** | 派单 §0.1 写着「本轨不新增 skill / agent / tool，所以 `--check` 应当一直 exit=0；变红说明改到了声明面」——**这个判据不成立**：任何往这三个源文件里加行的轨都会让它变红，而变红的原因跟「声明面被改」完全无关。同轮 T48（`gate.py`/`obs`）、T49（`agents/**`，喂 `agent-identity.md`）大概率各撞一次，每轨都要停下来判一次「我是不是动了不该动的」 | **处置口径已有先例，缺的是把它写下来**：`4bb6694` 那次就是「三份生成物按合并后的代码重跑 `gen_docs`」，即**由整合轮统一重跑**、各轨不碰生成物。建议把这条写进 `docs/phases/common.md` 的并行约定，并把派单模板里 §0.1 那句话改成「`--check` 报的若**只有行号**，属预期，交整合轮重跑；报到字段内容才是声明面被改」。另一个更彻底的方向是让 `gen_docs` 输出锚点（符号名 + 相对位置）而不是绝对行号，那样生成物就不再随无关改动漂 —— 但那是改生成器，得单独一轨 |
| 2026-09-01 | P8 | **`select_model_client` 的 docstring 与本轨改后的实现有一处措辞落差**（`maos/model/client.py:277-283`）：它写「降级回 ScriptedModelClient 并只记录**缺失的变量名**」，没说记在哪个级别 | 不是错，是**没说到点上**。这条 docstring 是 A-12 冻结签名的说明文字，而「降级留痕」这件事的关键恰恰是级别（INFO 看不见、WARNING 才看得见）。将来有人照着 docstring 重写这段，很容易又写回 `log.info` —— 本轨买到的东西就这么丢了 | 本轨**没改 docstring**：派单 §8 明写「只动降级分支那几行，不要顺手把 `select_model_client` 的其它部分改好看」。补一句「且级别为 WARNING（降级必须看得见）」是十个字的活，**留给下一个正当动这个函数的轨顺手带上**。当前有测试 `test_degradation_is_a_warning_not_an_info` 钉着，回归会红，不是无防护状态 |
| 2026-09-01 | P8 | **`_clean_env` 原样透传宿主 `PATH`，会让降级路径在削过 PATH 的环境里「跑不起来」而不是「裸跑」**（`maos/tools/sandbox.py:80` 的 `ENV_PASSTHROUGH`，用在 `:126` 附近）。本轨演示 fail-closed 时用 `env PATH=/usr/bin:/bin` 制造「找不到 docker」，同一削法让降级路径里的 pytest 也找不到了，报 `pytest 没有产出 junit 报告，多半根本没跑起来` | **不是新增缺陷，也不影响本轨结论**（正常 PATH 下降级路径跑得好好的，`test_unset_flag_still_degrades_exactly_as_before` 实测 `passed > 0`）。记在这里是因为它揭示了一个**误判形态**：`tool_error` 说的是「pytest 没跑起来」，真实原因是「这个 env 里根本没有 python」，两者在报告上分不开。演示现场若在削过 env 的 shell 里跑，会往「代码/靶场有问题」的方向查 | 与 `## task-T51` 最后一条（已核实判「不必改」的 `PATH` 透传）是同一处代码的**另一面**，那条判的是安全面、这条是可诊断性。处置成本很低：`_run_degraded` 起 pytest 前先确认解释器可执行，不可执行时把 `tool_error` 写成「降级路径的 env 里找不到 python 解释器」。**判复赛后** —— 赛前不会有人在削过 PATH 的 shell 里跑演示 |

## task-T48（闸与账本要留痕：阈值三态 + call_site 登记）

本轨修的是 `## task-T51` 的第 4、5 两条（第六道闸可被合法配置值静默停用 / `call_site`
没有登记表），一条不落。下面四条是修的过程中**白名单外**的发现，按铁律 4 只记不改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **`scripts/demo_preflight.sh` 的 `EXPECT_TESTS_NOPG=1370` 又过期了**。本轨加了 11 条测试，全量实测 **1381 passed / 39 skipped** | 这正是 `## task-T51` 第 1 条修完那个门禁时写下的规程要防的事：「**加测试的那一轨改这个数**」。本轨改不了 —— `scripts/**` 不在白名单里，且 T47 / T49 / T50 三轨同期也在加测试，谁单独改都会立刻被下一轨顶掉 | **四轨全部并轨之后，由并轨的那一轮按合并态实跑一次改成实测值**。在那之前这个门禁必然报假警（实际 > 期望），录制前看到「不要开始录制」要先确认是不是这个原因 |
| 2026-09-01 | P8 | **阈值留痕的触发面与任务级判据对齐，于是混合计划里有一个覆盖缺口**。`_finance_threshold_notice` 只在被评审的任务自己 `biz_type == "refund"` 时开口 | 一个计划里既有退款任务、又有非退款任务，而恰好只评审到非退款任务那一轮时，阈值被调过这件事不留痕。**当前不是活 bug**：`_gate_finance_plan` 是逐任务跑的，同一个 plan 里只要有一个退款任务过闸，痕就留下了 | **不建议扩大触发面**。扩到「plan 里有退款任务就留痕」要在留痕这条路上再扫一次 `list_tasks`（多一次存储读，为一条 info），而扩到「无条件留痕」会把场景 1-5 每一个任务的 finance 从 `pass` 变成 `noted`。有真实需求再说 |
| 2026-09-01 | P8 | **登记表守不住「根本不记账」的新调用点**。两条守卫（静态扫源码 / 动态扫真跑过的库）判据都落在 `record_model_usage` 的调用点上 | 新增一个模型调用点、**连 `record_model_usage` 都没调**，两条守卫都是绿的，而账照漏。T32 那次「六处补 `store=`」漏的正是这一类 | 要堵得把判据挪到模型客户端那一侧（`complete()` 被调用了却没有对应的 usage 行）。**判复赛后**：那需要在 client 上挂计数并与库对账，比登记表重得多，而登记表已经把「改了字符串 / 多了一个记账点」这两类挡住了 |
| 2026-09-01 | P8 | **`maos/obs/__init__.py` 仍是一行 docstring，`trace` 与新增的 `call_sites` 都没有导出** | 不是缺陷，是口径：`maos/obs` 的两个模块现在都靠全路径 import（`from maos.obs import trace as trace_mod` / `from maos.obs.call_sites import ...`）。记在这里只为一件事 —— 后来的人别以为「没导出」是漏了，顺手加一个 `__all__` 反而会把两种 import 写法都留在仓库里 | **不必改**。真要统一的话，等 `maos/obs` 有第三个模块时一次性定口径 |

## task-T49（Agent 输出面：自描述截断 + 失败必须给理由）

本轨修的是 `## task-T51` 的第 6 条（`reviewer.py:83` 裸切）与第 17 条
（「失败但没说为什么」在类型上合法），两条都已落地并配了用例。下面 3 条是本轨
新发现、按铁律 4 **不当场改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **`register_skill` 同名同版本静默覆盖，本轨未做**。`maos/skills/registry.py:39` 的 `SKILL_REGISTRY.setdefault(contract.name, {})[contract.version] = cls` | 与 `## task-T51` 第 10 条同一条，本轨派单 §5.3 把它列为可选项。**未做的原因不是判它不值得，而是 `maos/skills/registry.py` 不在本轨白名单** —— 派单明令不许自行扩白名单 | 一行 `if version in versions: log.warning(...)` 即可，推荐 `warning` 不推荐 `raise`（skill 是 import 注册的，raise 会让一次误 import 掀掉整个进程启动）。**待人类裁决是否扩白名单**，裁决前不动 |
| 2026-09-01 | P8 | **`docs/agent-identity.md` 记录的是声明行号，于是任何在 `maos/agents/*.py` 上方插代码的改动都会让 `gen_docs.py --check` 变红** | 本轨实测踩了两次：§5.1 在 `reviewer.py` 顶部加了 12 行常量，`ReviewerAgent` 声明位置 33→47；§5.2 在 `base.py` 中段加了 `__post_init__`，`BaseAgent` 位置 103→131。两次都与 `AgentIdentity` 的**字段**毫无关系，但门禁照红。派单 §0.1 也只预警了「动字段会红」这一种 | 不是 bug，是**行号作为标识的固有代价**（同 `## task-T51` 第 24 条那类「读数跟着 sha 走」的问题）。真要治就得让生成器记符号名而不是行号，收益不明显。**记在这里只为一件事**：下一轨看到这个红，别去查自己有没有动字段，直接重跑生成器 |
| 2026-09-01 | P8 | **`AgentOutput.status` 仍是自由字符串**，`__post_init__` 只对 `failed` / `blocked` 立了理由不变量，**没有校验 status 取值本身** | 拼错成 `AgentOutput(status="faild")` 照旧构造成功，且**绕过本轨刚立的不变量** —— 它既不等于 `failed` 也不等于 `blocked`，两条 if 都不命中。实跑核实过下游：`on_task_result` 的分支是 `ok` / `blocked` / `else: # failed`（`maos/core/control_plane.py:365`），拼错的值落进最后那个兜底档，`last_error=p.get("error")` 取到 `None` —— **任务判 FAILED 而库里的 `last_error` 是空的**，正是本轨要堵的那个形状换了个入口进来 | 本轨**没做**：加 status 白名单是一条独立的收严，会波及全仓 55 处构造点与事件契约面的 status 取值口径，超出派单范围（铁律 4）。做的话应与 `maos/contracts/events.py` 里 TaskResult 的 status 口径一起定，那是冻结面，**必须先问人类** |

## task-T50（判据与规程 · 失败姿态 + 回归守卫注释）

本轨零逻辑改动，产出是 `docs/failure-posture.md` 与两处回归守卫注释。读面很宽（全仓
`fail-closed` / `fail-open` 字样 10 处、`except → return 默认值` 分支 39 处），
下面三条是读到但**按铁律 4 没有当场改**的东西。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **「吞掉型分支必须说明方向」做不成机器判据 —— 判据太粗**。按「`except` 之后紧跟 `return <默认值>`」扫 `maos/`（不含 tests），命中 **39 处**，其中只有 **7 处**在同函数 docstring 或就近注释里出现过方向词（收严/放宽/fail-closed/fail-open/回落/降级/触发/保守/宁可）。7 处分别在 `maos/runtime/gate.py`（1）、`maos/skills/builtin/claim/_common.py`（2）、`maos/tools/sandbox.py`（4） | 39 > 派单 §5.3 定的 20 处门槛，所以**没有**做成测试 —— 硬做会得到一条 32 处红、且大多数是误报的门禁，下一个人只会给它加白名单直到它失效（`docs/refs/cumora-coordination.md` 附录 B #1 那条「恒假警的闸，成本是真的」） | 判据要先收窄再谈门禁。两个可行方向：①只扫**被判定的业务量**（金额、阈值、白名单、权限），把 `_has_column` 这种能力探测和 `_dec` 这种数值转换排除掉；②不扫代码扫**测试** —— 断言每个 fail-closed 分支都有一条名字带方向的用例（`test_*_is_fail_closed` 现有 5 条，形态已经在了）。复赛后做；在那之前 `docs/failure-posture.md` 判据三那份检查单靠人记得用 |
| 2026-09-01 | P8 | **`task.state` 一个字段同时是调度游标和展示状态**。控制面靠它决定下一步派谁（`dispatch_ready` 挑 PENDING、`claim` 要求 DISPATCHED、`_advance` 看依赖是不是 DONE），房间卡片与演示脚本靠它显示「现在到哪一步了」 | **现在不咬人**，理由是结构性的：单进程、事件总线单线程串行 drain、DAG 串行推进、无第二个 worker、房间与控制面同进程。异步审批 / 多 worker / 房间与控制面跨进程，三者任一成立即变成活 bug —— 展示的那份必然过期，而没有任何东西保证它与调度用的那份一致 | 出处 `docs/refs/cumora-data-model.md` §3 #3（cumora 侧 `seen-boundary.ts:8-15`）。判据全文已写进 `docs/failure-posture.md` 判据二。**现在唯一要做的是不再往这个字段上挂第三种用途**；真要拆是并行化那次改动的一部分（铁律 9 是同一句话在业务状态那一侧的说法）。与上游 `## task-T51` 那条「人工放行不绑定操作人被展示的那一版」是同一个根因的两个面 |
| 2026-09-01 | P8 | **回归守卫注释这一形态本轮只落了 2 处**，`maos/core/control_plane.py` 的 `on_review_verdict`（四条止损顺序）与 `claim`（幂等键顺序） | `docs/refs/cumora-coordination.md` §3 #6 点名的候选还有一处没挂：`maos/runtime/gate.py` 里四象限 severity 的判定（那处曾经分叉过）。形态缺失的代价是隐性的 —— 不会有人报「这里少一条注释」，只会在某次「顺手简化」之后回归 | 半天以内的事，但**必须由持有那个文件的轨来做**：本轮 T48 正在改 `gate.py`，本轨若同时改会撞车（这也是本轨判「零逻辑改动」的边界之一）。留给 T48 之后的任意一轨，或复赛后统一补。判据是「下一个想动它的人会先停一下」，不是「有注释」 |

## task-T52（文档引用守卫转绿 · 外部引用与工作区无关性）

以下五条都在**本轨白名单外**，或需要先拍板口径，一条都没有当场改（铁律 4）。
第 1 条是本轨最大的一笔欠账：它是把「文档守卫」从「永远红」拉到「阻断类全绿」时
被显式分出去的那一档，不是被消失掉的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | 🔴 **113 条排版欠账（提示类）**。`scripts/check_docs.py` 现在把判据分成阻断类与提示类，主判据只钉阻断类；当前提示类共 113 条：`A-lang` 82（代码围栏没写语言标注）、`C-dup` 30（同级标题锚点重名）、`C-h1` 1（第二个 H1）。按文件：`docs/matrix-room-runbook.md` 18、`docs/hiclaw-probe.md` 18、`docs/EXECUTION.md` 11+23、`docs/demo-script.md` 14、`docs/clone-smoke-report.md` 9、`docs/gateway-rationale.md` 7、`README.md` 4、`docs/ppt-outline.md` 2+1、`docs/authoritative-facts.md` 2、`REVIEW.md` / `CONTRIBUTING.md` / `CLAUDE.md` / `docs/submission-checklist.md` 各 1 | 不影响文档说的话是否为真，所以不该拦住阻断类的判据；但放着不管就是 113 条永远不会被清的账。`--all-blocking` 一开就能看到完整清单 | **归各文档的持有轨**，各清各的（`docs/demo-script.md` / `ppt-outline.md` / `submission-checklist.md` 三份归 T53）。`C-dup` 那 30 条要当心：`docs/EXECUTION.md` 里 `### 目标 / 步骤 / 验收 / 提交` 是每个 Phase 重复一遍的**有意结构**，改名会动到别处按标题引它的地方 —— 更可能的结论是把 `C-dup` 从判据里摘掉，而不是改文档 |
| 2026-09-01 | P8 | **cumora 六份解析里对外部仓 `.ts` 文件的引用没有统一前缀**。本轨给 46 处 `.md` 外部引用加了 `cumora:` 前缀，但同样指向外部仓的 `engine.ts:1323`、`cli.ts:2188-2222`、`daemon.ts:54`、`seen-boundary.ts:8-15` 等仍是裸写 | 它们不报错**纯属侥幸** —— `PATHREF` 的扩展名表里没有 `.ts`，守卫根本看不见它们。同一份文档里两种外部引用长得不一样，下一个读的人会以为 `.ts` 那批是本仓文件 | 归下一轮碰 `docs/refs/cumora-*.md` 的轨。本轨只动派单 §5.2 划定的那 46 处 `.md`，没有顺手扩面 |
| 2026-09-01 | P8 | **`docs/DECISIONS.md:1457` 正文里裸提 `COORDINATION.md`**（不在反引号里，守卫看不见），指的同样是 cumora 仓那份 | 与上一条同源：外部引用的写法在本仓还没有统一。这一条守卫永远抓不到，只能靠人 | `docs/DECISIONS.md` 是共享账本，不属任何一轨独占。归日后统一外部引用写法的那一次 |
| 2026-09-01 | P8 | **`PATHREF` 的扩展名表不含 `.png` / `.ts` / `.jpg` / `.svg`**，只认 `py/md/json/toml/sh/ya?ml/ini/cfg/lock/txt/Dockerfile` | 后果是双向的：`evidence/room/*.png` 这类引用**不会被误判红**（这正是本轮要的），但也**守不住**它们断链。真要守证据截图的引用，得先决定 `evidence/` 的口径（那批文件由脚本重生成，行号/存在性的语义和源码不同） | 想守证据引用时再做。**别顺手扩表** —— 扩表会立刻把一批现存引用判红，而它们分散在多轨手里 |
| 2026-09-01 | P8 | **`ALLOW_MISSING` 里有两条已成冗余**：`maos/skills/builtin/probe_autodiscovery_tmp.py` 与 `maos/skills/builtin/_private_probe.py` 都写在 `.gitignore` 里，被「git 忽略即出射程」这条新规则天然覆盖，豁免再也不会被用到 | 无功能影响，但 `test_allow_missing_has_no_dead_entries` 只查「被豁免的文件是不是已经建出来了」，查不出「这条豁免已经被另一条规则接管」。清单会慢慢积压这种看不见的死条目 | 归下一轮碰这个脚本的轨。删之前要确认 `.gitignore` 里那两行还在（它们是有意写死的两个确切文件名，不是通配） |

## task-T53（答辩自伤口径根治 · 白名单外的命中）

本轨把「真房间未接通」这个**已被本仓自己的证据推翻**的口径，从
`docs/submission-checklist.md` / `docs/ppt-outline.md` / `docs/demo-script.md`
三份文档里全量根治。下面是**白名单外**的命中，一处都没改（铁律 4）。

🔴 **第 1 条是本节唯一的高优先级项** —— 它在评委真正会打开的那份制品里。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | 🔴 **已导出的 PPT 制品里有 5 处旧房间口径，其中一处是三重错误**。`artifacts/maos-proposal.html`：`:517` 状态标签「代码就绪／真房间未接通」、`:541`「**真房间未接通。Synapse 账号需人类手工注册。matrix-nio 未安装**」、`:549` slot「房间实拍：待 T4 回填」、`:1027` A-4 表「真房间待接通」、`:1114`「失败与缺口没有删：……真房间未接通……」 | **这是评委真正会打开的东西**，比三份 Markdown 严重一个量级。`:541` 那句三个分句**逐条被实测证伪**：房间已接通；三个 Matrix 账号全部由 `register_new_matrix_user` 脚本注册、**没有一步需要人类点 GUI**（`docs/agentteams-mapping.md:64-66` 明写上一版那个前提是错的）；matrix-nio 0.26.0 装在 `~/.maos-matrix/venv/`，**只有系统 `python3` 没装**（漏了主语）。`:1114` 尤其伤 —— 它把一句错的自曝当成「我们很诚实」的例证 | **收尾轨，且优先级高于本节其余各条。** 改 `:549` 那个 slot 时按 `docs/ppt-outline.md` 的「留给整合轮的一件事」执行：填 `evidence/room/01-approval-card.png`，base64 内联不外链 |
| 2026-09-01 | P8 | **`artifacts/README.md` 两处同源旧口径**：`:68`「房间实拍：待 T4 回填」、`:89`「同时改 P6 与 P13 的口径。当前两页写的都是『真房间待接通』」 | 它是上一条那份 HTML 的回填手册。手册不改，回填的人照着做还是会写回旧口径 | **与上一条同轨同时改**，两者必须一起动，单改一边会再次分叉 |
| 2026-09-01 | P8 | **`docs/agentteams-mapping.md:89-92` 的注记已反向过期**。那段写着「`docs/submission-checklist.md` §A-4 那一行仍写着『真房间待接通』+ 复核结论『真房间未接通』——**该行已过期**，本轮只读不改，已记 `## task-T4`」 | 该行**已由本轨 T53 修好**。注记留着，会让下一个读 mapping 的人以为 §A-4 还是旧的，进而去「修」一个已经对的地方 | **收尾轨**：删掉那段注记，或改成「已由 T53 修正」。`docs/agentteams-mapping.md` 不在本轨可改面内 |
| 2026-09-01 | P8 | **三份文档的 pytest 条数家族全面过期**。实测 `36bd036` 上是 **1370 passed, 39 skipped**，而三份文档里散着 `1069` / `802`（7 处）/ `749`（7 处）/ `703`（8 处）/ `645`（5 处）/ `596`（3 处）等至少 15 个不同的历史值 | 台上任何一个数字被评委当场复现打脸，整份材料的可信度一起塌。但**现在刷没有意义** —— T47 / T48 / T49 / T50 / T52 五轨仍在加测试与判据，六轨并轨后还得再刷一遍 | **收尾轨，且必须是并轨之后的最后一步。** 判据：以 `python3 -m pytest maos/tests -q` 的当场输出为唯一真值，三份文档 + `README.md` + `docs/EXECUTION.md` + `artifacts/**` + `scripts/demo_preflight.sh` 的 `EXPECT_TESTS_NOPG` 一次刷齐 |
| 2026-09-01 | P8 | **`docs/submission-checklist.md` 内部对 `verify.py` 的读数自相矛盾**：`:34` 与 `:267` 写「`verify.py` → **8/8 PASS**」，而 `:123` / `:246` / `:474` / `:502` 写「**7/7 PASS**」 | 两个数指的很可能不是一回事（八束证据 vs 七项核验），但同一份自查清单里并排出现、都不带限定语，被追问「到底几项」时答不上来。**本轨未实跑核实**（`verify.py` 需先有 `evidence/scenario-*/maos.db`，跑它会把 `evidence/` 跑脏，超出本轨零改动范围） | **收尾轨**：跑一次 `make_evidence.py` + `verify.py`，按当场输出统一措辞，把「几束证据」与「七项核验」两个读数分开写 |
| 2026-09-01 | P8 | **`04-reject-compensation.png` 文件名名不副实，改名要一起动三处**。`evidence/room/README.md` 自己划了边界：房间里拍不到补偿（`CompensationExecuted` 只落 `event_log`、从不 publish），这张图证明的是「驳回生效 + Plan 落 FAILED」 | 文件名比它能证明的东西大，是最容易被追问穿的一处。已记 `## task-T4`，至今未做 | **收尾轨或复赛后。** 改名要同时动 `docs/EXECUTION.md:499/502` 与 `evidence/room/README.md` 三处；**本轨的处置是不改名、改口径** —— 三份文档里凡提到这张图的地方都已写明它证明不了补偿 |

## integrate-p8-t47-t53（七轨整合轮）

本轮只做合并与收口，不做手册范围外的改动。下面是合并态下**已消解**与**仍留账**的条目。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | ✅ **已消解**：`## task-T48` 第 1 条记的 `EXPECT_TESTS_NOPG=1370` 过期。合并态实测 **1456 passed / 39 skipped**，本轮已刷新为 1456 并同步了上面那行历史实测注释 | 录制门禁第 1 步在合并态重新判得准 | 已处理 |
| 2026-09-01 | P8 | **三份文档的 pytest 条数家族再次全面过期**（`## task-T51` 已记过一次，分母从 1370 变成 1456，那条的具体数字随之作废） | 交付面读数不准；`docs/submission-checklist.md` / `docs/ppt-outline.md` / `docs/demo-script.md` 里散落的 `1069` / `1370` 等数都要重刷 | 下一轮交付面口径轮统一刷，别一轨一轨改 —— 分母每合一轮就变一次 |
| 2026-09-01 | P8 | **主工作区（仓库根）留着另一会话的 MCP 集成在制品 83 处未提交**，其中 `scripts/check_docs.py` / `maos/tests/test_docs_guard.py` 是 8-31 的 untracked 旧版，与 T52 合入 git 的新版同名不同内容 | `integrate/p8-t47-t53` 快进回 `goai-restructure` 时会被这两个 untracked 文件挡住 | MCP 轨收工提交后，由人类决定用哪一版（T52 版已带 +211 行演进与 318 行测试） |

## task-T54（失败调用留账 + usage 两家口径）

本轨修的是 `## task-T51` 折账的第 7、18 两条，两条都已落地并配了变异检验
（M1 撤掉 `ask()` 的失败记账 / M2 把失败改回编 0 token 写进 `model_usage` /
M3 停掉 Anthropic 口径分支，三次分别让 1、6、3 条用例变红）。
下面是动手过程中撞到、按铁律 4 **不当场改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **`GatewayModelClient` 仍然零重试**（`## task-T51` 折账第 19 条，本轨没动）。现在失败**留痕了**，但一次网络抖动照旧等于一个任务 failed | 留痕让「演示当天为什么这个任务红了」查得到，但没有减少它红的概率。两件事要分开做 | 下一轨。重试要跟本轨的失败表配套 —— 每次重试各落一行，否则「重试了 3 次」在账上看起来像「失败了 3 个不同的调用」 |
| 2026-09-01 | P8 | **`estimated` 仍是一个字段扛两种语义**（折账第 20 条）。本轨给失败面新增了 `usage_detail.dialect`，其中 `dialect="unknown"` 恰好就是第 20 条说的第三种情况（真模型、真调用、但网关没回可识别的 usage） | 现在这个信息只在 `ModelResponse.meta` 里，**没有落库** —— 库里那行仍然是 `estimated=0` 且 `tokens_in=0`，读库的人分不出「真的没花」和「没读懂它回了什么」 | 与第 20 条一起做。落库要新增表（`model_usage` 是冻结面），或者接受只在日志里可见 |
| 2026-09-01 | P8 | **失败表没有 `attempt` 列，重试语义上无法归并**。`model_call_failure` 认得到 task_id，但同一个 task 的第 2 次尝试与第 1 次在表上长得一样 | 现在没有重试，所以还看不出来；上一条那个重试一旦做了，这张表立刻需要它 | 做重试的那一轨顺手加（新增列在新表上不违铁律 1 —— 冻结的是**现有**表结构，但要在同一轨里改完，不要留半张表） |
| 2026-09-01 | P8 | **`record_model_failure` 不做二次脱敏**，依赖 `model/client.py::_scrub` 在上游把 key 抹干净 | 判断是对的（该修的是产生它的那一处），但这条依赖**没有测试守着**：`_scrub` 哪天漏一条路径，密钥会经由 `error_msg` 落进库、再进 evidence | 与铁律 6 的哨兵机制合并做：证据束落盘时已有反查，但**库**没有。加一条守卫扫 `model_call_failure.error_msg` |

## task-mcp（接 `git-mcp` 时发现，本轮都不改）

2026-09-01 落地第一个 MCP 工具时发现的三条。前两条是**刻意不迁**的理由，
写在这里是因为 `docs/toolport-contract.md`「迁移到 MCP」一节直接引它们 ——
生成物里那句「两条都记在 docs/BACKLOG.md」得指得到东西。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P8 | **`sandbox.git_apply` / `sandbox.pytest_run` 本轮不迁 MCP** | 无。这两个工具的安全论证是「容器 `--network none --read-only --user 1000:1000`」，换成跨进程传输之后，隔离等价性要从头论证一遍（沙箱 server 自己跑在哪个边界里？降级路径怎么办？`sandbox_mode` 还测得准吗），而它们本来就已经是真调用，迁移收益为零 | 只有在「沙箱真的要跑到另一台机器上」时才值得做。届时先解决的不是传输，是隔离边界怎么跟着搬 |
| 2026-09-01 | P8 | **`gateway.refund` / `gateway.query` 迁 MCP 前必须先重构参数** | 这两个工具把 `GatewayPort` **活对象本身**当 params 传（`skills/builtin/refund/payment_execute.py:112`），跨进程之后传不过去。`maos/tools/gateway.py:235` 特意给 `MockGateway.__repr__` 去掉内存地址就是为了让 `params_digest` 可复现 —— 那是在给这个设计打补丁，不是在支持它 | 重构方向是「MCP server 侧持有 gateway，客户端只传 `gateway_name`」，配 `_common.py:88 register_gateway` 的注册表天然成立。归下一轮支付面轨，**先改参数再谈传输** |
| 2026-09-01 | P8 | **三处绕过 `invoke_tool` 的裸调用没有审计行**：`core/control_plane.py:801`、`runtime/gate.py:505`、`flows/common.py:249` 与 `:258` 直接调 `sandbox_git_apply` / `sandbox_pytest_run` 函数，不经 ToolPort | 这三处的补偿回滚与场景驱动**不产生 `ToolInvoked`**，`scripts/verify.py` 第 1 项校验也就看不见它们。今天无害（它们不是 agent 发起的调用），但它同时意味着：以后把 `sandbox.*` 的 `entry` 换掉时，这三处**不会跟着换**，且不会报错 —— 是静默失效 | `core/**` 与 `flows/**` 不在本轨白名单，没动。归下一轮：要么改成走 `invoke_tool`，要么在 ToolPort 声明里写明「本工具另有 N 处内部裸调用」。别默默留着 |

## task-T78（语料规则号撞号与失真文案 · 本轨只动语料与文档，零业务代码）

本轨一行业务逻辑都没改（唯一的 `.py` 是新建的守卫测试）。买的是「文档和语料说的话」
与「代码真做的事」重新对齐 —— 三处失真里两处已就地改准，一处够不着，逐条记在下面。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **同一个 `AS-003` 在本仓库里指着两件毫不相干的事**：`scenarios/custom/ledger.json` 的租户 `tnt-demo` 是「发错货全额退」（`rule_kind=wrong_item`，与证据无关），`scenarios/refund/**` 的租户 `tnt-mfg-a` / `tnt-mfg-b` 是「人为损坏免责，需图片举证」（`artificial_damage_exclusion`）。`policy_rule` 的主键是 `(tenant_id, rule_no, version)`，`rule_no` 不是全局主键 | 拿规则号当口径讲的地方会对错人：自定义入口实跑一单，回帖打出 `AS-003@v1`，听众按 `scenarios/refund/` 的语料理解会以为「举证判据生效了」，其实命中的是发错货那条 | **本轨了结到「加口径 + 加守卫」，编号本身没改** —— 理由见下一条。六份语料/文档补了固定措辞的租户作用域说明，两份 README 各加一张三租户对照表，机器判据在 `maos/tests/test_refund_corpus_rule_no.py`（4 条，负例注入实测 3 条会红）。**注**：派单把本条记作「了结 `docs/BACKLOG.md:1643`」，但 `b35c618` 上本文件只有 1641 行、grep 全文也没有撞号条目 —— 那两行属于主仓未提交的在制品，本条是**新立**的 |
| 2026-09-02 | P9 | **规则号为什么不能改**：`evidence/scenario-R5/business-objects.json`（生成自 `cfe4384`）里冻着 4 行 `rule_no=AS-003`（去重后是 `tnt-mfg-a` v1 一种），其 `body` 与 `scenarios/refund/policy/policy_rules.json` 里对应行**逐字节相同**（本轨实跑比对：同 4 / 不同 0 / 查无此行 0） | 改语料里的编号会让证据束与语料当场对不上，而证据必须来自真实命令输出、一个字节都不许手改（铁律 3）—— 唯一正当的修法是**全量重跑证据束**，那是整合轮的事，不是一条文案改动该拖出来的动作 | 将来真要重新编号（比如给不同租户加前缀），必须与「证据束按合并态全量重跑」同一轮做，且先确认没有别的束引用旧号。单独改语料 = 证据束失效 |
| 2026-09-02 | P9 | **三处文案会因为政策判定器落地而过期，逐处点名**：① `docs/EXECUTION.md` 附 B 的 AS-003 那一格与其下新增的注（写着「判据待实现」）；② `scenarios/refund/README.md` 「`body` 为什么是 JSON 字符串」一节新增的注（写着「`finance.settle` 只消费两个键」）；③ 同文件那句「其余键会原样进入 `matched_rules[].params` 供下游取用」 | 这三处**今天是准确的**（`b35c618` 实测），落地后会变成过期描述。本轨刻意**没有预先写成未来态** —— 那是把没跑过的现象写成实录，违铁律 3 | **交整合轮复核**：政策判定器与金额核算面合并后，逐处重跑一次判断再改。改之前先跑 `grep -rn "requires_evidence_kinds\|min_evidence_count\|evidence_source" --include=*.py maos/` 看消费方到底出现了没有，别按派单的预期改 |
| 2026-09-02 | P9 | **还有 4 份含 `policy_rule` 的语料没补租户作用域口径**：`scenarios/custom/refund-case.json`、`scenarios/refund/cases/case_r3a.json` / `case_r3b.json` / `case_r6.json`。其中 r3a / r3b 的 `_note` 已经用自然语言说了「同一条规则编号在两个租户下参数不同」，另两份什么都没说 | 本轨白名单只点了 4 份语料 + 2 份 README（派单 §4「只许动这些」），这 4 份在白名单外，按 CLAUDE.md「本轨白名单以外的文件一律停手问」没动 | 已写进 `maos/tests/test_refund_corpus_rule_no.py` 的 `PENDING` 集合，**不是从判据里删掉而是显式列着** —— 补完一份就挪进 `COVERED`，挪漏了第 1 条测试会红。整合轮顺手补，一份加一行抬头即可 |
| 2026-09-02 | P9 | **docs 目录下那份 ingress 配置手册（文件名 ingress-setup.md，此处刻意不写成反引号路径 —— 它还不存在，写成路径会让文档守卫报 `E-missing` 阻断）的那处口径本轨够不着**：该文件不在版本库里（`git ls-files docs/` 无此项），只作为未提交的在制品存在于主仓工作区，另有活跃会话正在 ingress 通道上作业 | 派单 §5.4 要求给它的第 188-189 行加一句时点标注。该处**当前描述是准确的**（如实记了三个键零消费方这一缺陷），所以不加标注的代价只是「判定器落地后它会变成过期描述」，不是现在就错 | 归 ingress 那一轨或整合轮：文件进版本库后，照 `docs/EXECUTION.md` 附 B 那条注的写法加一句「截至 `<sha>`；判定器落地后需复核」即可，**别改它的结论** |
| 2026-09-02 | P9 | **`run.py` 的输出不可能「与某个 sha 逐字节一致」**：`plan_*` / `task_*` / `actor` 都是每次运行现生成的随机 id，耗时也逐次不同。同一棵树连跑两次，裸 `diff` 就不一致 | 派单把「`run.py` 输出与基线逐字节一致」写成硬判据，字面上恒假 —— 照字面执行会把一次正常运行报成回归。本轨改成「规范化随机 id 与耗时后逐行比对」，实测 427 行一致 | 下次写派单时把这条判据改成规范化比对，或给 `run.py` 加一个 `--deterministic-ids` 开关。**判据恒假比没有判据更坏**：它会训练下一个人跳过这一条 |
## task-T79（调用面：actor 锚点与同名同版本覆盖）

本轨开工时实测到一件与派单前提不符的事，先写在这里，后面几条都建立在它上面：
**「actor 锚点断链」在基线 `b35c618` 上已经不存在了** —— `1ac85b3` 已经把
`invocation_id` 塞进 `SkillContext.extras`（`maos/skills/invoker.py:91`），
skill 侧、`SkillResult`、落库那行三处同值。本轨因此把 §5.1 做成**回归守卫**
（注释 + 测试），而不是再修一遍已经好了的东西。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | ✅ **已了结：`register_skill` 同名同版本静默覆盖**（本文件 `:1513` 与 `:1560` 记的同一条） | 后 import 的照旧赢（行为一个字没变），但现在会打一条 `WARNING`，串里带 skill 名、版本、被顶掉的类与新类的模块名。选 `warning` 不选 `raise` 的理由照抄 `:1560` 的推荐：skill 是 import 注册的，`_discover_builtin()` 一次 import 整个 builtin 包，`raise` 会让一次误 import 掀掉整个进程启动，而撞名本身并不影响已注册的那份能不能用 | **本轮已做**。实现落在 `maos/skills/registry.py` 末尾的 `_put()`，而不是 `register_skill()` 函数体里 —— 后者会把 `get()` / `versions()` 的行号往下推，而这两个行号被写死在 `docs/skill-catalog.md` 正文里（同本文件 `:1561` 记的那条「行号当标识」的固有代价）。三份生成物本轮归整合轮统一重跑、各轨不碰，故绕开而不是重跑 |
| 2026-09-02 | P9 | **四个域 `_common.py` 里 `invocation_id_of` 的第二条分支不是死代码** —— 「调用方经 extras 传入，传不到则本地生成」两条分支都还在，合并 invoker 之后走的是**第一条** | 单测直调 skill（不经 `SkillInvoker`）走的正是第二条，删掉它这类测试当场炸在 `guard._require_invocation_id`。已由 `maos/tests/test_skill_invocation_anchor.py` 对四个域各钉一条参数化用例，只读地断言两条分支都在 | **不要清理**。顺带记一笔：四个域 `_common.py` 的模块 docstring 第 2 条、以及 `maos/agents/*/_base.py::extras_of` 的注释，都还写着「invoker 那个 id 到不了 skill 里（invoker.py:69）」—— 这句自 `1ac85b3` 起已不成立，是本轮派单误判的源头。本轨不改（那五个文件分别归 T77 与引擎侧），留给持有它们的轨顺手刷 |
| 2026-09-02 | P9 | **`contract.py` 里 `SkillResult` 的 actor 溯源承诺现在有测试钉住了**，但「后续 Phase 的权威事实守卫」仍**没有真的用这个 id 对账** | `scripts/verify.py` 第 3 项 authoritative-fact 今天按 `plan_id` / 案子 / skill 名对齐，不是按 `invocation_id` 直接连表。所以「三处同值」目前只被单测守着，证据侧还没有一条判据会在它断掉时变红 —— 第 1 项 hash-integrity 守的是另一件事（同一份证据里 id 不许重复） | 归后续轨。真要接就是在第 3 项里把 `payment_observation.actor_invocation_id` 与 `SkillInvoked.detail.invocation_id` 直接对上，届时本轨这几条单测正好是它的前置保证 |
| 2026-09-02 | P9 | **派单 §5.1 约束 1（改成 `setdefault`、不覆盖调用方）实测会打红 `scripts/verify.py` 第 1 项**，本轨照实况没做 | 实跑取证：改成「调用方给了就用调用方的」之后 `hash-integrity 87/93`，scenario-6 / scenario-7 / scenario-R5 各出现「invocation_id 与上一条重复」，共 6 处。根因是调用方**允许**把同一个 `extras` dict 复用给相邻两次 invoke（`maos/agents/refund/payment_agent.py` 的 execute → observe 就是这么写的），`setdefault` 会让第二次捡起第一次留下的 id | **已了结**：`invoker.py` 里那段回归守卫注释与 `test_two_invocations_sharing_one_extras_dict_do_not_collide` 一起把它钉住。要留住调用方自己的标识，正确做法是另起键名，不是放宽这里。决策已记 `docs/DECISIONS.md ## task-T79` |
## task-T74（政策判定器：证据与条件成为真判据）

2026-09-02。本轨把 `policy.match` 的条件判据补上了，**但只补到 `eligibility` 为止**——
金额面归 T75。下面五条是本轨结清的、留下的、以及交给别人的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **`## task-D2` 那条（本文件 `:185`）已了结一半**：`no_reason_days` / `warranty_basis` / `min_evidence_count` / `requires_evidence_kinds` 四条判据现在都有判定器（`policy.py::evaluate_conditions`），R3 那组租户对照跑得出差异了（`test_refund_policy_eligibility.py::test_tenant_window_difference_makes_exactly_one_side_ineffective`） | **剩下的一半是金额**：判定结果只落在新出参 `eligibility` 里，`finance.settle` 还没读它，所以 R3 两侧的**金额仍然相同**。那条账在 T75 合并前不能划掉 | T75（`finance.settle` 消费 `ineffective_rules`）。两轨合完再回来划 `:185` |
| 2026-09-02 | P9 | **三个证据判据字段（`min_evidence_count` / `requires_evidence_kinds` / `evidence_source`）零消费方这条已结清**：本轨之前 `grep -rn` 在 `maos/**/*.py` 里零命中，现在 `policy.py` 有判定器且有 13 条测试守着 | 「同一个案子交一张图和不交，裁定结论逐字节相同」这条静默失效在 `policy.match` 这一侧消失了 —— 出参里的 `eligibility.evidence_seen` 与 `ineffective_rules` 会变 | 已结。**注意派单里把这条记成 `docs/BACKLOG.md:1642`，实际那一行是 MCP 节的「三处绕过 invoke_tool 的裸调用」**，内容对得上的是 `:185`。行号失真，按内容认 |
| 2026-09-02 | P9 | **中间态：举证不足只影响 `eligibility`，不影响金额**。`finance.settle` 的 `_params_of` 仍只读 `refund_ratio` / `deduct_fee`，不看 `ineffective_rules` | 在 T74 与 T75 合并之前，**端到端还没生效**：演示时交一张图与不交，`decision` 与最终退款金额仍然相同，只有 `policy.match` 的出参不同了。整合轮不要把本轨当成「已端到端生效」 | T75。这一条是本轮的已知中间态，不是缺陷 |
| 2026-09-02 | P9 | **`docs/EXECUTION.md:843` 仍写着「AS-003 人为损坏免责 / 需 `customer_evidence` 中有图片证据」**，措辞像是已实现 | 本轨让它**接近**成立了（判据真的在读证据），但那格所在的差异点表描述的是端到端效果，在 T75 合并前仍然偏乐观 | **归 T78**（`docs/EXECUTION.md` 是它的独占文件）。本轨只读未改 |
| 2026-09-02 | P9 | **`unmet[].direction` 目前是个只有一个取值的"枚举"**（恒为 `not_applied`），写死在出参里 | 今天无害，是刻意的：方向必须在出参里显式可读，不能靠下游推断。但一旦将来出现「条件不满足反而要收紧」的规则类型，这个字段需要第二个取值，而**加取值必须先改跨轨契约文件**（`review/refund-skill-contracts.md` §1.2 的同款约束） | 出现第二种方向时。别顺手加 —— T75 是按单值写的 |
| 2026-09-02 | P9 | **`gen_docs.py --check` 与 `test_generated_docs.py` 两条当前是红的**：本轨按派单补了 `policy.match` 的 `output_schema.eligibility` 与 `security_boundary`，`docs/skill-catalog.md` 随之对不上代码 | 不是回归，是派单 §0.3 预见到的情况（生成物由整合轮统一重跑 `python3 scripts/gen_docs.py`）。本轨一行没碰三份生成物 | 整合轮。重跑一次即转绿 |
## task-T75（金额核算面：让举证不足真的改变金额）

2026-09-02。本轨只动 `maos/skills/builtin/refund/finance.py` 一个代码文件，
下面两条是**留给整合轮的**，不是本轨没做完的事。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **本轨在政策侧那一轨合并前验不到端到端**：这个 worktree 里 `policy.match` 根本不产 `eligibility`，`finance.settle` 收到的入参恒无此键，走的全是「全部规则均适用」的缺省分支。新测试的 `eligibility` 一律是按跨轨契约 §1.1 自造的 fixture | 剔除逻辑本身有单测钉住，但「政策侧算出的形状与金额侧读的形状是否真对得上」这条**没有任何测试覆盖到** —— 两轨各自绿、合到一起才发作，正是本仓最怕的那类失效 | 整合轮合入政策侧之后**重跑本文件的第 1 / 2 条对照**，并补一条真跑 `policy.match` → `finance.settle` 的端到端：交一张图与不交图，金额必须不同 |
| 2026-09-02 | P9 | `_params_of` 的 `max()` 口径（比例取最大、扣费取最大）本轨**保留未动** | 它有一个不直观的推论：一条 `refund_ratio: "0"` 的排除规则**只有在它是唯一命中规则时**才压得动金额；命中集合里还有别的规则时，它对比例毫无影响，只能靠扣费咬金额。新测试第 1 / 2 条那组对照因此用的是单条 AS-003 的命中集合 | 谁将来想改这个口径，先读 `_params_of` 的 docstring —— 那不是随手定的方向，是「政策对客户的承诺是并集」的直接后果。真要改，连带改的是本文件这两组对照的期望值，别只改代码 |
## task-T76

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **本轨了结了 `2026-09-01` 那条（`gateway.refund` / `gateway.query` 迁 MCP）的「参数」那一半，「传输」那一半仍未做** | 已了结的：两个 ToolPort 不再收 `GatewayPort` 活对象，改收 `gateway_name`（`maos/tools/gateway.py` 自持一张 name → 实例注册表，取不到抛 `LookupError`）；params 从此全是标量，`params_digest` 的可复现性由机制保证，不再押在实现方自觉写 `__repr__` 上，那个补丁也已拆掉。**仍未做的**：传输本身。今天的形状离 MCP 还差三件事 —— ① 装配桥接还在客户端：`payment_execute.py` / `payment_observe.py` 各有一行 `register_tool_gateway(name, C.get_gateway(name))`，把 skill 层登记的实例转登记进工具侧表，跨进程之后这一行**必须搬到 server 侧装配处**，客户端只发名字；② 工具侧注册表是**进程内**的普通 dict，server 侧要换成随进程启动就装配好的形态，且要想清楚多 worker 时各自持有一份账本意味着什么（`MockGateway` 的幂等账本在内存里，两个 server 进程 = 两本账）；③ `AlipaySandboxAdapter` 仍是只抛 `NotImplementedError` 的壳，真网关接通前迁移无从验证 | 归后续支付面轨。**先把那两行桥接搬走，再谈传输** —— 它们是唯一还在跨层传活对象的地方，也是迁移时唯一会「本地跑得通、跨进程当场断」的点 |
| 2026-09-02 | P9 | 承上：§5.1 选了「工具层自持一张表」，**代价是同一个网关要注册两次** | `maos/tools/gateway.py` 与 `maos/skills/builtin/refund/_common.py` 各有一张 name → 实例表。本轨没去改 8 个装配点（`maos/kb/experiment.py:362`、`maos/flows/custom_case.py:239`、`maos/flows/scenario_6.py:267`、`maos/flows/scenario_7.py:468/470/598` 与 4 个存量测试文件都不在本轨白名单），而是让两个 payment skill 在调 `invoke_tool` 前**按调用现场转登记一次**。好处有二：装配点一行没动；且每次调用都刷新工具侧那一格，`reset_gateways()` 之后换了实例也不会读到上一轮的陈账。坏处是「注册」这件事散在调用路径上，不在装配处，读代码时不易一眼看见 | 后续轨若要把装配收回装配处，改这 8 个点即可，两个 skill 里的桥接行随之删掉。别在没搬走桥接前先删注册表 |
| 2026-09-02 | P9 | **`docs/toolport-contract.md` 本轨结束时落后于代码，`maos/tests/test_generated_docs.py` 因此红 2 条** | 差异只有三类，逐条核过：两个 port 的 `params_schema`（`gateway` → `gateway_name`）、`failure_modes` 各多一条 `LookupError`、以及行号锚点漂移。`security_boundary` 等其余字段一字未动。这是 §5.1 改签名的直接产物，不是声明面被误改 | 派单明令该生成物由整合轮统一重跑（口径同 `4bb6694`），本轨一律不碰。**整合轮跑一次 `python3 scripts/gen_docs.py` 即转绿** |
| 2026-09-02 | P9 | 硬判据「`grep "gateway":` 应 0 命中」在本轨实际是 **2 命中**，且这 2 条不该消除 | 两条都是 skill 自己的 `input_schema` 声明（`payment_execute.py:43`、`payment_observe.py:45`），值是「已 register_gateway 的**名字**，默认 demo」这个字符串口径，从来不是活对象。skill payload 这一层的键名叫 `gateway` 是既有约定，`maos/flows/scenario_6.py:150`、`scenario_7.py:269/306`、`custom_case.py:162`、`maos/agents/refund/payment_agent.py:82/90`、`maos/kb/experiment.py:155` 与 `compensate.py:140` 都按这个键名传名字 —— 改它要动 7 个白名单外文件，且与本轨要买的东西无关 | 判据的**意图**（活对象那个键名不复存在）已达成：ToolPort params 里再无 `gateway` 键。后续若要统一改名为 `gateway_name`，那是一次纯改名的独立轨 |
## task-T77（收案面：证据 kind 归一化与供应链审批单字段）

2026-09-02。本轨把 `refund.intake` 落库的证据 `kind` 收敛到五个规范值
（`maos/skills/builtin/refund/_common.py::EVIDENCE_KINDS`），并让审批单能进来。
以下四条是这次的**取舍**，不是遗漏，写在这里免得下一轮当成 bug 重查一遍。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **归一化表只覆盖常见写法**（`_common.py::_KIND_ALIASES`：photo/img/screenshot、mp4/mov、voice/录音、pdf/扫描件 等），真实渠道一定会送来表外的值 | 表外的值归 `attachment`。证据一条都不丢（成员判据仍是有没有 uri），但政策里若写 `requires_evidence_kinds:["image"]`，一张写成「实拍图」的照片仍然数不进 image。这是**已知的覆盖不全**，不是静默失效——`kind_raw` 留得下原值，查得出来 | 接真渠道后按 `event_log` 里 `kind_raw` 的实际分布补表。补表是加别名，不是加规范值——五个规范值是跨轨口径，改它要先改 `review/refund-skill-contracts.md` |
| 2026-09-02 | P9 | **`kind_raw` 不落库**：提交方的原始声明只在 `refund.intake` 的出参与 `event_log` 里，`customer_evidence` 表没有这一列 | 库里查不到「这条证据当初声明的是什么」——只查得到归一化后的规范值。要复原原始声明，得从 `event_log` 里那次 `SkillInvoked` 的 output 捞 | 本轮红线是退款域 14 张表一列不改，所以只能这么放。哪天证据面真要审计原始声明（比如渠道扯皮「我明明报的是 image」），再给 `customer_evidence` 加一列 `kind_raw`，那是**加列**不是改列，届时一并把历史行回填成空 |
| 2026-09-02 | P9 | **`applicant_ref` 走 `business_ref` 是不扩表的权宜**：供应链退款的审批单（supplier_id / po_no / approver / approved_amount / doc_no）只落成一条 `object_type="applicant_ref"`、`object_id=doc_no` 的引用，五个字段本身一个都没有自己的列 | 单号之外的字段库里查不到（`purpose` 那句文字里带了供应商/采购单/审批人，但那是给人看的说明，不是可查询的列）。按供应商统计、按采购单反查退款，今天都做不了 | 供应链退款真要做深，它该有自己的域（口径同 `ap` / `claim`：自己的表、自己的 guard、自己的 skill），不是往退款域塞列。塞列会把「消费者售后」与「供应链退款」两套完全不同的业务对象压进同一张表，然后一半的列永远为空 |
| 2026-09-02 | P9 | **`docs/skill-catalog.md` 落后于代码**：本轨改了 `RefundIntakeSkill.contract` 的 `input_schema` / `output_schema` / `security_boundary` 三个**字段值**（`SkillContract` 的 dataclass 字段一个没动），生成物随之陈旧 | `python3 scripts/gen_docs.py --check` 退出码 1，连带 `maos/tests/test_generated_docs.py` 两条变红：`test_generated_doc_matches_code[docs/skill-catalog.md]` 与 `test_check_mode_agrees_and_writes_nothing`。差异只有 `refund.intake` 一条目（三个字段值 + 实现行号 59→110），别的 skill 一个字没变 | **交整合轮统一重跑 `python3 scripts/gen_docs.py`**，本轨不碰生成物（`docs/skill-catalog.md` 是六轨都不许碰的文件，见 `review/refund-skill-contracts.md` §4；口径同 `4bb6694` 那次） |

## integrate-p9-t74-t79（整合轮：六轨合并、生成物重跑、接缝守卫、证据束重跑）

合并顺序按跨轨契约 §6：T78 → T79 → T74 → T75 → T76 → T77。
六轨的代码面零冲突（白名单确实不相交），全部冲突集中在两份账本的尾部追加，
按「两侧全留、按合并顺序拼接」解。合并态实测 1662 passed / 39 skipped、
`run.py` exit=0、`gen_docs --check` exit=0、`verify.py` 8/8 PASS。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | 🔴 **两轨之间的接缝无人看守**：T74 只验 `policy.match` **产出** `eligibility`，T75 只验 `finance.settle` **消费**（自造 fixture 入参，不跑 policy.match）。没有任何一条测试把真实出参喂进下游 | 形状分叉、方向写反、键名写错，**两轨的测试都不会红**。这一轮买的那句话（「交一张图和不交，金额必须不同」）在合并前从未被端到端证明过 | **本轮已补**：`maos/tests/test_refund_evidence_end_to_end.py`，6 条，拿真出参跑。实测 800.00（没图，排除规则不予适用）vs 0（有图，排除规则生效）。**这类接缝测试应当成为并行分轨的固定收尾**：凡是「A 轨产出、B 轨消费」的跨轨契约，整合轮都要补一条真链路断言，否则契约只被两份 fixture 各自守着 |
| 2026-09-02 | P9 | **派单 T76 的白名单漏列了存量测试 `maos/tests/test_gateway.py`** | T76 改了 `GATEWAY_REFUND_PORT` / `GATEWAY_QUERY_PORT` 的入参（活对象 → `gateway_name`），存量测试**必然**要跟着改，但那个文件不在它 §4 的独占清单里。T76 照实改了并记了账 —— 属合理越界，不是偷跑 | **是派单的缺陷不是执行的问题**。下次写「改某个 ToolPort / 公共签名」的派单时，要把**该签名的存量测试**一并列进白名单；不列的话执行方要么越界、要么交一个红的树 |
| 2026-09-02 | P9 | **`finance.settle` 的 `applied_rules` / `excluded_rules` 落在 `breakdown` 里，不在出参顶层** | 跨轨契约 §1.1 只冻结了 `eligibility` 的形状，没规定 finance 侧留痕放哪 —— 所以这不是违约，是实现自由。但整合轮写接缝测试时按顶层取，当场红了一条，查了两轮才定位 | 放 `breakdown` 是**对的**（它随 `finance_entry.breakdown_json` 一起落库，对账查得到；放顶层反而不落库）。要记的是：**跨轨契约只冻结了上游的出参形状，没冻结下游的**，下一轮若有第三方要读 finance 的留痕，得先把这个位置也写进契约 |
| 2026-09-02 | P9 | **`evidence/scenario-*/trace.json` 里没有 `decision` 字段**，契约 §1.3 那条「decision 必须与基线逐字节一致」的判据在 trace 上验不了 | trace 存的是 span 结构，不含 skill output 明细。整合轮改用**等价判据**验证：三个退款场景的 `rule_refs` 逐字节一致、`amount_approved` 全部未变（金额不变 = 裁定结果不变）。结论成立，但验的路径与契约写的不是一条 | 下次写这类判据时先确认它在证据里查得到。要让 `decision` 真的可外部核验，得让 `make_evidence` 把 skill output 的关键字段落进 `business-objects.json`，那是证据束轨的活 |
| 2026-09-02 | P9 | **`scripts/verify.py` 第 3 项 authoritative-fact 仍只认退款域**（承接本文件 `:1473` 那条，本轮未动） | 本轮六轨全在退款域，所以第 3 项 3/3 PASS 是**真的**核验到了。但 ap / claim / investigation 三个域仍在它视野外，那条老账没有因为本轮而变好 | 仍归证据束轨。本轮新增的 `eligibility` 也没有进第 3 项的对账口径 —— 举证判据是否被绕过，外部核验目前看不见 |
## task-T80（域存储骨架下沉时看到、本轮不改的三条）

2026-09-02 把四个域同构的存储骨架下沉成 `maos/domain/_case_store.py`、三个陪跑域接过去时记的。
基线 `b35c618`，跨轨契约见本轮那份 `domain-slim-contracts.md`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P8 | **本文件 `## orchestration-p3` 第 1 条（2026-08-28 那行）已过期** —— 那条写着「v4 手册 P1 第 7 步的 StorePort 抽象从未落地，`maos/store/` 目录不存在，`port.py` / `sqlite_store.py` / `pg_store.py` 三个文件一个都没有」。实测三个文件都在：`maos/store/port.py` 里有 `class StorePort(Protocol)`，`sqlite_store.py` 256 行、`pg_store.py` 438 行 | 那条现在会误导两拨人：一是按它去补 StorePort 的人，会发现要补的东西已经在了；二是 `## task-C` 里 2026-08-28 那条「`objects.py::_conn()` 取 `SqliteStore` 私有属性」的处理时机挂在「StorePort 落地时一并改」上 —— 前提已成立，那条其实**现在就能做**。本轮没做是因为它不是结构性下沉，是换依赖面，属铁律 4 的范围外优化 | **只在本节记这一条更正，不改 `## orchestration-p3` 那条旧记录** —— 旧记录是 2026-08-28 当时的真实判断，改了就看不出演进。下一轮做 `_conn` / `lock_of` 换 StorePort 时，改的是 `_case_store.py` 一处（三个域跟着走），比下沉前要改四处便宜 |
| 2026-09-02 | P8 | **`maos/domain/refund/objects.py` 仍是自己那份骨架副本，未接 `_case_store`** | 本轮终态是**中间态**：三个陪跑域接了骨架，refund 没接。不是漏了 —— 本轮它归同期 T74–T79 的只读面（跨轨契约 §4），改它两轮合并必冲突，而冲突点在存储地基上，解起来比在 skill 里解贵得多 | **归两轮之后的整合轮**。接入判据：接完之后 `_guarded` 的表名报错文案里仍是 `refund_case`，`test_refund_migration.py` 那组一条不减地绿。那组是四个域里唯一把迁移机制**整套演过一遍**的（`objects._atomic` / `_has_column` / `_SCHEMA_PATH` / `_MIGRATIONS` / `REFUND_SCHEMA_VERSION` 全被它直接点名调用，还自己临时造迁移步骤验记账），所以它同时是骨架的最强回归守卫 —— 接的时候先跑那组再说别的。四个域的 `_MIGRATIONS` 目前都是空的 |
| 2026-09-02 | P8 | **`maos/tools/ap_codes.py` / `claim_codes.py` / `investigation_codes.py`（合计约 2902 行的一半）判为「不可抽」** | 它们看起来像三份复制粘贴，实际是**真实规范编号表**：ap 那份是 Peppol BIS Billing 3.0 / EN 16931 的 `BR-xx` 系列，另两域同理各挂各的行业规范。抽公共骨架没有意义，删了就没法「拿编号去查规范查得到」—— 而那条可追溯性正是这三份文件唯一的价值 | **不处理，本条只为存档理由**。记在这里是为了免得下一轮又有人去数那 2902 行、再判一次。真要动的话先回答一个问题：抽完之后，评委拿着 `BR-CO-13` 还查不查得到它出自哪份规范 |
## task-T81（案件守卫骨架下沉，2026-09-02）

本轨把 ap / claim / investigation 三个域同构的案件守卫控制流下沉成
`maos/domain/_case_guard.py`。**纯结构性改动，行为一步没变**（1584 passed / 39 skipped
不变，`run.py` 仍 exit=0）。下面是过程中撞到、按铁律 4 **不当场改**的，
以及本轮终态是**中间态**这件事本身。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P8 | **退款域的 `guard.py` 仍是自己那份骨架副本，未接 `_case_guard`**。不是漏了 —— 本轮它归 T74–T79 只读面（跨轨契约 §4），本轮四轨若去改它，两轮合并时必冲突，而冲突点在守卫这种地基上，解起来比在 skill 里解贵得多 | 现在四个域里三个共用一份控制流、一个自己一份。整合轮如果不知道这件事，会误以为四个域已经统一，于是给骨架加一道闸却只保护了三个域 —— 而退款域是唯一跑通端到端场景的那个域，漏掉它就是漏掉了演示路径 | 两轮之后的整合轮。**接入判据**：接完之后越权写 `settled` 的报错原文里仍然是 `refund_case` 这个表名（而不是「本域的写入口径」这种看不出是哪张表的通用话），且 `test_refund_flow.py` / `test_refund_domain.py` 一条不红 |
| 2026-09-02 | P8 | **本轨没有 import T80 的 `_case_store`**（跨轨契约 §5：并行轨之间只对形状负责，不对代码负责）。`_case_guard.py` 里的 `_CaseStore` 是按契约 §1.1 的方法名自己写的一份**临时薄封装**，只包了 `query` / `lock_of` / `conn` 三个 | 这是**临时的两层**，不是有意设计。整合轮如果把它当成刻意的抽象层保留下来，以后每加一个存储原语都要在两处各写一遍 | 整合轮，与上一条同批。换掉时注意：`conn()` **不在契约 §1.1 的方法清单里**，但守卫离不开它 —— 「观察与状态更新同事务」这条要求必须拿到裸连接才做得到（`execute()` 是一句一提交）。要么给 §1.1 补这一条，要么给 `CaseStore` 加一个「同事务多写」的原语。本轨不替它定 |
| 2026-09-02 | P8 | **三个域的审计行 `detail` 里 `domain` 这个键三种写法**：ap 三条审计行都有且排在最前，claim 只有违规行有且排在 `invocation_id` **之后**，investigation 压根没有 | 骨架用 `conflict_detail_domain` / `violation_detail_domain` / `violation_detail_domain_first` / `event_detail_domain` 四个旋钮**照抄这份历史不一致**，不当场统一 —— 统一会改掉审计行的形状，那是行为变更（红线 R1），`scripts/verify.py` 那边按字段读得到。代价是骨架多了四个只为兼容而存在的参数 | 想统一的话要单独一轨：先确认 `scripts/verify.py` 与 evidence 束里没有按位置读 detail 的地方，再一次性改三个域并重跑证据束。不要顺手做 |
| 2026-09-02 | P8 | **第 ④ 道闸（「这份证据说的是不是这件事」）没进骨架**，由各域给一个 `check_evidence`。ap / claim 共用 `make_receipt_state_gate()`（只看 `observed_state`），investigation 自己一份（还要看报文族 / 退回金额 / 退回原因码） | 判对了，但代价是「加一个权威终态必须同时配判据」这条 fail-closed 姿态现在**有两份实现**，两份各自都有测试钉着（`test_missing_receipt_criterion_is_fail_closed` / `test_unconfigured_authoritative_state_is_fail_closed`），但没有一条测试断言「所有域的 ④ 道都 fail-closed」 | 加第四个域时。届时如果新域的 ④ 又是「只看状态」那一种，说明这两份该合成一份可组合的判据链；如果又是一种新结构，说明现在这个分法是对的 |
## task-T82（Agent 薄壳骨架三份合一，2026-09-02）

本轨把应付账款 / 理赔 / 差错处理三个域的 `extras_of` / `artifact` / `failed` 下沉到
`maos/agents/_domain_base.py`。以下是撞到、按铁律 4 与本轮契约 §R5 **不当场改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P8 | **退款域 Agent 层未接骨架**：`maos/agents/refund/_base.py` 仍是自己那份副本，没有转出 `_domain_base`。不是漏了 —— 本轮它归同期 T74–T79 的只读面，两轮若都改它，合并必冲突 | 终态是**中间态**：三个陪跑域接了骨架，退款域没接。整合轮若不看这一条，会误以为四个域已经统一 | 两轮之后的整合轮。接入判据：接完之后 `maos/agents/refund/*_agent.py` 的 `from ._base import artifact, extras_of, failed` 一个字不用改，且 `ALL_REFUND_KINDS` 仍留在退款域自己的 `_base.py` 里 |
| 2026-09-02 | P8 | **退款域那份 docstring 是第三种写法**，不是本轮合并的两种之一：它在两处带了硬行号（`invoker.py:69`、`gate.py:180/218`、`gate.py:37`），并把 `invocation_id` 的用途写成「本轨补的」。合并后的 `_domain_base` 刻意不带行号 —— 行号会随别轨改动静默失真 | 整合轮接入时若**直接覆盖**，会连同「这两个 Gate 判据在 gate.py 的哪一段」一起丢；若照抄行号，则把一批必然过期的引用带进共用件 | 整合轮接入前逐条核对，不要直接覆盖。行号那部分建议只留判据名（`_gate_evidence` / `_acceptance_by_self_check`），不留行号 |
| 2026-09-02 | P8 | **`agents/coding.py` / `architecture.py` / `requirement.py` 的非测试引用 grep 结果为 0，但它们不是死代码**（本轨实测：三个模块名与 `*Agent` 类名在 `maos/` `scripts/` `run.py` 里去掉测试后各 0 处命中） | 真实的注册路径是 `maos/agents/__init__.py` 扫包 + `@register` 让它们进 `AGENT_POOL`，Worker 再按 role 字符串动态实例化（`maos/runtime/worker.py:34` 那行 `for role, cls in AGENT_POOL.items()`）。**谁按 grep 结果去删它们，场景 1/2/5 当场炸**，且删之前静态检查一句话都不会说 | 记着别删。下次做「死代码清理」类的活时，`maos/agents/**` 一律不许按 grep 判 —— 判据是 `AGENT_POOL` 的条数（当前 22），不是引用数 |
| 2026-09-02 | P8 | **`maos/agents/claim/_base.py` 的 `RECEIPT_FIELD` 注释指向一个不存在的测试**：本该在 tests 目录下的 `test_claim_gate_isolation.py` 在 git 历史里从未出现过（实测 `git log` 查该路径 0 条）。名字最接近的真实文件是 `maos/tests/test_claim_isolation.py`，但它守的是 import 边界，不是第七道闸的码表边界 | 那条 🔴 边界（X12 的 CARC 不许拿支付宝码表去查）**当前没有回归测试守着**，而注释宣称有。本轨只做结构性下沉，原样保留了这句话，没有当场改注释也没有补测试 | 补一条真测试，然后把注释指向它。补之前不要只改注释文字 —— 改成指向 `test_claim_isolation.py` 会让这条边界看起来更像有人守，实际仍然没有 |
| 2026-09-02 | P8 | **各域 `skills/builtin/<域>/_common.py` 第 2 条说的「`SkillInvoker` 生成的 id 进不到 skill 里」与现状不符**：`maos/skills/invoker.py` 已经把它塞进 `SkillContext.extras`（那段注释自己写着「故意覆盖调用方传入的同名键」） | 结论没错（各域「传入则用、传不到则本地生成」的口径仍然成立、仍然必要），但**理由过期**。下一个读到它的人会以为 extras 里的 `invocation_id` 到不了 skill，从而对本轮合并后的 docstring 第 1 条感到矛盾 | 归动 `maos/skills/**` 的那一轮（本轮全禁）。改的是理由那半句，不是口径 |
## task-T83（受保护路径判定下沉，2026-09-02）

本节记的是**了结**与**没做的边界**，不是新欠账。

### 折账：`## task-B` 第 1 条（依赖方向反了）—— 本轨了结

2026-08-28 记的那条「`PROTECTED_SEGMENTS` / `_path_segments` 住在 skills 层，
tools 层要用只能延迟 import 绕环」已做完，了结到这个程度：

- 判定**只剩一处**：`maos/tools/paths.py`。`PROTECTED_SEGMENTS`、`unquote_c_style`、
  `_path_segments` 三个名字全在那里，skills 与 tools 都从那里取。
- **环已消**：`maos/tools/sandbox.py` 不再 import `maos.skills` 的任何东西，
  改成文件顶部的模块级 import。原先那两个函数内延迟 import 的壳
  （`_protected_path_rules` / `_unquote_c_style`）**已删**，不是留着不用。
- **没留第二个入口**：`code_repo_patch.py` 不转出这些名字，只 import 用。
  `maos/tests/test_protected_paths_single_source.py` 扫全仓源码，
  `PROTECTED_SEGMENTS` 的赋值出现第二次就红。

### 本轨**没碰** `scripts/guard_bash.py` —— 那是另一套东西

`scripts/guard_bash.py` 里也有一份「受保护路径」，名字像，但它是 **Bash 侧守卫**：
挡的是会话自己去改冻结契约面（`contracts/**`、`store.py`、`artifacts.py`、
`.contracts.lock` 等），判据、清单、触发时机与本轨这套**补丁路径判定**（挡的是
模型产出的补丁写进 `infra` / `.github` / `secrets` / `tests`）没有一处重叠。

**所以「受保护路径判定已经全仓统一到一处」是错的** —— 统一的只是补丁路径那一套。
这两套本来就该分开：一个管人/会话，一个管模型产出，合并只会让两边的清单互相污染。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P8 | `maos/tests/test_sandbox_isolation.py:199` / `:233` 的注释仍写「复用 code_repo_patch 的 `PROTECTED_SEGMENTS`」，判定已搬到 `maos/tools/paths.py` | 只是注释，不影响判定；但下一个照注释去 `code_repo_patch` 找定义的人会扑空 | 该文件不在本轨白名单（铁律 4，没当场改）。谁下次动那个文件顺手改掉即可 |

## integrate-p9-t80-t83（四轨整合轮 · 域骨架下沉）

T80/T81/T82/T83 四轨并入 `integrate/p9-t80-t83`，基线 `b35c618`，跨轨契约见
`review/domain-slim-contracts.md`（该目录走 `.git/info/exclude`，按惯例不入库）。
本轮只做合并、审查与收口，不做手册范围外的改动。

合并态实测（编排侧在 `.worktrees/integrate-p9-slim` 当场跑，不是转述）：

```text
python3 -m pytest maos/tests -q   →  1631 passed, 39 skipped   exit=0   （基线 1584/39，+47 条新测试，零回归）
python3 run.py                    →  exit=0，末行「全部场景通过：…」
python3 scripts/check_docs.py     →  阻断 0 / 提示 37          exit=0   （基线提示 38）
python3 scripts/gen_docs.py --check →  3 份文档与代码逐字节一致  exit=0
```

代码零冲突，四轨只在 `docs/BACKLOG.md` / `docs/DECISIONS.md` 尾部追加处撞车，
双方内容全保留。冻结契约一行没碰，T74–T79 那轮的持有面零越界。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P8 | **本轮生产代码净增 157 行，不是净减**（`maos/domain` + `maos/agents` + `maos/tools` + `maos/skills` 合计 +1676 / −1519）。骨架四个文件共 998 行，三域削掉 733 行、依赖方向纠正削掉 108 行 | 本轮真正买到的是「重复判定从 4 份收敛到 1 份 + tools→skills 依赖环消除 + 47 条测试守着行为不变」，**不是行数瘦身**。编排侧当初按 diff 同构度估的压缩空间偏乐观，记在这里是为了让后面的人不要拿「行数」当这类下沉轨的验收判据 —— 判据应当是「判定还剩几处」 | 不必处理，属口径备查 |
| 2026-09-02 | P8 | **T80 与 T81 的削减比例分化，根因在跨轨契约不在执行**。`objects.py` 三域 338→199 / 354→200 / 274→179（削 41–45%，净减 80 行）；`guard.py` 三域 493→408 / 410→320 / 600→527（削 12–17%，净增 242 行） | guard 层削不动是三条契约约束叠加的结果：①报错文案必须保住具体表名（→ 每域一张 `CaseGuardTexts`）②对外 import 路径不许变（→ 每域一层转出包装）③各域异常类型独立（`ApCaseIdentityConflict` 与 `ClaimCaseIdentityConflict` 是调用方要分开 catch 的两类）。三条各自都对，叠一起就让每个域必须留一套装配代码。**T81 是照契约做的，不是没做到位** | 下一轮若要继续收敛 guard 层，得先决定放弃上面三条里的哪一条 —— 不放弃就到此为止了。放弃任何一条都要先想清楚代价：①丢了排查时看不出是哪张表 ②动 import 路径要改全部调用方 ③合并异常类型会让调用方分不清是哪个域炸的 |
| 2026-09-02 | P8 | **T83 有三处白名单外改动，经复核判「被迫且正确」**：`scripts/check_docs.py`（删 `ALLOW_MISSING` 里那条 `maos/tools/paths.py` 尚未建的豁免）、`docs/skill-catalog.md`、`docs/toolport-contract.md`（纯行号漂移，`code_repo_patch.py:153→62`、`sandbox.py:723→706`） | 两处都是连锁：`paths.py` 一旦建出来，那条豁免就成死条目、`test_allow_missing_has_no_dead_entries` 变红；代码搬走后声明行号上移，`gen_docs.py --check` 变红。**是派单漏了预警**，不是子会话越界 —— T83 派单的 §4 白名单没列这三个文件，§0 也没像别的派单那样预警「本轨会让 gen_docs --check 变红」 | 已消解（本轮合并态四道门禁全绿）。**记在这里是给派单模板用的**：凡是新建文件或搬动带行号声明的代码，白名单里要预留 `scripts/check_docs.py` 与两份生成物文档 |
| 2026-09-02 | P8 | **`scripts/demo_preflight.sh` 的 `EXPECT_TESTS_NOPG=1476` 过期 155 条**（合并态实测 1631） | **不是本轮造成的** —— 基线 `b35c618` 上就已经是 1584 vs 1476。`## integrate-p8-t47-t53` 第 1 条把它刷到 1456 之后，主干又涨了两轮 | **不在本轮刷**。T74–T79 那轮（退款 skill 重定制六轨）尚未并轨，条数还会再变一次，现在刷会立刻再过期。按 `## task-T51` 立的规程：由**两轮都并完之后**的那一次按合并态实跑一次改成实测值 |
| 2026-09-02 | P8 | **退款域三层都没接骨架**（T80/T81/T82 各自记过一条，此处汇总）：`maos/domain/refund/objects.py` 未接 `_case_store`、`maos/domain/refund/guard.py` 未接 `_case_guard`、`maos/agents/refund/_base.py` 未接 `_domain_base` | 本轮终态是**中间态**：三个陪跑域接了骨架，refund 保持原样。不是漏了 —— 本轮它归 T74–T79 只读面（跨轨契约 §4），四轨若去改它，两轮合并时会在存储骨架这种地基上冲突 | **两轮都并完之后的整合轮接入**。接入判据三条：①`_guarded` 的表名报错文案里仍是 `refund_case` ②`agents/refund/_base.py` 的 docstring 是**第三种写法**，要与 `_domain_base` 里那两种用途一并核对，别直接覆盖 ③接完 `python3 run.py` 的场景 6/7 仍 exit=0 |
| 2026-09-02 | P8 | **`_case_guard.py` 里的 `_CaseStore` 是按契约 §5 自写的薄封装，没有 import T80 的 `_case_store`** | 跨轨契约明写「并行轨之间只对形状负责，不对代码负责」，所以这是有意的中间态，不是重复实现漏了合 | **下一轮整合时把这层薄封装换掉**，让 `_case_guard` 直接用 `_case_store`。换的时候要确认 `query` / `lock_of` / `conn` 三个方法的语义仍然对得上 |
| 2026-09-02 | P8 | **合回 `goai-restructure` 当前被挡**：主工作区有 12 处未提交，其中 `docs/BACKLOG.md`、`docs/DECISIONS.md` 是 `M`，属 ingress 轨（另一会话）的在制品，与本轮改的是同两份账本 | 直接 merge 必在这两份账本上撞。与 `## integrate-p8-t47-t53` 第 3 条是同一类情形（那次是 MCP 轨的 83 处在制品挡住快进） | 人类裁决三选一：①等 ingress 轨提交那两份账本 ②由人类先 stash ③本整合分支先挂着。**编排侧建议第 ①/③ 条**：本分支已自成一体且四门禁全绿，等 T74–T79 也并完再一次性合回并统一刷条数门禁，比现在合回少解一次账本冲突 |
## task-T70（供应链付款的表格入口）

2026-09-02 把一份 Excel 导出的付款数据接到应付账款引擎上时撞到的。引擎
（`maos/flows/scenario_10.py`、`maos/domain/ap/**`）是复用面，本轨一行没改，
下面五条按铁律 4 **不当场改**。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **`fixtures.seed_three_way` 不接 `ordered_at` / `received_at` / `warehouse`**：它统一拿开票日期当三单的读取时刻、仓库写死 `WH-1` | 底账 `ap-ledger.json` 里这三个字段目前**只留档不下传**。今天无害（匹配判据不读它们），但它让「收货比下单早」这类明显错的底账查不出来，而底账看起来是被完整读进去的 | 归引擎轨。改 `seed_three_way` 的形参属复用面改动，要连场景 10 与 `test_ap_*` 一起过一遍 —— 那正是「唯一构造路径」该有的代价 |
| 2026-09-02 | P9 | **申请表没有税种码与发票类型码两列**，入口一律按 `S`（标准税率）+ `380`（商业发票）落库 | 零税率行（UNCL5305 的 `Z`/`E`）与贷记单（`384`）走不了这条入口。`ap.match` 的 BR-CO-17 是**分税种**算的，一张混税种的发票现在只能当成单一税种，税额勾稽会给出一条假的拒付理由 | 要演混税种或红字发票时再加列。加列比改判据便宜 —— 引擎那边本来就是按行读税种码的 |
| 2026-09-02 | P9 | **`scenario_10._tasks` 把 `tenant_id` 与 `po_version` 写死**（前者是场景自己的演示租户，后者恒为 1） | 本轨复用它之后要回头覆盖 `inputs` 里这两个键才能跑自己的底账。覆盖是显式的、有测试钉着（`test_po_version_comes_from_ledger_not_hardcoded_one`），但形状上是在给一个不该写死的常量打补丁 | 归引擎轨：把这两个值提成 `_tasks` 的形参，场景 10 传自己的常量。改动比覆盖小，但它动的是复用面 |
| 2026-09-02 | P9 | **一张发票只对一份收货单**：底账里同一个 `(po_id, version)` 有两份 GR 时入口直接报错，不挑也不合并 | 分批收货 + 分期开票是正常业务，现在走不了。报错优于挑一份（挑等于替人做决定），但这条路今天是断的 | 等真有分批开票的诉求再做。做法不是让入口挑，是让申请表多一列收货单号 —— 那本来就是发票上印着的东西 |
| 2026-09-02 | P9 | **本入口没接进 `run.py` 与 `scripts/verify.py`**，场景 10 本身也不在 `DEFAULT_SCENARIOS` 里 | `python3 run.py` 跑不到应付账款域的任何东西，`verify.py` 也不校验这条入口。今天是有意的（同期三轨都新增 flow，谁改 `maos/main.py` 谁冲突） | 整合轮。接的时候要一并想清楚：自定义数据没有标准答案，`verify.py` 校验的只能是「跑得完 + 落库形状对」，不能是金额 |
## task-T71（发票图片抽取，本轮都不改）

2026-09-02 建 `maos/tools/invoice_extract.py` 时发现的四条。第一条是派单点名要记的，
其余三条是这一轨的边界：**都不是「抽得不够准」，而是「这条通道还缺的部件」**。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **PDF 发票不支持**。业务方手里相当一部分发票是 PDF（电子发票尤其），本模块只收 `.png` / `.jpg` / `.jpeg`，遇到 PDF 明确报错并让人先导出为图片 | 人要多一步手工转换，且转换质量（分辨率、是否分页）没人管。**没有静默降级**：报错文本直接给出下一步，不会被误当成「这张发票抽不出东西」 | 要装依赖（pdf → 位图，`pypdf` 只能取文本层、扫描件取不到），属于「先问人类」的类别，不许顺手装。做的时候连**多页**一起想：一份 PDF 常常是多张发票 |
| 2026-09-02 | P9 | **抽取调用没有 `ToolInvoked` 审计行**。`extract_invoice()` 是纯函数，不走 `invoke_tool()`，所以「哪张图、什么时候、用哪个模型抽的」只在日志里，不在库里 | 今天可接受（结果必须经人复核才进付款，图片路径也留在 `source` 里）。但出事之后要回答「这个字段当初是谁抽的、模型是哪一版」，只能翻日志 —— 而实验版模型换版本不会有任何记录 | 与 T72 的待复核表一起做：要么走 `invoke_tool` 补审计行，要么把 `model` 与抽取时间写进待复核表的列里。**别默默留着** |
| 2026-09-02 | P9 | **一次只抽一张图，没有批量入口**。一叠发票要靠调用方自己循环，失败一张之后怎么办（跳过 / 中止 / 重试）没有约定 | 演示够用。真用起来「20 张图抽到第 7 张网关 502」会很难受：前 6 张的结果在内存里，没人接 | 归 T72 那条链路 —— 批量的失败语义属于「入口」的事，不属于「抽一张图」的工具 |
| 2026-09-02 | P9 | **没有一条真图片回归**。全部 40 条用例喂的是假 HTTP 响应，`deepseek-v4-flash-vision-exp` 真实返回的形状（是否加围栏、键名是否照抄、中文字段是否被翻译成英文）**没有被任何测试覆盖** | 解析层对畸形返回已经足够宽容（非 JSON、缺键、`lines` 不是数组都不抛），所以最坏情况是「抽不到」而不是「抽错」。但「实验版模型换一版之后返回形状变了」不会有任何东西变红 | 拿到真发票图片之后补一条**手工**核对记录（不进 pytest —— 那会打真网络）。模型是 `exp` 版，形状本来就会漂，指望自动化钉住它是错的方向 |
## task-T72（待复核关口，本轮都不改）

2026-09-02 做「抽取结果 → 待复核表」时撞到、按铁律 4 不当场改的四条。
前两条是 R1 这道闸门的**另一半**，都不在本轨白名单里。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **R1 的另一半在 T70 侧，本轨验不到**：待复核表把每行都写成 `需人工确认=是`，但「付款入口拒收 `是` 的行」这条断言只能写在 T70 的入口测试里 | 本轨单独跑绿 ≠ 闸门成立。只要 T70 那侧不校验这一列，整条链路就是「有表没闸」，而且两轨各自都绿 | 整合轨。并轨后补一条**跨两轨**的端到端断言：待复核表原样喂给付款入口 → 全行被拒；把某一行改成「否」→ 只有那一行进得去 |
| 2026-09-02 | P9 | **抽取器接上之后 `--extracted` 该怎么办没定**。它现在是 T71 并入前唯一能整条跑通的路径，并入后就多了一条不经过视觉模型的入口 | 留着：可复现的离线演示与回归靠它（也是本轨测试的驱动方式）。但它同时是一条「绕过抽取直接造待复核表」的路 —— 有人拿它伪造证据时，表面上和真跑一模一样 | T71 并入那一轨。要么保留并在摘要里显式打上「本次未经抽取」的字样，要么收进测试专用入口 |
| 2026-09-02 | P9 | **待复核表没有落 evidence**。本轨产出的 CSV 与中文摘要都只在人的机器上，`evidence/**` 不在白名单，没动 | 答辩时「R1 闸门真的关着」只有测试可证，没有一份可截图的真实输出 | 取证轨。落 evidence 要连底账与抽取结果一起固化，否则那份 CSV 复现不出来 |
| 2026-09-02 | P9 | **底账里查不到发票号这件事没法查**：契约 §1.1 的底账只有 suppliers / purchase_orders / goods_receipts 三段，没有发票段，所以「抽出来的发票号」只做了「有没有抽到」的检查，没有与任何外部事实对过 | 重号发票（同一张发票抽两遍、或供应商重复开票）在本层看不出来，要等三单匹配那一步 | 需要时才做。真要在入口层拦重号，得先想清楚「已收发票」的权威事实归谁 —— 按铁律 8 那不是 MAOS 该持有的 |
## task-T73（供应链付款入口的文档与端到端冒烟）

本轨是四轨并行里的第四块（表格入口 / 票据抽取 / 复核关口 / 文档与冒烟），
落盘时前三块**都还没并进来**。下面五条全部因此而起，整合轮必须逐条回收。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **整合后需补七处路径引用**。文档与 README 里凡是指向另三轨产出的地方，现在一律写的是职责名（表格入口 / 票据抽取 / 复核关口）或围栏内占位，**没有一处写真实路径** —— 因为在本 worktree 里那七个文件（表格入口的 flow 与脚本、两份数据文件、抽取工具、复核 flow 与脚本）都不存在，写了 `scripts/check_docs.py` 判据 E 当场判红 | 文档现在**不说假话，但也不指路**：读的人知道有这么一块，不知道跑哪个命令。整合前这是对的，整合后就是缺口 | 三轨并入的那一轮。补齐后重跑 `python3 scripts/check_docs.py`，阻断类必须仍为 0 —— 那时七个路径都真的存在了 |
| 2026-09-02 | P9 | **冒烟脚本调表格入口的命令形状是类比来的，没被验证过**。`scripts/ap_smoke.py` 按退款域 `scripts/run_requests.py` 的形状写成「位置参数=申请表、`--ledger`=底账」，而表格入口那一轨的真实 CLI 本轨看不到 | 形状不一致的话，三块并齐后冒烟会以 **exit 1** 报「跑挂了」—— 而真实原因是参数没对上，不是回归。这正好是本脚本最想避免的那种误报 | 三轨并入的那一轮。**改冒烟脚本去对齐入口，不要反过来改入口** —— 入口的形状归那一轨定 |
| 2026-09-02 | P9 | **`docs/ap-entry.md` 里那张结果表样例是退款域的真实输出，不是供应链付款的**。本轨跑不出后者（表格入口不存在），于是借了前者并在正文写明出处与日期 | 形状是对的（两条通道本来就同形），但列名与内容属于另一个域。读的人可能以为供应链付款也出「裁定 / 核准金额」这几列 | 三轨并入的那一轮。跑一次真实的付款结果表，把样例换成它的原样输出（铁律 3：证据必须来自真实命令） |
| 2026-09-02 | P9 | **冒烟判「跑通」只看子进程退出码 + 两个中文关键词**，没有核库：三单匹配的差异明细、付款计划、`BLOCKED` 落点、观察行条数，一条都没查 | 现在够用（缺块时根本跑不到这一步），但整合后它会变成一条**很容易恒绿**的冒烟 —— 入口只要不抛异常就绿，哪怕结果表整张是空的 | 三轨并入之后紧接着做。判据应改成读库核 `ap_case` 的业务状态与付款观察行，与场景 10 的收口断言同源 |
| 2026-09-02 | P9 | **网关模型清单（三个模型，其中视觉那个带 `-exp` 后缀）是编排侧 2026-09-02 实测，本轨未复跑**。本轨全程零出网，文档里已标注出处 | 「实验版」这三个字是「抽取结果不许直接进付款」这条红线的**唯一事实依据**。清单哪天变了（比如网关上了正式版视觉模型），这条论证的前提就变了，而没有任何东西会提醒 | 答辩前复跑一次并更新文档那句。长期看该有一条脚本把网关模型清单落成证据，而不是靠人记 |
## task-T66（自然语言意图解析层，本轮都不改）

2026-09-02 建 `maos/nlu/` 时发现的三条，都在本轨白名单之外，按铁律 4 只记不改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **跨轨契约 `review/nl-contracts.md` 不在版本库里**，只躺在主仓工作区。四轨都被要求「照抄同一份定义」，但从 worktree 里 `git show` 不出来它 | 今天无害（四轨手里各有一份）。代价在并轨之后：谁都无法回答「当时那份契约到底怎么写的」，字段名一旦有分歧就成了各说各话，而这种冲突**不会有任何红灯** —— 两侧都能各自跑绿 | 整合轨落库。要么入 `docs/`，要么至少提交进 `review/`；答辩要讲「四轨零交集怎么保证」时也要指得到它 |
| 2026-09-02 | P9 | **关键词词表会有两份**：本轨的 `_KW_APPROVE` 等四张表，与 T67 自带的 `_KeywordParser`（跨轨契约 §1.3 明写 T67 不许 import `maos.nlu`） | 并行期间是对的，整合后就是同一件事的两处实现。分叉的症状很温和：房间里「不同意」判成驳回、而某条旁路仍判成同意，两边测试各自全绿 | 整合时按契约 §5 grep `# INTEGRATION-POINT:`，把 T67 那份换成本轨的 `parse_intent` 偏函数并**删掉**它的词表，不要留成兜底的兜底 |
| 2026-09-02 | P9 | **降级客户端认不出来，只能靠 `isinstance(model, ScriptedModelClient)` 判**。`ModelClient` 上没有任何「我是降级来的」标记，`ModelResponse.meta` 里也没有 | 本轨的关键词兜底就挂在这个 isinstance 上。哪天 `select_model_client` 的降级目标换成别的类（`HigressModelClient` 今天还是占位），兜底会**静默不触发** —— 无 key 的机器上一句「同意」直接变 unknown，没有任何报错 | 归动 `maos/model/client.py` 的那一轨（本轨只读，未动）：给 `ModelClient` 加一个 `degraded: bool` 类属性，或让降级路径在 `ModelResponse.meta` 里落一个 `degraded=True`。改完把本轨的 isinstance 换掉 |
## task-T67（房间常驻监听器，本轮都不改）

2026-09-02 建 `hiclaw/room_agent.py` 时撞到的四条。都在本轨白名单之外，一行没动。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **`MirrorChannel.listen` 的回调签名是 `(sender, body)`，不带 `event_id`** —— 真房间里监听器拿不到消息 id | 去重只能退到「`sender` + 正文指纹 + 5s 时间窗」这一档（`room_agent.RoomAgent._is_duplicate`）。它挡得住 sync 重连的连续重放，挡不住「重放隔了 5s 才来」；而窗口不敢调大，调大就会把人隔一会儿再说一遍的**合法第二次发言**一起吃掉。审批是不可逆动作，这个缺口有真实代价 | 要根治得放宽 `MirrorChannel` 协议、让 `_NioChannel.listen` 把 `event.event_id` 一路带下来 —— 那是**冻结参照物** `hiclaw/matrix_bus.py`，本轮不许改。归下一轮真房间轨，与「回调形状」一起改一次，别分两次 |
| 2026-09-02 | P9 | **`HumanApprovalQueue.decide()` 对库里不存在的 task_id 抛的是 `TypeError: 'NoneType' object is not subscriptable`** | 这句会被 `RoomApprovalBridge` 原样贴进房间回执：实跑截到的是「审批未生效：task_997ca4541e66 —— 'NoneType' object is not subscriptable」。演示当天房间里的人看到这句，既不知道是自己打错了 id，也不知道该怎么办 | 归 `maos/runtime/gate.py` 那一轨：`decide` 开头查一次任务，查不到就抛一个说人话的异常（「库里没有这条任务，请核对 task_id」）。不在本轨白名单 |
| 2026-09-02 | P9 | **`known_task_ids` 只能靠 `--plan-id` 显式喂**：`Store` 没有「跨 plan 按状态列任务」的方法，`HumanApprovalQueue.pending()` 又只接受单个 `plan_id` | 常驻监听器上线时并不知道房间里将来会出现哪些 plan。不给 `--plan-id` 时自然语言路径认不出任何 task_id（一律降 UNKNOWN 静默）——**保守是对的**，但可用性上等于自然语言只在「盯着某个 plan」时才活着。显式 `/approve` 不受影响 | 归下一轮：要么给 `Store` 加一个只读的 `list_blocked_tasks()`（新增方法不动现有表结构，不违铁律 1），要么让监听器订阅 `TaskBlocked` 事件自己维护待审集合。后者更贴事件溯源，但要碰 `maos/core/**` |
| 2026-09-02 | P9 | **`_KeywordParser` / `_StubDispatcher` 是并行期替身，整合后必须删掉** | 留着就是第二份解析与派发口径，而两份判据一定会漂；漂了的症状是「同一句话在冒烟里认得出、在房间里认不出」，且不会有任何测试变红 | 整合时（T66/T68 落地后）按两处 `# INTEGRATION-POINT:` 注释替换，**删掉替身类本身**，不要留成「默认实现」。`maos/tests/test_room_agent.py` 里针对替身的那两节（第 2、11 节）跟着删或改喂真实现 |
## task-T68（意图派发与权限闸，本轮都不改）

2026-09-02 做 `maos/runtime/intent_dispatch.py` 时发现的三条。都在本轨白名单外，
按铁律 4 记账不当场改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **两条审批路径在「env 没给名单」时行为不同**：房间侧 `RoomApprovalBridge._effective_approvers` 是 `current_approvers() or self.config.approvers` —— 读到空会**回落到构造时那份快照**；本轨的 `resolve_approvers(env)` 没有这一支，空就是空、一律拒绝 | 今天不出事：真房间跑的时候 `MAOS_APPROVERS` 是配着的，两边给出同一个名单（对照测试覆盖的正是这一段）。但 `MatrixBusConfig.from_env({...})` / `room_demo.py` 降级自检那种「env 没配、config 有快照」的场景下，同一个人走显式指令能批、走自然语言会被拒 —— **症状是「时灵时不灵」，不会报错** | 归整合轨（T66→T67/T68→T69 并轨那一步）。要么给 `dispatch_intent` 的调用方显式传 bridge 那份 effective 名单，要么把回落语义也搬进 `resolve_approvers`。**别让调用方各自决定**，那正是分叉的来源 |
| 2026-09-02 | P9 | **`maos/config/__init__.py` 的配置键登记表只记了一个读取点**：`MAOS_APPROVERS` 那行写的是 `hiclaw/matrix_bus.py::RoomApprovalBridge._effective_approvers`，本轮新增的 `maos/runtime/intent_dispatch.py::resolve_approvers` 没登记 | 那张表是「动这个键会影响谁」的唯一索引，也是安全事件时的排查起点。少一个读取点，排查时会漏掉自然语言这条路径 | `maos/config/**` 是配置审计面（只读，动它属于安全事件），本轨没动。归有权改配置面的那一轨，补一行即可 |
| 2026-09-02 | P9 | **状态查询只认单个任务，没有 plan 级概览**：`dispatch_intent` 的签名（跨轨契约 §1.2）不带 `plan_id`，所以「现在什么情况」这种不带 task_id 的问法只能反问「想查哪个任务」 | 人在房间里最自然的问法恰恰是不带 id 的那种。现在的回答虽然不错，但没解决他的问题 | 归 T69 端到端冒烟之后再定：要加就得改跨轨契约的签名，属于四轨共同口径，**不许单轨自己加参数** |
## task-T69（自然语言接入面的文档、runbook 补坑与冒烟）

本轨只动文档、冒烟脚本与一份文档守卫测试，不碰任何运行时代码。
下面四条是动手过程中撞到、按铁律 4 **不当场改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **整合后需补三处路径引用**。`docs/nl-interface.md` 与 `scripts/nl_smoke.py` 里，意图解析层 / 房间常驻监听器 / 意图派发与权限闸三个模块，文档侧一律只写职责不写路径 —— T66 / T67 / T68 与本轨并行，那三个文件在本轨的 worktree 里一个都不存在，写进反引号会被 `scripts/check_docs.py` 判据 E 当场判红 | 现在文档能自洽，但读的人拿不到指路：想去看解析层长什么样，只能靠猜模块名。冒烟脚本里的模块名是代码字符串，不受文档守卫管，那三处是准的 | 四轨整合完成之后另开一轮补。补的时候顺手核一遍模块名与实际落点是否一致 |
| 2026-09-02 | P9 | **Element 弹窗那一条本轨未复现**，写进 runbook 的逐字原文采信编排侧 2026-09-02 的实测。本轨没有 Synapse / Element 环境（探测 localhost 的命令被权限拦下），且那个弹窗是客户端 GUI 行为，无法用命令行复现 | 文字本身有出处（编排侧实测），但**没有截图**。按钮名一旦在 Element 某个版本里改了措辞，`maos/tests/test_nl_interface_doc.py` 钉的是文档里的字符串，钉不住真实按钮 | 正式取证窗口开真房间时，顺手截一张弹窗图进 `evidence/room/`，并核一次按钮名 |
| 2026-09-02 | P9 | **`scripts/nl_smoke.py` 的端到端分支在本轨没有真跑过**。三个模块都不存在，只能用 scratchpad 里的一次性假模块探针把那条分支走了一遍（含 R1 变异检验：把派发结果改成「已执行」，脚本退 1）。探针不入库 | 装配、打印与 R1 断言的形状是验过的，但**与三轨真实签名的契合没验**。签名若与跨轨契约有出入，第一次真跑才会炸 | 整合后第一件事就是真跑一次这个脚本，期望 exit=0，并把输出留进整合回执 |
| 2026-09-02 | P9 | **冒烟脚本的退出码语义没有机器守卫**。`maos/tests/test_nl_interface_doc.py` 钉的是两份文档的措辞，没有任何测试钉住「未合并 = 3、坏了 = 1」这条约定；`docs/nl-interface.md` §6 把它当结论写着 | 谁把 `EXIT_NOT_MERGED` 改成 1，文档当场说假话而全绿。这正是文档守卫本来要挡的那类腐烂，只是它这次落在脚本侧 | 整合后与上一条一起做：真跑通了再补一条测试，同时钉住三态退出码 |
## task-T55（多 Provider 模型客户端：第二家协议）

本轨新增 `maos/model/providers.py`（Anthropic Messages 客户端 + provider 注册表）
与 38 条离线测试。**只造零件不接线**是派单定的范围，下面五条都是动手时撞到、
按铁律 4 不当场改的。第 1 条是本轮范围裁剪留下的必然缺口，写在这里是为了让整合期
有个挂钩 —— 零件没有消费方这件事不会有任何测试变红。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **`PROVIDERS` 目前零消费方**。`select_model_client()`（`maos/model/client.py:329`）仍然只认 `MAOS_LLM_*` 三件套那一条 OpenAI 兼容路径，没有任何配置面能选中 `"anthropic"` | 新客户端能被 import、能被测试，但**跑不到生产路径上**。而「接不上」不会红任何一条用例：注册表是纯数据，没人读它也照样绿 | T57（构造入口）与整合期。接线时要连带决定「provider 名从哪个 env 读」——本轨刻意没定这个变量名，定了就是替 T57 拍板 |
| 2026-09-01 | P9 | **`HigressModelClient` 不在注册表里**。它是占位类（`complete()` 一进来就 `NotImplementedError`），本轨按派单不动它，于是仓库里有三个客户端类、注册表只两条 | 今天无害。但「注册表 = 全部可用 provider」这条不变量现在只是口头的，没有测试守着类集合与表的对应关系 | 接 Higress 的那一轨。要么补进表、要么在表的 docstring 里写明「占位类不入表」并加一条守卫 |
| 2026-09-01 | P9 | **零重试缺口被复制了一遍**（承接 `## task-T54` 记的第 19 条）。新客户端与 `GatewayModelClient` 一样，一次网络抖动就等于一个任务 failed | 现在是两家都没有，将来做重试要在两处做 —— 或者先把出网那段抽出来共用（但那要改 `client.py`，本轨的只读面） | 做重试的那一轨。抽公共出网层与加重试应当同一轨做完，别先抽后加 |
| 2026-09-01 | P9 | **`stop_reason == "max_tokens"` 的截断没有任何上层处置**。本轨把 `stop_reason` 记进了 `ModelResponse.meta`，但全仓没有一处读它 | 截断发生时正文是**半截 JSON**，下游解析失败会表现成「模型没按格式回」，而真因是 `max_tokens` 给小了。误诊方向完全相反：会有人去改 prompt，而不是调额度 | 与「按角色配 `max_tokens`」一起做（T56 路由表那一侧更自然）。至少要在解析失败的错误文本里带上 `stop_reason` |
| 2026-09-01 | P9 | **`model_usage` / `model_call_failure` 两张表都没有 provider 维度**（`maos/core/store.py:557`、`:595` 的参数表里只有 `model`，没有 provider） | 一家的时候不需要。两家并存之后，账上要靠 `model` 字符串反推是哪家 —— 而那一列的值来自服务端回显，不是我方可控的枚举 | 真正接第二家上生产的那一轮。表结构是冻结面，只能新增表或新增列，要和第 1 条的接线一起设计。注：`usage_is_estimated()` 这一处**不用改** —— 它判的是「是不是 `ScriptedModelClient`」，新客户端天然被判为真实计费 |

## task-T56（角色 → 模型路由表，本轮都不改）

2026-09-01 做路由表时撞到的四条。都在本轨白名单外（`conftest.py`、`model/client.py`、
`runtime/worker.py`、`agents/**`），按铁律 4 记账不动手。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **`maos/tests/conftest.py` 的起跑线不含 `MAOS_LLM_*`**。Matrix 五键与存储两键都有 autouse fixture 清场，模型这一族没有，每个用到它的测试文件各自 `delenv`（`test_model_client_hardening.py::_all_missing`、`test_registry_autodiscovery.py:451`，本轮 `test_model_routing.py` 又造了第三个） | 今天不红：现有文件都自防。但这是「靠每个作者记得」而不是「起跑线自己划」——漏一处的症状是**在配了 key 的机器上才红**，而那正是采集演示证据的那台。路由表落地后变量名从 3 个涨到「3 + 角色数 × 4 + tier 数 × 4」，靠人工写死名单必漏 | 归动 `conftest.py` 的那一轨：加一条按 `MAOS_LLM_` 前缀扫的 autouse fixture（本轮 `test_model_routing.py::_no_ambient_llm_env` 就是它的现成实现，抄过去即可），然后把三处各自的 delenv 收掉 |
| 2026-09-01 | P9 | **`MAOS_LLM_TIMEOUT` 没有进 `RouteSpec`**，超时仍是全局唯一一份（`model/client.py::_timeout_from_env`） | 路由表让不同角色走不同家的模型之后，超时却还是一个值。强模型跑长任务需要 300s，轻量分类角色 300s 等于把一次挂死拖成五分钟 | 归接线那一轨（`client.py` 的持有轨）：要么给 `RouteSpec` 加 `timeout_env`，要么明确写死「超时按 tier 分档，不按路由分」。两条都行，别留成「没人决定过」 |
| 2026-09-01 | P9 | **路由表本轮无人消费**：`routing.resolve()` / `describe()` 写完了，但 `select_model_client()`（A-12 冻结、T57 持有）与 `worker.py` 都还没调它 | 本轨范围就是「只出解析，不接线」，所以这不是缺陷。但它意味着：接线那一轨如果没做，这个模块是**没有任何红灯的死代码** —— 测试全绿，`run.py` 全绿，而 22 个 agent 照旧共用一个全局 client | 接线轨落地后，加一条守卫钉住「`describe()` 报出的 source 与 worker 实际注入的 client 对得上」。在那之前，`describe()` 说的是**配置意图**，不是**运行事实**，读它的人要知道这个区别 |
| 2026-09-01 | P9 | **tier 这一级路由粒度接近失效**：22 个在池角色里 17 个是 `light`（实测 light 17 / medium 2 / strong 3） | `MAOS_LLM_TIER_LIGHT_MODEL` 一配就同时改掉 17 个角色，等于第二个全局开关；真要给某个理赔角色单独换模型，只能退回角色级逐个配。分档本身没错，错的是「新角色默认落 light」这个惯性 | 归下一轮补角色的那一轨：新增 `AgentIdentity` 时把 `model_tier` 当成必须想一想的字段，而不是抄上一个。不建议加第四档（会撞 `client.py::Tier` 的三档口径） |

## task-T57（按角色注入模型客户端时发现，本轮都不改）

2026-09-01 接线「每个 worker 连一个大模型 API」时撞到的四条。按铁律 4 只记不改。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **生成物 `docs/agent-identity.md` 把 `maos/runtime/worker.py` 的 `__init__` 行号写进正文**（本轮 28 -> 39）。任何一轨在 worker.py 顶部加一行 import，`test_generated_docs` 立刻两条变红 | 红灯本身是对的（生成物确实过期了），坏的是**归因**：症状是「文档守卫红了」，原因是「别人加了个 import」，中间隔着一个没人会想到的行号。本轮为此多改了一个白名单外文件 | 做生成器那一轨。行号换成锚点（`AGENT_POOL` 那行的符号名），或者干脆只写文件名不写行号 —— 行号是全仓最容易过期的一种引用 |
| 2026-09-01 | P9 | **`core/store.py::usage_is_estimated` 判的是客户端的 `isinstance`，与「给客户端加包装层」天然互斥**。本轮靠「Scripted 一律不包」绕开 | 今天无害。但下一轮若真想给缺省路径也留路由归属（比如演示时想看「假模型也按角色分了流」），就没有出路了 —— 包了 `estimated` 翻面，不包就没有归属 | 真需要那天再动，且**不要**改成判 `ModelResponse.model` 字符串：两者同源会让核验器第 8 项判据 c 退化成自己跟自己对账 |
| 2026-09-01 | P9 | **provider 归属进不了 `model_usage` 表**。`RoutedModelClient` 把 role/provider/route_source 写进 `ModelResponse.meta`，而 `record_model_usage` 不落 meta（表结构是冻结面，铁律 1，本轮一列都没加） | 「这次调用花的钱是打给哪家的」在成本表里查不到，只能拿 `agent_role` 去关联 event_log 里那条 `ModelRouted`。一次运行里成立（路由是不变量），**一旦支持运行中改路由就不成立了** | 与 BACKLOG 里那条「`estimated` 一个字段扛两种语义」一起做，都要新增表 |
| 2026-09-01 | P9 | **`ModelRouted` 落在 `plan_id` 空串下，进不了 `trace.json`**。Worker 构造在任何 plan 之前，此刻确实没有 plan 可归（编一个更坏），而 `obs/trace.py` 按 plan_id 取 event_log | 路由留痕在库里查得到、在证据束里查不到。演示当天要证明「各角色真的分流了」，得单独开一条查询 | 做可观测那一轨。要么 trace 额外捞一次 `plan_id=""`，要么 Worker 在首次接到派单时补一条带 plan_id 的归属行 |

## task-T58（职责能力档案与声明一致性闸）

本轨只造「一张档案表 + 一台体检机 + 一条会红的测试」，**一处漂移都没修**（铁律 4）。
下面 11 条是 2026-09-01 在 `d386387` 上跑 `maos.capability.profiles.check_consistency()`
实测出来的，整合期照着这张单子裁定。

体检机报 **12 条 finding**（error 5 / warning 7），归并成 **11 条修改项**：
`ap.compensate` 一处同时触发 `owner-role-unknown` 与 `skill-unowned` 两条 finding，
但修法是同一处。四组的口径与派单 §5.1 一致：
甲（白名单放行不存在的工具）2 条、乙（owner_roles 与实际持有者不符）7 条、
丙（depends_tools 指向不存在的 ToolPort）2 条、丁（持有者缺依赖工具）**0 条**。

`maos/capability/profiles.py` 的 `PROFILES` 已经按**实际调用点**写好了应然值，
每条的「建议怎么修」就是把 identity / 契约改成与档案一致；
改完 `maos/tests/test_capability_profiles.py` 的 `BASELINE`、`TOOL_DIFF_BASELINE`
与分组计数会一起变红，那是**提醒把这张单子上对应的行划掉**，不是回归。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **甲-1（error）`coding.allowed_tools` 里的 `sandbox` 全仓没有对应的 ToolPort**。出处 `maos/agents/coding.py:33`。全仓真端口只有 11 个，其中沙箱侧叫 `sandbox.git_apply` / `sandbox.pytest_run`，没有裸的 `sandbox` | 白名单放行了一个不存在的东西 —— `check_tool("sandbox")` 会通过，但拿这个名字到任何地方都取不到端口。与 `maos/tools/mcp/git_tool.py` 文件头点名过的 `git-mcp` 那个洞同源，那处已补，这处还留着 | 整合期。建议改成 `frozenset({"git-mcp"})`：`code_repo_patch.py:197` 唯一的 `invoke_tool` 实参是 `GIT_MCP_PORT`，`coding` 今天并不调沙箱；`sandbox.git_apply` 至今没有任何生产调用方（全仓只有定义处 `maos/tools/sandbox.py:723`），不要顺手把它塞进白名单充数 |
| 2026-09-01 | P9 | **甲-2（error）`testing.allowed_tools` 里的 `sandbox` 同样查不到端口**。出处 `maos/agents/testing.py:165` | 同上。`testing` 是真的要跑沙箱的角色，所以这条一旦有人按 `check_tool` 的结论去接线，会拿着一个错名字接不上 | 整合期。建议改成 `frozenset({"sandbox.pytest_run"})` —— `test_verify.py:68` 调的就是 `PYTEST_RUN_PORT`，实名如此 |
| 2026-09-01 | P9 | **丙-1（error）`code.repo-patch` 的 `depends_tools` 含 `sandbox`**。出处 `maos/skills/builtin/code_repo_patch.py:171` | 契约自述依赖一个不存在的端口。`docs/toolport-contract.md` 这类生成物是照契约产出的，等于把不存在的依赖写进了对外文档 | 与甲-1 同一轨改，建议改成 `["git-mcp"]`。**两处要一起改**：只改 identity 不改契约，丁类（持有者缺依赖工具）会立刻从 0 条变成有条 |
| 2026-09-01 | P9 | **丙-2（error）`test.verify` 的 `depends_tools` 含 `sandbox`**。出处 `maos/skills/builtin/test_verify.py:46` | 同上。这份契约的 `security_boundary` 文案里已经写明「一律经 sandbox.pytest_run 这个 ToolPort」（`test_verify.py:53`），**文案是对的、字段是错的** | 与甲-2 同一轨改，建议改成 `["sandbox.pytest_run"]`，与同文件的文案对齐 |
| 2026-09-01 | P9 | **乙-1（error + warning，两条 finding 一处修法）`ap.compensate` 的 `owner_roles=["ap_compensation"]` 指向一个全仓不存在的角色**，且没有任何角色的 `allowed_skills` 含它。出处 `maos/skills/builtin/ap/compensate.py:97` | 判 error：契约指向落空，装配期照它接线必然接不上，而今天没有任何机制会发现（这正是本轨造这台机器的理由）。同时应付账款域的补偿路径**没有任何角色调得起来** | 整合期，需人类裁定业务口径：应付域的补偿到底该归 `ap_treasury`（它持有 `ap.execute` / `ap.observe`，是唯一碰银行的角色）还是新设角色。对齐参照：`investigation.compensate` 归 `investigation_observe` 且**真的被持有**（`maos/agents/investigation/observe_agent.py`） |
| 2026-09-01 | P9 | **乙-2（warning）`claim.compensate` 有实现但无人持有**。契约自述 `owner_roles=["claim_payment"]`（`maos/skills/builtin/claim/compensate.py:102`），而 `claim_payment.allowed_skills` 只有 `{claim.pay, claim.observe}`（`maos/agents/claim/payment_agent.py:73`） | 判 warning 而非 error：指向的角色是真存在的，接线接得上，问题是白名单**少授权了一项**。后果是理赔域补偿路径调不起来 —— `SkillInvoker` 会按白名单拒掉 | 整合期。建议把 `claim.compensate` 加进 `claim_payment.allowed_skills`（自述已经这么写了，改白名单比改自述更贴业务）。与乙-3、乙-4 是同一个模式，建议同轨一起改 |
| 2026-09-01 | P9 | **乙-3（warning）`refund.compensate` 有实现但无人持有**。自述 `owner_roles=["refund_payment"]`（`maos/skills/builtin/refund/compensate.py:103`），而 `refund_payment.allowed_skills` 只有 `{payment.execute, payment.observe}`（`maos/agents/refund/payment_agent.py:64`） | 同乙-2，退款域补偿路径调不起来 | 整合期，同乙-2 的改法 |
| 2026-09-01 | P9 | **乙-4（warning）`kb.sink` 有实现但无人持有**。自述 `owner_roles=["manager"]`（`maos/skills/builtin/kb_sink.py:50`），而 `manager.allowed_skills` 是 `{req.normalize, kb.retrieve}`（`maos/agents/manager.py:38`） | 知识沉淀这条路今天没有任何 agent 走得通。注意 `maos/runtime/plan_finalizer.py` 的复盘沉淀是**绕开 agent 白名单**直接做的（run.py 输出里那句「复盘完成，沉淀 3 条」），所以现在看不出问题 | 整合期，需裁定：要么给 `manager` 加上 `kb.sink`，要么把自述改成「本 skill 由 plan_finalizer 直接调用，不经 agent 白名单」并在契约里写明。**别只改一边** |
| 2026-09-01 | P9 | **乙-5（warning）`issue.aggregate` 的实际持有者与自述完全不相交**。自述 `owner_roles=["manager"]`（`maos/skills/builtin/issue_aggregate.py:84`），实际持有 `claim_intake`（`maos/agents/claim/intake_agent.py:26`）与 `refund_intake`（`maos/agents/refund/intake_agent.py:35`） | 判 warning：接线接得上，是自述漂了。但这条漂得最厉害 —— 自述指的角色一个都没持有，两个真持有者一个都没写上 | 整合期。建议把自述改成 `["claim_intake", "refund_intake"]`（多源聚合去重本来就是受理侧的活，两个 intake 角色的 duty 都写着「聚合去重」）。**改自述、不改白名单** |
| 2026-09-01 | P9 | **乙-6（warning）`policy.match` 的自述少了一个持有者**。自述 `owner_roles=["refund_policy"]`，实际持有 `refund_policy` + `refund_finance`（`maos/agents/refund/finance_agent.py:32`）。两个版本的契约都要改：`maos/skills/builtin/refund/policy.py:91` 与 `refund/policy_v1_1.py:148` | 判 warning。`refund_finance` 持有它是**刻意的**（duty 写着「自行复核规则」，不接受政策侧的结论口述），所以错的是自述 | 整合期，建议两个版本文件的自述都改成 `["refund_policy", "refund_finance"]`。**别删 `refund_finance` 的授权** —— 那会把「财务自行复核」这条设计砍掉 |
| 2026-09-01 | P9 | **乙-7（warning）`req.normalize` 的自述少了 `requirement`**。自述 `owner_roles=["manager"]`（`maos/skills/builtin/req_normalize.py:69`），实际持有 `manager`（`maos/agents/manager.py:38`）+ `requirement`（`maos/agents/requirement.py:35`） | 判 warning，同乙-6。顺带一条口径提醒：`manager` 有完整 identity 但刻意不进 `AGENT_POOL`（C-2），只按池核对的话这条根本发现不了 —— 本轨的体检机因此按**全仓 23 个 AgentIdentity** 取事实源 | 整合期，建议自述改成 `["manager", "requirement"]` |

## task-T59（MCP server 注册表与按角色挂载，本轮都不改）

2026-09-01 建 `maos/tools/mcp/registry.py` 时发现的四条。注册表消灭了
「server 说的」与「MAOS 认的」这一处分家，但**没有**消灭下面这几处 ——
写在这里免得下一个人以为注册表已经把对账做全了。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **`ToolPort.params_schema` 是自然语言描述，不是 JSON Schema**（`git_tool.py` 里写成 `"op": "str（baseline / ls_files / show_file）"`） | `reconcile()` 的 `schema-drift` 只能判到**键名级**：server 把某个参数从 string 改成 array、或把可选改成必填但键名不变，对账一律看不见。判宽是本轮的刻意选择（见 DECISIONS `## task-T59` 第 2 条），但代价是真的存在 | 要收紧就得把 `params_schema` 换成真 JSON Schema，那要动 `maos/tools/port.py` 与全仓每一个 ToolPort —— 是一轮独立的活，且必须一次改完，不能留半张表 |
| 2026-09-01 | P9 | **`git_tool.py::OPS` 没有被纳入对账**。`OPS`（op -> MCP 工具名的手写映射）与 `SERVERS[...].exposes` 仍是两份各自维护的清单，当前值相同纯属人记着 | server 加一个工具时，`exposes` 漏登记会被 `reconcile()` 报 `undeclared`，但 `OPS` 漏加**没有任何东西会红** —— 上层调用点拿到的仍是「未知的 git-mcp 操作」 | 泛化的做法是把 op 映射放进 `McpServerSpec`（比如 `ops: Mapping[str, str]`），让 `reconcile()` 三方对账。本轮没做：那要改 §5.2 定死的 spec 形状，且只有一个 server 时看不出这个抽象对不对 |
| 2026-09-01 | P9 | **`reconcile()` 没有挂进任何自动入口**，只有 `maos/tests/test_mcp_registry.py` 在跑它 | 加第二个 server 的人如果只跑自己那几条测试、不跑全量 pytest，对账就形同虚设。`scripts/verify.py` 与 `gen_docs --check` 都没有引它 | 挂进 `scripts/verify.py` 之前要先想清楚一件事：对账要**真拉子进程**，而 verify 是证据束核验，多一个会 fork 的步骤要评估它在无网/受限环境下的表现。归做 verify 那一轨 |
| 2026-09-01 | P9 | **角色到工具的映射仍是两份手写表**：`registry.DEFAULT_ROLE_SERVERS` 与 `maos/agents/*.py` 各自的 `allowed_tools` | 本轮加了一条测试守着「`ports_for("coding")` 挑出来的 port 必须在 `CodingAgent.identity.allowed_tools` 里」，但那是**单向**的：白名单里有而映射里没有的（比如 `sandbox`）无人过问。真正的档案表是别轨的产出，本轨的映射只是兜底 | 归能力档案表那一轨（`profiles` 注入口已经留好）。合并后应当让档案表成为唯一出处，`DEFAULT_ROLE_SERVERS` 退化成「没人注入时的最小可跑集」或直接删掉 |

## task-T60（能力装配层落地时发现，本轮都不改）

2026-09-01 把 Identity 里的名字解析成真能调的 ToolPort 时撞见的四条。装配层只让它们
**显形**（落进 `AssemblyReport` 与 `evidence/capability-matrix.json`），一处都没修 —— 铁律 4。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-01 | P9 | **`coding` / `testing` 白名单里的 `sandbox` 全仓没有对应 ToolPort**（实际存在的是 `sandbox.git_apply` 与 `sandbox.pytest_run`），22 个角色里就这两处 | 白名单放行了一个不存在的名字 —— 与 `git-mcp` 补上之前是同一个洞。今天无害（没有调用点用这个名字取工具），但装配层一接进 Worker，这两个角色启动时就会看到「有授权无实现」 | 归整合期。两条路二选一：把白名单改成那两个真名，或者真加一个叫 `sandbox` 的聚合 ToolPort。**别只改测试** |
| 2026-09-01 | P9 | **装配函数还没有生产调用方**：`assemble()` 目前只在测试与证据脚本里被调，Worker 起 Agent 时没有接线 | 每个 Agent 仍然各自 import 各自的 ToolPort，装配层的收窄约束在生产路径上还没生效 —— 它守得住的只是「调过 assemble 的那些」 | 归整合期。接线点在 `maos/runtime/worker.py`（本轨白名单外，T57 持有），一行 `assemble(agent, profile=..., mcp_ports=...)`，等 T57/T58/T59 落地后一起接 |
| 2026-09-01 | P9 | **`evidence/capability-matrix.json` 没进 `evidence/INDEX.json`，也不在 `scripts/verify.py` 的核验项里** | 这份证据目前只有「首行有出处」这一层保证，没被证据束的索引与核验链条覆盖 | 归整合期。`INDEX.json` 有主仓在制品在动（本轨不许改），`verify.py` 是事实源 |
| 2026-09-01 | P9 | **`implemented_without_authorization` 目前等于「全仓目录减本角色白名单」**，22 个角色每个都是 8–10 条 | 当计数指标可用（矩阵里只落了 count），但当作「该给谁加授权」的建议清单就是噪音 —— 它没区分「本该有」与「本来就不该有」 | 等职责能力档案（T58）落地后再收窄：档案说得出「这个职责应该有哪些」，差集才有意义 |

## task-T64（RTV 域五个薄壳 Agent 与场景 11，本轮都不改）

2026-09-02 往 `AGENT_POOL` 投放 5 个新角色（22 → 27）时撞见的四条。全部落在**本轨
白名单之外**的文件上，按铁律 4 一处都没改，只记账。**四条同源**：T58 那批「以
`AGENT_POOL` 为事实源的快照」没有为「新增业务域」留过口子 —— 任何一轨新增 Agent 都会
同时打红这四处，这不是本轨特有的问题，T61/T62/T63 新增 skill / ToolPort 时会撞上
它的另外几张脸。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **`maos/capability/profiles.py::PROFILES` 缺 RTV 域五条职责档案**。`test_profiles_cover_every_pooled_role` / `test_profiles_cover_manager_too` 两条以 `AGENT_POOL` 与 `identities()` 为事实源，新角色一进池就报「这些在池角色没有职责档案」 | 2 条红。档案表是 T60 装配层发权限的依据，缺档案意味着这五个角色装配时拿不到收窄约束 | 归整合期，或由人类当场授权本轨补。补的内容已经确定（duty 键 `rtv.intake` / `rtv.disposition` / `rtv.logistics` / `rtv.reconciliation` / `rtv.settlement`，skills 与 tools 照契约 C-R4 / C-R7、tier 全 `light`），**不是**一次判断，是一次誊抄 |
| 2026-09-02 | P9 | **`test_capability_profiles.py` 的 `BASELINE` / 分组计数快照会随新增角色涨**，且**涨多少取决于测试执行顺序**：`test_rtv_flow.py` 先跑（stub skill 已注册进全局 `SKILL_REGISTRY`）时是 `depends-tool-missing` +4、`skill-not-registered` 0；后跑时是 `skill-not-registered` +6、`depends-tool-missing` 0 | 2 条红，且**改成任一个数都会在另一种执行顺序下翻红** —— 这是比「快照过期」更麻烦的一类：它不是数字不对，是这份快照对「进程级注册表 + 按需注册」这种形状不成立 | 归整合期，且要**先定口径再改数**。两条路：① T62/T63 的真 ToolPort 与真 skill 落地后这些 finding 自然消失，届时快照只需减不需加；② 若要在那之前绿，得让 `check_consistency()` 对「同一进程里注册表内容会变」这件事有明确态度（比如只认 builtin 动态发现的那批） |
| 2026-09-02 | P9 | **`test_capability_assembly.py::test_drift_matches_the_repo_wide_scan` 的 `TOOL_DIFF` 快照同样以 `AGENT_POOL` 为事实源**；`test_checked_in_matrix_has_the_same_shape` 断言 `evidence/capability-matrix.json` 的角色集合等于 `AGENT_POOL` | 2 条红。后者要重跑证据脚本才能绿，而 `evidence/**` 禁止手改（铁律 3） | 归整合期。矩阵那条必须**重跑生成脚本**，不许手改 json |
| 2026-09-02 | P9 | **`test_worker_model_routing.py::test_factory_receives_the_role_and_its_declared_tier` 把 tier 分布写死成 `{"light": 17, "medium": 2, "strong": 3}`** | 1 条红（新增五个 `light` 角色 → 22）。判据本身没问题，但它把「池里有多少个 light 角色」当成了不变量，而那恰恰是每加一个业务域就会变的数 | 归整合期。改数只要一行；更耐用的写法是从 `AGENT_POOL` 现算分布再断言「至少有这三档、且与各 identity 自述一致」—— 但那是改判据，不是刷数，得人类裁定 |
| 2026-09-02 | P9 | **`docs/agent-identity.md` 是 `scripts/gen_docs.py` 的生成投影**，新增五个 identity 后 `test_generated_docs.py` 的两条当场红 | 2 条红。派单 §0.1 明令本轮不许跑 `gen_docs.py`（投影要等五轨齐了统一跑），所以本轨**必然**留着这两条红 | 归整合期，跑一次 `python3 scripts/gen_docs.py` 即可。⚠️ 别手改这份文档 —— 基线 commit `4c956a8` 记的就是上一次手改它踩的坑 |
## task-T65（RTV SOP 文档与域可移植性论证，本轮都不改）

2026-09-02 写 `docs/sop-rtv.md` 与追加 `docs/domain-portability.md` 时撞见的四条。
本轨只写文档、不写代码（除一个测试文件），四条一处都没修 —— 铁律 4。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **派单与冻结契约对不上**：T65 派单 §5.1/§5.4 写「七个状态十条边」，而契约 C-R2 的 `BIZ_STATUS_FLOW` 实测是七个状态、**九条边**（`received` 2 + `disposed` 3 + `shipped` 2 + `credited` 2） | 照派单写会让文档说假话，照派单写测试会让断言恒红。本轨以契约为准（记进 DECISIONS） | 归整合期：刷派单模板与后续轮次的引用，别再照抄「十条」。契约本身没错，错的是派单里的数 |
| 2026-09-02 | P9 | **跨轨契约不在版本库里**：`review/rtv-contracts.md` 走 `.git/info/exclude`，只存在于主仓的文件系统。五轨都拿它当唯一口径，却没有任何机器保证各 worktree 手上是同一份 | 契约漂了没人会发现 —— 四轨各自按自己那份写代码，症状要到整合期才出现。本轨的做法是拷一份只读副本进 worktree（gitignored），并让契约缺席时测试 skip 而不是判红 | 归整合期。两条路：把契约（或它的 sha256 指纹）纳入版本库，或在整合脚本里加一条「各 worktree 契约 sha 必须相同」的核验 |
| 2026-09-02 | P9 | **`docs/domain-portability.md` §3 的「退款域 14 张表」与当前实测对不上**：`grep -c 'CREATE TABLE' maos/domain/refund/schema.sql` 现在回 15 | 那一节的数字与历史 HEAD 绑定，且本轨只许追加、不许改既有 393 行，所以本轮如实记账不改。但它已经是「文档在说过期的话」的一例 | 归整合期或专做数字回填的那一轨。回填时要连它的端点口径一起写清楚（是哪个 sha 上的 14） |
| 2026-09-02 | P9 | **文档守卫判据 E 对「尚不存在的未来落点」没有豁免通道**：反引号里的路径一律做存在性校验，`ALLOW_MISSING` 是脚本里的硬编码字典 | 并行造域时四轨的文档都会撞这条 —— 要写「RTV 的表将落在哪」，只能把路径写成不带反引号的纯文本，排版上与代码引用不一致 | 归文档守卫那一轨。可选：加一档「待建路径」形状（如 `path (待建)`），或让 `ALLOW_MISSING` 支持从一份 md 里读（登记时必须写理由，口径不变） |
## task-T63（RTV 域 Skill 层落地时发现，本轮都不改）

2026-09-02 把 SOP 五步做成六个 skill 时撞见的六条。本轨只做 `maos/skills/builtin/rtv/`
与它的测试，其余一处没修 —— 铁律 4。前两条是**整合期必须处理**的，不是可选项。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **四条既有测试因「注册表多了六个 skill」变红**：`test_generated_docs.py` 两条（`docs/skill-catalog.md` 仍写着 30 个 skill）、`test_capability_profiles.py` 两条（新增 16 条 `rtv.*` finding，全部是 `depends-tool-missing` 4 条 + `owner-role-unknown` 6 条 + `skill-unowned` 6 条） | 本轮判据要求 1707 条一条不许变红，实测 `1731 passed, 4 failed`。这四条**不是本轨的实现缺陷**：投影文档要按派单 §0.1 等五轨齐了整合期统一跑 `gen_docs.py`；16 条 finding 全部源自 T62（工具未落地 -> depends-tool-missing）与 T64（角色未落地 -> owner-role-unknown / skill-unowned），那两轨一到就自然消失 | **整合期第一件事**：先跑 `python3 scripts/gen_docs.py`，再按当时的真实结果刷 `test_capability_profiles.py` 的 `BASELINE` 与计数表。刷之前要逐条确认剩下的 finding 确实只剩「已知漂移」，不是把本轨的洞一起洗白 |
| 2026-09-02 | P9 | **`maos/skills/builtin/rtv/_common.py` 里有一份与 T61 重复的权威守卫**（`AUTHORITATIVE_*` 常量、`create_case` / `update_biz_status` / 回执落库） | T61 的 rtv 域 guard 模块不在本轨基线里（契约 C-R8），而「只有 rtv.observe 写得进 credited/settled」这条不能等到整合期才成立。整合前两份实现并存，**常量一旦漂开，守的就不是同一条边界了** | 整合期把 `_common.py` 的守卫段删掉、改调 T61 的 `guard`，并保留本轨那条 `test_frozen_constants_match_the_contract` 作为两边同步的哨兵。`_ensure_schema_fallback` 与内嵌的 C-R1 SQL 同批删除（`_rtv_domain_objects()` 已经把切换点收在一个函数里） |
| 2026-09-02 | P9 | **退货理由码与规则编号（`RETURN_REASONS` / `RULES` / `REASON_RULE`）是本轨自己造的临时表**，权威那份在 T62 的 rtv 码表模块（`rtv_codes`）| 契约 C-R1 要求 `rationale_json` / `findings_json` 的每条 `rule_id` 必在 `rtv_codes.RULES` 里，而契约没冻结编号本身。两份表的编号很可能对不上，届时落库的历史裁定会引用查不到的编号 | 整合期把这三个 dict 换成 `rtv_codes` 的同名表（`dispose.py` / `reconcile.py` 只用 `require_reason` / `require_rule` / `cite`，改的是 `_common.py` 一处）。若编号确实对不上，要同时决定既有 `rtv_disposition` 行怎么处理 |
| 2026-09-02 | P9 | **契约 C-R1 的两张回执表都没有 `poll_count` 列**（`credit_note` / `rtv_settlement_observation`） | 「终态是问出来的」这条证据只能落在 `RtvBizStatusChanged` 事件的 `detail.poll_count` 里，查证据要从事件日志走，而不是从回执那一行直接看到 | 契约冻结，本轮不动。将来若要把轮询次数落进回执表，那是一次契约变更，得走人类解锁 |
| 2026-09-02 | P9 | **`rtv_business_ref` 没有主键**（契约 C-R1 原样如此，与 ap 域的 `ap_business_ref` 同形） | `INSERT OR REPLACE` 在无主键表上不去重，返工重跑会攒出重复行。本轨用「先删后插」自守，但那是**约定**，不是数据库保证 —— 别的轨往这张表写时不照做就会漂 | 归整合期或 T61：要么加唯一索引，要么把写入口径收到一个函数里。两条都要动契约或 T61 的文件，本轨不碰 |
| 2026-09-02 | P9 | **本轨的 fallback 建表顺带执行了 `maos/domain/ap/schema.sql`**（读文件、不 import），好让 `supplier` / `purchase_order` / `goods_receipt` 三类源单表存在 | 那五张表按契约 C-R1 属于复用面，本域「只引用不重建」；由 skill 层的 fallback 去建它们是权宜之计，正式归属应当由 T61 的 `maos/domain/rtv/objects.py::ensure_schema` 或场景装配决定 | 整合期随 fallback 一起删。删之前要确认 T61 或 T64 的装配路径确实建了那五张表，否则 `rtv.intake` 会以 `no such table` 的面目失败，而真实原因是「这库还没装配 ap 域源单」 |
## task-T62（RTV 域 ToolPort 与编码表落地时发现，本轮都不改）

2026-09-02 把 RTV 域碰得到外部世界的五个口子做成 ToolPort 时撞见的四条。一处都没修 —— 铁律 4。
第一条是**当前就红的**，不是将来才要处理的，已在回执里升级人类。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **新增 ToolPort 会当场打红 `maos/tests/test_generated_docs.py` 两条**（`docs/toolport-contract.md` 与代码不一致：11 个 → 16 个）。派单 §0.1 明令本轮不许跑 `scripts/gen_docs.py`（要等五轨的 ToolPort/Skill/Agent 齐了整合期统一跑），而那份投影又是被测试逐字节钉住的 | 派单内部矛盾：§0.1「不许跑生成器」与硬判据 1「原有 1707 条一条都不许变红」不可兼得。**同期 T63（新增 skill）/ T64（新增 agent）必然撞同一堵墙**，那两份投影 `docs/skill-catalog.md` / `docs/agent-identity.md` 同理 | 归整合期，且**只能在整合期跑一次**：五轨各自跑会让这三份生成物在合并时逐轨冲突。整合期顺序应是「先合全部代码 → 再跑一次 `gen_docs.py` → 再提交投影」 |
| 2026-09-02 | P9 | **`381` 不在 `maos/tools/ap_codes.py` 的 `INVOICE_TYPE_CODES` 里**。派单 §5.2 与契约 §7 都写「直接引用 ap_codes 里已有的那份（`LIST_INVOICE_TYPE` / `CODE_*` 那一族）」，但站点把 UNCL1001 拆成 `-inv`（发票）与 `-cn`（贷记通知单）两页，ap_codes 的 docstring 明说只抄了前者 | 派单的括注与代码事实不符。本轨按「同一份规范的兄弟页」处置（见 DECISIONS），未动 ap_codes 一个字节 | 归整合期或下一轮。若要统一，是把整张 `UNCL1001-cn` 子集重抓一次落到一处，**不是**把 `381` 塞进 `-inv` 那张表（那张表的边界本身是信息） |
| 2026-09-02 | P9 | **`review/rtv-contracts.md` 不在本 worktree 的基线 `4c956a8` 里**，`git log --all` 也搜不到；它只存在于主仓 `~/Documents/MAOS/review/` 的未提交在制品中 | 派单 §0「动手前先读一份东西」在 worktree 内无法照做。本轨从主仓只读读到了全文（未写），执行未受影响；但「五轨唯一的跨轨契约」不在任何一轨的基线里，是个可复现的派单/基线错配 | 下次派单前：把冻结契约先提交进基线，或在派单里写明它只在主仓、给出绝对路径 |
| 2026-09-02 | P9 | **五个 port 目前零生产调用方**：调它们的 skill 在 T63、agent 在 T64，本轨只造零件不接线 | 与 T55..T60 那批同一个形态（BACKLOG 里已有同类条目）。今天无害，但「授权面收窄」这类约束在生产路径上还没生效 | 归整合期，随 T63/T64 一起接线 |
## task-T61（RTV 域领域层地基落地时发现，本轮都不改）

2026-09-02 把 `review/rtv-contracts.md` 的 C-R1 / C-R2 / C-R3 落成代码时撞见的七条。
本轨只落业务对象、状态机与权威事实守卫，一处都没修 —— 铁律 4。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-02 | P9 | **RTV 域不是自足的**：`supplier` / `purchase_order` / `purchase_order_line` / `goods_receipt` / `goods_receipt_line` 五张源单据表归 `ap` 域持有，本域只引用不重建（契约红线），所以跑本域之前必须先有人调 `maos.domain.ap.objects.ensure_schema` | 场景 11（T64）与任何只 `ensure_schema` 了 rtv 的调用方，一读源单据就撞 `UpstreamSchemaMissing`。本轨给了显式探针 + 指得出去处的错误信息，但**没有**替调用方建表 —— 建了就是重建，正是这条红线要挡的事 | 归整合期。`scenario_11.py` 的建库那一步要显式先跑 ap 域的 `ensure_schema`，或者由更上层统一建所有域的 schema。别在 rtv 的 schema.sql 里补一份定义 |
| 2026-09-02 | P9 | **`rtv_line.reason_code` 与 `rtv_disposition.rationale_json` 的 `rule_id` 本域不校验取值域** —— 码表 `RETURN_REASONS` / `RULES` 在 T62 的 `rtv_codes` 模块里，该模块在本轨基线里还不存在（**所以这里刻意不写它的完整路径** —— `test_docs_guard.py` 会把指向不存在文件的路径引用判红，而它此刻确实不存在） | 现在往 `rtv_line` 里塞任何字符串都进得去。「理由可核对」这句话目前只由 ToolPort / Skill 层保证，域层是敞的 | 归整合期。校验该放在**能拿到那份码表的层**（T62 的 ToolPort 或 T63 的 skill）；在域层塞一份「自己的码表」就是第二份取值域，两份一定会漂 |
| 2026-09-02 | P9 | **`_OBSERVATION_REQUIRED` 比派单 §5.3 第 4 条的最小集多要求两个字段**：`credited` 多要 `amount_credited`，`settled` 多要 `ap_reference`（理由见 DECISIONS `## task-T61` 第 3 条） | 若 T63 的 `rtv.observe` 只按派单最小集构造回执，整合时会被守卫拒，报「缺字段」 | 归整合期，与 T63 对一次口径。**不许为了让它绿而放宽守卫** —— 该改的是回执的构造方 |
| 2026-09-02 | P9 | **推进到 `disposed` 必须同时给出 `return_action`**（`DispositionRequired`），这是本轨自定的不变量，契约没写 | 若 T63 的 `rtv.dispose` 只调 `update_biz_status(..., "disposed", ...)` 而不带裁定结果，整合时会红 | 同上，归整合期与 T63 对口径。理由见 DECISIONS `## task-T61` 第 4 条 |
| 2026-09-02 | P9 | **`rtv_disposition` / `rtv_shipment` / `rtv_reconciliation` / `rtv_compensation_record` 四张表本域只建表 + 只读查询，没有写入口径** | 这四张不是权威事实表，写入走 `objects.execute()` 即可，但目前每个调用方要自己拼 SQL —— 拼法一多就有第二份口径 | 归 T63。真正的写入形状要等 skill 落地才看得清（比如 attempt 号怎么算），现在造一层包装是凭空猜 |
| 2026-09-02 | P9 | **派单 §6 硬判据第 6 条说 `BIZ_STATUS_FLOW` 是「七个状态、十条边」，而冻结契约 C-R2 那份实际是七个状态、九条边** | 照契约落就与派单的自证句对不上一条，容易被后来的人当成回归 | 已按派单自己那句「对不上就改回契约那份」处置：代码逐键照抄 C-R2，并在 `test_rtv_guard.py::test_biz_status_flow_matches_the_frozen_contract` 里把 9 这个数钉死。派单那句计数请编排侧刷一次 |
| 2026-09-02 | P9 | **`ap` 域的 `objects.execute()` 对 `ap_payment_observation` 不设限**（那边 `guard.record_observation` 的 docstring 自陈「等于给伪造回单留了个后门」）。本域把同类的两张权威表一并封住了，**ap 侧一个字节没动** | ap 域仍有一条运行时旁路可以伪造银行回单 —— 今天靠「guard 自己不走 execute」维持 | 归 ap 域自己那一轨。改法照抄本域 `objects._GUARDED_TABLES`（正则里多列两张表名即可），但那是别人的白名单，本轨不碰（铁律 4） |
| 2026-09-02 | P8 | 🔴 **政策里那三个证据判据字段没有任何代码消费方**：`scenarios/refund/cases/case_r4a.json` 的 AS-003 body 里写着 `requires_evidence_kinds:["image"]`、`min_evidence_count:1`、`evidence_source:"customer_evidence"`（v2 收紧到 2 张、加 video），而全仓 `grep` 这三个键在 `maos/**/*.py` 里**零命中** —— 政策引擎只读 `refund_ratio` / `deduct_fee`（`skills/builtin/refund/finance.py:186`） | `docs/EXECUTION.md:843` 把「AS-003 人为损坏免责 / 需 `customer_evidence` 中有图片证据」当成已实现的差异点列在表里，而它其实是**语料里的字面量**。本轮补完照片入口后这条更扎眼：证据真的进库了，但没有一行代码会因为「有没有图」而改变裁定 —— 交一张图和不交，结论一模一样，且不报错 | 两条路。①**最小**：`policy.match` 读 `requires_evidence_kinds` / `min_evidence_count`，与 `customer_evidence` 实际行数比对，不足则不予适用该排除规则 —— 这才让「需图片举证」成为真判据。②**先降噪**：把 `EXECUTION.md:843` 那格改成「语料已定义，判据待实现」，别让它继续读起来像已完成。建议先做 ②（一处文案），①归政策面下一轮：它要动 `maos/skills/builtin/refund/policy.py`，不在本轮附件轨白名单内（`maos/skills/builtin/refund/policy.py`） |
| 2026-09-02 | P8 | **`scenarios/custom/ledger.json` 的 AS-003 与 `case_r4a.json` 的 AS-003 是两条完全不同的规则**：前者「发错货全额退」（`wrong_item`），后者「人为损坏免责（需图片举证）」（`artificial_damage`） | 同一个规则号在两套语料里指两件事。本轮实跑 `/refund ORD-2026-0001 质量问题` 命中了 `AS-003@v1`，看回帖会以为「证据判据生效了」，其实命中的是发错货那条、与证据无关。凡是拿规则号当口径讲的地方（答辩、PPT、EXECUTION 的差异表）都可能对错人 | 要么给两套语料的规则号加租户前缀（`custom` 是 `tnt-demo`、r4a 是 `tnt-mfg-a`，本来就分得开），要么在两份语料的抬头各写一句「本文件的 AS-00x 编号只在本租户内有意义」。归语料面，本轮不动 |
| 2026-09-02 | P8 | **`var/attachments/` 没有清理机制**，`AttachmentStore` 刻意只写不删 | 长期跑下去盘会满，而症状是落盘那一步抛 OSError → 回执说「取件失败」，指向完全错误的方向 | 删证据的判断需要知道案子状态（结案多久可清），本层不知道也不该知道。正确做法是一条按 mtime + 案子终态的运维清理，或在 `refund_case` 终态时登记可清列表。归运维面 |
| 2026-09-03 | P8 | `scripts/run_requests.py::read_sheet` 三处与群里那条入口不一致：fail-fast（第一行错就停）、只认 utf-8-sig（中文 Windows 上 Excel 另存的 GBK 直接抛 UnicodeDecodeError traceback）、行号用 `enumerate(DictReader)` 而 csv 模块会悄悄跳过空行，空行之后报的行号全错一位 | 这条是给不写代码的人用的 CLI 入口，三条都会让他对着 Excel 找不到「第 N 行」。`maos/ingress/sheet.py` 在群入口绕开了三条（按 `reader.line_num` 计物理行号、逐行收错、utf-8-sig → gbk），但 CLI 那条没动（既有文件，铁律 4） | 让 `read_sheet` 也改用 `sheet.parse`（同一份解析）或至少改行号与编码。归申请表入口下一轮 |
| 2026-09-03 | P8 | `router._render_evidence` 用 `size // 1024` 报体积，小于 1 KB 的附件显示「0 KB」 | 演示用的测试图都很小，回帖里一排 0 KB 看起来像没收到内容 | 改成 `<1 KB` 或按字节报。一行文案，归附件面 |
| 2026-09-03 | P8 | `_NioChannel.listen` 的回调只给 `(sender, body)` / `(sender, Attachment)`，不带 Matrix 的 `event_id`；`hiclaw/room_ingress.py` 只能用进程内计数器当 `msg_id`，幂等键在进程重启后不成立 | 今天无害（幂等表本来就在 `:memory:`），但将来把幂等落库时，房间消息是唯一一个没有平台原生 id 的渠道 | 给 `listen` 的回调加一个 keyword-only 的 `event_id`（或让 Attachment.msg_ref 带上），保持现存 `lambda sender, body` 调用方不变 |
| 2026-09-03 | P8 | 申请表回帖在 Matrix 侧已按 20 K 字符拆条，但飞书 / 企微 adapter 没有拆：企微 `message/send` 文本上限 2 KB（平台截断），飞书更大但也有界 | 一张 50 行的表在企微群里会被截在半句上，且平台不报错 | 给 `FeishuAdapter.send` / `WeComAdapter.send` 各加按平台上限拆条，口径同 `hiclaw/room_ingress.py::split_message`；归 IM 渠道面 |

## task-T85（证据核验 skill + Agent —— 政策证据判据的第一个消费方）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **本条了结 `docs/BACKLOG.md` 2026-09-02「政策里那三个证据判据字段没有任何代码消费方」中的两个**：`refund.evidence_check` 成为 `requires_evidence_kinds` / `min_evidence_count` 的第一个消费方（`maos/skills/builtin/refund/evidence_check.py::_declared_requirements`）。**第三个字段 `evidence_source` 仍然零消费方** | 那条 BACKLOG 提的两条路里，本轨走的既不是①也不是②：①是让 `policy.match` 自己读判据并影响裁定，本轨是**旁路观察**（R4，不进 DAG、不改 `policy.py`），所以证据够不够只出现在证据核验岗的产物里，**不改变 `policy.match` 的裁定**。也就是说「交一张图和不交，policy 的结论仍然一模一样」这句话在处置主路径上依然成立 | ①仍归政策面下一轮。`evidence_source`（去哪张表数证据）在 T74 的 `policy.py` 里已有 `_require_evidence_source` 校验但本 skill 不读它 —— 本 skill 的证据一律来自入参 `customer_evidence`，多数据源时这个字段才有意义，归底账多源那一轮 |
| 2026-09-03 | P9 | `EVIDENCE_KINDS` / kind 词表在本轨是**私有常量副本**（`evidence_check.py` 类属性，留了 `# INTEGRATION-POINT` 注释），与 T77 在 `integrate/p9-t74-t79:_common.py` 的那份同值但不同源 | 两份词表一旦分叉，症状是「政策写 image、库里落 photo，举证闸恒判不足且不报错」—— 正是 T77 那份词表的 docstring 里写的那条静默失效 | **整合轮以 T77 的 `_common.EVIDENCE_KINDS` / `_KIND_ALIASES` 为准**，把本文件的两个类属性改成 import。本轨增补的 MIME 型（`image/jpeg`、`application/pdf` 等六项 + `image/` `video/` `audio/` 前缀兜底）要不要一起并进 `_common`，由整合轮定：并进去则收案面也吃得到 Matrix 附件的 mimetype，留本地则 `_common` 保持「只认自由文本同义词」的单一口径 |
| 2026-09-03 | P9 | `QC_EXPECT` 里质检结论的期望取值是本轨拍的（`quality_defect` -> `defect`/`fail`，`wrong_item` -> `mismatch`/`wrong_item`），而底账里 `qc_report.result` 的真实取值域由 T89 定 | 对不上的后果不是报错，是**交叉核对恒判不一致**：房间里每一单都挂一条「质检结论与诉求不符」，看的人会以为每单都有问题。反过来若 T89 用了别的词，本 skill 又会静默走「无期望值，跳过」分支 | T89 的底账落地后，拿 `scenarios/custom/ledger.json` 里新订单 `payload_json.qc_report.result` 的实际取值核一遍 `QC_EXPECT`，不一致就改类属性（改词表不改代码逻辑）。整合轮或 T89 验收时做 |
| 2026-09-03 | P9 | `KIND_EVIDENCE_REPORT = "refund_evidence_report"` 写在 `maos/agents/refund/evidence_agent.py` 自己文件里，**不在** `_base.py::ALL_REFUND_KINDS` 里 | 今天无害（Gate 对非代码类产物只按 `self_check` / `summary` 判，不查 kind 白名单）。但 `ALL_REFUND_KINDS` 从此不再是「退款域全部产物类型」的完整清单，任何按它做遍历断言的新代码都会漏掉证据核验岗与风险反欺诈岗两份产物 | 整合轮把两轨的 kind 常量一起收拢进 `ALL_REFUND_KINDS`（T86 那边有同型一条）。两轨各自加会撞同一行，所以留到整合轮一次做完 |
## task-T86

| 发现日期 | P9 | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **风险历史只来自底账 `refund_history`，运行时不累计**：`refund.risk_screen` 是纯函数，只看调用方递进来的那份历史，而 `custom_case.run_payload` 每次都新建一个 `:memory:` store | 同一个客户今天连退两单，第二单的 `frequency_30d` 看不见第一单 —— 演示里「刷单式连环退款」这个剧情只能靠底账预先写好，现场连着起两单是复现不出来的。而且它不报错，分数就是安静地偏低 | 要真累计得先有一层持久的观察记录（谁在什么时候起过哪些单），那是存储面的事，不在本轨白名单。归退款域下一轮；在那之前别在 skill 里加模块级缓存，那只会造出一个「进程活着时准、重启后失忆」的假账 |
| 2026-09-03 | P9 | **`refund_history[*].amount` 是契约里的入参字段但零消费方**：六个信号里没有一个看金额历史（如「近 30 天累计退款金额 / 累计实付」），`amount` 解析出来就被丢掉 | 大额连环退款与小额高频退款今天算出来是同一个分数。底账里那一列看着像在被用，其实没有 —— 与 `docs/BACKLOG.md` 2026-09-02 那条「政策证据判据零消费方」是同一类失真 | 加一个 `amount_ratio_30d` 信号即可，权重与阈值照现有类属性的写法加。归风控面下一轮：本轮的六个信号是跨轨契约 §1.5 定死的，多加一个要先改契约 |
| 2026-09-03 | P9 | **`duplicate_refund` 只按 `order_id` 匹配**：同一批货拆成两单分别申请退款，两单各自都是「首次」 | 最典型的一种规避手法识别不到，且看起来一切正常。今天可接受（`multi_order_same_account` 从侧面兜了一层），但那条信号只数订单数、不看这些订单是不是在同时退 | 需要「同客户近 N 天内在退的订单集合」这个口径，与上面第一条同属「要有持久观察层」。两条一起做，别单独补 |
## task-T87（退款圆桌引擎 —— 平台无关的 Speaker / 五岗事实 / 三个钩子）

本轨白名单只有 `maos/roundtable/**` 与两个新测试文件，下面几条都是过程中撞见、**没当场改**的。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **圆桌发言的 token 没有成本行**：`roundtable/speaker.py::Speaker.speak` 直接调 `model.complete(...)`，不经 `BaseAgent.ask`，因此不落 `model_usage`、也不登记 call_site | 五岗 × 每单一次真模型调用完全不进成本账。`scripts/verify.py` 的成本项与 `docs/` 里任何「本次运行花了多少」的读数，都会**少算**圆桌这一块，而且不报错 —— 读起来像是没花钱。同一取舍已经在 `hiclaw/ap_room.py::Speaker` 与 `maos/ingress/chat.py::ChatResponder` 上各有一份，本轨是第三处 | 三处一起收：给 `model_usage` 加一种不挂 plan/task 的归属（例如 `source="roundtable"`），或让 `BaseAgent.ask` 支持无 plan 调用。别只改一处 —— 只改一处会让三条路的成本口径分叉，而分叉不报错 |
| 2026-09-03 | P9 | **`roundtable/team.py::FALLBACK_IDENTITIES` 是过渡件**：`refund_evidence` / `refund_risk` 两个岗位的最小 `AgentIdentity` 写死在这里，因为它们的真 Agent 在 T85 / T86 上新建、本轨基线里 `AGENT_POOL` 没有 | 两轨并进来之后 `identity_of()` 会自动改用真身份（池子优先），这两条即成死代码。留着不报错，但房间里的自我介绍来自哪份声明变得要读两个文件才知道 | T85 / T86 并进主干、`AGENT_POOL` 里有 `refund_evidence` / `refund_risk` 之后，删掉 `FALLBACK_IDENTITIES` 与 `identity_of` 里的退路，同时删掉 `test_roundtable_team.py` 对它的断言。整合轮收，别在本轮动 |
| 2026-09-03 | P9 | **`hiclaw/ap_room.py` 与本模块有一份重复的圆桌实现**：`SPEECH_LIMIT` / `SYSTEM_TMPL` / `Speaker` / `render_speech` / `run_roundtable.say` 五处与 `maos/roundtable/` 同形 | 改了一边不改另一边就分叉。今天分叉是**刻意**的（AP 域四岗、没模型即 EXIT；退款域五岗、没模型退事实卡），但「刻意的差异」和「忘了同步」在代码上长得一模一样 | 整合轮之后另开一个小 commit：让 `ap_room.py` 的 `Speaker` 改用 `maos/roundtable/speaker.py`（`title` / `room` / 退化姿态都已参数化，AP 那边只要保留自己的 `EXIT_NO_MODEL` 分支即可），`TITLES` 各留各的。`hiclaw/ap_room.py` 是本轮六轨的共同只读面，**本轮一个字都不许动** |
| 2026-09-03 | P9 | **同一次 `/refund` 会灌两遍 `:memory:` 库**：`router.preflight()` 内部 `seed_case` 一次算出 `checked`，证据岗为了拿带 `params` 的规则又 `seed_case` + `contrast.policy_view` 一次（`roundtable/stages.py::_rules_of`），财务岗预演再灌第三次 | 实测三次合计仍是毫秒级（`run_payload` 整跑才 17 ms），演示规模无感。但它是「同一份事实算了三遍」，将来底账变大或改成真库时，这里是第一个变慢的地方 | 要收就往「`preflight` 返回里带上 `view["rules"]`」的方向收，让三处共用一次灌库。那要改 `maos/ingress/router.py` 的返回形状（T88 的文件、且跨轨契约 §1.4 明写不要求 T88 改），所以本轮走重算。归圆桌整合后的一次性重构 |
| 2026-09-03 | P9 | **`stages.facts_*` 返回的 `data` 没有形状守卫**：各岗键只写在 docstring 与跨轨契约里，`StageReport.data` 是裸 `dict` | T88 的 `/pending` 要读哪个键全靠约定；哪天某一岗改了键名，读的那头拿到 `None`，房间里显示空白而不报错 | 等 T88 真的读起 `data` 之后，按实际读到的键补一条形状断言（或给五岗各一个 `TypedDict`）。现在补是凭空猜消费方 |
## task-T84（Matrix 多身份：建号进房 + send-only 发声面 + 监听忽略名单）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **五岗 = 五条 `_NioChannel` = 5 个私有事件循环 + 5 条守护线程 + 10 个启动 GET**（每岗 whoami + 查加密）。matrix-nio 的 `AsyncClient` 是一个账号一个实例，没有多账号复用的形态，所以 `open_voices` 只能逐岗开 | 演示规模（5 岗、一次起停）完全够用，实测起停干净、`close()` 每条都收得回去。但岗位数一旦上去（十几个域各五岗），线程数与启动 GET 数是线性涨的，且它们都堆在**启动那一刻** —— 症状会是「常驻入口起得越来越慢」，而不是报错 | 归房间面下一轮。方向是一个 client 池或一个多账号 client（Application Service 的 `user_id` 伪装是 Matrix 的正解，但那要在 homeserver 上注册 appservice，属「改 Docker」类）。**别为此改 `MatrixBusConfig`** —— 那是 C-6 冻结面 |
| 2026-09-03 | P9 | **`MAOS_ROOM_BOTS` 这道忽略名单今天走不到**：岗位账号经 `_NioChannel.send` 发的是 `m.notice`，nio 解析成 `RoomMessageNotice`，而 `listen` 只 `add_event_callback(_cb, RoomMessageText)` —— 名单命中之前，事件根本不会被派发到那条回调上 | 是**第二道保险**而不是唯一防线，所以它的回归只由 `test_matrix_bus.py` 那四条守着，真房间里观察不到「名单挡住了什么」。哪天有人把岗位发言改成 `m.text`（想让 Element 推送提醒），或者拿岗位号在 Element 里手打字，它才成为唯一防线 —— 而那一刻没人会记得它存在 | 不用改，但**改 msgtype 的那一轮必须回头看这条**。要更硬的话，给 `listen` 也挂 `RoomMessageNotice` 并让名单在那条路径上真正生效，这样名单从第二道保险变成随时可观察的第一道 |
| 2026-09-03 | P9 | **口令存了两处**：`~/.maos-matrix/creds.txt` 是 `up.sh` 写的三个地基号，`~/.maos-matrix/agents.env` 是本轨写的五个岗位号（`MAOS_AGENT_*_PASSWORD`）。两个文件、两套格式（前者裸 `K=V`，后者 `export K=V`） | 今天无害（都在 `~/.maos-matrix/`、都 600、都不入库），但「换口令」这件事现在要去两个地方，而漏掉一个的症状是某几个号登不上、报 `M_FORBIDDEN` | 归运维面。合并成一份的前提是 `up.sh` 与 `add_agents.sh` 都改（两个脚本都不在同一轨白名单里），所以本轨没并。真要并的话让 `up.sh` 也写 `export` 形式，两份合成一个 `~/.maos-matrix/creds.env` |
| 2026-09-03 | P9 | **`add_agents.sh` 与 `up.sh` 各有一份 `reg()` / `login()` / `whoami_ok()` / `jget()`**，逐字重复（本轨照抄 `up.sh:63`、`:113-122`、`:149-161`、`:163-167`） | shell 没有 import，抽公共库要么 `source` 一个第三文件、要么把两个脚本合并 —— 前者给两个脚本各加一条运行时依赖，后者让 `up.sh` 变成一个什么都干的巨脚本。今天的代价只是「改限流退避要改两处」 | 归 hiclaw 部署面。真要抽就在 `deploy/synapse/` 下新开一个公共 shell 库、由两个脚本各 `source` 一次，**同一轨里两个脚本一起改**，别留半截 |
| 2026-09-03 | P9 | **`$(...)` 命令替换里的函数改不了外层变量** —— 本轨的 `login()` 第一版把「登录次数 + 两次之间歇 3 秒」写在函数里，而它是被 `token="$(login ...)"` 调的，跑在子 shell：计数自增传不回来，判据恒为假，**五个号连着登、一次都没歇**。实跑没撞 429 纯粹因为 `rc_login.address` 的 `burst_count` 正好是 5。本轨已把计数与 sleep 挪回主循环 | 本轨已修。记在这里是因为 **`up.sh` 的 `login()` 也是在 `$(...)` 里被调的**（`:175`、`:226`）：它今天没有跨调用状态所以没踩，但任何人给它加计数、加统计、加「这次一共登了几次」都会当场踩同一个坑，而症状是**限流保护静默失效**，不报错 | 给 `up.sh` 的 `login()` 上方补一行注释点破（它不在本轨白名单，没动）。真要在函数里维护状态，只能改成「函数把 token 写进一个全局变量、调用方读它」，不用命令替换 |
## task-T88（房间接线：router 三个圆桌钩子 + /team + 装配）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | 三个圆桌钩子跑在 Matrix 的**回调线程**上，而那条线程只有一个 worker（`hiclaw/matrix_bus.py:378-381`，单 worker 是刻意的：房间消息的到达顺序就是处理顺序，先 /refund 后 /approve 不许颠倒）。一次 `/refund` 的钩子里要发生五次模型调用 | 圆桌说完之前，房间里下一条消息一直排队 —— 五岗各一次真模型调用按 DeepSeek 的响应算是十几秒，期间打 `/help` 也不会有反应，症状与「机器人卡住了」无法分辨。而本模块承诺的正是「不会沉默」 | 两条路：把钩子丢进第二个 executor（代价是丢掉那个顺序保证），或让圆桌先在房间里发一句「五岗正在看这一单」。归整合轮真房间实跑之后按实际耗时定，别先猜 |
| 2026-09-03 | P9 | `docs/ingress-setup.md:34` 那张 §1 命令面总表里没有 `/team` 这一行 | 命令面有两处口径：§1 的总表与本轮新写的 §4.8。总表少一条，拿它当清单去核对命令的人会漏掉 `/team` | §1 不在本轨的段落白名单内（本轨只许在 §4.7 之后追加一节），归整合轮统一补。届时「谁能用」那一列写「所有人」—— 它刻意不吃 `ALLOW_APPROVAL` 那道渠道闸 |
| 2026-09-03 | P9 | `hiclaw/room_ingress.py` 的 `_open_voices` 只兜 `ImportError`，不兜发声面构造期自己抛出来的异常 | 契约 §1.3 把「缺号 / token 失效 / 号没进房」的退化责任放在发声面自己身上，所以今天成立；但它哪天在构造期抛了别的异常，`main` 会连命令面一起起不来 —— 而那正是本模块承诺不会发生的事 | 派单只写了 ImportError 一档，本轨没自行扩大（铁律 4）。等 T84 的发声面落地、实跑过一次缺号路径之后再定：要么在发声面里兜死，要么这里补一档退回代言 |
| 2026-09-03 | P9 | 自然语言起单（房间里说人话直接开单）不在本轮范围 | `/team` 与五岗发言都要先打命令或拖表；房间里说「帮我退了 ORD-2026-0001」仍然只会得到一句闲聊回话，而人不会觉得那是「没起单」 | 归另一个整合轮 `integrate/p9-t66-t69`（房间自然语言值守），本轮不碰 NLU 面（契约 §5 第 6 条） |
## task-T89

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **2026-09-02 那条「同号不同规则」还在，且本轮又押了三张订单上去**：`scenarios/custom/ledger.json` 的 `AS-003` 是「发错货全额退」（`wrong_item`），`scenarios/refund/policy/policy_rules.json` 的 `AS-003` 是「人为损坏免责（需图片举证）」（`artificial_damage`） | 新加的 `ORD-2026-0004/0005/0006` 也都命中演示底账那三条 AS-，房间演示里规则审核岗会念出 `AS-003@v1`。拿规则号当口径讲的地方（答辩、runbook §10、PPT）一旦跨语料引用就会对错人，而两边都不报错 | 沿用 2026-09-02 那条的建议：给两套语料的规则号加租户前缀，或在两份语料抬头各写一句「本文件的 AS-00x 只在本租户内有意义」。语料面，本轮仍不动 |
| 2026-09-03 | P9 | **「证据齐」剧情在 CSV 那条路上只演得出一半**：随案证据喂不进去（理由见 `docs/DECISIONS.md ## task-T89` 第三行），只能演「质检报告与诉求对得上」 | 只跑 `python3 scripts/room_team_smoke.py` 的人看不到 `verdict="complete"`，要进房间拖一张图才凑齐。README 与 runbook 都写明了这一点，但它仍是一条「文档补代码」的补丁 | 两条路：① 给 `build_case` 一个可选的按 `case_id` 过滤的证据来源（要动 `scripts/run_requests.py`，本轮只读面）；② 给冒烟脚本加 `--evidence <目录>`，把本地文件当随案证据喂进圆桌的 `evidence` 入参。②更小，归圆桌面下一轮 |
| 2026-09-03 | P9 | **`scripts/room_team_smoke.py` 的装载成功路径本轨一次都没实跑过**：T87 的 `maos/roundtable/` 并入前，它只走得到「圆桌引擎未装载 → exit 3」那条退化分支 | 装载后的排版、`TEAM_ORDER` 排序、`check_said` 自检、`--json` 形状，全部只有单元级假件在测，没有端到端实跑过。`RefundRoundtable(model, voices, ledger_loader=...)` 的调法一旦与 T87 实现对不上，会以 `TypeError` 落在 exit 1 上 | 整合轮并完 T87 的第一件事：跑一次 `python3 scripts/room_team_smoke.py` 与 `--json`，把输出贴进整合回执；对不上就在整合轮当场改这一个文件 |

## merge-p9-t84-t89（退款圆桌六轨整合轮，2026-09-03）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | `docs/DECISIONS.md` 里 2026-09-03 那批 ingress 在制品的决策行（申请表入口、闲聊回话器、`room_ingress` 常驻、nio 回调死锁）挂在 `## merge-integrate-p8-t47-t53（主干并入整合轮，2026-09-01）` 这个标题下 —— 标题的日期与轮次都对不上那批内容，它们是主仓在制品，不是那次整合的产物 | 按标题找 ingress 那批决策会找不到；后来人读到 09-01 的标题下有 09-03 的行，会以为记账时间线乱了 | 下次动 `docs/DECISIONS.md` 结构时另起一节安置；本轮属铁律 4 范围外，不当场改 |
| 2026-09-03 | P9 | 两个新 artifact kind（`refund_evidence_report`、`refund_risk_report`）按契约 §1.1 各写在自己的 Agent 文件里，没有进 `maos/agents/refund/_base.py::ALL_REFUND_KINDS` —— T85 / T86 两轨各自记过一条，整合后它们仍然散着 | `ALL_REFUND_KINDS` 不再是本域 kind 的全集，按它做遍历的地方会漏掉两个新 kind（目前没有这样的消费方，所以还没有症状） | 下一次动退款域 Agent 时收拢进 `_base.py`；收拢时两轨的 DECISIONS 各有一行说明为什么当初分开放 |
| 2026-09-03 | P9 | 演示表 `scenarios/custom/refund-requests-team.csv` 里 `ORD-2026-0006` 占两行（剧情③大额 + 剧情④重复退款），走老链路 `scripts/run_requests.py` 会把同一单批准两次、合计金额把 88000 算两遍 | 单看老链路的汇总行会以为演示数据造错了；实际是有意让「同一单被重复申请」这件事在没有风控岗时看起来完全正常，有了风控岗才亮信号 —— 但这层意图只写在 T89 的 DECISIONS 里，汇总输出本身不解释 | 真房间演示时口头点明；若将来要在 `run_requests` 的汇总里提示重复订单号，那是另一轨的事（`run_requests.py` 本轮只读面） |

## task-T90（圆桌合议收口引擎，2026-09-03）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | `verdict.py` 的 `_NEUTRAL` 禁止词改写是**兜底**，不是根治：真正该做的是让 skill 出参本身不宣布终态（`refund.risk_screen` 的 reasons 里目前就有「已有一笔退款到账（settled）」这类措辞，只是恰好不含那五个连续词） | 现在的分工是「skill 可以说观察到的外部状态，收口卡负责改写」。哪天有人直接把 `risk.reasons` 贴进房间（不经 `decide()`），铁律 8 就漏了一个口子，且不会有任何测试红 | 下一次动退款域 skill 的出参文案时，把「外部状态一律写成观察句式」收进 skill 侧；那时 `_NEUTRAL` 可降级为纯守卫（命中即 log.error 而不改写） |
| 2026-09-03 | P9 | `on_sheet()` 返回的 reports 喂不进 `decide()`：`facts_sheet_*` 的 data 键（`rows`/`approve`/`reject` 计数）与逐单五岗的键完全不同，`decide()` 会一路走到第 6 行兜底给 `need_more` | 一张表没有收口卡。现在的形态是 `/refund` 拖 CSV 之后房间里五岗各汇总一句，然后没有主席发言 —— 与逐单那条路不一致，但不报错 | 本轮不做（契约 §0.2 只把收口卡定在 `on_preflight` 上）。要做的话是另一张卡（「这批 N 单里 M 单可批、K 单缺件」），形状要先进跨轨契约，归下一轮圆桌面 |
| 2026-09-03 | P9 | 现有演示语料让 `decide()` **演不出 `approve` 与 `escalate`**：五单的证据核验全是 `missing`（成因见 `docs/BACKLOG.md ## task-T89` 第二条：CSV 那条路喂不进随案证据），真值表第 3 行先于第 4、5 行命中，实测四单 `need_more` + 一单 `reject` | 真值表六行里有四行在演示中走不到，只有单元测试覆盖。答辩现场若只跑冒烟脚本，看不到「建议批复」与「建议升级审批」这两张卡 | 归 T93（演示与证据轨）：按 T89 那条建议的方案②给冒烟脚本加 `--evidence <目录>`，或在演示表里补一单证据齐全的。**不许**为了让演示好看去调真值表顺序或放宽第 3 行 |

## task-T91（房间收口面，2026-09-03）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | 契约 §2.3「模型把 `Verdict` 说成人话」本轨**没做**：收口卡是纯渲染，逐字复述 `decide()` 给的 `headline` / `reasons` / `blockers` | 房间里那张卡读起来是结构化的，不是人话。四种 recommend 的措辞完全由 §2.2 的逐字模板决定，演示时观感偏「系统输出」而非「主席发言」 | 派单 §2 的四件事没列它，测试要求里也没有，故不当场做。要做的话须先解决 R8：本轨测试里一次都不许无参调 `select_model_client()` / `ChatResponder()`，模型必须显式注入。归真房间实跑（T93）之后按观感定 |
| 2026-09-03 | P9 | `mention()` 只按契约 §5.1 的逐字判据（`@` 开头 + 含 `:`）判合法，`@a<b&c:x.org` 这种含非法字符的 mxid 仍会拼出 `matrix.to` 链接 | 转义后不会破 HTML（`&lt;` / `&amp;`），但在 Element 里是一个点不开的蓝字 —— 正是契约那句话想避开的形态。真房间里 mxid 由 Synapse 发，出现非法字符的概率极低 | 判据要不要加严（比如按 Matrix 的 localpart 字符集校验）是契约方（`review/verdict-contracts.md` §5.1）的事，不是本轨能自己拍板的。下次动那份契约时一并定 |
| 2026-09-03 | P9 | 预告与收口卡都走主通道 `channel.send`，与圆桌钩子在**同一个 executor 线程**上 | `MAOS_TEAM_PACE_MS` 设大了会把那个线程占得更久（五岗 = 五次 sleep）。缺省 0 时行为与本轮之前逐字一致，所以今天没有症状 | 派单已写明「把钩子挪到第二个 executor 本轮谁都不做」，BACKLOG 原有那条记的是同一件事。挪线程时把 `pace` 的总耗时（`5 × MAOS_TEAM_PACE_MS`）一起纳入考量 |

## task-T92

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | `USAGE`（`/help` 的输出）与 `/team` 的回帖里都没有「@<岗位名> <问题>」这条用法 | 点名问答做出来了但不可发现：房间里的人不会知道可以直接 @ 某一岗，只会继续对着 `maos-bot` 说话 —— 而那正是这一轨要消灭的观感。`USAGE` 同时会进 `_chat_facts`，模型也因此答不出这条用法 | 派单 §3 没列这一条，本轨没自行扩大（铁律 4）。归整合轮与 `docs/ingress-setup.md` 的命令面总表一起补（那张表里 `/team` 也还缺一行，T88 已记） |
| 2026-09-03 | P9 | `_chat_facts` 喂给闲聊模型的待办清单不带合议建议，`/pending` 带 —— 同一份待办两处口径 | 人问「这几单什么情况」（走闲聊）与打 `/pending` 会得到详略不同的两个答案，而两边都不报错。收口卡落地后差距会更明显 | 等 T90 的 `decide()` 并进来、真房间实跑过一次收口卡之后再收：届时确认 `headline` 的措辞进模型提示词不会诱发复述失真（红线 R1），再把这一行加进 `_chat_facts` |
| 2026-09-03 | P9 | 点名只认纯文本里的**句首显示名**与手打 mxid，认不了 Element 真正的 @提及结构 —— `on_message(sender, body)` 只给纯文本 body，`formatted_body` 里那个 `matrix.to` 链接到不了这一层（`hiclaw/matrix_bus.py` 回调签名是共同只读面） | 显示名改名（Synapse 上改 displayname）之后，房间里 @ 出来的 pill 文本就与 `TITLES` 对不上，点名静默失效，回帖退回闲聊 —— 没有任何报错，只是「@ 了没反应」 | 要根治得先动 `matrix_bus` 的回调签名（把 `formatted_body` 一并交上来），那是跨轨共同只读面，得先有人拍板。归房间接线面下一轮；在那之前，运维侧的约束是「岗位账号的 displayname 必须与 `TITLES` 一致」 |

## task-T93（收口轮演示面，2026-09-03）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-03 | P9 | **`docs/ingress-setup.md` §5「已知边界」第二条现在是假话**：它写着 `requires_evidence_kinds` / `min_evidence_count` 这两个键「在 `maos/**/*.py` 里零命中，政策引擎只读 `refund_ratio` / `deduct_fee`」。T85 的 `maos/skills/builtin/refund/evidence_check.py` 已经是这两个字段的消费方（`_declared_requirements` 逐条读它们） | 读那一节的人会以为交一张图和不交结论一模一样 —— 而本轨的四种结局演示恰恰建立在「交了图证据核验就从 missing 变 complete」上，两处说法直接打架。那条的后半句（政策规则本身不因证据改裁定）仍然成立，错的是「零命中」这个事实陈述 | 本轨白名单只覆盖该文件的 §1 命令面（派单 §3.4），改 §5 属手册范围外的顺手优化（铁律 4），不当场改。归整合轮与 runbook 刷数同批 |
| 2026-09-03 | P9 | **四种结局的收口卡本轮只在冒烟脚本里验过形态，真房间一次都没看到过** —— 合议引擎（`maos.roundtable.verdict`）归 T90，本轨基线上不存在 | 本轨回执里那四行 `headline` 的**数据**（金额、风险档、缺口、驳回理由）来自实跑，措辞来自跨轨契约 §2.2 的逐字模板，但没有任何一行是 `decide()` 真吐出来的。措辞若与 T90 的实现有出入，runbook §10.5 那四张卡就要跟着刷 | 整合轮并完 T90/T91 的第一件事：跑一次 `python3 scripts/room_team_smoke.py --evidence scenarios/custom/evidence`，把四张真卡与 runbook §10.5 逐字比对，对不上就刷文档 |
| 2026-09-03 | P9 | **`ORD-2026-0006` 那两行（剧情③大额、剧情④重复退款）的收口结论完全相同**（都是 `escalate` / 风险 high / 核准预演 88000.00），两行的差别只体现在 blockers 多一条「申报金额 92000.00 高于订单实付」 | 演示时连着出现两张几乎一样的收口卡，boss 看不出这两行在演不同的东西；而它们本来分别要拎的是金额面与历史面 | 语料面（`refund-requests-team.csv` 本轮可改，但四种结局已经齐了，不为这个再动数据）。真房间演示时口头点明，或下一轮把剧情③换成一张风险 low 的大额单，让「大额」单独成一格 |
| 2026-09-03 | P9 | **`var/attachments/` 被冒烟脚本写入，而它只写不删、没有清理机制**（`docs/ingress-setup.md` §5 已记过这条，本轨给它添了一个新的写入方） | 每跑一次 `--evidence` 就往库里落一次；内容寻址下同一张图只落一份，所以演示语料不变时目录不会长大 —— 但换一批演示图就会留下旧的 | 沿用原条建议（一条按 mtime 的运维清理）。本轨不新增清理逻辑：能删证据的代码路径越少越好 |
| 2026-09-04 | P9 | **表头缺列时，房间里五岗仍只听得到「N 行填写有问题」，听不到「错在表头一处」**。`sheet.render` / `sheet.summary` 已经把缺列说成表头问题，但圆桌拿的是契约 §1.4 的八个键，`sheet_stats` 只数三态计数、不看 `problems` 文本 | 群里那份回帖是对的，房间合议那一路仍会把人往「去改那 N 行」带 —— 而那 N 行本来全是对的。程度比修复前轻：每行 `problems` 的文本已经是「表头里没有『诉求类型』这一列」，五岗要看行文本才看得见 | 要么给 §1.4 加一个表头级的键（**动契约面，必须先问人**），要么让事实卡在 `problem_rows` 非零时带上第一条 `problems` 原文。后者只动 `maos/roundtable/stages.py` 的事实卡措辞，归下一次动那个文件的批次 |


## task-C1（多域证据核实的范围外缺口，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | C1 | 场景 8–10 均未注入 replanner，域内补偿没有逆补丁产物，不经过 `_gate_compensation` 干跑；财务闸仍限定 refund | 内核实现可共用，但三新域的重规划、补偿预验证及通用财务闸尚无端到端证据；网关码闸限制已见 task-T37 / task-T39 | 后续由域流程与运行时负责轨补覆盖；本轮仅如实标 ⚠️，不改内核 |
| 2026-09-05 | C1 | `collect_business_objects` / `check_business_ref` 仍只认退款 `business_ref`，未接理赔 `claim_business_ref` 与 AP `ap_business_ref`；调查本来没有引用表（已见 task-T38） | 本次三项核心核验覆盖新域，但第 2 项及 business-objects.json 尚不核验理赔/AP 的业务引用，不能把聚合 PASS 当作该项已跨域覆盖 | 下一次扩展第 2 项时接各域现有 resolver 与引用表；本轮派单只要求第 3/6/7 项，不顺手扩大 |
| 2026-09-05 | C1 | `scripts/demo_preflight.sh` 的 EXPECT_TESTS_NOPG 仍为 1476，已落后于本轮实测的 1986；默认束数仍正确固定为 8 | 直接运行完整预检会因测试数量旧值报错，独立执行本派单逐条验收可通过 | 后续整合轮刷新测试数量；当前可显式设置 MAOS_EXPECT_TESTS=1986，C1 不改预检脚本及冻结束数 |

## task-T95（可视化出口的范围外缺口，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | T95 | **`make_evidence.py` 与 `render_trace.py` 之间没有机器绑定**：单跑前者不会带出 HTML，`evidence/report.html` 当场变陈旧 | 表现是 `python3 scripts/render_trace.py --check` 与 `maos/tests/test_render_trace.py::test_committed_report_is_in_sync` 一起变红。这是**正确的红**（证据变了投影没跟），但它出现在一条谁都会走的日常路径上：跑一次证据束就红一次，两条命令的先后关系只写在文档里 | 整合轮把 `python3 scripts/render_trace.py` 接进 `scripts/demo_preflight.sh` 与 `scripts/make_release.sh` 的证据环节（跑完证据束顺手渲染）。本轨按派单 §3.4 选了独立命令，不动 `make_evidence.py` 的缺省输出，所以这条绑定留在脚本外 |
| 2026-09-05 | T95 | **OTLP 产物不能直接喂 collector**：`python3 scripts/render_trace.py --otlp` 产出的那份 JSON，首行是铁律 3 的 `# generated at …` 注释，不是合法 JSON（该文件缺省不产、也不入库，所以这里不写死路径） | 「这是标准格式，可以喂给任何 collector」这句话要跟一句「先 `tail -n +2`」。真接 collector 时还有第二道：`trace_id` 的 32 hex 是本脚本 sha256 映射出来的，collector 侧看到的 id 与库里、与 `verify.py` 报告里的 `trace_b81e77f2522c` 对不上，要靠 span 属性 `maos.trace_id` 反查 | 真要接 collector 的那一轮再定：要么给这一个出口破铁律 3 的例，要么在导出侧再产一份无头副本。本轨不预先破例（`docs/gateway-rationale.md` 已记过 `trace_id` 格式这条硬阻塞） |
| 2026-09-05 | T95 | **`artifacts/` 下那两份手写 HTML（183K / 223K）仍然是手写的**，本轨没有取代它们 | 它们仍然是「跑完场景要有人回来改、于是没人改」的那一类材料，与新产的 `evidence/report.html` 讲同一批事实却各自维护，有对不上的风险 | 范围外，不当场改（铁律 4）。下一轮决定：要么把这两份也变成脚本产物并纳入 `--check`，要么明确它们只讲静态叙事、动态数字一律指向 `evidence/report.html` |
| 2026-09-05 | T95 | **页面把八束里 13 条 trace 全量摊开成一页 304 KB**，`scenario-R5`（3 条 trace / 117 span）与 `scenario-7`（2 条 / 103 span）是大头 | 现在还很轻快（浏览器打开无卡顿，`<details>` 默认收起了结构树与完整事件序列）。但域扩展到 8–10 之后束数会翻倍，单页会到 MB 级 | 到那一步再拆：按束分页、或把结构树与完整事件序列改成点开才渲染的内联数据。现在拆是过早优化，且会破掉「一个文件、双击就能看」这条最值钱的性质 |
## task-T94（跨域协同场景 11 的范围外发现，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | T94 | 一个 plan 同时挂两个域的业务对象时，`verify.py --domains` 会把**同一条** business-outcome 失败在两个域的检查项里各报一次 | 实测（把 scenario-11 的 returned 观察篡改成 `camt.029` 之后）`investigation/business-outcome` 与 `ap/business-outcome` 打印的是同一条理由，读起来像两处独立缺陷，实际是一个 plan 的一份 `business_outcome` 坏了。判定本身是**对的**也更严（跨域那份结论装着两个域的判据，坏一条整份不成立），只是措辞没说清「这条是从哪个域看过去的」 | 下次动 `check_business_outcome` 时给理由前缀加上「按 X 域核验」；本轨不改 —— verify.py 是四个域共用的判据面，为措辞动它风险不划算 |
| 2026-09-05 | T94 | 跨域场景的 DAG 与前面四个业务域一样，是**直接交给 `create_plan` 的规格列表**，不走 `ManagerAgent` 规划（`scenario_9.py:231` 已就单域说明过这一点） | 于是「Manager 能不能**规划出**一条跨域 DAG」这件事一次都没被证明过 —— 现在证明的是「跨域 DAG 交给内核能跑通」。这两句话的差距，正是评委最可能追问的一处：方案是人写死的，还是系统排出来的 | 真要证明得先解决 `ScriptedModelClient` 按关键字查表的确定性问题（塞两份方案 JSON 就得靠提示词关键字分派）。建议留到接真模型客户端的那一轮，与「跨域 replan」一并做 |
| 2026-09-05 | T94 | task-C1 记的「`collect_business_objects` / `check_business_ref` 只认退款 `business_ref`」在跨域这一束同样成立：scenario-11 的库里 `ap_business_ref` 有 **4 行**，而 `business-ref` 检查项的计数在有无 scenario-11 时都是 `35/35` | 补一个实测数据点：跨域没有改善这个缺口，也没有让它变坏。第 2 项对新域业务引用的覆盖仍然是零，不能把聚合 PASS 读成「业务引用已跨域核验」 | 与 task-C1 那条同批处理；本轨派单只要求第 3/6/7 项，不顺手扩大 |
| 2026-09-05 | T94 | `scripts/demo_preflight.sh` 的 `EXPECT_TESTS_NOPG` 仍是 1476（task-C1 已记落后于 1986），本轨再增 21 条后实测为 **1992** | 直接跑完整预检会因测试条数旧值报错；逐条执行派单验收不受影响 | 与 task-C1 那条同批刷新。当前可显式 `MAOS_EXPECT_TESTS=1992`；本轨不改预检脚本，也不动冻结的默认八束 |
## task-T96（补偿失败要有人管，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | T96 | **行号锚点集体漂移**：本轨给 `maos/core/control_plane.py` 加了 82 行、`maos/runtime/gate.py` 加了 22 行，仓库内约 95 处 `<file>.py:<line>` 形式的文档锚点跟着漂 | `scripts/check_docs.py` 的 `E-line` 只判「行号超出文件实际行数」，加行不会触发，所以**一条阻断都不会报** —— 漂掉的锚点是静默的：评委照锚点跳过去看到的是另一件事。这正是 `## task-T4x` 里那条「行号锚做一次全仓校验器」预言的形态 | 本轨已回填 `docs/defense-brief.md`（14 处，白名单内，逐条比对过内容逐字相同）。其余在白名单外，**没动**，映射规则如下，照它一次回填即可：`control_plane.py` 旧 14–120 → +1、旧 121–753 → +11、旧 754 起见 `git diff` 逐段对；`gate.py` 旧 58–875 → +1、旧 876 起 → +21。涉及 `docs/ppt-outline.md`、`docs/matrix-room-runbook.md`、`docs/domain-portability.md`、`docs/parallel/contracts.md`、`docs/refs/cumora-*.md`、`docs/authoritative-facts.md`、`docs/demo-script.md`。**`docs/BACKLOG.md` 与 `docs/DECISIONS.md` 里的锚点不要回填** —— 那些是历史记录，描述的是当时的代码 |
| 2026-09-05 | T96 | `control_plane.py:901` 的 `except NotImplementedError` 分支**现在走不到**：`maos/tools/sandbox.py` 全文零 `NotImplementedError`（Task-B 合并后 `sandbox_git_apply` 是真实现），注释里那句「Task-B 合并前的预期状态」已经过期 | 无害但误导：它构造的 `stage="sandbox_unavailable"` 是一个再也不会出现的取值，读代码的人会以为沙箱缺位仍是一条活着的失败路径。**它同时是本轨处置的失败路径构造点之一**，所以更不能顺手删了了事 | 派单第 2.4 条明写「确认走不到就记 BACKLOG，不当场改」（铁律 4），故只记不改。真要动的时候二选一：连注释一起改成「沙箱实现被替换成不支持 reverse 的版本时的兜底」，或者删掉并给 `sandbox_git_apply` 加一条「永不抛 NotImplementedError」的守卫测试。别默默留着一个自称临时的分支跨越两个阶段 |
| 2026-09-05 | T96 | `evidence/room/README.md:95-102` 的「房间里拍不到补偿」现在**只对一半** | 那段说的两条依据里，「`CompensationExecuted` 走 `append_event_log`、从不 publish」仍然成立；但补偿失败开出的工单挂在 `BLOCKED -> FAILED` 那一跳的 `detail` 上，而**状态迁移是逐条镜像进房间的** —— 本轨实跑 `python -m hiclaw.room_demo --case reject --auto-approve --allow-degraded`，工单整份 JSON（含 `ticket_id` / `workdir` / git 报的原话）出现在房间消息里。也就是说「驳回之后补偿到底成没成功」在房间里第一次有了外化形态，`04-reject-compensation.png` 那个文件名比改造前贴近事实了 | `evidence/room/**` 不在本轨白名单，没动。下一次采集房间截图的轨顺手改那段话，并考虑重拍 `04`（现在它拍得到工单了）。⚠️ 只是这一条不要说过头：**成功**路径仍然一行都不打、房间里仍然看不见 —— 变的只有失败路径 |

## task-T98（暂存挑着取 + 认表判据显式化，2026-09-05）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-05 | T98 | `ALLOWED_MIME` 今天只收 jpeg / png / gif / webp / heic / pdf。老板把退款申请表存成 `.xlsx`（Excel 的默认格式，不是「另存为 CSV」）拖进群，会被类型闸原样拒掉 —— 而 `sheet.py` 那条入口正是给不写代码的人用的 | 拒得出声（回一句「不收这个类型」），不是静默失败，所以不是 bug 只是缺口。但它与 `ENCODINGS` 认 gbk 是同一个取向的两半：认 gbk 是为了迁就中文 Windows 的 Excel，而那个 Excel 默认存出来的其实是 xlsx | 派单 3.3 明写不许在本轨扩白名单（铁律 4），只记不改。真要做的是**两件事**不是一件：白名单加一类，和 `looks_like_sheet` / `parse` 认 xlsx（zip 容器，得解 sheet1.xml，本机没有 openpyxl）。第二件才是主要成本，别把它读成「加一行白名单」 |
| 2026-09-05 | T98 | `looks_like_sheet` 的 NUL 闸（`b"\x00" in data[:4096]`）与新加的类型闸有重叠：PNG 两条都撞，PDF 只撞后一条 | 没有害处，两条判据各挡各的一类（类型闸只认识六种魔数，NUL 闸兜住其余一切二进制，比如那个改名成 .csv 的 ELF）。记下来是因为读代码的人会问「有了类型闸为什么还留着 NUL 闸」 | 不要删任何一条。真要动的时候先想清楚：删 NUL 闸，ELF 那条测试就得靠附件白名单兜；删类型闸，本轨那条对抗样本会红 |

## task-T111（计划审批停靠点，2026-09-08）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-08 | T111 | `Store` 抽象类没有「列出全部 plan」这个方法。`maos/obs/trace.py::list_plan_ids` 为此自己开了一条 `sqlite3.connect`，本轨的 `PlanApprovalQueue._all_plan_ids` 则借核心 store 的 `_conn` / `_lock` 写了同一条 SQL —— 同一个需求现在有两份实现，且都绕过了 `Store` 抽象 | 换非 sqlite 后端时两处都得改，而 `trace.py` 那条另开连接的写法还绕过了全仓唯一的写互斥（只读，暂时无害）。`store.py` 表结构禁改但**加方法不改表**，`list_plans()` 属于可以做的增量 | 整合期或下一轨给 `Store` 加 `list_plans() -> list[dict]`（纯新增抽象方法，不动五张表），把这两处收口。本轨没做：`store.py` 在白名单外（铁律 4） |
| 2026-09-08 | T111 | `ControlPlane._apply_replan` 与 `_replanner` 是私有的，但它们是「新规格接管旧任务」的唯一口径，本轨与 `_replan` 两个调用点都得从外面伸手进去拿 | 私有名没有兼容承诺，改签名时不会有人想到 worktree 外还有一个调用点。本轨已在模块 docstring 与 DECISIONS 里写明这是有意为之，但那只是留痕，不是约束 | 整合期把 `_apply_replan` 提升为公开方法（比如 `apply_replan`），或给 `ControlPlane` 加一个「重规划但不启动」的公开入口。**别在提升之前顺手改它的签名** |
| 2026-09-08 | T111 | `Store.claim_idempotency(key, op, task_id)` 的第三个参数与 `processed_key.task_id` 这一列，名字都写死成 task。本轨往里传的是 plan_id | 只是旁注列、不参与唯一性判定（唯一键是 `idempotency_key`），所以行为没问题；但按 `task_id` 去查 `processed_key` 的人会拿到一个其实是 plan_id 的值，排查时会绕路 | 真要治得改列名，属于改表结构（铁律 1 禁改），成本远高于收益。建议只在 `store.py` 那个方法的 docstring 上补一句「这一列是旁注，不限于 task」。本轨没动：白名单外 |
| 2026-09-08 | T111 | 没有任何生产链路接进 `PlanApprovalQueue` —— 所有场景仍是 `create_plan` 之后立刻 `start_plan`，`test_nothing_in_production_imports_the_approval_queue` 这条守卫把这件事钉住了 | 本轨交付的是能力与那条缝，不是演示。缺省路径逐字节不变（这正是铁律「缺省零影响」要的），但也意味着评委在 `run.py` 里看不到人工审批 | 整合期主会话决定**哪一条**链路从此要过人工审批，接的那一刻上面那条守卫测试会红 —— 红得应该，它逼人明确回答这个问题，而不是让审批悄悄生效。接线时记得同步改那条测试 |
| 2026-09-08 | T111 | `pending()` 缺省会把库里每个 plan 都 `get_plan` + `list_event_log` 扫一遍，判「有没有 PENDING→RUNNING 过」 | 演示规模（几十个 plan）下无感；plan 数量上千之后这是一次全表 + 全事件扫描 | 真到那个量级再治，治法是给 `event_log` 加一条 `(plan_id, event_type)` 索引，或在 SQL 里直接把两条判据一起筛掉。现在做属于过早优化 |
| 2026-09-08 | T111 | **变异实测会被 `__pycache__` 骗**：本轨把「幂等闸排到状态校验前面」这条变异做完、`git checkout --` 还原之后，测试仍然红 2 条，而 `git status` 干净、`md5` 与 HEAD 里的 blob 逐字节相同 | 差点被当成真回归上报。真因是那条变异只**挪动**了一行，改后文件与原文件**字节数相同**，而变异与还原发生在**同一秒**内 —— CPython 的 pyc 失效判据就是「源文件的 mtime 秒 + 字节数」，两项都没变，于是解释器一直在跑变异版的字节码。`inspect.getsource` 读的是源文件所以显示正确，行为却是变异的，两边对不上 | 以后做变异实测一律加 `PYTHONDONTWRITEBYTECODE=1`（本轨复测就是这么做的，六条变异全部复现、还原全绿）。要收进脚本的话，`scripts/` 下哪个跑测试的入口都可以带上这个环境变量；本轨没动 `scripts/`（白名单外）。同类症状的通用排查手法：`find . -name __pycache__ -type d -exec rm -rf {} +` 之后再跑一遍，结果变了就是它 |
| 2026-09-08 | T111 | `PlanApprovalQueue._reattach_trace` 是一段**事后补救**：`ControlPlane._apply_replan` 在 `open_tasks` 为空时给新建任务现造 trace_id，本轨调完再把它改回 plan 的 trace_id | 中间态在库里存在过一瞬（insert 用现造的 id、随后 update 回来）。同一个事务里没有别人看得到它，但这是绕行不是修根 | 与上面那条「把 `_apply_replan` 提升为公开方法」一起做：让它接受 plan 的 trace_id（或直接从 `store.get_plan` 取），`_reattach_trace` 连同它那段 docstring 一起删掉。**别只删补丁不改上游** —— 删了那条路就退回「用量在成本视图里整段消失」 |
## task-T110（生命周期 hook，2026-09-08）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-08 | T110 | `WORKER_IDLE` **只定义、未接线**：常量、payload 形状（`worker_id` / `roles` / `just_finished_task_id`）与一条用假 registry 直接 `fire` 的测试都在，但 `maos/runtime/worker.py` 里一行都没加 | 三个挂点里只有两个真的挂上了。写 hook 的人照着 `EVENTS` 注册 `WORKER_IDLE` 会注册成功、然后永远不被调用 —— 正是本模块开篇要治的「静默失效」那个病的一个残留 | 接线点是 `maos/runtime/worker.py::WorkerRuntime._reply` 之后。本轨没接是因为 `worker.py` 这一轮归 T107，跨轨改同一个文件只会给整合期多制造一个冲突。**整合期接**，接的时候连 `test_worker_idle_is_not_wired_into_the_worker_runtime` 那条反向守卫一起改掉（它现在断言 worker.py 里不出现 `worker_idle`） |
| 2026-09-08 | T110 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` 与 `docs/submission-checklist.md:24` 的锚点行仍是基线的 2259，而 T107–T111 五轨各自都新增了测试。**本轨刻意不改**（一度改成 2284，已 `git checkout 8a6c2f9 --` 回滚） | 每一轨交回来的全量都会剩一条**预期内**的红：`test_expected_metrics.py::test_collected_count_matches_source_of_truth`。这不是回归，也不许任何一轨自己去修 —— 五轨改同一行只会撞出四份冲突，而且没有哪一轨手里的数是最终数（各轨新增会重叠，合并后测试之间还可能互相影响） | 整合期，合完五轨之后、打 commit 之前：在合并完成的那棵树上重新实跑一次 `python3 -m pytest maos/tests -q`，拿末行两个数回填两份文件。挑一个值填进去会让门禁在下一个人身上误红 —— 那正是这条守卫的 docstring 里记的、已经发生过五次的那个事故 |
| 2026-09-08 | T110 | `TASK_CREATED` **只覆盖 `create_plan` 一个入口**。`ControlPlane._apply_replan` 的覆写与 `insert_task` 两条路都绕过它，不 fire、不留痕 | 「哪些任务可以被创建」这个判据只管住了首次规划。装一条「role == payment 一律 Veto」的 hook，首次规划拦得住；随后任一任务返工命中 `_should_replan`，重规划就能把 `coding/L` 的任务就地覆写成 `payment/H`、并另建一个 payment 任务 —— 治理明令禁止的两件事都建成了，hook 一次都没被调用，`TaskCreationVetoed` / `HookVetoed` 都是 0 行。**绕过是系统自己触发的，不需要人参与**。已在 `hooks.py` 的常量注释标出覆盖面，并由反向守卫测试 `test_known_gap_task_created_does_not_cover_the_replan_path` 钉住 | 整合期，且必须在 T107 那一轨合进来之后（`_replan` / `_apply_replan` 是它的面）。接的时候两条路都要接：**覆写也算一次「这个任务该不该以这个形态存在」**，只接 `insert_task` 等于只堵了一半。接完把那条反向守卫改成正向断言，别只删掉它 |
| 2026-09-08 | T110 | `TASK_COMPLETED` **不覆盖 `effect_risk ∈ NEEDS_HUMAN_APPROVAL`（今天是 H）的任务**：那一支 Gate 判 pass 直接转人工，最终的 DONE 由 `ControlPlane.human_decision` 的 approved 分支落，挂点两次都不开火 | 同一条治理回调对普通任务生效、对最高 effect_risk 的那一类完全不生效，而且没有任何 `HookFailed` / `HookVetoed` 之类的痕迹说明它被跳过了。这是**刻意的边界**（挂点不参与人的决定），不是 bug —— 但写 hook 的人只看 `EVENTS` 是看不出来的，所以边界写在 `hooks.py` 的常量注释上，并由 `test_task_completed_does_not_fire_for_high_effect_risk_even_via_human_approval` 钉住 | 想覆盖 H 的话，做法是在 `human_decision` 的 approved 分支落 DONE 之前也 fire 一次：人仍可覆盖否决，但「挂点被问过、被人驳回」这件事至少进得了 event_log。`human_decision` 在本轨白名单之外，留给整合期。别用「H 走不到判定完成」当免责理由 —— 它走得到 |
| 2026-09-08 | T110 | 交给回调的 payload 现在是副本（`copy.deepcopy`），但只覆盖了本轨接的这两个挂点；`WORKER_IDLE` 接线时同样要拷贝 | 不拷贝的话，一条 `return None` 的回调就能改写控制面的活对象而不留任何痕迹 —— 与「只认 Veto 一种否决形态」自相矛盾 | 整合期接 `WORKER_IDLE` 时一并做。`deepcopy` 的代价也一并想清楚：今天 spec 是小 dict，哪天 payload 里开始塞大产物就要换成显式的浅投影，别无脑 deepcopy 一个 100MB 的东西 |
| 2026-09-08 | T110 | 「全部 task 被否决」那一档，`HookVetoed` 与 `TaskCreationVetoed` 落在一个**不存在的** plan_id 上（plan 行按设计没建） | 这些行 `list_event_log(plan_id)` 捞得到（event_log 没有外键），但 Trace 侧按 plan 树组织时它们会成为孤儿 —— 与 `docs/BACKLOG.md ## task-X4` 记的 `stray_events` 是同一类形态。不是 bug：这一档本来就没有树可挂，把它们丢掉才是错的 | 下一轨动 Trace / 可观测面时，给这一类补一个「被否决的计划」视图，别让它们只能靠翻库看见。别为了消 warn 去建一个假 plan 行 —— 那是拿假绿换绿 |
| 2026-09-08 | T110 | `HookRegistry` 没有注销（`off`）、没有超时、没有并发保护：一个慢回调能把 `create_plan` 整个拖住，一个死循环回调能把控制面挂死 | 今天不成问题（回调由装配代码在进程内注册，不是第三方动态挂载），但「能否决的挂点」天然会吸引越来越重的回调 —— 一旦有人在里面发网络请求，`create_plan` 的耗时就不再由本仓库说了算 | 真有人往回调里塞 I/O 的那一天再做，且做的应该是超时而不是线程池：超时到了按 fail-open 放行并落 `HookFailed`，与现有的异常姿态同源。现在就加是过度设计 |

## task-T108（邮箱：agent 之间第一条点对点通道，2026-09-08）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-08 | T108 | 本轨新增 33 条测试（收口轮又补了 5 条），`docs/expected-metrics.json` 的 `pytest_passed_nopg` 仍是 2259，于是 `test_expected_metrics.py::test_collected_count_matches_source_of_truth` 红（实际收集 2333，真源期望 2300） | 全量 `python3 -m pytest maos/tests -q` 因此 `1 failed, 2291 passed, 41 skipped`。**这一条失败与收件箱本身无关**：单跑 `maos/tests/test_mailbox.py` 是 33 passed，其余一条没动。守卫本身是对的，它就是来拦这件事的 | 整合收口时把 `pytest_passed_nopg` 刷成**五轨合并后的实测值**（只算本轨是 2292 = 2259 + 33），不要各轨各加各的 —— git 历史里这个数一直由整合那一步回填（`557d1cf`） |
| 2026-09-08 | T108 | 收件箱**尚未接线**：`maos/runtime/worker.py` 没有在执行前调 `deliver_into`，`scripts/verify.py` 也没有对应核验项 | 于是「Agent 不轮询、消息自动到手」这句话目前只有单测证明，真链路上还没跑过。缺省路径因此逐字节不变（这是本轨要的），但也意味着 `run.py` 的七个场景里一条 `AgentMessage` 都不会落 | 派单第 4 节明令接线留给整合期（worker.py 是 T107 的面）。接线只需两处：Worker 执行前 `Mailbox(store).deliver_into(role, now_iso=...)` 塞进 `ctx.inputs["inbox"]`；verify 加一项「`AgentMessage` 事件的 detail 里没有正文」 |
| 2026-09-08 | T108 | `agent_message` 表没有清理/归档口径：已读消息永久留在库里，也没有 TTL 或按 plan 的级联删除 | 演示期的库都是 `:memory:` 或每次新建，看不出来；PolarDB 那种持久库上，这张表会随消息量单调增长，而 `inbox` 的索引是 `(to_agent, read_at)`，未读扫描不受影响、全量 `unread_only=False` 的查询会越来越慢 | 等这条通道真在长跑环境里用起来再定策略（按 plan 归档还是按时间 TTL），现在定等于凭空猜。定的时候要连带想清楚「已读消息属于审计证据还是运行时状态」—— 前者不能删 |
| 2026-09-08 | T108 | **`deliver_into` 取走即已读，是 at-most-once 不是 exactly-once**：本次 attempt 取走消息后执行失败，下一次 attempt 拿到的是一份全新的 `TaskContext`，那条消息不在里面，而它已读了，此后再也不会出现 | 症状是「消息偶尔莫名丢失」，且丢的正是重试路径 —— 也就是最需要上下文的那一次。审计侧不丢（库里查得到），丢的是「送达 Agent」这件事。收口轮已把这条代价写进 docstring 并钉成测试（`test_delivered_message_is_lost_when_the_attempt_retries`），所以它现在是一条**写明的**账单，不是暗雷 | 要改成 exactly-once 得把标已读推迟到「Agent 交回结果之后」，那要动 `runtime/worker.py`（T107 的面，本轮明令不碰）。真做的时候先想清楚它只是把代价换一头：worker 在中途崩掉时消息会**重复**注入，而重复注入的症状（Agent 反复回应同一条请求）比丢失更难看。两头都要的话得给消息加一个「投递中」的中间态，那是第三种设计，别顺手做 |
## task-T107（认领租约与跨 plan 任务板的范围外发现，2026-09-08）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-08 | T107 | **租约层做出来了，但一处也没接线**：`LeaseBook` 缺省不注入，`reap_expired_leases` 没有任何调用方，`pull_and_claim` 也没有。全仓仍然只有推模式 | 本轨买到的是「能力存在且被测住」，不是「演示链路上真的会超时接管」。裸跑 `python3 run.py` 与改造前逐字节一致 —— 这是刻意的（铁律 4），但别把 39 条绿读成「超时接管已经在跑」 | 接线是整合期主会话的事（派单第 5 节明写本轨不碰）。要接三处：`flows/common.py::build` 注入 `LeaseBook`、场景侧起一个周期调 `reap_expired_leases` 的循环、异构队友按 `roles` 构造 |
| 2026-09-08 | T107 | **`reap_expired_leases` 的触发者还没有**：它是一个要被周期性调用的方法，而仓库里没有任何调度器 | 现在只能由调用方手动调。真跑起来之后，「谁来调、多久调一次」是一个必须有答案的问题 —— 调用间隔大于 TTL 时，任务的实际停摆时长是 TTL + 间隔，而不是 TTL | 接线那一轮一并定。别在 `ControlPlane` 里起线程：控制面持有线程会让所有现有测试的生命周期变复杂，而那是 2298 条测试的地基 |
| 2026-09-08 | T107 | **`claim_lease` 表只在 sqlite 后端上活得下来**：`LeaseBook` 借的是 `SqliteStore._conn`，`maos/store/pg_store.py` 那条后端拿不到它，构造时直接 `TypeError` | 选了 PG 后端就用不了租约层。当前无害（PG 后端由 `MAOS_PG_DSN` 门控，缺省不走），但「换后端等于丢掉超时接管」这件事没有任何测试盯着 | 真要在 PG 上跑租约的那一轮，在 `_conn_of` 加一条分支（源码里留了这句话），**不要去改冻结的 `store.py`** |
| 2026-09-08 | T107 | **`(DISPATCHED, PENDING) -> claim_timeout` 之外，冻结迁移表里是否还有零实现的边**，本轨没有系统扫过 | 本轨只把点名的那一条实现了。迁移表里若还躺着别的零实现边，它们和 `claim_timeout` 改造前一样：写在契约里、看着像已支持，实际没有任何代码走得到 | 值得一条守卫测试：遍历 `TASK_TRANSITIONS`，对每条边要求全仓生产代码里至少有一个可达的构造点。范围外，本轨不当场做（铁律 4） |

## task-T107 收口（第一轮复核的范围外发现，2026-09-08）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-08 | T107 收口 | **一个全队伍都不承接的 role 杀不掉，只能被无限重投。** 冻结迁移表里 `DISPATCHED -> FAILED` 与 `PENDING -> FAILED` 都不存在，`dispatch_ready` 也不校验 `max_attempts`；于是新加的派发超时源只能把它按 `claim_timeout` 放回队列，它会在 DISPATCHED/PENDING 之间按 TTL 慢速弹跳，`attempt` 无上限增长，plan 永远 RUNNING | 比改造前的「静默永久停摆」好：每轮落一条 `LeaseExpired`（reason=claim_timeout，detail.source=dispatch），可告警、可查、可计数，不再是无声的。但它**仍然不会自己收敛**，只是从「安静地卡住」变成「吵闹地卡住」 | 真要判死它得往冻结迁移表里加一条边（`DISPATCHED -> FAILED` 或 `PENDING -> FAILED`），那是铁律 1/铁律 9 的面，必须人拍板，本轨不碰。另一条不动契约的路子是在 `create_plan` 那一侧校验 role 属于 `AGENT_POOL` —— 实测它今天直接接受池外的 `"devops"`；但 `create_plan` 是 T110 的方法，本轨不许动 |
| 2026-09-08 | T107 收口 | **`_fail_plan` 的「plan 已是终态就别再判一次」只加在 `_reap_one` 这一个调用点上。** 全仓另有 5 处调它（`control_plane.py` 里 `on_task_result` 的 failed 分支、评审终判、replan 上限、`_advance` 的全冻结分支等），都是无条件调 | 那 5 处今天是否也会撞 `FAILED -> FAILED`，本轨没有逐条验证 —— 只验证了回收这一条会撞（实测 `IllegalTransition: 非法迁移: FAILED -> FAILED`）。它们各自的时序未必允许同一个 plan 被判两次死，但没有任何判据盯着这件事 | 判据应该收进 `_fail_plan` 自身（进去先看 plan 是不是 RUNNING），一处生效六处受益。本轨没这么做是因为 `_fail_plan` 是 6 个调用点的共用方法、且与 T110 共用这个文件，改它会越出「只动 claim 与 on_task_result + 新增 reap_expired_leases」的白名单。整合期一并收 |
| 2026-09-08 | T107 收口 | **派发超时源的 TTL 与租约 TTL 共用同一个 `LeaseBook.ttl_s`**，但两者量的是不同的东西：租约 TTL 量「一次执行要多久」（缺省 300s 按真模型调用时长取），派发超时量「一个活在板上等多久算没人来」 | 缺省下派发超时也是 300s。队伍空闲时这偏长（一个没人接的活要等 5 分钟才回队列），但偏长是安全的一侧 —— 偏短会把**正在排队等人来拿**的正常任务判成无人认领，反复重投 | `unclaimed_dispatched` 已经留了 `ttl_s` 参数可以单独传。接线那一轮量一下真实的「派发到认领」间隔再定要不要拆成两个配置，别现在拍脑袋拆 |

## 整合 T107–T111 期间的环境发现（2026-09-09）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-09 | p9 | **`~/.maos.env` 里的 DeepSeek key 已失效**：模型网关回 HTTP 401 `Authentication Fails, Your api key: ****2309 is invalid`（`maos/model/client.py::GatewayModelClient.complete` 抛 RuntimeError） | 全量测试里 `test_cost_metrics::test_a_real_scenario_run_records_only_registered_call_sites` 恒红；房间 bot 的闲聊接话、`refund.reason_classify` 的模型判据、一切走真模型的路径都会当场抛异常 —— 房间里的表现是回一句处理失败，而不是降级。复赛演示若要展示真模型接话，这是硬前置 | 换 key（人类操作，只改 `~/.maos.env`，不入库、不进 evidence）。换完重启 launchd 的 `com.maos.room-ingress` 让 bot 重新读环境 |
| 2026-09-09 | p9 | 不带 `~/.maos.env` 直接跑全量测试时，`test_domain_evidence` 7 条 + `test_verify_warn` 4 条恒 error，根因是 `SSL_CERT_FILE` 未设（本机 Python.framework 3.11 从未跑过 Install Certificates.command，`ssl` 的 cafile 是 None） | 裸跑 `python3 -m pytest maos/tests -q` 在这台机器上永远是 11 errors，且报错文本指向「证据束生成失败」而不是证书 —— 每次都要重新往下挖两层才看得见 `CERTIFICATE_VERIFY_FAILED` | 二选一：conftest 里对「取不到 cafile」显式 skip 并在原因里点名证书；或把 source 前缀写进 CLAUDE.md 的常用命令。别把它当回归查 |

## 换 key 后的复测：真模型下场景 1 走不到 DONE（2026-09-09）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-09 | p9 | 上一节「DeepSeek key 失效」**已解除**：当天换了新 key，实测网关正常应答（`test_cost_metrics` 从 1.3s 秒挂变成跑满 50s，Reviewer 产出了完整的中文语义审查结论） | 房间 bot 的闲聊接话、`refund.reason_classify` 的模型判据恢复可用 | 已完成，留档 |
| 2026-09-09 | p9 | **真模型模式下 `scenario_1` 走不到 DONE**：coding 岗产出的 unified diff 格式不合格（v1 缺 diff 头、v2 缺 `@@` 行且两个文件为空），沙箱 `validate` 阶段报 `corrupt patch at line 14`，三次 attempt 全被拒 -> `retry_exhausted` -> plan FAILED。Reviewer 的语义审查反而判得很准，把两版补丁的毛病逐条点了出来 | 只要环境里 `MAOS_LLM_BASE_URL` / `MAOS_LLM_API_KEY` / `MAOS_LLM_MODEL` 三项齐全，`select_model_client` 就走真模型 —— 也就是说 **source 过 `~/.maos.env` 再跑 `python3 run.py`，场景 1 必红**；裸跑（Scripted）不受影响，房间那条链路也不受影响（不产补丁）。`test_cost_metrics::test_a_real_scenario_run_records_only_registered_call_sites` 因此在配了 key 的机器上恒红 | 独立一轨：要么在 coding 岗的提示词里把 diff 格式约束写死并加一轮自校验，要么在落盘前做一次 `git apply --check` 的重试。**与 T107-T111 无关** —— 合并前的 `9d4df28` 用同一个 key 跑同一条测试，失败在同一行（`scenario_1.py:151`） |

## 整合 T55–T83 的遗留（2026-09-09）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-09 | p9 | **RTV 五轨（T61–T65）没进主干**，进度在 `integrate/p9-t55-t83-with-rtv`（已含：`worker.py` 接线、RTV fixture 补建 ap 源单表、T64 的场景改 `scenario_12`）。卡在两套写权威表的实现：T63 rtv skills 那份 `_common.py` 直写 `rtv_case`，T65 `test_rtv_guard.py` 只许 rtv 域那份 `guard.py` 写。另有测试自陈的整合期项：`test_rtv_flow.py:138` Agent 池 22->27 实际会是 29；`:690` 场景是否进 `ALL_SCENARIOS`；`test_rtv_sop_doc.py:226` `docs/domain-portability.md` 前 393 行被主干改过；`scenario_12.py:714` `KeyError: supplier_id`（16 条） | RTV 域（采购退货退款）整个不可用；`maos/capability/`、`maos/agents/rtv/` 里引用它的部分也一并缺席 | 单独一轨：先由人类定「谁能写权威表」这一条，其余四项是机械收口 |
| 2026-09-09 | p9 | `evidence/scenario-*`（50 份）与 `evidence/INDEX.json` 是**合并前主干态**，首行 sha 与当前 HEAD 对不上 | `scripts/verify.py` 的证据出处守卫（T105）会把它判成「不是当前代码跑出来的」 | 合并进主干后按合并态重跑 `scripts/make_evidence.py`（本机要先 `. ~/.maos.env`：证书与 key 都在里面），同前几轮的「证据束按合并态重跑」 |
| 2026-09-09 | p9 | `scripts/gen_capability_matrix.py` 报「有授权无实现的工具：sandbox（2 个角色受影响）」 | 能力矩阵里两个角色声明了一个不存在的 ToolPort；脚本只记账不修 | T58 时代的老账，与本次整合无关；补 `sandbox` 的 ToolPort 或从档案里摘掉，二选一 |
| 2026-09-09 | p9 | `track-a-prerebase` 分支的内容已全部在主干（产物判据核过） | 只是 `--no-merged` 列表里多一行噪音 | 随手 `git branch -D track-a-prerebase` |

## RTV 并入后的遗留（2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p9 | **场景 12（RTV 五步 SOP）与 `test_rtv_flow.py` 没进主干**，文件在 `task-T64`（原名 `scenario_11.py`）与 `integrate/p9-t55-t83-with-rtv`（已改名 `scenario_12.py`、引用已随改）。替换清单在那份文件第 59 行起，共三段：① 业务对象层 -> `maos/domain/rtv/{objects,guard,fixtures}`；② 五个 ToolPort -> `maos/tools/rtv.py` 的 `Mock*` + `*_PORT`，**但 T62 的 entry 要 `supplier=/carrier=/ap_system=` 实例、T63 的 skill 只从 `ctx.extras["tools"]` 按名取、T64 的 `agents/rtv/_base.py::extras_of` 不注入工具** —— 需要一层绑定（把 mock 实例 partial 进 port 再注进 extras）；③ 六个 stub skill 整段删。另：`seed_case_inputs` 改走 `fixtures.seed_source_documents`（真 intake 从 ap 五张表定位供应商），`_tasks()` 的 payload 按真 skill 的形状改，`test_rtv_flow.py` 的 50 处 `s11.*` 跟改，`EXPECTED_POLLS_OK=3` 等断言要按 `MockSupplier(ack_after/issue_after)` 的语义重对 | RTV 域在主干上没有 `run.py` 演示入口；五个 Agent 只有池 / 档案 / 生成文档测试覆盖，没有端到端 | **已由 T112 完成（2026-09-10）**：场景 12 与 test_rtv_flow.py 已落主干形态，三段 stub 全换真件。实际撞到的接缝比预估多：观察一次一跳导致 DAG 改六节点、对账要排到观察之后、两份理由码表要对照 —— 逐条在 DECISIONS 的 T112 那批里 |
| 2026-09-10 | p9 | `skills/builtin/rtv/_common.py` 里 T63 时代的 fallback 还在：`_ensure_schema_fallback`、内嵌 `_SCHEMA_SQL`、`_AP_SCHEMA_PATH`、第三节「临时码表」（`RETURN_REASONS` / `RULES` 应换成 `maos/tools/rtv_codes.py`）| 全是走不到的死码，但两份 SQL / 两份码表并存，改一处漏一处不报错 | 与上一条同一轨做：文件抬头第 1、3 条写明了怎么删 |
| 2026-09-10 | p9 | `scripts/gen_capability_matrix.py` 仍报「有授权无实现的工具：sandbox（2 个角色）」 | 同 09-09 那条 | 同 09-09 那条 |

## 场景 12 去 stub 之后的新账（T112，2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p9 | **两份退货理由码表并存**：T63 的 `skills/builtin/rtv/_common.py::RETURN_REASONS`（`defective` / `damaged` / `wrong_item` / `not_ordered` / `over_shipment` / `spec_change`，值带裁定倾向）与 T62 的 `tools/rtv_codes.py::RETURN_REASONS`（`RTV-RSN-01..06`，带 source / basis 出处）。T112 在 `tools/rtv.py::REASON_CODE_ALIASES` 加了一张 6 条的对照表把前者翻成后者 | 库里 `rtv_line.reason_code` 存的是 skill 那套，递到供应商门户的是 codes 那套 —— 同一笔退货的理由在两处是两个字符串，对账查证时要多翻一次对照表。三处（skill 表 / codes 表 / 对照表）改一处漏两处不报错 | 与 `_common.py` 删 fallback 那条一起做：`_common` 的第三节临时码表整节换成 `rtv_codes`，对照表随之删掉。**要动 T63 六个 skill 的判定分支**（`REASON_RULE` 的键要跟着换），不是纯删死码 |
| 2026-09-10 | p9 | `REASON_CODE_ALIASES` 里 `spec_change -> RTV-RSN-02`（验收未通过）与 `not_ordered -> RTV-RSN-03`（错发/超发）两条是**近似**对应，不是同义 —— codes 表里没有「规格变更」和「未订购」这两档 | 这两个理由递到供应商门户时，对方看到的理由与我方库里记的不完全是一件事。演示数据只用 `defective`（精确对应 `RTV-RSN-04`），所以当前跑不到 | 同上一条。真要分开的话是往 `rtv_codes._REASON_ROWS` 加两行，而那要先核到出处（该表的规矩是「出处写做法，不写编号」） |
| 2026-09-10 | p9 | `fixtures.seed_source_documents` 的语义是「`quantity_rejected` 就是可退来源」，而 `rtv.intake` 卡的是「退货量 ≤ **合格数**（received - rejected）」—— 两条口径在 `rejected > received/2` 时互相矛盾 | 按 fixtures 的意思造一套「到货 20 件、其中 12 件不合格」的数据，intake 会以「退得比收得多」拒掉。T112 的靶场因此把到货数放大到 24 才跑得通，而那不是这批货真实的到货量 | 单独一轨核一次 PeopleSoft 口径：退货量该跟 `quantity_rejected` 比还是跟 accepted 比。改的是 `intake.py` 那一条判据或 `fixtures` 的注释，两者只能留一个说法 |
| 2026-09-10 | p9 | `agents/rtv/settlement_agent.py:15` 的模块 docstring 里有 `maos/flows/scenario_12.py::compensate` 字样（T64 原有，本轨未动） | 验收口径「`maos/` 与 `scripts/` 里不许出现 `scenario_12`」在 grep 层面有这一处命中；机器守卫（`test_rtv_flow.py` 那条）只查 `run.py` 与 `scripts/make_evidence.py`，所以不红 | 无需处理：那是一条指得准的交叉引用，指向的函数确实存在。若要 grep 恒空，是把守卫的判据写进验收命令，不是删这句注释 |
| 2026-09-10 | p9 | 主干工作区上 `scripts/make_evidence.py` 跑不成：内部 `assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE` 不成立，Plan FAILED —— coding agent 三次重试耗尽（`RUNNING -> PENDING [retry]` x2 后 `retry_exhausted`），Reviewer 判「无产物可审查，三项验收标准均无法验证」。连带 `test_verify_warn.py` 4 条、`test_domain_evidence.py` 7 条 ERROR，`test_cost_metrics.py::test_a_real_scenario_run_records_only_registered_call_sites` FAILED。全量口径：1 failed / 3202 passed / 41 skipped / 11 errors | 证据束现产不出来，`verify.py` 的九项判据这一路无法自证 | **不是这一轮改的**：`git stash` 掉当轮 `router.py` 改动后同样的测试照样红（1 failed / 4 errors），带不带 `~/.maos.env`（含 `SSL_CERT_FILE`）都红，所以既不是模型 key 也不是根证书那条。单开一轨查 coding agent 那次重试耗尽 |
| 2026-09-10 | p9 | `hiclaw/ap_room.py::render_speech` 有同一类缺陷，而且是两处：① 正文没过 `html.escape`（模型吐一个 `<` 或 `&` 就把 `formatted_body` 破掉，Synapse 不报错、Element 默默吞掉半句 —— `room_ingress._ProxyVoice` 的 docstring 已经点名「那边没转义」，契约 §1.3 说是要避开的坑）；② 换行没转 `<br/>`，多行发言在房间里会流成一段（退款圆桌这一侧本轮已修）| AP 房间的多行发言与带尖括号的发言都显示不对；两处都不报错，只能靠人盯房间发现 | 改法现成：`hiclaw/matrix_bus.py::html_block` 一次解决两条，照 `room_voices.RoomVoice._render` 本轮的改法搬一遍即可。本轮没动是因为 AP 域不在本轨范围（铁律 4）|
| 2026-09-10 | p9 | <字段归属表> 说受理岗对封顶**只标记、不解释算法、不给金额**，逐单那条已收成「本单触发封顶」，但**整表**受理岗念的是 `ingress/sheet.py:293` 造的 warning 原文：「申报 9999 超过订单实付 6800，核算时会按实付封顶」—— 金额和算法两样都在，经 `stages._warning_lines` 原样进事实卡 | 同一张表里，受理岗把封顶算法和两个金额说了一遍，财务岗逐单预演后再说一遍。差一分钱（优惠券、运费另算）就要在群里当场对账，而受理岗手上没有账 | 本轮没动：那条 warning 是 ingress 层的数据，申请表回帖等别处也在用，改它超出五岗范围（铁律 4）。要做的话是把 warning 拆成两截 —— 给人看的回帖保留金额与算法，喂进受理岗事实卡的那份只留触发标记，落点在 `stages._warning_lines` 而不是 `sheet.py`。连带 `test_roundtable_stages.py:351` 的 `按实付封顶 in intake` 要翻 |
| 2026-09-10 | p9 | **`test_cost_metrics::test_a_real_scenario_run_records_only_registered_call_sites` 的真根因查明，与本文件 `:2263` / `:2290` 记的都不一样。**（1）`:2290` 那句「带不带 `~/.maos.env` 都红」是**误判**：`MAOS_LLM_*` 是 shell profile 灌进来的，`git stash` 重跑时环境里照样有 key，两次跑的都是真模型。实测 `env -u MAOS_LLM_BASE_URL -u MAOS_LLM_API_KEY -u MAOS_LLM_MODEL python3 -m pytest maos/tests/test_cost_metrics.py -q` -> **39 passed**。（2）`:2263` 记的形态（`corrupt patch at line 14`）已经不是现在的形态。现在失败在 `CodingAgent.run` 三次都回 `ValueError: 补丁集为空`，而模型返回的是 `{"files":[],"summary":"no missing token validation found","self_check":{...}}` —— **模型没错**：Manager 规划的第一个任务是「定位 demo/app 中 token 校验缺失的具体位置」，它的验收标准第 3 条原文就是「清单为空时需明确输出 'no missing token validation found'」，模型逐字照做了。错的是把一个纯**定位/调查**任务派给了只会产补丁的 `code.repo-patch` | 配了 key 的机器上这条测试恒红（本机所有 shell 都从 profile 带 key，所以是「总是」不是「偶尔」）；`scripts/make_evidence.py` 同一个断言同样跑不成，连带 `test_domain_evidence` 7 条、`test_verify_warn` 4 条 ERROR | **空补丁集要升格成合法结论**，判据是 `summary` 非空（区分「查过了，没有需要改的」与「模型没产出」）。落点**两处不是一处**：`maos/skills/builtin/code_repo_patch.py:154` 与 `maos/tools/sandbox.py:449`，Gate 那侧 `runtime/gate.py:514` 调 `sandbox_git_apply` 也要跟着看。方向与仓库既有设计一致 —— `agents/refund/policy_agent.py` 的模块 docstring 明写「裁定为 reject 时不产出空产物、也不 failed：结论是「不予退款」，那是一个有效的业务结论，不是一次执行失败」。`sandbox.py` 是冻结签名面，单开一轨做 |
| 2026-09-10 | p9 | 补件页（`hiclaw/upload_server.py`）**没有登录**：缺省只听 127.0.0.1，演示机上够用；`MAOS_UPLOAD_BIND=0.0.0.0:…` 开给局域网后，任何能连到这个口的人都能往任何底账里有的单挂证据 | 只影响主动开外网口的部署；证据本身仍过 `AttachmentStore` 的体积与类型白名单 | 要开局域网时给按钮地址加一个随机 token（URL 里带、POST 校验），token 随进程生成、打在启动日志里；本轮不做 |
| 2026-09-10 | p9 | 补件页的 `router.handle` 跑在 HTTP 线程上，与 Matrix 监听的 worker 线程可能**同时**进 router：`_tickets` 有锁、`_pending` 是线程本地、`SqliteStore` 有 RLock，但 `handle` 整体不是原子的（同一单一边在复检一边被 /approve） | 演示场景下人一次只做一件事，实际撞上的概率很低；撞上时最坏是两条回帖交错 | 要根治就在 `IngressRouter.handle` 外面加一把全局锁（`server.py` 的单 worker 已是同一取向）；不在本轮范围 |
| 2026-09-10 | p9 | 下游三岗的逐单清单与按钮都以 `ROW_CAP`(=12) 封顶：50 行表里缺材料超过 12 单时，第 13 单起没有按钮、也不在清单里，只有一句「余下的…补材料时本岗逐单再报」 | 大表演示时缺件多的那些单要靠拖图（文件名带订单号）或 `/refund` 单独起单才会被点名 | 可以按单另发一条「缺件清单」消息（Matrix 单事件 64 KB 够装 50 行），把清单与发言分开；本轮没做是因为一条消息 12 个按钮已是可读上限 |
| 2026-09-10 | p9 | 证据岗整表卡的缺口措辞照搬 skill 的 `gaps`（「缺少 image 类证据」），`image` 没翻成中文；按钮文案已翻（「上传照片」） | 老板读到「image 类证据」要转一下 | 翻译的落点在 `evidence_check._gaps`（skill 出参是「人话」的承诺），改那里单案卡、整表卡一起变；属 skill 面，不在本轮范围 |

## task-t115

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
| :-- | :-- | :-- | :-- | :-- |
| 2026-09-10 | p10 | 🔴 `scenarios/refund/history/history_cases.json` 那 24 条历史案例的 `workflow_version` 是 `"1.0.0"` / `"1.1.0"`，而 `maos/kb/schema.sql` 的 `kb_doc.workflow_version` 声明的是 `INTEGER`。SQLite 的类型亲和性照单收下（存成文本），PG 直接拒：`invalid input syntax for type integer: "1.0.0"` | `fixtures.seed_history_kb()` 在 PG 上灌不进去 —— **9/18 真跑日要用到这 24 条历史**。更隐蔽的一层：`retriever.prefilter` 拿 `workflow_version = ?` 去比的时候，比的是字符串还是整数取决于当初存进去的是什么，两边都不报错 | 二选一，都在 T115 白名单外：① 语料改成整数（那 `policy_version` 已经是整数，口径统一）；② `kb_doc.workflow_version` 改成 TEXT 并补一条 `kb._MIGRATIONS`。**改完把 `maos/tests/test_kb_pg_prefilter.py::test_fixtures_seed_history_kb_lands_on_postgres` 的 `xfail` 摘掉**（它是 `strict=True`，改对了会「意外通过」而报错，跑不掉）|
| 2026-09-10 | p10 | PG 上 `kb_doc.embedding` 是 TEXT（与 SQLite 同构的直接后果），pgvector 的 `<=>` 用不了 | `vector_search` 抛 `LookupError`，检索器退化成纯 Python 余弦。召回照常、会告警一次，但**PG 上没有 HNSW**，语料上量之后会慢 | 要真用 HNSW 得给 PG 侧单开一列 `vector(64)` 并在写入侧双写 —— 那是两个后端形状分叉，得先定「同构」这条原则让不让步。归性能优化那一轮，不急在复赛前 |
| 2026-09-10 | p10 | `plan` / `task` / `artifact` / `event_log` 四张内核表仍在 SQLite（派单 §4 明确本期不上 PG） | `MAOS_DOMAIN_BACKEND=postgres` 时业务表在 PG、编排表在 SQLite，**跨两个库**。演示与测试都跑得通（两边没有外键），但「全都在 PolarDB 上」这句话现在不成立，对外别这么说 | 要搬得先给 `maos/core/store.py` 做后端抽象，而那是冻结面（铁律 1，表结构禁改）。单开一轨，且要先想清楚跨库事务这件事 |
| 2026-09-10 | p10 | 阶段一预过滤把查询值直接绑进 `policy_version = %s` / `workflow_version = %s`。调用方递字符串时 PG 报 `operator does not exist: integer = text`，SQLite 则靠亲和性静默比较 | 换后端时同一份查询字典可能一边报错一边静默出结果。当前调用方递的都是整数，所以跑不到；上一条那个语料缺陷就是同一个病根的另一面 | 与上面第 1 条一起处理：定死 `kb_doc` 那几个版本列到底是整数还是版本串，然后在 `retriever.prefilter` 入口做一次类型归一。改 `prefilter` 的字段顺序与权重是禁区，只归一取值 |
| 2026-09-10 | p10 | `scripts/polardb_smoke.py` 第 6 步的两段 DDL 是照 `maos/domain/refund/schema.sql` 手抄的（脚本不 import maos，拿不到翻译器）| 那两段与 schema.sql 会漂。漂了不影响任何真表，只让这一步的判据强度下降（比如以后 `refund_case` 加了一条 CHECK，冒烟这边验不到）| 无需急改。真要收口的话是给脚本加一个可选的 `--schema <文件>` 参数、在有仓库的机器上读真 schema，但那会削掉「不 import maos、单文件可跑」这个性质，得权衡 |
| 2026-09-10 | p10 | 另外四个业务域（`ap` / `claim` / `investigation` / `rtv`）的 `objects.py` 与共用骨架 `maos/domain/_case_store.py` 仍然硬取 `SqliteStore._conn`，一个都没上 PG | `MAOS_DOMAIN_BACKEND=postgres` 只搬走退款域。别的域此时仍写 SQLite —— 跑它们的场景不会报错，但「业务域都在 PolarDB 上」同样不成立 | 骨架已经在 `_case_store.py` 收过一次口，改造面比退款域小：把它的 `_conn` / `lock_of` / `execute` / `query` / `_atomic` / `ensure_schema` 换成 `_dbport.DomainConn` 即可，退款域这一份就是现成样板。归复赛后 |
| 2026-09-10 | p10 | `maos/tests/test_expected_metrics.py` 在本轨的工作区上是红的：新增 80 条测试而 `docs/expected-metrics.json` 按契约 §A 不许各轨去刷 | 本波五轨都会红这一条，**是预期内的**（同 `verify.py` 的 `provenance 4/5`）。红着的时候它守不住别的东西 | 整合期统一刷：收集条数 3261 -> 3341（本轨贡献 80 条 = 45 条无库可跑的翻译器测试 + 35 条 PG 门禁测试），`pytest_passed_nopg` 3220 -> 3265，`pytest_skipped_nopg` 41 -> 76；`docs/submission-checklist.md:24` 的锚点行跟着刷。**五轨合完之后按实跑末行重新取数**，不要拿这里的数字直接填 —— 这只是本轨单独的增量 |

## task-t116

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | `objects.resolve_business_ref` 的版本收窄拼的是**写死的** `AND version=?`，所以 T116 加的四个 `revision` 列（`approval_record` / `finance_entry` / `notification` / `compensation_record`）进不了 `_VERSIONED_REF_TABLES` | 这四类引用不按版本收窄：拿一条旧修订的引用去 resolve，会**指到当前那一行**而不是指不到。修订号仍然留在 `business_ref.object_version` 上，所以对账查得出来，只是 resolve 这一层不挡 | 整合期：把那句 SQL 改成按表取版本列名（`_VERSIONED_REF_TABLES` 从 set 改成 `表 -> 列名` 的 dict）。`resolve_business_ref` 不是本轨可动的面，所以本轮没改 |
| 2026-09-10 | p10 | **验收 3 的字面做不到**：「付款任务落 `BLOCKED` 且钱不退」两全不了。`effect_risk=H` 判在 Gate 之后，而付款任务在**执行时**就把钱退出去了（实测：钱照退、`biz_status` 走到 `settled`、然后停在 BLOCKED） | 本轮取「钱不退」：漂移时核算任务停 `BLOCKED`、付款任务停 `PENDING`。于是验收 3 的原文对不上，需要人看一眼再定 | 要两全得让 `refund.snapshot_check` 成为 DAG 里的一个节点、付款 `depends_on` 它 —— 漂移时它自己 BLOCKED，付款因依赖未满足而不派发。那需要一个能跑它的 Agent（`agents/refund/**` 加一个薄壳，或给现有岗加 `step` 分派），属本轨白名单外。**下一轨的第一件事** |
| 2026-09-10 | p10 | `refund.snapshot_check` 是 `skill-unowned`：没有任何 `AGENT_POOL` 里的角色被授权调它（与 `refund.compensate` / `ap.compensate` / `claim.compensate` / `kb.sink` 同类） | 能力矩阵体检多一条 finding；`scripts/gen_capability_matrix.py` 会报「有实现无持有者」 | 与上一条同一轨做：那个 Agent 一旦有了，它的 `allowed_skills` 收进这个 skill，这条 finding 自动消失，`test_capability_profiles` 的 BASELINE 与计数跟着回退 |
| 2026-09-10 | p10 | `flows/custom_case.py` 的失败路径**不做域内补偿**（它的模块 docstring 原话「那是场景 7 的面」），`--fail-with` 那一跑网关明确失败后案子就悬着 | 单跑一条案例最多挂上**九类**业务对象引用，第十类（人工补偿）永远缺席 —— 「十类齐」这句话在单条命令上证明不了，要靠场景 7 或单测补 | 与上一条同轨：把 `refund.compensate` 接进 `custom_case` 的失败收口（照 `scenario_7` 的姿势，走 `COMPENSATION_IDENTITY`）。接上之后 `run_case.py --fail-with` 那一跑就是真正的 10/10 |
| 2026-09-10 | p10 | `AS-004@v2` 的 `dual_approval: true` 在 `maos/**/*.py` 里**没有任何消费方**（与 README 里 `min_evidence_count` / `requires_evidence_kinds` / `evidence_source` 那三个键同一处境） | 政策声明了「双人审批」，但流程只落一条 `approval_record`，且不报错。答辩时若拿这条规则讲「政策驱动审批人数」会被问穿 | 与 `docs/BACKLOG.md ## task-T78` 那条一起做：政策判定器落地时一并给这几个键找消费方，或把它们从语料里摘掉 |
| 2026-09-10 | p10 | `product_snapshot` 没有对应的「下单当时锁定哪一版」的外部事实（订单快照上有 `policy_version_at_order`，商品没有 `product_version_at_order` 之类） | `intake` 挂商品快照引用时只能取库里**最大**的那一版。质保月数将来若改版，一笔历史订单会按新版判质保 —— 与「用最新政策判历史订单」是同一类错，只是发生在商品维度 | 需要外部订单系统先提供这个字段，不是 MAOS 单方面能补的。真接订单系统那一轨核一次口径 |
| 2026-09-10 | p10 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` 本轨**没刷**（跨轨契约 §A：谁都不许动，整合期统一刷） | `test_expected_metrics::test_collected_count_matches_source_of_truth` 在本轨分支上恒红一条。本轨新增 59 条，collected 3261 → 3320，该刷成 `pytest_passed_nopg = 3279`（3320 − skipped 41） | 整合期：把 Wave A 五轨各自新增的条数合并，一次刷这一个数。`scripts/demo_preflight.sh` 与 `docs/submission-checklist.md` 会自动跟上 |
| 2026-09-10 | p10 | `case_pack._add_column_if_missing` 是跨轨契约 §B.2 要求「三轨各自复制一份」的六行助手，本轨这份还多带了一层 PRAGMA 探不动时的回落 | 三份重复代码，改一处漏两处不报错 | 整合期由主会话去重（契约原话）。去重时注意本轨这份的回落分支：换 PG 后端后 `PRAGMA table_info` 不保证有 |

## task-t120（业务结果四判据 + 自动晋升 + 失败聚合，2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | `outcome.EVIDENCE_REF_TYPES` 当前只有 4 类，契约 §E 要的是 10 类 —— 缺的 6 类（customer_evidence / approval_record / finance_entry / product_snapshot / notification / compensation_record）现在挂不上 `business_ref` | `evidence_complete` 判得偏松：一单少挂上述任何一类引用，现在照样算「证据完整」，于是它可能被晋升成规划正例 | **整合期 T116 合入后立刻补**：把 `EVIDENCE_REF_TYPES_PENDING_T116` 的 6 项并进 `EVIDENCE_REF_TYPES`，`test_case_outcome.py::test_evidence_ref_types_documents_the_t116_gap` 会当场提醒（它钉着 4 + 6 = 10） |
| 2026-09-10 | p10 | `domain/refund/fixtures.py::seed_history_kb` 仍自己抄了一份「按 outcome 分流」的口径，没走 `kb/promotion.py::classify_corpus_row()` | 两份口径迟早分叉，而分叉的症状是「语料里写坏 kind 的行悄悄进了正例知识层」—— 写入侧发生、几周后检索侧才暴露 | 整合期把那两行换成 `kind, outcome = promotion.classify_corpus_row(row)`（1 行改动）。本轨没动是因为 `fixtures.py` 属于 T115 的白名单面（契约 §A） |
| 2026-09-10 | p10 | 自动晋升只接在 `scripts/make_evidence.py::run_child`（人类拍板的最小接线），`flows/scenario_6.py` / `scenario_7.py` 的运行时**仍不调** `PlanFinalizer.poll()` | 裸跑 `python3 run.py --scenario 6` 不会产生 `case_outcome` / `kb_doc` / `failure_hint_index` 行 —— 只有走证据束那条路才有。演示时若直接跑 run.py 再查库，会看到空表 | 整合期在 `scenario_6.py` / `scenario_7.py` 的收口段各加一句 `PlanFinalizer(store).poll(plan_id)`（各 1 行）。本轨没动是因为 `scenario_6.py` 属于 T116 的白名单面 |
| 2026-09-10 | p10 | `manual_correction = overridden` 这一档目前**无人产**：它认的是观察行回执里 `source == 'manual'`，而写这种回执的 `ManualReceiptAdapter` 是 T117 的交付物，未合入 | 四判据里这一格现在只会取到 `none` 或 `compensated`。少判一档但不会误判 —— 这是这一处该有的失败方向 | T117 合入后跑一遍它的闭环（ticket → assign → resolve）核对：`manual_correction` 应为 `compensated`、`arrival` 应为 `settled` 且 basis 指向 manual 观察。`test_case_outcome.py` 已有纯函数级的正例钉着这条分支 |
| 2026-09-10 | p10 | `docs/expected-metrics.json` 与 `docs/submission-checklist.md` 本轨禁改（契约 §A），而本轨把测试从 3261 条加到 3346 条（+85）、`verify.py` 从 9 项加到 10 项 | `test_expected_metrics.py::test_collected_count_matches_source_of_truth` 恒红；`scripts/demo_preflight.sh` 读的 `verify_result_line` 仍写着 `RESULT: 9/9 PASS`，与实际的 `RESULT: 9/10 PASS`（provenance 那条另有原因）对不上 | **整合期统一刷**：那条测试只断言 `pytest_passed_nopg + pytest_skipped_nopg == collected`，而本机实跑（447a184）是 **3346 collected = 3301 passed + 44 skipped + 1 failed**（那 1 条就是它自己，刷完即绿）。按实跑拆分应写 `pytest_passed_nopg` 3220 → **3302**、`pytest_skipped_nopg` 41 → **44** —— 注意现文件里的 41 与实跑的 44 本来就对不上，只是和为 3261 时凑上了，别照着 41 往上加。`verify_result_line` → `RESULT: 10/10 PASS`（要等 `evidence/domains` 那束重产，它现在还落后 HEAD），以及 `docs/submission-checklist.md:24` 的锚点行 |
| 2026-09-10 | p10 | 第 10 项 `check_case_outcome` 只覆盖退款域：`case_outcome` 表是退款域的，理赔 / 银行差错 / 应付账款 / RTV 四个域没有对应的四判据 | `verify.py --domains` 展开那三个域时，业务结果这一层仍只有旧的 `business_outcome`；「所有 Agent 都回复完成 ≠ 业务成功」这句话目前只在退款域可核 | 四判据要不要做成跨域的（`DOMAIN_REGISTRY` 那套的第 4 条 spec），等复赛后按实际需要定 —— 本期评委问的是退款纵切，不做扩张（铁律 4） |


## task-t118（九类流程知识语料的范围外发现，2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | `kb/schema.sql` 的 `kind` CHECK 扩了四个取值，但整份 schema 都是 `IF NOT EXISTS`，**对已存在的表一个字都改不动**。老库要认新 kind 只能走「建新表→拷数据→换名」那种迁移，`kb.__init__._MIGRATIONS` 里还没有这一条 | 演示期的库都是 `:memory:` 或每次新建，看不出区别；PolarDB 是持久库，上线第一天写新 kind 就撞 CHECK 失败，而失败点在写入侧、离改动很远 | T115（知识层建表 / 迁移执行路径是它的面）；本轨按白名单不碰 `_MIGRATIONS` |
| 2026-09-10 | p10 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` 落后 17 条（本轨新增，3217 -> 3234），`test_expected_metrics.py::test_collected_count_matches_source_of_truth` 因此红 | 全量测试非零退出；五轨都在加测试，这个数会一路落后 | 整合期统一刷（契约 §A 明列，各轨不许各改各的） |
| 2026-09-10 | p10 | `evidence/` 下的证据束仍是 `c7d923d` 产的，不含本轨的新语料与错误码通道命中。本轨的验收 4 是把 R5 束产到 scratchpad 验的，仓库里那份没动 | `evidence/scenario-R5/kb-hits.json` 与当前代码的实跑结果不一致（漏斗 40/21/7 vs 133/49/27）；`verify.py` 的 provenance 项也仍落后两个 commit | 整合期统一重跑（派单 §6 已把 provenance 那条列为预期内的红） |
| 2026-09-10 | p10 | `task_pattern` 的 body 是可照做的步骤清单，但它不在 `kb.POSITIVE_KINDS` 里，所以 `apply_suggestions` 今天吃不到它 —— 这类知识检索得回来，却没有任何代码用它改计划 | 「任务拆分」这一类知识目前只有 `retrieve_task_patterns` 一个取用口，实际效果要等规划面接上才看得到 | T119（Manager 规划面，Wave B）。启用只需把它加进 `POSITIVE_KINDS` 一处 |
| 2026-09-10 | p10 | 租户 `tnt-demo` 那 7 条驳回知识的**理由**是构造的：底账 `ledger-bulk.json` 只给了 `status='rejected'`，没给理由。生成器按四条写死的规则从订单事实推导（没签收 / 超窗口 / 无质检报告 / 举证不足） | 单号、金额、时点是底账里的真数据，理由不是 —— 引用时不能说成「真实的驳回记录」 | 长期；口径已写进 `scenarios/refund/kb/*.json` 的 `_provenance` 与 README |
| 2026-09-10 | p10 | 检索仍是「四通道加权求和 + hash embedding + 中文按字切」：没有 RRF 融合、没有真向量模型、没有中文分词器 | 语义通道的区分度靠 64 维 hash embedding，同类文档之间分数拉不开（本轨实测 5 条处置手册里，除精确命中那条外其余分数在 0.068~0.089 之间） | 派单 §4 明确不在本轨；装依赖属停手条件。留给后续专轨 |
| 2026-09-10 | p10 | `maos/tests/test_kb_schema_migration.py` 里内嵌着一份老库的 `kind` CHECK 快照（只有四个取值），与当前 `schema.sql` 已不同 | 那是模拟老库用的、有意为之，今天不红；但下一个读它的人会以为是漏改 | 长期；补一行注释说明它是快照即可 |

## task-t117

人工补偿闭环 + 审批人角色目录顺手发现的账。**都没当场改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | `_add_column_if_missing` 现在有**三份**（T116 / T117 / T120 各抄一份，契约 §B.2 明写要这样，整合期去重）。而它们用的 `PRAGMA table_info` 探针与 `maos/domain/refund/objects.py::_has_column` 的判据**不一样** —— 后者明写「不用 PRAGMA」，两条理由：PRAGMA 是 SQLite 方言（PG 没有），且它**不列生成列**（要 `table_xinfo` 才看得到），拿它判一个生成列会答「不在」，于是每次都去 ALTER、每次都撞 duplicate column name | 三份重复本身只是噪音；判据分歧是真账：等 T115 的 PG 后端合进来，三份 PRAGMA 探针在 PG 上一律取不到列，于是**每次启动都试着加一遍列**。退款域今天没有生成列，所以第二条暂时打不到 | 整合期去重时一并收敛到 `objects._has_column` 那版 SELECT 探针，签名保持 `(conn, table, col, decl)` 不变。这件事要在 T115 的 `DomainConn` 落地之前做完 |
| 2026-09-10 | p10 | 两套角色名并存：本轨的 `maos/domain/refund/roles.py`（`after_sales_supervisor` / `finance_reviewer` / `payment_ops` / `region_manager`）与 `maos/roundtable/verdict.py` 的 `ESCALATION`（`supervisor` / `finance_manager`）、`maos/flows/contrast.py` 的 `region_manager`。目前靠目录里的 `verdict_role` 一列 + `canonical_role()` 两头认 | 每处新增角色都要记得在两边都加一遍。漏了不报错：`verdict._approver` 认不出就只 log 一行然后不升档 —— 风险高档的案子停在原审批人手上，屏幕上一切正常。本轨已把这条钉成测试（`test_refund_roles.py::test_escalation_table_roles_are_all_in_the_directory`），所以现在**会**红，但那是事后拦，不是根治 | 整合期收敛成一套（建议以目录那套为准，`verdict.py` 与 `contrast.py` 改成从 `roles` import 常量）。要动 `maos/roundtable/**`，本轨明确不许碰 |
| 2026-09-10 | p10 | `payment_observation` 没有 `gateway` 列，人工凭证的标记藏在 `raw_receipt_json` 的 `$.detail.gateway` 里 | 「把所有人工回填的观察捞出来」得靠 JSON 提取，不能按列过滤也建不了索引。验收 2 那句「`gateway` 应为 `manual`」现在要 `json.loads` 才读得到 | 等 T115/T116/T120 都并完、退款域不再有并行改动时，用一个片段给它加一列 `gateway TEXT NOT NULL DEFAULT ''` 并回填。本轨没加是因为那是三轨共用的表，加列属跨轨风险（契约 §A） |
| 2026-09-10 | p10 | `maos/ingress/outcome_commands.py` 的四条命令**没有接进** `maos/ingress/router.py` —— 房间里现在打 `/assign` 不会有任何反应 | 功能只在测试与场景 7 的演示里跑得到，真房间用不上 | 整合期由主会话在 router 里接 `COMMAND_HANDLERS` 那一张表（一行）。本轨明确不许动 router（同期另一会话在大改它） |
| 2026-09-10 | p10 | `/confirm` 与 `/complain` 现在共用 `MAOS_APPROVERS` 那道名单闸，但**客户不是审批人** —— 确认收款和投诉本来该由客户发起，而客户永远不会在那份名单里 | 现在这两条命令实际只有内部人员打得动。fail-closed 所以不会误放行，但语义是错的：拿一份「谁能批一笔钱」的名单去管「客户能不能说自己收到钱了」 | 归 T120 / ingress 那一轨：客户侧身份从哪来（渠道账号？案子上的 `applicant_ref`？）要和 `case_outcome` 的四判据一起定。本轨只把闸做成 fail-closed 并留下这条账 |
| 2026-09-10 | p10 | `MANUAL.SETTLED` / `MANUAL.NOT_SETTLED` 刻意不在 `maos/tools/gateway_codes.py` 的官方码表里，所以 `lookup()` 对它俩抛 `KeyError` | 第七道闸 `ReviewerGate._gate_gateway` 按码查表判 disposition；哪天有人把人工凭证做成一份进闸的产物，那里会撞 `KeyError`。今天走不到：关单不产 artifact、不过闸 | 真要让人工凭证进闸时再处理，做法是给闸加一条「`MANUAL.` 前缀直接判 human_terminal」的分支，**不是**把这两个码塞进官方表 |
| 2026-09-10 | p10 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` / `pytest_skipped_nopg`（3220 / 41）在基线 `1f4bb1f` 上**就已经不准**（实跑 3217 / 44），只是两数之和 3261 恰好等于当时的收集数，所以那条断言当时是绿的 | 本轨 +53 条之后收集数变 3314，断言当场红。契约 §A 不许任何一轨动那个文件 | 整合期统一刷：`pytest_passed_nopg` → 3270、`pytest_skipped_nopg` → 44（本轨实跑值，五轨并完还要再刷一次）。顺带把两数各自刷准，别再靠「和恰好对上」蒙混 |
| 2026-09-10 | p10 | `maos/domain/refund/roles.py::_load()` 每次调用都读一遍 `scenarios/refund/roles.json`，不缓存；而 `role_of()` 会对每个岗调一次 `accounts_of()`，一次查询最多读盘四次 | 几十行的静态语料，实测无感。但它挂在 `/assign` 与每次 `can_approve` 上，真房间高频用时会变成可见的开销 | 真觉得慢了再加缓存，**同时**要给测试留一个 `reset_cache()`。现在不加是刻意的：缓存会让测试之间互相串（换一份目录得先想起来清缓存），那种耦合不值这点开销 |

## 整合期 p10-a（Wave A 五轨合并，2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | T117 的 `maos/ingress/outcome_commands.py` 还没接进 `router.py`（`COMMAND_HANDLERS` 表 + `dispatch()` 已就位，只差 router 认这四个动词） | 房间里 `/assign /resolve /confirm /complain` 目前无人应答 | Wave B 整合（T113 收工后）：`IngressRouter` 认不出的斜杠命令交给 `outcome_commands.dispatch`。注意 T117 的处理函数要的 store 是**装着退款域表的那个库**，而房间的 `/refund` 是在 `custom_case` 自建的 `:memory:` 里跑完的 —— 接线时要先定「命令对着哪个库」，T113 的 `MAOS_INGRESS_DB` 是一半答案 |
| 2026-09-10 | p10 | T116 的三态投影 `projection.public_status()` 未接财务岗卡片；T117 的角色名（`after_sales_supervisor` 等）与 `verdict.py` 的 `supervisor / finance_manager` 靠 `roles.canonical_role()` 两边认，`verdict.py` 本身未改 | 房间里财务岗仍按老文案说状态；升档表用的仍是旧角色名 | Wave B 整合，与 T113 一起动 `maos/roundtable/**` |
| 2026-09-10 | p10 | 裸跑 `python3 run.py --scenario 6` 仍不落 `case_outcome` / 晋升行（只有 `make_evidence` 那条路接了 `PlanFinalizer.poll`，T120 期人类拍板） | 演示时若直接跑 run.py 再查库会看到空表 | 演示脚本定稿前决定：要么演示走 `run_case.py` / `make_evidence.py`，要么给 `scenario_6/7` 各加一句 `poll()`（各 1 行，幂等由 claim 兜着） |
| 2026-09-10 | p10 | `_add_column_if_missing` 三份复制（`case_pack.py` / `compensate.py` / `outcome.py`），契约 §B.2 说整合期去重 | 改一处漏两处不报错；`case_pack.py` 那份多一层 PRAGMA 回落 | Wave B 整合一并收成 `maos/domain/refund/objects.py` 的一个助手（T115 的 `DomainConn` 上 PG 后 PRAGMA 不保证有，回落分支要留） |
| 2026-09-10 | p10 | T113 的基线是 `7af9022`，比本分支的五轨基线 `1f4bb1f` 新一个 commit，但不含本分支任何内容 | Wave B 整合时 T113 要合到本分支之上，`maos/roundtable/**` 以 T113 为准、其余以本分支为准 | 9/14 前后 T113 收工时 |

## task-t114（单案例端到端证据束，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | ~~🔴 **同渠道重试会留下悬空的 `business_ref`**。`payment_execute.py:143` 用 `next_version` 把 `refund_request.version` 原地推高（`INSERT OR REPLACE`，一个案子恒一行），而每次重试都按当时的版本挂一条引用；`:163` 的 DELETE 只摘 `object_id` **不同**的旧引用 —— 同一个 `request_id` 的 v1/v2 引用留在表上，指向一个已经变成 v3 的行~~ | ~~`resolve_business_ref` 按 `AND version=?` 收窄，两条当场悬空。实测注入可重试码 `40005`（付款跑三趟）后 `verify.py` 第 2 项判负 2 条；`scenario-7` 撞不到是因为它重规划**换了渠道**、`request_id` 变了，正好走进那条 DELETE。本轨因此改用终态失败码绕开，代价是 case 束里没有 `REWORK`~~ | **已处理（2026-09-11，task-t124）**：按推荐的 ① 做，判据改成 `(object_id<>? OR object_version<>?)`（多绑一个 `request_version`），换渠道那条行为不变。回归守卫 `maos/tests/test_payment_retry_refs.py` 前两条 —— 判据退回只比 `object_id` 时第一条立刻红，报出 `[(gw_xxx, 1), (gw_xxx, 2)]` 两条引用。`make_case_bundle.GATEWAY_FAIL_CODE` 随之换回 `40005`，那条路径现在**同时有 `REWORK` 和补偿闭环**（第二段改口成终态失败码转人工，见 DECISIONS `## task-t124` 第 2 条） |
| 2026-09-11 | p10 | 🔴 **已补偿的案子会被晋升成「成功范本」**。`kb/guardrails.py::_classify_by_outcome` 的正例分支只看 `business_success && evidence_complete && 有 settled 观察`，**不看 `biz_status`**；而 T117 的关单（`resolution_kind=settled`）会给一个 `compensated` 的案子回填一条 settled 观察 | 那一单被写成 `kb_doc(kind=history_case, outcome=success)`，进默认知识层当规划正例；而 `verify.py` 第 7 项按 `biz_status ∈ authoritative_states`（只有 `settled`）判，当场判它「追不到成功收口的真实 case」。两侧口径不一致 —— 实测在本轨的 gateway_fail 束上复现过一次。`scenario-7` 撞不到只是因为它的十类引用没挂齐、`evidence_complete` 恰好为假 | 收敛成一句话：**补偿收口的案子不进正例**。落点在 `_classify_by_outcome` 的正例分支加一条 `status not in FAILED_BIZ_STATUS`（1 行）。`maos/kb/**` 是 T119 的面，整合期做；做完 `make_case_bundle.RESOLUTION_KIND` 可以换回 `settled`，那条路径就能演「人工线下退成了」的闭环 |
| 2026-09-11 | p10 | `maos/ingress/outcome_commands.py::handle_resolve` 把 `resolution_kind` 写死成 `CP.RESOLUTION_SETTLED` | 房间里关不出一张「线下核对过，这笔确实没退成」的单 —— 而那正是余额不足、账户冻结这类工单最常见的结局。`refund.compensation_close` 本身两种结论都支持（`RESOLUTION_KINDS`），缺口只在命令面 | 给 `/resolve` 加一个可选末位参数（`/resolve <单号> <凭证摘要> [not_settled]`），或另开 `/close-unpaid`。`maos/ingress/**` 属 T113 分区，整合期做。本轨绕开的方式是直接经 `SkillInvoker` 调 skill（同一条事件、同一份校验） |
| 2026-09-11 | p10 | `maos/roundtable/speaker.py` 的真模型调用**不落 `model_usage`**（跨轨契约 §C 把这件事划给 T113，基线 137c960 上未并入） | `--live-model` 束的 `model-usage.json` 是空数组：五岗真的用真模型发了言（`roundtable.json` 里 `spoken_by_model=true` 可查），但一条用量都记不到。手册 §6-5 要求「≥ 5 条」，本轨交不出 | T113 合入后自动满足。**注意它还要一步**：`record_model_usage` 的 `call_site` 必须先登记进 `maos/obs/call_sites.py::REGISTERED_CALL_SITES`（穷举集合），否则 `verify.py` 第 8 项会把那几行判成未登记调用点。那个文件不在 T113/T114 任何一方的白名单里，整合期一起加 |
| 2026-09-11 | p10 | `source ~/.maos.env && python3 scripts/make_evidence.py` 仍然跑不成（exit 2，场景 1 停在 `assert plan_state == DONE`），但**根因已经换了一个**：本轨修掉「空补丁集恒被拒」之后，两次实跑分别停在 ① `git apply` 报 `corrupt patch at line 39`（模型产出的 unified diff 不合法）② 沙箱回归 4 过 / 1 挂（补丁应用成功但没修好）。Reviewer 的原话「三轮 patch_set 均未产出有效内容，**v1/v3 为空交付**」证明空补丁集这一层确实放行了 | 配了 key 的机器上 `test_cost_metrics::test_a_real_scenario_run_records_only_registered_call_sites` 仍恒红（它要求 `scenario_1.run()` 返回 0）；真模型那一路的证据束仍产不出来。**与 BACKLOG:2263 / :2290 记的形态都不一样** | 这是模型产出质量问题，不是判定 bug，`≤ 40 行`的根因面修不动它。真要收口有两条路：① 按 BACKLOG:2293 的原话，别把「定位/调查」任务派给只会产补丁的 `code.repo-patch`（改 Manager 的规划提示或加一个调查类 skill）；② 给补丁应用加一层格式修复/重问。都要单开一轨。**在此之前，真模型口径的验收命令只能记「跑不成 + 停在哪一步」，不能记 exit 0** |
| 2026-09-11 | p10 | `gateway_fail` 路径上 `notify.customer` 缺席：付款任务被人驳回之后通知岗因依赖未满足不派发 | 束里 8 个契约 skill 只有 7 个 present，`notification` 那类业务对象也挂不上引用（该路径 9/10）。更要紧的是业务上的含义：**退款失败了，客户没有被通知过** | 这是 DAG 形状的问题不是证据的问题：失败路径该有一条「通知客户退款未成功」的边。改法是 `contrast.plan_tasks` 给通知任务改依赖（依赖付款任务的**结束**而不是**成功**），或由 `refund.compensate` 收口时补发一次通知。`flows/contrast.py` 是 T119 的面，`refund.compensate` 是 skill 面 —— 整合期定 |
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的收集数本轨**没刷**（契约 §A：谁都不许动，整合期统一刷） | `test_expected_metrics::test_collected_count_matches_source_of_truth` 在本轨分支上恒红一条。本轨新增 27 条（`maos/tests/test_make_case_bundle.py`），实跑 collected 3541 + 79 skipped → 3566 passed / 79 skipped | 整合期一次刷：`pytest_passed_nopg` 按 Wave B 全部合完之后的实跑末行取数，不要拿这里的数字直接填 |
| 2026-09-11 | p10 | `drift` 路径的 `outcome.json` 里 `public_status` 是空字符串 | `projection.public_status("submitted", has_request=False, None)` 按设计返回 `NO_PUBLIC_STATUS=""`（「投不出来」）。读束的人看到一个空串，分不清是「没有对外可说的状态」还是「这个字段没算」 | 束里补一个并列的布尔/说明字段（`public_status_projectable`），或让 `projection` 另给一个 `describe()`。前者在本轨白名单内但属于「顺手优化」（铁律 4），本轮没做 |
## task-t119

Planner 建议与 R8 顺手发现的账。**都没当场改**（铁律 4）。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `maos/flows/scenario_6.py` 的 `kb_context` 不带 `case_id`，于是场景 6 落下的 `PlanAdvised` detail 里 `case_id` 是**空串** —— 契约 §B 要求「detail 里一律带 `tenant_id` 与 `case_id`」 | 键在、值为空。按 case 回查建议时这一条挂不到任何一单上；R8 那两段自己带了 `case_id`，所以只影响裸跑 `run.py --scenario 6` 的那份证据 | 整合期给 `scenario_6.py` 的 `kb_context` 加一行 `"case_id": CASE_ID`（一行）。本轨不许动 `maos/flows/scenario_6.py`（白名单外）。同样的一行 `scenario_7.py` 大概率也要 |
| 2026-09-11 | p10 | `scenarios/refund/kb/task_patterns.json` 里两份语料的步骤形状不一致：`kb-tp-*-happy-path` / `compensate-path` 用 `depends_on_keys`（步骤名对），而 `kb-tp-*-dealer-writeoff` 用 `depends_on` + `task_id`（`task-case-pattern-*` 这种只在那份语料里有意义的串） | `guardrails._rewire` 只认 `depends_on_keys`，所以从 dealer 那份补进 DAG 的渠道核销任务 `depends_on=[]` —— 它与受理并行跑。今天不出事（`RefundChannelAgent` 零依赖，只要 inputs 有 `rule_ref`），但语义上它该排在裁定之后 | 归 T118 / 语料那一轨：把 dealer 那份的 `depends_on` 改写成 `depends_on_keys`。改完 R8 的 `delta_tasks` 不变，只是那一步会接上「裁定之后」这条边。本轨不许动 `scenarios/**` |
| 2026-09-11 | p10 | `MAOS_KB_ADVICE` 没进 `maos/config/source.py` 的 `GOVERNED_KEYS` —— 与 `MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS` 同一格 | **能治理，变更不落审计**：Nacos 上改得到、不用重启，但推送到达时不会落 `ConfigChanged` | 与 kb 那两个旋钮一起加。代价是「一行改动 + 一条断言」：`GOVERNED_KEYS` 加三个串，`test_config_source.py::test_governed_keys_are_exactly_the_four_this_track_owns` 跟着改名改断言。那个文件本轨不许动。**已处理（2026-09-11，task-t131）**：已加，实际代价与估的一致 |
| 2026-09-11 | p10 | R8 的 `dag-diff.json` 没有 `kb_funnel` 段（R5 那份有）—— `experiment._kb_funnel(store)` 把 R5 的七维（`CHANNEL_ID='ch-online'` 等）写死在函数体里，R8 是经销渠道，直接调会报一份**别的渠道**的漏斗 | R8 的证据里看不到「库存 -> 同租户 -> 预过滤后」那三级数字。两段的命中逐条仍在 `kb-hits.json` 里 | 整合期把 `_kb_funnel` 的七维参数化（keyword-only、默认值取现在这几个常量，R5 的调用零改动），R8 再加一段。本轨没做是因为它要动 `run_r5` 那条路上的函数，而契约写着「`run_r5` 不动」 |
| 2026-09-11 | p10 | `maos/tests/test_expected_metrics.py::test_collected_count_matches_source_of_truth` 变红：本轨 +42 条测试后收集数与 `docs/expected-metrics.json` 对不上 | 一条红，**预期内**（派单 §6 点名） | 整合期统一刷。契约 §A 把那个文件列进「谁都不许动」，理由是每轨都加测试、各自去改同一个整数意味着整合时多方冲突，且中间值全是错的 |
| 2026-09-11 | p10 | `experiment._r8_replanner` 原样重出同一份规格，**不换渠道** | R8 演的是「知道什么时候该停」，不是「聪明地换一条路」。换渠道那一档由 `flows/scenario_7._switch_channel` 演 | 不必改。留这条账只为说明这是刻意的：40005 是「调用频次超限」，换哪个渠道都照撞 —— 而那正是准备段那行 `failure_hint_index` 记下来的事实 |
| 2026-09-11 | p10 | `plan_advice.advise_and_log` 的政策规则从**命中的 `kind='policy'` 文档**重建，而不是查 `policy_rule` 表 | 知识库里没有投影过的政策规则，建议就看不见它。今天所有消费方（`experiment._seed` / `scenario_6._seed_kb` / `fixtures.seed_policy_kb`）都投影全量政策，所以取不到的情形不存在 | 真出现「库里有规则、kb_doc 里没有」的库时再说。走知识层是刻意的：`maos/kb/**` 是领域无关内核，查 `policy_rule` 表等于把退款域挂到它的 import 图上（那条边界只在证据生成器 `experiment.py` 上破例，且仅此一处），而且知识层那条路带得出 `doc_id`，`citations` 才是可回查的 |
## task-t113（圆桌落库之后的范围外发现，2026-09-10）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-10 | p10 | `model_usage` 表没有一列能放 `model_call_id`，互查只能按 `(plan_id, agent_role)` 的写入顺序对齐（见 DECISIONS ## task-t113 第 2 条） | 并发跑同一 case 的同一座位时理论上会错位（真房间里一次只过一单，实际撞不上）；读的人要知道这条口径才查得动 | `maos/core/store.py` 现有表冻结（铁律 1）。哪天有一次「允许给冻结表加列」的整体决策，给 `model_usage` 加一列 `call_id TEXT`，互查就从顺序变成等值 —— 不在本轨范围 |
| 2026-09-10 | p10 | `RefundRoundtable._tenant_of_plan` 是一个只增不减的 dict：常驻房间每过一个新 case 多一条（`plan_id -> tenant_id`，几十字节） | 演示与真房间的量级下可以忽略；跑几个月的进程会慢慢涨 | 圆桌真的常驻起来之后换成 LRU（或干脆让 `verdict_of` 的调用方把 tenant 传进来）；本轮不做 |
| 2026-09-10 | p10 | `hiclaw/room_ingress.py::wire()` 与 `IngressRouter.__init__` 各调一次 `attach_store` | 同一个 store 调两次是幂等的，只是读代码的人会问「为什么两遍」（两处都有注释说明） | 整合期若确认 `wire()` 之外没有别的建圆桌路径，去掉 `wire()` 里那一处 |
| 2026-09-10 | p10 | 圆桌的 `model_usage` 行 `trace_id` 恒为空串，于是 `trace.json` 的 `summary.model_calls` 会把圆桌与 DAG 的调用加在一起，而 `attributed_model_calls` 只数 DAG 的 | 「本次演示烧了多少 token」这个数现在含圆桌；看板上要分开报时得自己按 `call_site = maos/roundtable/speaker.py::Speaker.complete` 过滤 | T114 把圆桌事件并进证据束时，顺手在成本那一段按 call_site 拆一行「其中圆桌」 |
| 2026-09-10 | p10 | `speaker.Speaker.complete` 只在**成功**的模型调用上记账；`complete()` 抛异常那次不落 `model_call_failure`（`record_model_failure` 存在但本轨没接） | 圆桌的失败调用在成本表里看不见，「网关抖了几次」这个问题在圆桌这一侧答不了 | 接 `record_model_failure` 是几行的事，但它要一个新的判据（失败行的 call_site 同样要登记），留给整合期或专门一轨 |

## 整合期 p10-b（Wave B 三轨合并，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | 「整合期 p10-a」那三处 roundtable / router 接线仍未做（`outcome_commands` 进 router、财务岗卡片接投影、`verdict.py` 角色名对齐），本轮 push 只合三轨 | 房间里 `/assign /resolve /confirm /complain` 仍无人应答；财务岗仍按老文案说状态 | 复赛前单开一轨（白名单 `maos/ingress/router.py` + `maos/roundtable/**`），先定「命令对着哪个库」：T113 的 `MAOS_INGRESS_DB` 让 router 的 store 落文件，但 `/refund` 的处置仍在 `custom_case` 自建的 `:memory:` 里 |
| 2026-09-11 | p10 | ~~T114 的 `gateway_fail` 路径没有 `REWORK`：可重试码让付款跑三趟时 `payment_execute.py:143/163` 同渠道重试会留 2 条悬空 `business_ref`（verify 第 2 项判负），只好换终态失败码绕开~~ | ~~单案例束里「返工」这一环只能指去 `evidence/scenario-2/`~~ | **已处理（2026-09-11，task-t124）**：DELETE 判据加上版本那一维之后可重试码用得回来了。`evidence/case-real-01/gateway_fail` 现在有一条 `AWAITING_REVIEW -> REWORK [gate_rework]`（`hitl-trace.json` 里 `kind=rework` / `actor=gate`），返工之后 `payment.execute` 真的又发起了一次（`invocations=2`），第二次网关改口终态失败转人工，补偿闭环原样保留。没有走「作废引用」那条路 —— `objects.py` 不在白名单，也不需要动 |
| 2026-09-11 | p10 | `source ~/.maos.env && python3 scripts/make_evidence.py` 仍 exit 2：空补丁集已放行，现在停在模型产出质量（`corrupt patch` / 回归 4 过 1 挂） | 带 key 的 shell 下 `test_cost_metrics` 那一条仍红；Scripted 口径不受影响 | 复赛口径保持「证据束一律 Scripted 产，真模型束单独标」；要治只能在 coding 岗提示词 + `git apply --check` 重试那一轨 |

## task-t125

五条旧账的处理状态（行号按本文件合入前的版本）：

| 旧账 | 记的是什么 | 本轨处理 |
|---|---|---|
| `:2263`（2026-09-09） | 真模型下 `scenario_1` 走不到 DONE，coding 岗产的 unified diff 不合格，`validate` 报 `corrupt patch at line 14`，三次 attempt 全被拒 | **两条都接上了**：`SYSTEM` 补三句 unified diff 硬约束（文件头 / hunk 头行号真实 / 不许省略上下文），落盘前加 `git apply --check` 预检并最多重问 2 次。原话「要么把 diff 格式约束写死并加一轮自校验，要么在落盘前做一次 `git apply --check` 的重试」—— 两条都做了，没有二选一 |
| `:2290`（2026-09-10） | `make_evidence.py` 跑不成，连带 `test_verify_warn` 4 条 + `test_domain_evidence` 7 条 ERROR + `test_cost_metrics` 1 条 FAILED | **归零**，但治的是另一半：`MAOS_FORCE_SCRIPTED` 让 `make_evidence.py` 的子进程缺省走 Scripted，那 11 条 ERROR 与 1 条 FAILED 在带 key 的 shell 下不再出现（验收 2 与验收 1 同数）。这一条里「单开一轨查 coding agent 那次重试耗尽」的部分由上一行接管 |
| `:2293`（2026-09-10） | 真根因是「把纯定位/调查任务派给了只会产补丁的 `code.repo-patch`」，空补丁集要升格成合法结论 | 空补丁集那一层 T114 已做（`code_repo_patch.py` 与 `sandbox.py:450-457` 两处）。**本轨只保证它没被预检重新判死** —— `sandbox_git_apply` 对空 files 在碰 workdir 之前就早返回 ok，`test_an_empty_patch_set_with_summary_survives_the_precheck` 钉住。「别把调查任务派给补丁 skill」那一半仍未做，见下表 |
| `:2381`（2026-09-11） | 根因换成两个：① `corrupt patch at line 39` ② 沙箱回归 4 过 / 1 挂。自陈「`≤ 40 行`的根因面修不动」 | **① 治了，② 不治。** ① 由预检 + 自修复 + 提示词三句消掉；② 是「模型修得对不对」，`git apply --check` 从原理上答不了 —— 补丁合法与补丁正确是两件事。派单 §2 第 5 条明说本轨不承诺 ② |
| `:2414`（2026-09-11，整合期 p10-b） | 同 `:2381` 的复述；「复赛口径保持『证据束一律 Scripted 产、真模型束单独标』」 | **口径从人记得变成机器缺省**：`run.py` / `demo_preflight.sh` / `make_evidence.py` 子进程 / `conftest.py` 四处 `setdefault`，`--live-model` 是唯一显式开关；真模型束由 `INDEX.json` 与逐束 `model_mode` 标出来（字面值与 `make_case_bundle.py` 对齐） |

本轨新留的账：

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **「沙箱回归 4 过 / 1 挂」仍在**（`gate.py:374-383` 判 major）。本轨消掉的是「补丁打不上」，没碰「补丁修得对不对」 | 带 key 跑 `run.py --live-model --scenario 1` 时 Plan 仍可能 FAILED，但失败理由从 `tool_error: 补丁没能落进沙箱` 变成 Gate 的回归 finding —— 后者是**返工链正常工作**的样子，不是工具坏了 | 要治只有两条路，都超出 skill 面：① 按 `:2293` 的原话，别把「定位/调查」任务派给只会产补丁的 `code.repo-patch`（改 Manager 规划提示或加一个调查类 skill）；② 把沙箱回归的失败用例逐条喂回模型再修一轮（那是 attempt 层的事，不是 skill 层） |
| 2026-09-11 | p10 | `MAOS_FORCE_SCRIPTED` 是走配置面的**第四个**不进 `GOVERNED_KEYS` 的旋钮（另三个：`MAOS_KB_ENABLED` / `MAOS_KB_WEIGHTS` / `MAOS_KB_ADVICE`）。（2026-09-11 整合期更正：本行原写「第五个，前四个：」后面只列了三个，数目对不上。）现况是「能治理（Nacos 上改得到、不用重启），变更不落审计」 | Nacos 上有人把它从 1 改成 0，`event_log` 里不会有 `ConfigChanged` —— 而这个旋钮一改，整批证据束的成本读数含义就变了。比 kb 那三个更值得审计 | 补齐是「四行改动 + 一条断言」：`GOVERNED_KEYS` 加这四个 key，`test_config_source.py::test_governed_keys_are_exactly_the_four_this_track_owns` 改名并改断言。那个文件从来不在加旋钮那一轨的白名单里，所以要单开一轨或整合期顺手做。`## task-T35` 记的是同一笔账。**已处理（2026-09-11，task-t131）**：单开了一轨（T131）。**「第四个」这个数也不对** —— 实跑 `grep -rn "get_config_source()" --include=*.py` 是 11 个读取点、10 个旋钮，`MAOS_SANDBOX_REQUIRE_CONTAINER` 与 `MAOS_MAX_PLAN_REJECT` 从来没进过那张表，所以当时是**六个**不在清单里。本轨只补了被点名的四个，那两个见 `## task-t131` |
| 2026-09-11 | p10 | 预检每次现造一份靶场副本（`prepare_sandbox_workdir()`，实测 ~80ms / 次），一次 invoke 内的多轮重问复用同一份，但**跨 invoke 不复用** | 全量测试与 `run.py` 各多付几次 80ms（只在 `max_self_repair > 0` 的岗位上，目前只有 coding）。实测全量条数与耗时都没有可见变化（100.5s -> 104.3s，在正常波动内） | 真嫌慢了再说。缓存一份基线副本要处理并发与脏化两件事，为 80ms 引入那两个问题不划算 |
| 2026-09-11 | p10 | `docs/skill-catalog.md` 里 `code.repo-patch` 的行号随本轨改动从 `:62` 漂到 `:119`（重跑 `scripts/gen_docs.py` 产出，2 行纯行号） | 整合期若别轨也动了 `maos/skills/**`，这两行会冲突 | 冲突时不要手改，重跑 `python3 scripts/gen_docs.py` 即可 —— 那三份是生成物，手改会被 `test_generated_docs.py` 逮住 |
| 2026-09-11 | p10 | `--live-model` 只加在 `run.py` 与 `scripts/make_evidence.py` 上，`scripts/demo_preflight.sh` 没有对应开关（它 `export MAOS_FORCE_SCRIPTED=1` 写死） | **preflight 根本没有真模型这条路**（下面「仍然盖得住」那句是错的，2026-09-11 整合期更正） | 不做。preflight 是复赛现场那道门，它的全部意义就是确定性 —— 给它一个「真模型」开关等于给那道门开一个后门。**2026-09-11 整合期更正**：本行原写「`MAOS_FORCE_SCRIPTED=0 bash …` 仍然盖得住（shell 语义）」是错的 —— 脚本内部是 `export MAOS_FORCE_SCRIPTED=1`，它覆盖调用方前缀设的值（实测 `MAOS_FORCE_SCRIPTED=0 bash -c 'export MAOS_FORCE_SCRIPTED=1; echo $MAOS_FORCE_SCRIPTED'` -> `1`）。也就是说 preflight **写死 Scripted，没有任何开关**。这正是想要的形状，只是别把那句错的绕法当真 |
| 2026-09-11 | p10 | **整合期必做**：`evidence/case-real-01/**`（6 束）与 `evidence/contrast-R8/` 的出处仍是 `283ce68`，本轨 commit 后它们落后 HEAD 2 个 commit，`verify.py` 的 provenance 判负（9/10，7 条 FAIL 全是它们） | 单看本轨 worktree 时 `verify.py` 恒 9/10。**8 束 `scenario-*` 与 `evidence/domains/` 已刷到本轨 HEAD，无 `-dirty`**；`room/` 那 3 条是 warn 不计入 | 整合期在干净树上重跑：`make_evidence.py` + `--domains` + `make_case_bundle.py --all-paths` + R8 那条。这不是本轨引入的问题 —— provenance 的判据是「出处落后 HEAD 时看这中间动过什么」，**任何一轨只要 commit 了代码，这 7 条都会判负**（`283ce68..4756832` 只动过 `evidence/` 所以基线判 info、10/10） |
| 2026-09-11 | p10 | `bash scripts/demo_preflight.sh` 卡在第 1 步：新增 34 条测试让全量变成 `1 failed, 3667 passed`，而那 1 failed 正是派单点名的预期内的红（`test_expected_metrics`），preflight 的「全绿」判据不区分它 | 复赛现场那道门在本轨 worktree 里过不了。第 2-5 步逐条实跑均通过（`run.py` exit 0 / `--scenario 7` exit 0 且镜 5 镜 6 两个判据都命中 / `make_evidence` + `--domains` exit 0 共 8 束 / verify 9/10 见上一条） | 整合期刷 `docs/expected-metrics.json` 的 `pytest_passed_nopg`（3636 -> 本轨 3667，五轨合并后按实跑重取）与 `collected` 那个数，第 1 步即自动转绿。派单硬约束禁止本轨动那个文件，所以只能留到整合期 |
## task-t122

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `maos/tests/test_compensation_close.py::test_confirm_and_complain_parse_and_authorize_but_do_not_persist` 钉的是 T117 那条「`/confirm` `/complain` 只解析不落库」的边界，而派单 §3.2 显式要求把它反转成真写表 | 全量测试多一条红（本轨留着没改：该文件不在白名单） | 整合期或人类点头后改：两处 `assert ok.kind == OC.KIND_PENDING` 换成 `KIND_DONE`，`data["field"]/["value"]` 那两句换成查 `case_outcome` 落没落行，末尾那句「这两条命令一行都不该落库」整条删掉 |
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` 要从 3636 加到 3673（本轨新增 37 条） | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红，是派单点名的「预期内的红」 | 整合期统一刷（本轨按派单不许动这个文件） |
| 2026-09-11 | p10 | `outcome._receipt_source()` 读观察回执的**顶层** `source` / `gateway`，而 `ManualReceiptAdapter` 把 `gateway='manual'` 放在 `detail` 里、顶层 `source` 写的是一段中文说明 | `_correction_of` 的 `overridden` 分支**恒不命中** —— 人工回执从来判不出「人工纠错」这一档。当前不误判（工单仍把 `manual_correction` 判成 `compensated`），但少判一档，而且没有任何测试会红 | `maos/domain/refund/outcome.py` 那一轨：`_receipt_source` 再读一层 `detail.gateway`，或让 `ManualReceiptAdapter` 在顶层也写一份 |
| 2026-09-11 | p10 | ~~房间路径里网关失败的单，Plan 仍收在 **DONE**：`handle_execute` 恒 `approve=True`，付款闸上没人拒签~~ | 一个 DONE 的计划配一个「钱没到账」的结局，正是评委那句「所有 Agent 都回复完成不代表业务成功」指的病 | **已处理（2026-09-11，task-t135）**：走的**不是**这条建议。原判「要让付款闸自己停在 BLOCKED（`runtime/gate.py` 面）」不成立 —— 实跑证明它**本来就停在那里**（`control_plane._escalate_to_human` 的第三出口，`detail.await=human_decision`）；病在房间路径把它**代签**掉了。改法是 `run_payload` 加 `hold_awaits`、房间传`(AWAIT_HUMAN_DECISION,)` 不代签，人在 `/reject` `/approve` 上做第二次决定。`runtime/gate.py` 一个字节没动 |
| 2026-09-11 | p10 | 演示底账里**没有**一单带失败网关码：`scenarios/custom/ledger.json` 加一单会打破 `test_room_team_fixture.py:184` 那条严格订单列表断言（白名单外），`scenarios/bulk/ledger-bulk.json` 是 `generate.py` 的生成产物、文件抬头写着「勿手改」 | 真房间里手打演不出「网关失败 → 工单 → 派单 → 关单」，得自己先造一份带 `gateway.fail_orders` 的底账。机器判据不受影响（`test_room_outcome_commands.py` 的链路测试用 tmp 底账注入，已全绿） | 二选一，都要人点头：① custom 加一单并同步 `test_room_team_fixture.py` 的 `NEW_ORDERS`；② 改 `scenarios/bulk/generate.py` 让它生成那一单再重跑生成器 |
## task-t123（圆桌三态之后的范围外发现，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | 房间里的**另一张嘴**仍念七态：`maos/ingress/router.py:1199` 借 `scripts/run_requests.py` 的 `STATUS_CN` 拼回帖，没有接 `projection.public_status()` | 同一单在圆桌财务岗那里已经有了「对客户口径」，router 回帖仍是内部七态 —— 两套措辞的问题只治好了一半 | 归 T122（契约 §D 把 router 那一行明确划给它）。本轨不许动 `maos/ingress/**`。两轨合完之后房间里两张嘴才算对齐 |
| 2026-09-11 | p10 | 整表模式（`facts_sheet_finance`）没有对外口径那一句 | 一次过一张表时，群里只有逐单预演金额与合计，没有任何一单的三态 | 刻意不做（见 DECISIONS 第 5 条）：整表是**放行前**的预演，`payment_observation` 还是空的，投不出三态。真要在整表上报三态，得先有一条「整表放行后回看」的路径，那是另一件事 |
| 2026-09-11 | p10 | `roles._load()` 改成缓存之后，运行期改 `scenarios/refund/roles.json` 不再即时生效 | 换目录的测试与真去改目录文件的人都要显式 `roles.clear_cache()`；漏了的症状是「改了没生效」而屏幕上一切正常 | 已用一条专门的测试钉住（`test_clear_cache_picks_up_a_changed_directory_file`）。哪天目录真要热更新（Nacos 一类），把缓存换成带 mtime 判据的那种，而不是去掉缓存 |
| 2026-09-11 | p10 | 两套角色名只在**圆桌这条边**上收敛了：`maos/flows/contrast.py` 与 `maos/kb/plan_advice.py`（`DEFAULT_APPROVER_ROLE = "supervisor"`）仍直接写房间那套字面量 | 不出错（那套名字经 `canonical_role()` 认得），但「角色名的权威在目录」这件事还差这两处才算完整 | 单开一轨把 `contrast.py` / `plan_advice.py` 的角色名也走目录常量。本轨白名单外，且 `plan_advice` 属 `maos/kb/**`（领域无关内核，不该硬依赖退款域 —— 那一轨要先定怎么传） |
| 2026-09-11 | p10 | `maos/tests/test_expected_metrics.py::test_collected_count_matches_source_of_truth` 变红：本轨 +14 条测试后收集数 3729 与 `docs/expected-metrics.json` 的 3715 对不上 | 一条红，**预期内**（派单 §6 点名） | 整合期统一刷。契约 §A 把那个文件列进「谁都不许动」 |
## task-t124（返工证据之后的范围外发现，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | 🔴 **`docs/toolport-contract.md` 行号过期，本轨没刷**。`maos/tools/gateway.py` 里 `MockGateway` 新增了注入选项（约 +97 行），两个 `ToolPort` 常量的行号从 597/627 下移到 694/724 —— 那份生成物记着行号 | `test_generated_docs.py` 两条红：`test_generated_doc_matches_code[docs/toolport-contract.md]`、`test_check_mode_agrees_and_writes_nothing`。**纯行号漂移，4 处，没有语义差异** | **2026-09-11 整合期更正：本轨其实已经刷了** —— `fe6ab80` 里跑过 `python3 scripts/gen_docs.py`，`test_generated_docs.py` 在本轨 HEAD 上是绿的，本行原记的「停手不刷、两条红」与 diff 相反。**注意 T125 大概率同病**（它动 `maos/skills/builtin/code_repo_patch.py` 与 `maos/agents/coding.py`，对应 `skill-catalog.md` / `agent-identity.md`），整合期一次跑生成器把三份一起平掉最省事 |
| 2026-09-11 | p10 | `maos/tests/test_expected_metrics.py::test_collected_count_matches_source_of_truth` 变红：本轨 +11 条测试（`test_payment_retry_refs.py` 8 条 + `test_make_case_bundle.py` 3 条）后收集数与 `docs/expected-metrics.json` 对不上 | 一条红，**预期内**（派单 §6 点名） | 整合期统一刷。契约 §A 把那个文件列进「谁都不许动」 |
| 2026-09-11 | p10 | `scripts/make_case_bundle.py::_gateway_retry_injection()` 是一层猴补丁：`custom_case._gateway_of` 只认 `settle_after` 与 `fail_with`，认不了本轨新加的 `payload["gateway"]["fail_times"]` / `["then_fail_with"]` | 多一层间接。功能上没有缺口（注入实跑有效），只是读的人要多跳一次才找得到网关是怎么造出来的 | `maos/flows/custom_case.py` 是 T122 的面。等它空出来之后，在 `_gateway_of` 里原生转发这两个键（照 `settle_after` 那三行的写法），然后**把 `_gateway_retry_injection` 连同 `run_path` 里那一层 `with` 整个删掉** —— 路径配置一个字不用改。手法同文件里的 `_file_backed_store` 也是同一类临时包装，可以一起收 |
| 2026-09-11 | p10 | `MockGateway.fail_times` 的改判**只发生一次**：改判后的码要么 `outcome=success`、要么 `retriable=False`，两者都落不进 `_supersede_due` 的第二条判据 | 演不出「失败 3 次、每次换一个码」这种多段时序。本轨不需要（`gateway_fail` 只要两段） | 真需要多段时再说。那时的形状多半是让 `script` 的值接受一个码序列（末位重复），比现在两个 dict 更省 —— 但那是改既有选项，要一并把 `_gateway_of` 的透传口径定下来 |
| 2026-09-11 | p10 | `payment.observe` 在本轨的 `gateway_fail` 束里被调了 3 次（两次付款尝试 + `refund.compensate` 的留档），而 `payment.execute` 2 次 | 不是缺陷，只是 `skills.json` 上两个数不相等，读的人可能会问 | 不必改。留这条账只为说明第三次来自补偿留档（`refund.compensate` 先记最后一次观察），不是重发 |
## task-t121（材料轨的范围外发现，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `scripts/verify.py` 的模块 docstring 抬头仍写「**九项**：」并只列到第 9 项 `provenance`，第 10 项 `case-outcome` 没进那份清单（函数本体与 `CHECKS` 都在，输出也是 10 项） | 读代码的人从抬头数出来是 9 项，与 `RESULT: 10/10 PASS` 对不上；材料侧只能绕开抬头去引 `CHECKS` | ✅ **T128 已落地（2026-09-11 Wave D）**：抬头改成「十项：」，第 10 项 `case-outcome` 连同它那两句「它在核什么 / 失败意味着什么」一并进了清单（判据照 `check_case_outcome` 的四条实写：到账 / 客户确认 / 人工纠错 / 投诉取值合法、`business_success` 是算出来的、`arrival_basis` 回查得到观察行）。同时把「前八项…第 9 项」那句改成「第 9 项之外的九项」。`maos/tests/test_verify_warn.py::test_module_docstring_lists_all_ten_checks` 钉住：`CHECKS` 每一项的 key 都得出现在抬头里，第 11 项进来那天这条会红在「抬头没跟着改」上 |
| 2026-09-11 | p10 | `scripts/render_trace.py` 的束扫描写死 `glob("scenario-*")`，`evidence/case-real-01/**` 与 `evidence/contrast-R8/` 进不了 `report.html` | 本仓库的**主证据**（单案例纵切）在可视化页上一格都没有；材料里只能反复写「别在 report.html 里找单案例」 | 复赛前值得单开一轨：把 glob 放宽成「有 `trace.json` 的目录」，或显式加一份束清单。改完要连 `report.html` 一起重渲染并核 `case-real-01` 的四束都出得来 |
| 2026-09-11 | p10 | ~~`evidence/case-real-01/**` 各束里没有 `maos.db`~~ —— **这条记反了，2026-09-11 整合期 p10-d 实测更正**（跨轨契约 §J 点名由整合期改）。每束都落了 `maos.db`（524KB），只是 `.gitignore` 的 `*.db` 让它不入版本库 | **没有影响**：`python3 scripts/replay_roundtable.py --db evidence/case-real-01/happy/maos.db --list` 当场列出 `roundtable:RC-2026-0904-001（8 条）`，台上演「圆桌顺序从 `event_log` 重建」指的就是这一单，不必再用 `room_team_smoke.py --db` 现落一个别的库 | **无需处理**。留着这一行是为了记住这条账错在哪：写它的时候查的是 `git status`（被 ignore 挡着看不见），没有去 `ls` 那个目录 —— 「版本库里没有」与「本机没有」是两件事。重跑过 `make_case_bundle.py` 的机器上它就在那儿；只 checkout 不重跑的机器上确实没有，那时演示前先跑一次束即可 |
| 2026-09-11 | p10 | `docs/demo-script.md` 新主线九镜的**逐字念词尚未定稿**，秒数表里给的是「念词预算」上限而非实测 | 录制前如果直接按预算写词，很可能超 —— 上一版那张表的每一镜富余都只有 3–5 秒 | 念词定稿后重掐一遍，把「念词预算」列换成「讲稿字数 / 念完约需 / 富余」三列（上一版的口径：中文 4 字/秒，英文标识符按 1.5 字粗算） |
| 2026-09-11 | p10 | `docs/submission-checklist.md` 的 `metric:frozen` 举例行里举的是 `802 passed` / `7/7 PASS` / `35/35`，三个都已过期 | 读者可能把举例当现行值。该行自带注释说明「这里是举例说明什么算一个数字，不是现行读数」 | **不动**（本轨白名单排除 `metric:frozen` 区）。下次有人重写那一节时换成不带具体数字的举例，或换成当时的现行值并保留 frozen 标记 |
| 2026-09-11 | p10 | 四条路径的 `hitl-trace.json` 里 `kind` 只有 `plan_approval` / `task_approval` / `blocked` / `drift`，**没有 `rework`**；实跑可见闸判 `gateway: fail -> rework` 紧跟着 `gateway_needs_human`，直接转人工、不走 REWORK 那一跳 | 「返工 / HITL Trace」这一条在单案例上指不到证据，只能指去 `evidence/scenario-2/` 与 `evidence/scenario-7/` | ✅ **T124 已落地（2026-09-11 Wave C）**：`gateway_fail` 束现在有 `kind=rework` 一条（`AWAITING_REVIEW->REWORK [gate_rework]`，挂在付款任务上），紧随 `REWORK->PENDING [requeue]`；剧情是可重试码 `40005` 被拒 → **同渠道**重发一次 → 网关改口 `ACQ.SELLER_BALANCE_NOT_ENOUGH`（终态）→ 转人工 → 人在付款闸拒签 → 补偿工单闭环。本行原写的「直接转人工、不走 REWORK 那一跳」已不再成立。四处材料占位在同一轮整合期一并按实跑刷掉了 |
| 2026-09-11 | p10 | 房间里的真人审批还没进任何证据束：`evidence/room/` 那五张截图跑的是 `role=coding` 的软件域任务，不是退款案例 | 「HITL 是真人在房间里做的」这句话今天只能说到 `/approve` `/reject` 命令生效，说不到「这一单是在房间里批的」 | **9/18 真跑日**采集。采完要改的四处与上一行同一批（`docs/demo-script.md` 镜 7 的红线里已写死了这一句占位） |

## 整合期 p10-c（Wave C 五轨合并，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | T123 给圆桌财务岗加的「对客户口径」一行，**在任何证据束里都看不到** —— `maos/flows/custom_case.py` 只调 `team.on_preflight`，而那行所在的 `facts_finance_result` 走的是 `on_execute`，全仓唯一调用点是 `maos/ingress/router.py`（真 Matrix 房间那条路） | 「圆桌财务岗会对外说人话」这件事今天只能在真房间里演，`make_case_bundle.py` 产的五束 `roundtable.json` 里没有它。材料因此不许拿证据束当它的出处 | 9/18 真跑日在真房间放行一单、`capture_room_transcript.py` 采进束，那之后材料才能指到具体文件。**在那之前别把这一行写进任何「证据路径」表** |
| 2026-09-11 | p10 | 圆桌事实卡与 router 回帖对「没有对外口径」的处置不一致：圆桌打一句「尚未到可对外说的三态」（T123 派单明文要求），router 空串**不打行** | 同一个案子两张嘴措辞会漂 —— 正是 `maos/domain/refund/projection.py` 抬头警告的那件事。两边各自合规，合起来不一致 | 抽一个共享的「对客户口径」渲染函数让两处复用（推荐用圆桌那句，契约与派单都有据）。要动 `maos/roundtable/**` 与 `maos/ingress/router.py` 两个面，单开一轨 |
| 2026-09-11 | p10 | 两份「缺省审批岗」各写各的：`maos/roundtable/verdict.py` 用 `roles.ROLE_AFTER_SALES_SUPERVISOR`，`maos/kb/plan_advice.py` 用 `DEFAULT_APPROVER_ROLE = "supervisor"` | 今天同解，改一处不改另一处会**静默分叉** | `maos/domain/refund/roles.py` 定义一个 `DEFAULT_APPROVER_ROLE`，两处都引它。注意 `maos/kb/**` 是领域无关内核（铁律 9），不能直接 import 退款域 —— 要么让调用方传进去，要么在 kb 侧留成参数 |
| 2026-09-11 | p10 | 事实卡上两句「到账」判据不同源：`payment_line` 数 settled 行，`public_status` 看最后一条观察 | 观察序列是 `[settled, processing]` 时，同一张卡会同时出现「已观察到账」与「对客户口径：支付处理中」 | 那个序列本身意味着库里自相矛盾（`guard.py` 本该拦住），比文案更值得查。`payment_line` 三句被 `test_roundtable_stages.py:208/223/234` 钉死，改文案要先动那三条 |
| 2026-09-11 | p10 | `maos/domain/refund/roles.py` 的 `canonical_role()` 只 `strip` 不折大小写，`"Supervisor"` 原样透传、被 `_approver` 当未知角色念出去 | T117 既有行为，语料全小写所以撞不上 | 真接外部角色名（真 Matrix 房间的显示名）之前顺手折一次大小写 |
| 2026-09-11 | p10 | `scripts/run_ingress.py::_store()` 没有 `hiclaw/room_ingress.py::wire()` 的三个 ensure | 两个房间入口建表口径不一。功能靠 skill 懒建表撑住（`/assign` 早于 `/approve` 时回「这个案子没在本房间跑过」，不崩） | 下次动 `run_ingress.py` 的轨顺手补齐，或索性让两个入口共用一个 `_ensure_room_schema()` |
| 2026-09-11 | p10 | 房间补偿开单失败后**没有任何命令能补开**：`/approve` 被「不重跑」闸拦死，`require_ticket` 不补开 | 长命库（`MAOS_INGRESS_DB` 指向文件）里这种案子会永久锁死，只能换库。演示当场撞上就没救 | 值得一个小轨：加一条 `/compensate <案号>` 补开，或让「不重跑」闸对「案子在 compensated 但没工单」这一格放行 |
| 2026-09-11 | p10 | `router.handle_outcome` 的 extras 只带 `plan_id`，于是 `CompensationAssigned` / `CompensationResolved` / `CaseOutcomeComputed` 三个事件的 `trace_id` / `task_id` 是空的；同文件的 `_compensate_if_stuck` 三者齐全 | 按 trace 串起「房间里这一串命令」时，命令落的事件接不上 DAG 那一半 | 与上一条一起做，`_compensate_if_stuck` 已有现成写法可抄 |
| 2026-09-11 | p10 | `outcome_commands.KIND_PENDING` 在 T122 之后已无人产出（四条命令都真落库了），`COMMAND_HANDLERS` 上方还留着「本轨一个字都不动那个文件」的旧注释 | 死常量 + 过期注释，读的人会以为还有一条 pending 路径 | 下次动 `outcome_commands.py` 的轨顺手删。`KINDS` 是对外形状，删之前 grep 一遍消费方 |
| 2026-09-11 | p10 | 重复 `/resolve` 的回帖原样带着 `ValueError:` 前缀（直接回显 `res.error`） | 房间里的人看到一行 Python 异常名，不是人话 | 措辞问题，下次动命令层顺手改 |
| 2026-09-11 | p10 | `maos/model/client.py` 打的「启用真模型：base_url=…」那行仍会进 `run.log`；本轮已把 `MAOS_LLM_BASE_URL` 加进 `make_evidence.py` 的恒脱敏名单，但脱敏是**产物侧**的兜底 | 兜底生效前，那行在终端与 CI 日志里仍是明文 | ✅ **T128 已落地（2026-09-11 Wave D）**：那行现在打 `base_url.host=<host[:port]>`，userinfo 与路径都剥掉（`_url_host()`），解析不出 host 时打 `?` 而不回退到原串。`base_url` 的取值、拼接、请求逻辑一个字未动。残留（host 本身仍进 `run.log`，全串哨兵扫不到它）另记在 `## task-t128` 那一节 |
| 2026-09-11 | p10 | `scripts/make_evidence.py --live-model` 产出的束落在与 Scripted 束**同一个** `evidence/scenario-*` 目录（不像 `make_case_bundle.py` 出到 `<path>-live/`），而 `verify.py` 不看 `model_mode` | 真模型束可以覆盖 Scripted 束并照样过 verify。本轮修掉了「标签说谎」那一半（旗标现在真能传到子进程），但「覆盖」这一口子还在 | ✅ **T128 已落地（2026-09-11 Wave D）**：两件都做了 —— `--live-model` 缺省出到 `evidence/live/`（`--domains` 则 `evidence/live/domains/`，`--contrast` 同根），显式 `--out` 指进另一种模式的根由 `assert_root_mode()` 当场拦下；`verify.py` 按 `model_mode` 把真模型束排除出核验对象**与**第 9 项的分母，结尾点名，整根全 live 时明确拒绝而不是印 `0/0 PASS` |
| 2026-09-11 | p10 | `code_repo_patch.py` 的重问提示词不带上一版 diff 原文，模型看不到 `corrupt patch at line 6` 指的是哪一行 | 只影响自修复命中率，不影响正确性 | 下次调这个 skill 的提示词时一起带上。另：重问轮里模型抛异常或产非法 JSON 会直接 failed，把第一轮那份「合法但打不上、本可交给 Gate」的补丁丢掉了 |
| 2026-09-11 | p10 | `MAOS_FORCE_SCRIPTED` 与 kb 三个旋钮共四个仍不进 `maos/config/source.py` 的 `GOVERNED_KEYS` | 能治理（Nacos 上改得到、不用重启），但变更不落 `ConfigChanged` 审计。这个旋钮一改，整批证据束的成本读数含义就变了，比 kb 那三个更值得审计 | 「四行改动 + 一条断言」，`test_config_source.py` 那条按数目写的测试要一起改名。`## task-T35` 记的是同一笔账，本轮只更正了数目笔误没有补齐 |

## task-t126（PolarDB 束之后的范围外发现，2026-09-11）

| 日期 | Phase | 现象 | 影响 | 建议 |
|---|---|---|---|---|
| 2026-09-11 | p10 | 知识层（`kb_doc` 与它的全文影子表）**仍在 SQLite**，本轨只把退款域业务表搬上了 PG。`docs/architecture.md` §5 的标题「业务对象**与知识层**在 PolarDB」因此比实况说得多半句 | 答辩时若照标题说「知识层也在 PolarDB 上」，一条 `\dt` 就能证伪 | 归持有 `maos/kb/**` 或 `docs/architecture.md` 的轨。知识层走的是另一套开关（`MAOS_STORE_BACKEND` + `kb.port_of(store)`），`pg_store.py` 的 tsvector / pgvector 两条通道都已填实，缺的是把它接到装配上。**在接上之前，材料面口径应改成「业务对象在 PolarDB，知识层可切、本轮未切」** |
| 2026-09-11 | p10 | `docs/architecture.md:183` 那一行只写了 `MAOS_DOMAIN_BACKEND=postgres`（进程级），本轨之后还有一个**装配级**的 `MAOS_FLOW_DOMAIN_BACKEND` —— 而两者的差别不是风格问题：只设进程级那个，整条 DAG 会停在第一步 | 照文档那一行操作的人会撞上「受理幂等闸失败」，而报错完全不提后端开关 | 归持有 `docs/architecture.md` 的轨（本轨白名单外，没动）。补一行并点名「跑整条 DAG 用装配级那个」。成因与实测症状见 `docs/DECISIONS.md ## task-t126` 第 1 条 |
| 2026-09-11 | p10 | PG 束的 `business-objects.json` 是**空的**（内容在同目录 `pg-tables.json`），因此 `verify.py` 第 2 项（business_ref 解析）对 PG 束必判负 | `verify.py` 目前只跑 `evidence/case-real-01/`，PG 束不在它的扫描面上，所以现在不红。但哪天有人把 PG 束也喂进去，会得到一条看起来像回归的假警 | 两条路任选：让 `verify.py` 认 `INDEX.json` 的 `domain_backend` 字段、PG 束改判 `pg-tables.json`；或让 `make_evidence.collect_business_objects()` 接受一个可替换的业务连接。后者更收口，但那个文件是 T128 的面 |
| 2026-09-11 | p10 | `maos/kb/promotion.py:414` 与 `maos/domain/rtv/objects.py:141` 也用 `sqlite_master` 探表。本轨的方言翻译让它们**顺带**能在 PG 上工作了，但没有针对性测试 | 现在是「能用但没证过」。RTV 域真要上 PG 时，`SELECT name FROM sqlite_master WHERE type='table'`（不带 name 收窄）那一句会把 `information_schema` 里**全部**表都列出来，包括不属于该域的 | 归各自的轨。翻译本身有测试（`test_flow_domain_backend.py` 三条），缺的是这两个调用点的端到端确认 |
| 2026-09-11 | p10 | PG 束与 SQLite 束的 `business_ref` 行数对不上：PG 侧 62 条里只有 34 条 resolve 得动（跨多次跑累积，旧版本对象被覆盖后引用悬空）。清场之后本案是干净的，但**清场只清本租户本案** | 同一个库上跑过别的案子时，`business_ref` 会留下解析不动的历史行。不影响本案判据（都按 `plan_id` 收窄），但全表扫的人会看到一堆悬空引用 | 不是 bug，是 PG 持久性的自然结果。真上 PolarDB 时按租户/案号定期清理，或给 `business_ref` 加一条按 `plan_id` 的保留策略。本轮不做 |
## task-t127（report.html 收进单案例束，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **材料里那几处「别在 report.html 里找单案例束」现在是错的**，本轨已把事实改掉，措辞归 9/20 材料轮。逐处：`README.md:288`（「只覆盖 8 个场景束，见下方红字」）、`README.md:325-328`（整段红字，其中 `contrast-R8/` 那半句**仍成立**，只有 `case-real-01/` 那半句要删）、`docs/demo-script.md:377-378`（「别在 `evidence/report.html` 里找这条时间线」整条红线可删）、`docs/defense-brief.md:52-55`（「实话是**没有**」「台上别点开 report.html 去找单案例」整条可删，改成「有，而且 `happy` 与 `happy-live` 并排」） | 不改的话材料在劝评委别看一份现在已经能看的东西 —— 这是本期最可惜的一种错 | 9/20–9/21 材料轮。改之前先确认整合期已重产 `report.html`（判据：`python3 scripts/render_trace.py --check` 绿且那行写 `13 束`） |
| 2026-09-11 | p10 | `happy-live` 那束的 **5 次真模型调用全部落在 `unattributed_usage` 里**：`trace.json` 的 `summary.model_calls=6 / measured=5`，而 per-trace 的 `cost` 块只算到脚本那 1 次（`calls=1 / tokens_in=133 / all_estimated=true`），与 `happy` 束逐字相同 | 页面已把两个数并排印出来（对比表「模型调用」列 6 对 1、「告警」列标「归属不上的用量 5」，束内「挂不上树的东西」把 5 条全摊开），**看得见**。但数据侧仍是「有花销、指不到是谁花的」，真模型那一跑的成本在 `cost` 块里等于查不到 | 是 `make_case_bundle.py --live-model` 那条路的归属问题（圆桌五岗的调用没带 task 归属），属 T126/T128 的面，不归本轨。复赛前值得修 —— 「真模型花了多少」是会被问的 |
| 2026-09-11 | p10 | `evidence/contrast-R8/`（Planner 建议有无对照）与 `evidence/room/`（真房间实拍）仍进不了 `report.html`。前者有 `trace.json`、结构与场景束一样，改起来是一行 glob；后者是截图与逐字副本，不是 trace，本来就不该进这张页 | R8 那一束今天只能读 json。`README.md:325-328` 那段红字里关于 `contrast-R8/` 的半句因此**仍然成立**，删红字时别连它一起删 | 收 R8 是「`load_bundles` 的 glob 多认一个前缀 + 一张对比表怎么摆」的活，值得一轨；`room/` 不必收 |
| 2026-09-11 | p10 | 单案例**每一束**都带 `stray_events: 8`（四条脚本路径与真模型束一模一样的 8 条），八个场景束则是 0 | 页面照口径把 8 条全摊开了（「洞不许被藏起来」），评委看得见。但没人查过这 8 条是什么、为什么每束恒定 8 条 —— 恒定说明它是**结构性**的（多半是建 Plan 之前就发生的那几条 `SkillInvoked`），不是偶发 | 查一次，要么消掉、要么在 `make_case_bundle.py` 的 `INDEX.json` 里给它一句说明。复赛前做：评委问「这 8 条是什么」而答不上来，比页面上没有这一栏更难看 |
## task-t128（真模型束隔离之后的范围外发现，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `maos/model/client.py` 那行改成只打 host 之后，**host 本身**仍会进 `run.log`；`make_evidence.py` 的 `MAOS_LLM_BASE_URL` 哨兵匹配的是**全串**，匹配不到 host 这一截，兜底扫不出它 | 内网网关的主机名会随 `--live-model` 产的束一起发出去。凭据不再泄漏（userinfo 与路径已剥掉），泄漏的是拓扑 | 真要堵就得让哨兵按 host 也扫一遍，或那行连 host 都不打（代价是日志说不清连的是哪个网关）。复赛材料只发 Scripted 束，`evidence/live/` 不入库 —— 优先级低于「答辩现场能说清哪些束是真模型产的」 |
| 2026-09-11 | p10 | 两处真模型束的位置口径**不同**：`make_evidence.py` 分到根（`evidence/live/`），`make_case_bundle.py` 分到束名（`evidence/case-real-01/<路径>-live/`） | 读者要记两套。`verify.py` 两种都认（`is_live_bundle` 取索引与目录名的或），所以不影响正确性 | 要统一得动 `scripts/make_case_bundle.py`（本波是 T126 的面，本轨不碰）。统一的代价是改动一批既有路径引用（材料里写死了 `happy-live`），收益只是少记一套 —— 不急 |
| 2026-09-11 | p10 | 先跑过 `--live-model` 的机器上，之后那一跑缺省 `make_evidence.py` 的 `INDEX.json` 会多一条 `aux_bundles: live`（`scan_aux_bundles` 按目录扫，它确实在 `evidence/` 下） | 缺省束的**内容**因此取决于本机跑没跑过 live。整合期的顺序是 Scripted 先跑、且跑完 `git clean -fd evidence/`，所以现行流程碰不到 | 登记本身是对的（「漏登记一整个目录的索引不叫索引」），要改的是整合期收尾：重产证据前先 `git clean -fd evidence/`。已在本轨回执里点名，值得写进 `docs/phases/common.md` 的检查单（那是手册面，本轨不碰） |
| 2026-09-11 | p10 | `python3 scripts/make_evidence.py --live-model` 跑**缺省八束**会硬失败在场景 1：真模型下 `maos/flows/scenario_1.py` 自己的 `assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE` 不成立（实跑 c47b07e，真 DeepSeek key 走了网络），按「不留半份产物」的规矩整批不产 | `--live-model` 目前只能**挑场景**跑（`--scenarios N`）。真模型束的正式来源仍是 `scripts/make_case_bundle.py --path happy --live-model` —— 它只把圆桌五岗换成真模型，DAG 那一段仍是脚本回放，所以断言照旧成立 | 要么给 `make_evidence.py --live-model` 也做「只换某几个岗」的粒度（口径抄 `make_case_bundle.run_path` 的 `seat_model`），要么在 `--help` 与手册里写明它只适用于挑场景跑。复赛前不急：现场演示用的是单案例束那一条 |
| 2026-09-11 | p10 | `--live-model` 会把**内部钉死 `force_scripted=True` 的场景**（5/6/7/8/9/10/11/12）产的束也标 `model_mode: live` —— `model_mode()` 只看 `MAOS_FORCE_SCRIPTED`，看不见调用点传的参数 | 一批 live 束里可能大部分实际是脚本回放，标签比实跑宽。真相在各束 `model-usage.json` 的行数与 token 数里（T128 实跑的 `evidence/live/scenario-6` 就是这种：标 live、实跑 Scripted） | 让 `_bundle_info` 的 `model_mode` 逐束取「这束实际有没有真模型调用」（`model_usage` 行数），或在 INDEX 里并列记「本次意图」与「实际调用」两栏。后者更诚实：意图本身也是要记的信息 |
| 2026-09-11 | p10 | 收尾的 `git clean -fd evidence/` 收不掉 `evidence/live/`：束里的 `maos.db` 被 `.gitignore` 的 `*.db` 挡着，`git clean` 不删被忽略的文件，目录整棵留下 | 跑过 `--live-model` 的机器上，`evidence/live/` 会一直在（`git status` 看不见它，`--ignored` 才看得见），下一次缺省跑的 `INDEX.json` 会多登记一条 `aux_bundles: live` | 收尾命令改成 `git clean -fdx evidence/live`（本轨回执用的就是这条）。派单与 `docs/phases/common.md` 的检查单里那条 `git clean -fd evidence/` 要跟着补一句 —— 手册面本轨不碰，留给整合期 |
## task-t129

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `CaseOutcomeComputed` 事件只挂得上 `plan_id`，`trace_id` / `task_id` 落库为空 | 按 trace 串「这一单发生过什么」时，四判据那几条事件接不上 DAG 那一半。房间命令面的另两个事件（`CompensationAssigned` / `CompensationResolved`）T129 已补齐，只剩这一条 | 要动 `maos/domain/refund/outcome.py`（今天在契约 §A 禁动面上）。那个面解冻时，给 `record_case_outcome` 加一对 `trace_id` / `task_id` 入参并在 `append_event_log` 里带上即可。现状由 `test_room_outcome_commands.py::test_case_outcome_computed_still_only_carries_the_plan_id` 钉着，改完那条测试会当场提醒 |
| 2026-09-11 | p10 | `hiclaw/room_ingress.py::wire()` 里仍是三句手写的 ensure，没换成 T129 新抽的 `router.ensure_room_schema()` | 建表口径今天**一致**（`test_both_room_entrypoints_build_the_same_schema` 逐表比对钉着），但仍是两份写法，再加一张表时可能只改一处 | `hiclaw/**` 不在 T129 白名单上。下次动房间入口的轨把那三句换成一次 `ensure_room_schema(store)`，删掉重复的注释 |
| 2026-09-11 | p10 | `maos/tests/test_compensation_close.py:580` 的 docstring 仍提到已删的 `KIND_PENDING` | 只是历史叙述（「T117 时它们停在…」），读起来会以为那个常量还在 | 该文件不在 T129 白名单上。下次动它的轨顺手把那半句改成「停在一个待落库的过渡结局」 |
| 2026-09-11 | p10 | `docs/demo-script.md` 与 `docs/defense-brief.md` 里都写「结果面的**四条**命令」，T129 之后是五条（多了 `/compensate`） | 材料与代码对不上一个数。不影响运行，但台上被问「房间里能做哪些动作」会漏掉补开这条救命路径 | 材料面归 **9/20–9/21** 那一轮（与「房间真人审批 9/18 采集」同批）。改的时候连 `docs/ingress-setup.md` 一起 grep 一遍 |
| 2026-09-11 | p10 | `/compensate` 补开的工单，承接岗落在**打命令那个人**的岗上（走 `compensate._opening_role` 的缺省），演示里就是 `after_sales_supervisor` | 与自动开单行为一致，不是 bug；但补开的场景里「支付运维」几乎总是真正该接的人，多一步 `/assign` | 若真跑日觉得那一步多余，可给 `/compensate` 加一个可选的第二参数（岗位）透传成 `assignee_role`。今天刻意不加：派单要求的是「有救」，多一个参数就多一处要测的鉴权面 |
## task-t130（角色名收口之后的范围外发现，2026-09-11）

> 上面 `## 整合期 p10-c` 小节里的两条由本轨收掉了：「两份缺省审批岗各写各的」（现有
> `test_refund_roles.py::test_the_two_spellings_of_the_default_approver_seat_agree` 钉着）
> 与「`canonical_role()` 只 `strip` 不折大小写」（已折，矩阵测试钉着）。那两行按契约不归本轨改，记在这里。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的 `pytest_passed_nopg` 欠本轨 +10（3746 -> 3756，worktree 口径 3743 -> 3753） | `test_expected_metrics` 一条红。派单与契约都要求各轨不动这个文件，所以这是**有意欠着**的账 | 整合期合完本波六轨，按合并树的实跑末行一次刷到位 |
| 2026-09-11 | p10 | 跨轨契约 §E 写「`maos/kb/**` 是领域无关内核，不许 import 退款域」，实测 `maos/kb/promotion.py:54` 是**模块级** `from maos.domain.refund import guard, objects, outcome`，`experiment.py` 有 7 处函数内局部 import | 契约那句话今天只对 `plan_advice.py` 成立（本轨给它立了 AST 判据）。`import maos.kb.promotion` 会实打实拖进整个退款域 —— 换个业务域就得改这个文件 | 要么承认 `promotion.py` 是按域写的知识层、把契约那句话收窄成「`plan_advice.py` + 检索内核」，要么给它也做局部 import 化。前者是文档活，后者要动 `promotion.py`（不在任何轨的白名单）。`docs/domain-portability.md` 末尾本来就说「`flows/` 与 `kb/` 本就是按域写的，不在内核零改动的主张范围内」，与契约 §E 口径不一 —— 两处措辞一起对齐 |
| 2026-09-11 | p10 | `scenarios/refund/roles.json` 的角色名与 `verdict_role` **必须全小写**（`canonical_role()` 折大小写之后，目录里写大写名字会查不到自己），但这条约束没有机器校验 | 今天全小写，撞不上。将来有人往目录里加一个 `Night_Lead`，`role_of` / `accounts_of` 会静默查不到，症状是「这个岗查不到人」而不报错 | `_load()` 里加一条 `name == name.lower()` 的校验（连带 `verdict_role`），或在 `test_refund_roles.py` 加一条扫目录的断言。一行的活，下次动 `roles.py` 的轨顺手做 |
| 2026-09-11 | p10 | `verdict._approver` 对「政策没写审批人」（空串）的处置是**原样留空**，房间里那句话渲染成「请 ⟨两个空格⟩ 拍板」 | 十六格矩阵里的 `("", low)` / `("", high)` 两格。不是本轨引入的（改前改后逐格相同），但那行文案在真房间里会被人看见 | 按 `plan_advice` 那边的口径，空审批人应当是「没人知道该谁批」而不是「不需要审批」（`guardrails` 第三条红线就拦这个）。要么降级到 `roles.DEFAULT_APPROVER_SEAT`、要么让 headline 换一句话。改的是房间里念出来的字，会动 `PLAIN_STDOUT_MD5`，得单开一轨并当场重刷指纹 |
| 2026-09-11 | p10 | `maos/model/client.py` 打的「启用真模型：base_url=…」那行仍会进 `run.log`；本轮已把 `MAOS_LLM_BASE_URL` 加进 `make_evidence.py` 的恒脱敏名单，但脱敏是**产物侧**的兜底 | 兜底生效前，那行在终端与 CI 日志里仍是明文 | 更彻底的做法是让 client 那行只打 host 不打完整 URL。铁律 6 点名的是「输出前必须过脱敏」，现况已合规，这条是加固 |
| 2026-09-11 | p10 | `scripts/make_evidence.py --live-model` 产出的束落在与 Scripted 束**同一个** `evidence/scenario-*` 目录（不像 `make_case_bundle.py` 出到 `<path>-live/`），而 `verify.py` 不看 `model_mode` | 真模型束可以覆盖 Scripted 束并照样过 verify。本轮修掉了「标签说谎」那一半（旗标现在真能传到子进程），但「覆盖」这一口子还在 | 让 `--live-model` 出到独立根目录，或让 `verify.py` 直接拒 `model_mode=live` 的束。复赛前做 |
| 2026-09-11 | p10 | ~~`code_repo_patch.py` 的重问提示词不带上一版 diff 原文，模型看不到 `corrupt patch at line 6` 指的是哪一行~~ | 只影响自修复命中率，不影响正确性 | 下次调这个 skill 的提示词时一起带上。另：重问轮里模型抛异常或产非法 JSON 会直接 failed，把第一轮那份「合法但打不上、本可交给 Gate」的补丁丢掉了。**已处理（2026-09-11，task-t131）**：两条都做了。正文按 `sandbox_git_apply` 拼 payload 的**同一口径**重现并逐行编号，于是 git 那句「第 6 行」在提示词里查得到是哪一行；重问轮塌掉时上一轮那份补丁照旧交给 Gate |
| 2026-09-11 | p10 | ~~`MAOS_FORCE_SCRIPTED` 与 kb 三个旋钮共四个仍不进 `maos/config/source.py` 的 `GOVERNED_KEYS`~~ | 能治理（Nacos 上改得到、不用重启），但变更不落 `ConfigChanged` 审计。这个旋钮一改，整批证据束的成本读数含义就变了，比 kb 那三个更值得审计 | 「四行改动 + 一条断言」，`test_config_source.py` 那条按数目写的测试要一起改名。`## task-T35` 记的是同一笔账，本轮只更正了数目笔误没有补齐。**已处理（2026-09-11，task-t131）**：已补齐，实际改动就是估的那么多 |

## task-t131（配置治理补齐 + 重问提示词带错误原文，2026-09-11）

| 日期 | Phase | 现象 | 影响 | 怎么做 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **`MAOS_SANDBOX_REQUIRE_CONTAINER` 与 `MAOS_MAX_PLAN_REJECT` 仍不进 `GOVERNED_KEYS`**。这两个是本轨重核 `maos/config/__init__.py` 那张自称完整的表格时发现的：它们走配置面（`tools/sandbox.py::require_container`、`runtime/plan_approval.py::_max_reject`），表里却从来没有过 | 与四个旋钮补齐之前一样：**能治理，变更不落审计**。前者还是**安全旋钮**（要不要强制容器隔离），按「比 kb 那三个更值得审计」的同一把尺，它比 `MAOS_FORCE_SCRIPTED` 更靠前 | 「两行改动 + 一条断言」，与本轨补那四个完全同形。`test_the_two_knobs_t131_did_not_take_are_still_unaudited` 是这笔账的提醒装置 —— 补进清单的那一刻它会红，红了就来把这行划掉。没一并做是守派单范围（那一轨点名的是四个） |
| 2026-09-11 | p10 | `GOVERNED_KEYS` 变长之后，**五处白名单外的注释与文档口径过期**：`maos/model/client.py:42`（「**不进** `GOVERNED_KEYS`」）、`maos/kb/plan_advice.py:69`（同）、`deploy/nacos.md:142`（「只有那四个 key 会被 diff 出变更」）、`deploy/nacos-live.md:236`（kb 两个「没进 `GOVERNED_KEYS`」）、`docs/agent-teams-gap-analysis.md:110`（「治理键只有 4 个全局旋钮 `source.py:96-101`」，行号也已漂） | 只是文字，无行为影响。但前两处正是下一个人加旋钮时会读的那段字，读到一个已经反了的结论会绕远路；`deploy/nacos*.md` 两处是**真连 Nacos 时照着做的操作手册**，说「只有四个会落审计」会让人以为改 kb 旋钮不留痕 | 五处都在本轨白名单外（契约 §A），一个字没动。归各自持有轨或整合期：前两处删掉那句「不进」即可，`deploy/nacos*.md` 改成八个，`gap-analysis` 那条连行号一起刷（现在是 `source.py:108-124`） |
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的 `collected` 仍是 **3822**，而本轨新增 11 条测试后实跑是 **3833** | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红。这是**预期内**的：派单 §5.1 明写「红的只许是 `test_expected_metrics`」，契约 §A 把 `docs/expected-metrics.json` 列进「谁都不许动」 | 整合期刷一次真源即可（那个文件自己的报错原文写着：改一个数，不是改四处；真跑的记 `pytest_passed_nopg`、门控 skip 的记 `pytest_skipped_nopg`）。本轨在 worktree 里的实测是 `3753 passed, 79 skipped, 1 failed`，主仓因 `test_rtv_sop_doc.py` 那 3 条会是 `3756 / 76` |

## 整合期 p10-d（Wave D 六轨合并，2026-09-11）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `maos/skills/builtin/refund/compensate.py::_add_column_if_missing` 用 `PRAGMA table_info` 探列且**没有回落**，而同一份助手在 `maos/domain/refund/case_pack.py:126` 那一份有 `try/except` 退回 SELECT 探针。PG 上 `PRAGMA` 直接 `SyntaxError: syntax error at or near "PRAGMA"` | `--domain-backend postgres` 时**补偿路径跑不通**：`gateway_fail` 束在「域内补偿失败，不许静默收口」处退非零，于是 PG 上只有 `happy` 一条路径产得出来（本轮 PG 束因此只有 happy，与 T126 派单 §3.2 的要求一致，但 `--all-paths` 在 PG 上是红的） | 跨轨契约 §B.2 明写「三轨各自复制一份，**整合期由主会话去重**」——这三份（`case_pack.py` / `compensate.py` / T120 那份）该收成一处，收的时候统一带上 `case_pack.py` 那份的回落。去重那一轨顺手把 `--all-paths --domain-backend postgres` 跑绿当验收 |
| 2026-09-11 | p10 | `evidence/case-real-01-pg/` 没有顶层 `INDEX.json`（`make_case_bundle.py` 只在 `--all-paths` 跑完才写它），脚本自己会 `[WARN]` 点名 | `verify.py` 第 9 项少一个 provenance 锚（不影响 10/10，锚数从 11 变不到 12）；`report.html` 里这一组的抬头印「顶层 INDEX.json 缺失」而不是案号 | 与上一条同轨：`--all-paths` 在 PG 上能跑通那天，顶层 INDEX.json 自然就有了。在那之前不要为它单开一条「只写 INDEX 不跑路径」的旁路——那份汇总的内容正是四条路径的产出 |
| 2026-09-11 | p10 | T129 的 `test_two_room_entries_build_the_same_schema` 断言的是**两个入口的表集合相等**，而注释写的是「两处都该走 `router.ensure_room_schema()`」。实际只有 `scripts/run_ingress.py` 改成了走它，`hiclaw/room_ingress.py::wire()` 仍是自己写的四句（`init_schema` + 三句 ensure） | 断言比注释弱一档：哪天有人往 `ensure_room_schema()` 里加第四句 ensure，两处会再次分叉，而表集合相等这条断言**抓不到**（新表两边都建了才叫分叉，只有一边建才红——正是这条断言要防的，所以它今天有效；失效的场景是有人给 `wire()` 也补上同一张表却不走共用函数） | `hiclaw/**` 在 T129 白名单外，所以这一轮没并。下一次动 `hiclaw/room_ingress.py` 的轨把那四句换成 `ensure_room_schema(store)`，断言同时收紧成「`wire()` 的源码里出现 `ensure_room_schema`」 |
| 2026-09-11 | p10 | `maos/config/source.py::GOVERNED_KEYS` 补到八个之后，仍有两个走配置面的旋钮不在清单里：`MAOS_SANDBOX_REQUIRE_CONTAINER`（安全旋钮，要不要强制容器隔离）与 `MAOS_MAX_PLAN_REJECT` | 这两个在 Nacos 上改了**能改到、但变更不落审计**（现况与 T131 补齐前的那四个一样）。前者是安全面，比 kb 那三个更值得审计 | T131 的 `## task-t131` 已记同一笔账，这里并记一次是为了整合期的人一眼看到「本轮补了四个、还差两个」。补齐仍是「两行改动 + 一条断言」 |

## task-t132（知识层上 PG 之后的范围外发现，2026-09-11）

| 日期 | Phase | 现象 | 影响 | 建议 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **`kb.fts_text()` 的切词与 PG 索引侧 `to_tsvector` 的切词对英数复合词不一致**。查询侧 `_TOKEN_RE` 把 `ACQ.TRADE_NOT_EXIST` 切成 `acq trade not exist` 四个 token；PG 侧 `to_tsvector('simple', body)` 把同一串当成**一个** token `acq.trade_not_exist`（默认 parser 的 file/host 规则）。实测：`fts_search(body,'acq')` 0 命中，而普通英文词 `lesson` 5 命中 | 端口全文通道上，**按错误码检索恒不命中且不报错** —— 正是 `kb.fts_text` docstring 里警告的那类「两边各写一份 = 召回恒为空」，只是这次漂的不是两份代码而是两个后端的分词规则。本地 FTS5 那条不受影响（写入侧也过同一个 `fts_text`，两边一致）。今天没造成可见损失：本仓库语料以中文为主，而中文那条在本机 PG 上本来就退化 | 归下一轨动 `maos/store/pg_store.py` 或 `maos/kb/retriever.py` 的人。两条路：① 端口侧改用 `to_tsquery` 并把 `kb.fts_text()` 切好的 token 用 `&` 连；② 写入侧给 PG 建索引时也过一遍同样的切词（代价是索引表达式与 body 不再同形）。选①更收口。**别只改一边** |
| 2026-09-11 | p10 | 本机 `pgvector/pgvector:pg16` 没装中文分词器，`MAOS_PG_FTS_CONFIG` 走 `simple`；于是**装配级 PG 上跑 DAG 时，fts 通道每次都退化**为本地实现，而本地那条在 PG 上也走不通（`bm25()` / `MATCH` 是 FTS5 专有）—— 结果是 fts 这一路记 0 分，四通道实际只有向量与精确两路在算 | 召回照常（本次实跑 `hit_count=5 / candidate_count=10`），但混合召回的权重分布与 SQLite 上**不一样**，而两边都不报错。R4 的四通道对照实验若在 PG 上重跑，结论会与 SQLite 版对不上 | 装 `zhparser` 或 `pg_jieba` 再把 `MAOS_PG_FTS_CONFIG` 指过去，本层立刻把中文交给 PG（`pg_schema.sql` 末尾已写好那五句 DDL）。要改 Docker 镜像，归有权动 `deploy/**` 的轨。在那之前**不许在材料里说「缺省支持中文分词检索」**（跨轨契约 §J 已有同款约束） |
| 2026-09-11 | p10 | `kb_doc.embedding` 在 PG 上是 TEXT，`vector_search` 抛 `LookupError: operator does not exist: text <=> vector`，检索器退化成纯 Python 余弦 —— **PG 上没有 HNSW** | 召回照常，但「向量走 pgvector 的 `<=>`」这句话在装配级 PG 上今天不成立。`deploy/polardb-live.md` 里 `ef_search` 那组实测读数量的是**另一张表**（当时的 `kb_doc_pg`），别拿来当 `kb_doc` 的读数 | `## task-t115` 已记同一笔账，本轨实测复核确认并加了断言（`test_vector_channel_degrades_on_text_column`）。真要在 PG 上用 HNSW：给 PG 侧单开一列 `vector(64)` + 写入侧双写 + 建 HNSW 索引，是「两个后端形状分叉」的决定，值得一轨 |
| 2026-09-11 | p10 | 本轨实跑中**观察到一次**：`MAOS_FLOW_KB_BACKEND=postgres scripts/run_case.py` 退 0、日志里 PG DDL 翻译跑了 19 次，但跑完 PG 上 `kb_doc` 不存在。随后用同一条命令、同一份 case **三次重跑均为 40 行**，无法复现 | 若真存在，症状是「开关设了、日志像是走了 PG、数据却没落库」—— 无症状失效里最难查的一类。但仅此一次，且三次重跑都正常，也可能是当时 shell 里的 drop 与跑序被我搞乱了 | **不为它写代码**（铁律 4，且没有可复现的现象）。记在这里只为：下一个动这条路的人如果再遇到，知道这不是第一次。真要复现就在 `attach_backend` 里临时打一行「连上了哪个 db/schema」的 INFO |
| 2026-09-11 | p10 | 本轨还掉了 `## task-t126` 的两条账：①「知识层仍在 SQLite，§5 标题比实况说得多半句」——已做成真的并改了标题；②「`:183` 只写进程级开关，照着操作会撞受理幂等闸」——那张表已重排成「进程级 / 装配级」两列并加了红字段落 | —— | 无需再处理。t126 那一节的原文**不改**（历史记录照留），以本节为准 |
## task-t133（两份加列助手去重 + PG 上四条路径，2026-09-11）

| 日期 | Phase | 现象 | 影响 | 怎么做 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **`maos/domain/_dbport.py` 的 DDL 翻译器不认 `ALTER TABLE <表> DROP COLUMN <列>`**，发过去当场 `UnsupportedDdlError`（它认的是 CREATE TABLE / CREATE INDEX / ALTER TABLE ADD COLUMN / DROP TABLE / CREATE EXTENSION） | 不挡任何生产路径 —— 本域从来不删列。挡的是**测试**：想在真 PG 上先把 T117 那六列 DROP 掉再验「加得回来」就做不到，于是那条回归守卫只能退成「跑得过 + 六列齐」，另起一条用自建表守「列确实不在时真加得上」（`test_schema_util_t133.py` 那两条） | 派单 §5.4 明写「只修挡住四条路径的那些，别把 `_dbport` 改成一个通用 SQL 翻译器」——`DROP COLUMN` 不挡四条路径，所以本轨一个字没动。哪天真有迁移要删列，连翻译带测试一起补；在那之前不值得为一条测试扩翻译器 |
| 2026-09-11 | p10 | `scripts/pg_case_snapshot.py::VOLATILE_KEYS` 含片段 `"basis"`，于是 `case_outcome.arrival_basis` 被归进「每跑一次都现生成、字节相同反而是异常」那一栏。但在**钱没到账**的路径上它恒为空字符串 —— `drift` 与 `reject` 两条因此各报一条 `suspicious_identical`（字段就是 `outcome.case_outcome.arrival_basis`，两束都是 `''`） | 纯误报，`verdict` 仍是 `isomorphic`、`不符 0`。但「可疑相同 1」这行字会让读证据的人以为有东西没对上，而它恰恰出现在**最该干净**的那两条对照路径上 | 收窄那条分类：`arrival_basis` 只在真到账时才现生成，空值不该进 `differ_by_design`。`scripts/pg_case_snapshot.py` 在本轨白名单外（派单 §4），一个字没动。改法建议是给 `VOLATILE_KEYS` 那栏加一句「两边都空则不计可疑」，而不是把 `basis` 从清单里摘掉（真到账时它确实该每跑一次都不同） |
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的 `collected` 与 `pytest_passed_nopg` 落后：本轨新增 9 条测试（4 条 PG 门控），worktree 实跑 `3820 passed, 86 skipped, 1 failed`（无库；基线 `3816 passed, 82 skipped`，收集数 3898 -> 3907） | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红。**预期内**，见本轨 DECISIONS 末条 | 整合期按合并树的实跑末行一次刷到位。**本轨新增的 4 条 PG 门控 `-k pg` 一条都选不中**（名字里是 `postgres`，不含 `pg` 这个子串），所以 `-k pg` 实跑仍是 `93 passed`、与基线一模一样——这正是契约 §0 那条警告的第二个例证。门控条数看全量两档差值：无库 86 skipped、有库 15 skipped，**71**（基线 67，+4） |

### 上一轮记在整合期 p10-d 名下、本轨已办结的两条

| 日期 | Phase | 原记录 | 结论 |
|---|---|---|---|
| 2026-09-11 | p10 | 「`compensate.py::_add_column_if_missing` 用 `PRAGMA` 且没有回落……去重那一轨顺手把 `--all-paths --domain-backend postgres` 跑绿当验收」 | **已办**。两份私有助手都删了，两处走 `_schema_util.apply_columns()` → `_dbport.add_column_if_missing()`。`--all-paths --domain-backend postgres` exit 0，四条路径各一个 `[OK]`，业务状态与 SQLite 束逐字一致 |
| 2026-09-11 | p10 | 「`evidence/case-real-01-pg/` 没有顶层 `INDEX.json`……`--all-paths` 在 PG 上能跑通那天，顶层 INDEX.json 自然就有了」 | **已办**，且原话应验：没有开任何旁路，四条路径跑通之后 `[OK] evidence/case-real-01-pg/INDEX.json 汇总 4 束` 自己就出来了。`verify.py` 的 provenance 锚从 11 涨到 **16**（四条 PG 路径各一个 + 顶层一个） |
## task-t134

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `evidence/case-real-01/*/model-usage.json` 的 `note` 仍写着「`maos/roundtable/speaker.py` 目前**不落 model_usage**（跨轨契约 §C 把这件事划给 T113，基线 137c960 上它还没并入）」，而同一份文件的 `rows` 里那五条圆桌用量**逐字都在** | 一份文件自己打自己的脸。读的人若信了 note，会以为圆桌那 5067+396 tokens 是别处来的；`--live-model` 那束的 note 尤其误导 —— 它正是唯一一束**有**真圆桌用量的 | 出处是 `scripts/make_case_bundle.py` 写 `model-usage.json` 那一段（T133 的白名单面，本轨不碰）。下一次动那个脚本的轨顺手把这句改成现况；顺带可以在 note 里点一句「圆桌那几行按 `plan_id LIKE 'roundtable:%'` 归进 `trace.json` 的 `roundtable_traces[].cost`」 |
| 2026-09-11 | p10 | 本轨解决了 BACKLOG 里三条旧账，但**旧账那三行还挂着**（各轨只许在末尾追加自己的小节，不许改别人写的行）：`## task-t114` 那条「圆桌的 `model_usage` 行 `trace_id` 恒空，`attributed_model_calls` 只数 DAG 的」、`## task-t128`/p10 那条「`happy-live` 5 次真调用全落 `unattributed_usage`」、以及「单案例每束恒 8 条 `stray_events`，没人查过是什么」 | 整合期的人按旧账去查，会发现现象已经不在了，白花一轮 | 整合期把那三行标成已由 t134 解决（或直接划掉）。第三条的答案顺手写进去：那 8 条是 `RoundtableRound` 1 + `RoundtableSeatSpoke` 5 + `SkillInvoked` 2，恒定 8 条是因为它是**结构性**的 —— 每一单圆桌一轮五岗、中间两次 skill，与走哪条业务路径无关（四条路径的圆桌段逐条相同） |
| 2026-09-11 | p10 | `evidence/case-real-01/happy-live/` 那束的 `trace.json` 出处停在 `cc86ed0`，形状是本轨改动**之前**的（8 条 stray / 5 条 unattributed / `attributed_tokens_total: 1042`）。本轨没有重产它 —— 重产要真跑一次模型（派单 §6.7 明令不许烧钱），而它的 `maos.db` 由 `.gitignore` 排掉、worktree 里根本没有 | `report.html` 里**这一束**看不到圆桌时间线，「挂不上树的东西」那一栏照旧摊开 8 条 —— 而它恰好是唯一一束有真 token 数的。改后的数已由「把主仓那份 `maos.db` 拷到 tmp 直接重导」验过（`attributed_tokens_total` 1042 → 6505、圆桌 5 次 measured），见本轨回执 | 整合期照跨轨契约 §B 的顺序重产证据时会跑 `make_case_bundle.py --path happy --live-model`，那一步过后这束自然就是新形状。**不要**为了让页面好看去手写它的 `trace.json`（铁律 3） |
| 2026-09-11 | p10 | `maos/obs/trace.py::stray_events` 的 docstring 仍拿 `scenario_5` 的 `issue.aggregate`（`plan_id` 空串）当现存例子，而那条事件早在 P5 的 D 轨就并进了已有的树 —— 今天八个场景束的 `stray_event_count` 全是 0 | 照 docstring 去库里找那条例子的人会找不到，进而怀疑判据失效。判据本身没问题（两条新测试各在手搭的库里证过） | 本轨没改那段 docstring：它说的是判据的**来由**（为什么要有这一族清单），不是「今天还有这个现象」。下一次动 `trace.py` 的轨可以加半句「该例已于 P5 D 轨并进树，此处留作判据来由」 |
| 2026-09-11 | p10 | 第 4 项 `trace-tree` 的 warn 归零之后，`maos/tests/test_verify_warn.py` 的 `WARN_BASELINE` **没有跟着变** —— 它的 fixture 只跑 `make_evidence.py`（八个场景束），而那四行 warn 来自单案例束，从来不在它的分母里 | 那条基线守卫对单案例束的 warn **是盲的**：单案例束多出/少掉一行 warn，它一次都不会红。本轨让四行 warn 归零，它照旧绿 —— 绿得没有信息 | 下一次动核验器或 `test_verify_warn.py` 的轨：让那个 fixture 也产一次 `make_case_bundle.py --all-paths`（约 20 s），把单案例束的 warn 纳进基线。别为此改 `WARN_BASELINE` 的现有值 —— 现在的 1 行是八束那一族的真实数 |
## task-t135（房间那一次 /approve 不替人签所有的闸，2026-09-11）

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | **付款被驳回之后，客户一条通知都收不到。** `notify.customer` 在 DAG 上依赖付款那一步，付款落 FAILED 之后它停在 PENDING 不再跑。于是 `/confirm` 回一句「一条通知都没发出去，客户无从确认」，那条命令在补偿链上无路可走 | 钱没退出去、补偿工单开了、`/assign` `/resolve` 都走完了，而客户**始终没被告知过任何事**。这不是 T135 引入的回归：改造前那条通知是在一个**本不该完成**的任务（被代签的付款闸）跑完之后才发出去的，T135 只是把这件事暴露出来 | 补偿收口那一侧该给客户发一条通知（`refund.compensation_close` 面，或 `/resolve` 之后补一次 `notify.customer`）。本轨白名单外（`maos/skills/**`），铁律 4 不当场改。已用 `test_a_rejected_payment_leaves_the_customer_un_notified` 钉住 —— 补上那天它会红，红了就来把这行划掉 |
| 2026-09-11 | p10 | **核算那道闸（`effect_risk=H`）的操作者仍是 CLI 写死的「沈思锴（supervisor）」**，不是房间里按 `/approve` 的那个人。付款闸这一跳 T135 已经改成真实操作者（`@boss:maos.local`），两跳因此署了两个不同来源的名字 | HITL trace 上一半真一半假。核算那一跳的语义确实是「由处置流程代跑」（卡片上也这么写），所以不算说谎；但事后问「谁批的这一单」，库里给的是一个 CLI 常量 | `custom_case.APPROVER` 那个常量是 CLI 代跑用的，要透传得给 `run_payload` 的闸循环也加一个 operator 入参（`approval_operator` 现在只管计划级停靠）。本轨没做：派单要的是「签不到的闸不代签」，代签的那一道署名是另一件事，且改它会动 `reject_roles` 之外的那条路 |
| 2026-09-11 | p10 | **房间那次驳回不落 `approval_record`**：`handle_gate_decision` 走的是 `cp.human_decision()`，留痕落在 `event_log` 的迁移事件上；而 `custom_case` 里 `reject_roles` 那条路会另落一条 `C.record_approval(decision="rejected")` 并 `stamp_approval_revision` 挂成 business_ref。两条路的留痕位置因此不一样 | HITL 证据束**不受影响**（`make_case_bundle.collect_hitl` 读的是 `event_log`，这一跳带操作者完整在册）。缺的是退款域那张审批表上的一行 —— 而 `custom_case` 那条路的注释明写它的用处：「『谁在什么时候驳回了这笔』在库里查不到，而客户投诉时要对的第一件事就是它」。房间里那次驳回是**真人**做的，更该落 | 在 `handle_gate_decision` 里照 `custom_case` 那两句补上即可（`_common.record_approval` + `case_pack.stamp_approval_revision`，都是既有函数，不改它们）。本轨没做是铁律 4：派单要的是「签不到的闸不代签」，补审批表是另一件事，且要先定清楚「驳回付款闸」与「驳回整个案子」在那张表上怎么区分 —— `_reject_case` 那条 submitted/approved 守卫正是为这个分界设的 |
| 2026-09-11 | p10 | `docs/expected-metrics.json` 的 `collected` 仍是 3898，本轨新增 20 条测试后实跑 3918 | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红。**预期内**（派单 §6.1、契约 §0 都点名） | 整合期统一刷。契约 §A 把那个文件列进「谁都不许动」。本轨在 worktree 里实测 `3835 passed, 82 skipped, 1 failed`，主仓会是 `3838 / 79` |
## task-t136（收尾四小件，2026-09-11）

> `## task-t131` 的两笔账**本轨已处理**，按契约 §A 不回头改中间那两行（`merge=union` 会留下同一行的两个版本），
> 在这里划掉：`docs/BACKLOG.md` 的 `## task-t131` 第 1 行与 `## 整合期 p10-d` 第 4 行（两条记的是同一件事）
> —— `MAOS_SANDBOX_REQUIRE_CONTAINER` 与 `MAOS_MAX_PLAN_REJECT` 已进 `GOVERNED_KEYS`，
> 实际代价与 T131 估的一致（两行改动 + 一条断言）。那条提醒装置测试已翻成正向判据。
>
> `## 整合期 p10-d` 第 3 行（两个房间入口的建表口径）也由本轨收掉：`hiclaw/room_ingress.py::wire()`
> 现在走 `ensure_room_schema(store)`，断言收紧成「两个入口的源码里都必须出现那个调用」。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-11 | p10 | `maos/kb/plan_advice.py` 的两个兜底角色名字面量（`DEFAULT_APPROVER_ROLE = "supervisor"` / `_FALLBACK_TICKET_ROLE = "payment_ops"`）**去不掉**。本轨把审批岗那一处接上了「先问 `roles.DEFAULT_APPROVER_SEAT`、问不到才兜底」，但目录读不出来时总得给一个岗 | 领域无关内核里仍有两个退款域的角色名。今天它们只在「目录读不出来」这条路上生效，取值由域说了算 —— 所以这是**层次上的不干净**，不是行为上的 bug | 真正清零要把它们变成 `advise()` / `_ticket_role()` 的**必传参数**，那要改签名并改所有调用方（`experiment.py` / `manager.py` / `channel_agent.py`）。等**第二个业务域**真的用 `advise()` 那天做 —— 那时参数才有第二个实参，签名的代价才换得到东西。在那之前，`test_no_third_refund_role_literal_creeps_into_this_module` 钉住「不许长出第三个」 |
| 2026-09-11 | p10 | `GOVERNED_KEYS` 补齐到十个之后，「清单 = 走配置面的旋钮全集」这件事**没有机器判据**：`maos/config/__init__.py` 那张读取点表格仍靠人跑一遍 `grep -rn "get_config_source()"` 去核（这已经是第三次靠人核了：T131 一次、本轨一次） | 下一个人接一个新旋钮却不进清单时，现况会**静默**退回「能治理，变更不落审计」，且那张自称完整的表格会再次过期 —— 前两轮各花了一整轨去发现这件事 | 可做的形状：一条 AST 判据，扫全仓 `get_config_source().get(<第一个实参>)`，把解析得出的 key 与 `GOVERNED_KEYS` 比对，不在清单里的**逐个列出来**（不是断言为空 —— 契约上「接读取点」与「进清单」仍是两件事，可以有意不进）。它把「有没有人想过」从注释升成一次必答题。代价约三十行，归下次动 `test_config_source.py` 的轨 |
| 2026-09-11 | p10 | `GOVERNED_KEYS` 的数目从八变十，让 `## task-t131` 记的那五处过期口径**又老了一档**：`deploy/nacos.md:142`（「只有那四个 key 会被 diff 出变更」）、`deploy/nacos-live.md:236`、`docs/agent-teams-gap-analysis.md:110`（「治理键只有 4 个全局旋钮 `source.py:96-101`」，行号也已漂到 `:108-140` 附近）、`maos/model/client.py:42`（「**不进** `GOVERNED_KEYS`」） | 只是文字，无行为影响。但 `deploy/nacos*.md` 两处是**真连 Nacos 时照着做的操作手册**，说「只有四个会落审计」会让人以为改别的旋钮不留痕 | 四处都在本轨白名单外，一个字没动（第五处 `maos/kb/plan_advice.py` 在白名单里，本轨修了）。归各自持有轨或整合期，改法是把数目改成十并删掉那句「不进」 |
| 2026-09-11 | p10 | 空审批人那条病在**申请表批量路径**上没有判据：`scripts/room_team_smoke.py` 的演示语料每一单都写了 `approver_role`，所以第 3 件改前改后 `PLAIN_STDOUT_MD5` 逐字节相同 | 这不是问题本身，是**判据的盲区**：房间里那句「请 X 拍板」今天只被一个不会撞到空审批人的语料覆盖着。换一条没写审批人的政策规则（真房间的真政策就可能这样）当场就撞上，而没有任何束级判据会红 | 两条路：① 给演示语料加一单「政策没写审批人」的案子（会动 `PLAIN_STDOUT_MD5`，且 `scenarios/custom/refund-requests-team.csv` 一直是禁动面）；② 在 `test_room_team_recheck.py` 里加一条只跑单单元的断言。本轨走的是第三条 —— 在 `test_roundtable_verdict.py` 里钉两条针对性测试，够用但不覆盖批量路径。真跑日前若要演「政策缺字段」这一幕再说 |

## 整合期 p10-e（Wave E 五轨合并，2026-09-12）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **跨轨契约的 §A 分区只写生产代码，不写测试文件**。本波 T135 与 T136 各自改了 `maos/tests/test_room_outcome_commands.py` 的不同段落 —— 两轨的白名单都只说「测试」，没说哪些测试文件归谁 | 这次侥幸自动合了（两段相隔很远），但同一个文件被两轨改的一般情形是冲突，而整合期手工合测试文件比合生产代码更容易出错（断言之间没有语法依赖，串了也不报错） | 下一波派单时把**测试文件也写进 §A 的分区表**，尤其是几个被反复改的大文件（`test_room_outcome_commands.py` / `test_roundtable_verdict.py` / `test_trace_evidence.py`）。写不清就明确「这个文件本波谁都不许改，要加断言就新建文件」 |
| 2026-09-12 | p10 | `pytest -k pg` 与真正的 PG 门控条数差了 **12 条**（`-k pg` 数 93，门控实为 78+15=93… 实际门控 78 条里有 12 条名字不含 `pg`）。T132 的 `test_kb_flow_backend.py`（7 条）与 T133 的 `test_schema_util_t133.py`（4 条）整文件都选不中，加上 T126 那 1 条 | 拿 `-k pg` 当门控计数的人会漏掉 12 条，而 `docs/expected-metrics.json` 的 `pg_gated_tests` 又是「有库档 = 无库档 + N」那条算式的地基。差距还在扩大 | 两条路任选：① 给 PG 门控测试统一加一个 pytest marker（`@pytest.mark.pg`），计数改用 `-m pg`；② 在 `test_expected_metrics.py` 里加一条「按 skip 原因数门控条数」的自动判据，不再靠人记。前者更彻底但要动十几个文件的装饰器，后者是一条断言。**复赛前不必做**，记着别再用 `-k pg` 数就行 |
| 2026-09-12 | p10 | 铁律 9（`maos/kb/**` 不许 import 退款域）在 `maos/kb/plan_advice.py` 上**实际是有例外的**：`_ticket_role()`（T119）与 `_approver_role()`（T136）都用「局部 import + 兜底」问退款域要缺省岗位名 | 不是 bug（拿不到就回落到本模块字面量，内核仍能独立跑），但铁律的字面与代码的实况不一致，下一个读铁律的人会以为这两处是违规 | 把这条边界写进 `CLAUDE.md` 铁律 9 的括号里，或写进 `maos/kb/__init__.py` 的模块 docstring：**取值可以局部 import + 兜底，断言不行**。整合期 p10-e 的 DECISIONS 有完整原文。材料面归 9/20–9/21 那一轮，一起改 |

## task-t137（驳回之后的三件，2026-09-12）

> `## task-t135` 那三条账**本轨全部处理完**，按契约 §A.3 不回头改中间那几行，在这里划掉：
>
> 1. 「付款被驳回之后，客户一条通知都收不到」—— **已补**。房间那条路补在 `/resolve`
>    成功之后（`router._tell_customer_after_resolve`），案子被整体驳回那一档补在
>    `custom_case` 闸循环的驳回分支。那条反向测试
>    `test_a_rejected_payment_leaves_the_customer_un_notified` 已改判成正向
>    `test_a_rejected_payment_still_tells_the_customer_what_happened`。
> 2. 「核算那道闸的操作者仍是 CLI 写死的常量」—— **已透传**。`run_payload(gate_operator=…)`，
>    房间那条路传 `msg.sender`；不给仍是 `APPROVER`，CLI 缺省逐字节未变。
> 3. 「房间那次驳回不落 `approval_record`」—— **已补**（`router._record_gate_approval`），
>    放行那一支一并落。两种驳回的区分方式见 `docs/DECISIONS.md ## task-t137`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **证据束那条补偿路径（`gateway_fail`）仍然不通知客户**。房间走 `/resolve` -> router 补一次，而 `scripts/make_case_bundle.py::_compensate()` 是直接经 `SkillInvoker` 调 `refund.compensate` + `refund.compensation_close`，不经过 router，于是那一束 `notification` 仍是 0 行 | 演示与证据两条路在「客户被告知了没有」这件事上不一致：真跑日演的房间那条有通知（真跑日会当场演），而评委翻 `evidence/case-real-01/gateway_fail/` 时查不到。不是错的（那一束如实记录了它自己那一跑发生过什么），但两条路该收敛 | `scripts/make_case_bundle.py` 是 **T140 独占**，本轨一个字没动（铁律 4）。改法是在 `_compensate()` 的第三步之后调一次 `custom_case.notify_customer(store, tenant_id, case_id, plan_id=…, trace_id=…)` —— 那个函数已经是公开的，正为第二个调用方准备。归 T140 或整合期，代价约两行 |
| 2026-09-12 | p10 | **`run.py` 场景 7 的补偿收口同样不通知客户**（`maos/flows/scenario_7.py` 走 `refund.compensation_close` 关单，结论 `settled`） | 同上一条，第三条路。场景 7 是八个场景束里演「流程卡住后应由谁补偿」的那一个，客户那一侧在它里面仍是空白 | `maos/flows/scenario_7.py` 在本轨白名单外，一个字没动。改法与上一条逐字相同（调一次 `custom_case.notify_customer`）。它会让场景 7 的输出多一行、`notification` 多一条 —— 也就是会动八个场景束的证据字节，所以**不该在本波做**，归复赛之后 |
| 2026-09-12 | p10 | `docs/expected-metrics.json` 的 `collected` 仍是基线值，本轨新增 18 条测试后 worktree 实跑 `3889 passed, 93 skipped` | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红。**预期内**（契约 §0 点名） | 整合期按合并树的实跑末行一次刷到位 |
| 2026-09-12 | p10 | `notify.customer` 的正文只落 `content_digest`，**表上不存正文**。于是「客户到底被告知了哪一句」只能靠拿库里的事实**重算一遍**再比对摘要（本轨的两处测试都是这么钉的） | 今天够用（重算走的就是 `_default_content` 那一条路，措辞漂了摘要就对不上）。但证据束里查不到那句话本身 —— 评委问「给客户发的是什么」，要么跑一次重算，要么读代码 | 加一列存正文会动 `schema.sql`（冻结面）。可做的形状：`compensation_close` / `notify.customer` 落事件时把正文进 `detail`（事件表不冻结），那样束里 `grep` 一下就能看到。代价约三行，归下次动那两个 skill 的轨 |
## task-t138（三条判据补盲，2026-09-12）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | `maos/config/__init__.py` 抬头那张读取点表格的**行数**判据认的是「以 `| \`MAOS_` 或 `| \`maos` 开头的行」，形状上依赖那张表的 Markdown 排版 | 有人把表格改成别的排版（比如首列换成读取点）时，行数那半条会红在一件与旋钮无关的事上。key 覆盖那半条不受影响 | 真要改排版时把判据的取行规则一起改。**复赛前不必动** —— 今天两者一致，且表格三轮没换过排版 |
| 2026-09-12 | p10 | `WARN_BASELINE_CASE` 里 `history-case` 那行 warn 的正文带着一个**会涨的条数**（实测 64 条，再并一束变 80 条），而判据只钉行数 | 不是 bug（钉行数正是为了不让它随语料漂），但读 warn 正文的人会以为那个数字是被钉住的 | 无需处理，记一笔免得下一个人去「修」它。真要钉数字得先有一个稳定的分母，而历史知识是外部导入的，分母本就会涨 |
| 2026-09-12 | p10 | 派单 §3.3 与 `docs/BACKLOG.md ## task-t131` 都把 `maos/model/client.py:42` 当成「有意不进 `GOVERNED_KEYS`」的现存例子，实际 `MAOS_FORCE_SCRIPTED` 早在 T131 就进清单了 | 只是过期文字（那笔账 `## task-t131` 已记，归 T140 改），但它让派单给出的 `INTENTIONALLY_UNGOVERNED` 应当非空的预期落了空 —— 本轨据实跑把白名单留空 | 与 `## task-t131` 记的另外四处过期口径（`deploy/nacos.md:142`、`deploy/nacos-live.md:236`、`docs/agent-teams-gap-analysis.md:110`）一起，**归 T140**。本轨白名单外一个字没动 |
| 2026-09-12 | p10 | `docs/BACKLOG.md` 里「单案例束恒 8 条 `stray_events`」的账**已过期**（T134 把圆桌挂上时间线之后归零） | 照着它写新文字的人会钉一个不存在的数。本轨实测今天八束 + 单案例束四条路径**共 12 束，`stray_event_count` 与 `unattributed_usage_count` 全是 0** | 本轨已按实测在 `maos/obs/trace.py::stray_events` 的 docstring 里补了半句（「该例已于 P5 D 轨并进树，此处留作判据来由，不是现存例子」）。BACKLOG 那些旧行按契约 §A.3 不回头改，在这里说明 |
## task-t139（PG 上的检索真成立，2026-09-12）

先结掉三条旧账（都在 `## task-t132`，原文不改，以本节为准）：

- **第 1 条「按错误码检索恒不命中」→ 已解决**。全文改查影子表 `kb_doc_fts` +
  `to_tsquery`，`fts_search(body,'acq')` 从 0 命中变成命中。
- **第 3 条「PG 上没有 HNSW」→ 已解决**。PG 侧加 `embedding_vec vector(64)` 生成列 +
  `idx_kb_doc_embedding_hnsw`，执行计划实测 `Index Scan`。`## task-t115` 里同一笔账
  （「要真用 HNSW 得单开一列并双写，是形状分叉的决定，值得一轨」）一并结掉。
- **第 2 条「中文通道在 PG 上退化」→ 没结，但有判据了**。见下面第 2 行。

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **`## task-t132` 第 1 条对病根的描述不够准确**。原文写「`to_tsvector('simple', body)` 把 `ACQ.TRADE_NOT_EXIST` 当成**一个** token `acq.trade_not_exist`」。本轨实测：默认 parser 在第一个下划线处断开，实际切成 `'acq.trade'` / `'not'` / `'exist'` **三个** token | 不影响结论（带点的前缀被黏住，查 `acq` 照样 0 命中），但照原文去复现的人会以为自己搞错了 | 已在 `test_error_code_is_searchable_after_the_shadow_table_switch` 的 docstring 里写清实测形状，并把断言改成钉 `'acq.trade' in tv and 'acq' not in tv`。t132 那节的原文**不改**（历史记录照留） |
| 2026-09-12 | p10 | **PG 上中文全文通道仍然是 0 分通道**，`## task-t132` 第 2 条那笔账没结。换影子表之后 `simple` 档技术上已经匹配得上（影子表里的中文已按字切开），但 `fts_search` 仍然对 CJK 抛 `LookupError` —— 这是 T139 **有意保留**的口径（按字 AND 不是中文检索，当成「中文通了」会诱出那句不许说的话），不是漏改 | 本机 PG 上跑 DAG 时 fts 这一路记 0 分，四通道实际只有向量与精确两路在算。混合召回的权重分布与 SQLite 上不一样，而两边都不报错 | 真正的解法仍是装 `zhparser` / `pg_jieba`（要改 Docker 镜像，不是本轨的面）。**但要先想清楚形状**：影子表口径下 zhparser 拿到的是已按字切开的文本，词典分词能力发挥不出来；要让它真按词切就得把 zhcfg 索引建回 `kb_doc` 原文列且查询侧不过 `fts_text()`，那会把错误码通道重新打瞎。两者不可兼得，取舍与两档判据见 `docs/DECISIONS.md ## task-t139` 与 `deploy/polardb.md` §1 |
| 2026-09-12 | p10 | 旧的两条 GIN 索引 `idx_kb_doc_fts_simple_{title,body}`（建在 `kb_doc` 原文列上）**今天没有调用方**：`fts_search("kb_doc", ...)` 已改查影子表 | 只费写入开销与磁盘，不影响正确性。留着是因为跨轨契约只许新增索引不许删（删了在别人的库上不可逆），且影子表不在的旧库上 `_fts_target()` 会回落查 `kb_doc`，那时它们还得上 | 等影子表这条路在 PolarDB 真实例上也跑过一轮之后，单独一轨收掉。**别顺手删** —— 删索引这件事在持久库上没有回头路 |
| 2026-09-12 | p10 | `PgStorePort.query()` 现在会剔除 `_ACCEL_COLUMNS` 里的列。这是一份**手工维护的清单**，将来 PG 侧再加派生列时容易忘记加进去 | 忘了的症状与本轨踩到的一模一样：`prefilter` 在两个后端返回的行不再相等，`test_kb_pg_prefilter.py::test_prefilter_limit_is_honoured_on_both` 变红，案例证据束的「逐字一致」也跟着破。**这次是有测试兜住的**，所以症状会当场显形，不算无声失效 | 加列的人自己记得加。真要机器化，可以让 `_ACCEL_COLUMNS` 从 `pg_schema.sql` 里现解析（那份 DDL 已经是单一事实源），但正则解析 DDL 又是一处新的漂移源 —— 条目只有一条时不值得 |
| 2026-09-12 | p10 | `deploy/polardb-live.md` §3.6 那组 `ef_search` 实测读数（HNSW p50 0.62ms、召回 99.3%、20 万行）量的是 **8/30 当时临时建的 `kb_doc_pg`**，不是装配路径上的 `kb_doc`。T139 把同类能力接到了 `kb_doc` 上，但**没有在 20 万行规模上重量过** | 那组数字现在更有参考价值了（同一种索引、同一个算子），但仍然不是 `kb_doc` 的读数。对外引用时别把两者说成一回事 | 真跑日若有余裕，在 PolarDB 上对 `kb_doc` 跑一次同规模的对照，把读数补进 `polardb-live.md` 的新一节。**不是必须**：复赛演示的语料只有几百条，HNSW 与顺序扫描在这个规模上都看不出差别，那组 20 万行的数字本来就是「能撑住」的旁证而非演示路径的读数 |
## task-t140（评委会读到的四处，2026-09-12）

> 本轨办结了 `## task-t134` 第 1 条（`model-usage.json` 的 note 自打自脸）、`## task-t133` 第 2 条
> （`arrival_basis` 被判「可疑相同」）、`## task-t136` 第 3 条（`GOVERNED_KEYS` 四处口径过期）。
> 按契约 §A 不回头改那三行，在这里划掉。第三条的四处已全部改完，行号也刷准：
> `deploy/nacos.md:142`（连带 `:116` / `:125`）、`deploy/nacos-live.md:236`、
> `docs/agent-teams-gap-analysis.md:110`（`source.py:96-101` → `:124-142`）、`maos/model/client.py:41-47`。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | `scripts/make_case_bundle.py` 写 `roundtable.json` 那一段的注释仍写着「圆桌基线上不落库（T113 未并入）」—— 与本轨刚改掉的那条 note 是同一个过期说法的另一份 | 只在**源码注释**里，读证据的人看不到（产物里没有这句）。但下一个改这个脚本的人会照它判断 | 本轨没改：派单 §3.1 明令「别顺手改这个脚本的别的段落」，且这个脚本同时是 T139 / T141 要读的产物来源。下一次动 `make_case_bundle.py` 的轨顺手删掉那半句 |
| 2026-09-12 | p10 | `maos/roundtable/speaker.py` 模块 docstring（`:14-18`）说圆桌用量「如实落进 `trace.json` 的 `unattributed_usage`」——**T134 之后不是了**，那几行按 `plan_id LIKE 'roundtable:%'` 归进了 `roundtable_traces[].cost`，`unattributed` 那一栏已归零 | 只是文字。但它是「圆桌的钱记在哪」这件事最权威的一段说明，照它去 `unattributed_usage` 里找那五行的人会找不到 | `maos/roundtable/**` 是本波谁都不许动的面（契约 §A.4），本轨一个字没改。归下一次动 roundtable 的轨，或材料面 9/20–9/21 那一轮 |
| 2026-09-12 | p10 | `deploy/nacos.md` 的标题与 §1 / §8 仍写「四个治理旋钮」（`:1` / `:26` / `:54` / `:336` / `:343`），只有「今天有几个会落审计」那几处改成了十 | **有意的**，不是漏改：那几处说的是 T28 / T35 那两轮**做了什么**，是历史叙述与实测小结 | 不必改。若哪天有人觉得整份文档的标题该跟着现况走，那是一次重写，不是刷数目 —— 重写前先看这一行 |
| 2026-09-12 | p10 | `docs/agent-teams-gap-analysis.md:109` 也写着「这五岗的模型调用**不落 `model_usage`**」——与本轨刚改掉的那条 note 是**同一个谎的第三处**（另两处：`make_case_bundle` 的 note 已改、`speaker.py` 的 docstring 见上一行） | 只是文字。这份文件是内部差距分析，不在评委的证据面上，但照它判断「圆桌的钱记在哪」会错 | 本轨没改：整份文件自述是**钉在 `8a6c2f9`（2026-09-07）的只读测绘快照**，「行号会漂，核对请用同一 sha」。派单 §3.3 只点名 `:110` 那一行的治理键数目，改它是派单明令；顺手再改 `:109` 就越过了「刷过期数目」与「重写一份快照」的界。下一次有人重写这份分析时一并改 |
| 2026-09-12 | p10 | `docs/expected-metrics.json` 的 `collected` 落后：本轨新增 8 条测试（`test_pg_snapshot_t140.py`，**一条 PG 门控都没有**，纯单元），worktree 实跑无库档 `1 failed, 3878 passed, 93 skipped`、有库档 `1 failed, 3956 passed, 15 skipped` | `test_expected_metrics::test_collected_count_matches_source_of_truth` 红。**预期内**（契约 §0 点名「谁都不许动那个文件」） | 整合期按合并树的实跑末行一次刷到位。本轨的 8 条在两档里都 passed，所以 `pg_gated_tests` 不变（仍是 78） |
## task-t141

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **`scripts/polardb_smoke.py::_egress_ip()` 在本机实际取不到出口 IP**。实测 `dig myip.opendns.com @resolver1.opendns.com` 返回 `NOERROR / ANSWER: 0`（查询走到了某个服务器，但那不是真的 OpenDNS resolver），而同一时刻 `dig example.com` 正常出 IP —— **不是网络不通，是这条查询在本机被接管了** | 连带后果是 `_diagnose_unreachable()` 那句「DNS 通 + TCP 静默超时 = 大概率白名单没放行本机出口 IP。本机出口 IP: …」**打不出 IP**，只会打「取不到（dig 不可用或网络不通）」。而这一句正是 `## polardb-live` 那条老账（BACKLOG:1146、:1432）推荐加上去省排障往返的。真跑日白名单漂移时，人会既看不到该加哪个 IP、又被「网络不通」指错方向 | **本轨没改** `polardb_smoke.py`（8/30 的验收工具，本轨只读）。T141 的 `scripts/real_run_preflight.py` 已把这一档单独择出来报（「查询返回空答案（这条查询在本机被接管了，不是网络不通）」）并给出路（控制台白名单页面看当前来访 IP），真跑日照 `docs/real-run-runbook.md` 0 段那两条走即可。要根治归下一个能动 `polardb_smoke.py` 的轨：把 `_egress_ip()` 的空答案与超时分开报，措辞同 preflight |
| 2026-09-12 | p10 | **真跑日采集脚本的测试守的是脚本自身，不是「采下来够不够用」**。`maos/tests/test_room_transcript_capture.py` 基线 13 条全在守边界文件、原子追加、token 不泄漏，没有一条回答「采下来的东西够不够填 `docs/demo-script.md:420` 与 `docs/defense-brief.md:78 / :86` 那两处占位」 | 症状是**测试全绿而脚本已经过时**：`render_section()` 的段标题硬编码着 P8 的案号，13 条测试没有一条会红。这次是标题，下次可能是别的（房间侧改了三波命令面，脚本一次没跟） | **本轨已补**：新增 11 条离线演练（夹具 `maos/tests/fixtures/room_transcript_live_run.json`），把四类内容的产物形状钉住。**这一节的维护约定**：房间侧改了命令面 / 卡片格式之后，夹具要跟着改 —— 夹具的 body 形状对齐的是 `room_team_smoke.py` 实跑、`_ProxyVoice._render`、`RefundRouter._render` 与 `projection.PUBLIC_*`，改其中任何一处都该回头看这个夹具 |
## 整合期 p10-f（Wave F 五轨合并，2026-09-12）

> 本波首次**每轨派独立审查员 + 每条 finding 三视角对抗验证**（code / repro / intent，
> 默认立场「它是误报」）。捞出 5 条 major + 4 条 minor/nit，**全部 3/3 存活**，
> 整合期当场修掉 8 条，剩下的记在本节。对抗验证还纠正了我自己的一处修法（见
> `docs/DECISIONS.md ## 整合期 p10-f`「出口 IP」那一行）—— 那一条值回票价。

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **`scripts/polardb_smoke.py::_egress_ip()` 还没带 `-4`**。整合期查实：本机取不到出口 IP 的病根不是「查询被接管」，是 `dig` 走了 IPv6 到不了真的 OpenDNS resolver；`dig -4 +short myip.opendns.com @resolver1.opendns.com` 稳定返回 `112.10.190.130`（连跑三次一致，偶发一次超时）。`real_run_preflight.py` 的第 4 条已经改好，冒烟脚本那份没改 | 真跑日连不上 PolarDB 时，`_diagnose_unreachable()` 那句「本机出口 IP: …，去控制台把它加进白名单」仍然打不出 IP —— 而那正是白名单漂移时唯一要抄的值 | **一个 flag 的事**：`_egress_ip()` 里给 dig 加 `-4`，措辞与 preflight 对齐（顺带备一台 `@208.67.222.222`）。`polardb_smoke.py` 本波不在任何一轨的白名单里，归下一波能动它的轨（Wave G 的 T142 独占它） |
| 2026-09-12 | p10 | **`docs/real-run-runbook.md` C.6 的首行 sha 检查只扫到 52 个证据 JSON**（`evidence/` 下深度 ≤2 的那些），而全树有 241 个；`evidence/*/INDEX.json` 与 `evidence/*/*.json` 两个 pattern 还会把 4 份 INDEX.json 各匹配一次（`ls` 数 56、去重 52）。且它在当前树上就返回 1，不是手册写的 0 | 真跑日照 C 段收工时，这条「证据首行 sha 没有 `-dirty`」的判据**漏掉四分之三的文件**，而屏幕上是一个看起来跑过的结果。返回 1 那一档手册给的处置是「干净树重跑」，那条处置在这棵树上永远消不掉 | 改成递归找（`find evidence -name '*.json'`）并去重；期望值按现树实测重写。runbook 归 Wave G 的 T151 独占，代码侧无改动 |
| 2026-09-12 | p10 | **T139 中文第二档那条判据建在原文靶表上**（`test_pg_store_live.py:212`）：靶表 body 存原文，而 `fts_search` 内部一律把中文查询按字切开发 `to_tsquery`。按 `pg_schema.sql:134-137` 记的那套 zhcfg 建法（`PARSER = zhparser` + `ADD MAPPING FOR n,v,a,i,e,l`），索引侧出词、查询侧出字，粒度对不上恒 0 命中 | 9/18 真跑日若在 PolarDB 上装好 zhparser 并 `export MAOS_PG_FTS_CONFIG=zhcfg`，这条会**假红**，而它的 docstring 写着「红了就知道不该切」—— 会把人指向一个错误的结论。今天它双重门控（有 PG + 有 zhparser），本机 skip，所以不显形 | 搬到生产装配路径上（`kb.ensure_schema` / `kb.upsert_doc` 灌语料，断言 `pg.fts_search('kb_doc', ...)` 召回），与 `test_pg_vector_channel_t139.py` 的 pg fixture 同一套。**归 Wave G 的 T142**（它独占 PG 通道并要决定 zhparser 与影子表的形状取舍）。整合期没动：真要改就得先把那个取舍定下来，那是 T142 的面 |
| 2026-09-12 | p10 | `docs/BACKLOG.md ## task-t140` 里指向 `docs/agent-teams-gap-analysis.md:109` 的行号差一行（实为 `:108`），且同一份文件 `:232` 还有第二处同样的过期说法，条目里没提 | 下一轮照这条去 `:109` 找，落在的是「- 房间身份：每岗可有独立 Matrix 账号…」那一行；按「另两处」的说法只改两处，`:232` 会被漏掉 | 纯文字。`agent-teams-gap-analysis.md` 自述是钉在 `8a6c2f9` 的只读测绘快照，重写它是另一件事 —— 归 Wave G 的 T152（它独占那一族文档的漂移清单） |
| 2026-09-12 | p10 | **离线演练夹具此前整体偏离真房间**：四条结果面回执的文案、两张回帖卡的首行全是手写的，三波房间文案改动一次都没让它红。整合期用真 router 抓原文逐条改回（`/assign` 是「已派单」不是「已改派」；`/resolve` 是「已关单 …（settled）· 提交人 …」+ 四判据行，没有「对客户口径」那一行；`/confirm` `/complain` 第二行是四判据行不是「结果面：…」；回帖卡首行是 `已放行 <案号>（操作人 …）` + `<摘要> · 案子 <案号>` 两行） | 已修，并加了 `test_the_fixture_outcome_replies_match_the_real_templates` 把夹具钉到 `outcome_commands.py` 的字面模板上 | 遗留一件：夹具里那张 `┌─ 收口 · approve` 箱线卡与 `[事实卡]` 后缀，对抗验证指出房间侧**确实**有一张收口卡（`hiclaw/room_ingress.py::_ChairTeam._chair()` → `hiclaw/room_voices.py::render_verdict_card()`），但形状是 HTML，不是 `room_team_smoke.py` 打到 stdout 的那个箱线形状。**没改**（`hiclaw/**` 本波无人独占）。归下一波动 `hiclaw/` 的轨：按 `render_verdict_card()` 的真产物改夹具那一条 |
| 2026-09-12 | p10 | 同一张 `approval_record` 上，闸循环那几行的 `approver` 带岗位后缀（`@boss:maos.local（supervisor）`），房间补落那行是裸账号（`@boss:maos.local`）—— 同一个人两种写法 | 今天没有按 `approver` 精确匹配的查询，所以不是缺陷。但「这一单谁签的」若哪天要按人聚合，两种写法会把同一个人算成两个 | 无需当场处理，记一笔免得下一个人以为是数据脏。真要统一，统一到带岗位后缀那一种（它信息更全），并同时改两处写入点 |

## task-t142（PG 真跑日血管，2026-09-12）

> 办结 `## 整合期 p10-f` 里那条「**T139 中文第二档那条判据建在原文靶表上**」：已搬到
> `maos/tests/test_pg_vector_channel_t139.py` 的「4. 中文第二档」（装配路径），并拆成
> 「链路档（本机每次真跑）+ 真分词器档（真跑日跑）」两条。形状取舍见
> `docs/DECISIONS.md` 的 `## task-t142`。
>
> 办结 `## polardb-live` / `:1431` 那条「**白名单判据方向写反了**」的**代码那一半**：
> `_diagnose_unreachable()` 已改成四档，第 3 档按 SQLSTATE 分。**但那条要求的「改之前
> 先把两种链路各复现一次」本轨没做到** —— 见下表第 1 条。

| 发现日期 | Phase | 问题 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **四档诊断的判据没有在真链路上复现过**。`BACKLOG:1431` 明写「改之前先把两种链路各复现一次，别照抄本条」，而链路 B（公网 + SLB + 白名单不放行）要真实例才造得出，本轨一律打本机容器（红线 1） | 新分档在「有 SQLSTATE」那一支上与旧行为等价，所以不会比今天更坏；但「无 SQLSTATE → 优先怀疑白名单」这句话的**依据是 2026-08-31 的一次别人的实测**，本轨只验了「拿到这种症状会说哪句话」 | **9/18 真跑日当场核**：撞上连不上时，把 `polardb_smoke.py` 的完整输出连同控制台白名单的实际状态一起记下来，回头核这一段的措辞。真跑日没撞上就留着 —— 不必为了复现去故意把白名单改坏 |
| 2026-09-12 | p10 | `_diagnose_unreachable()` 只探**第一个** A 记录（`addrs[0]`）。实例有多个 A 记录（SLB 多节点）时，第一个通不代表其余都通，反之亦然 | 今天不影响结论：四档给的都是「往哪个方向查」，不是「这条连接一定能/不能建」。但多节点实例上若只有部分节点被白名单放行，诊断会按撞上的那一个下结论，而复跑可能换一个节点、结论跟着变 | 真跑日若出现「同一条 DSN 反复跑结论不一样」，先看这里。要改就是逐个 A 记录探一遍并汇总，成本不高，但**没有真多节点实例就验不出来**，所以没当场改（铁律 4） |
| 2026-09-12 | p10 | `deploy/polardb.md` 的「已知差异 / 局限」一节里，`plainto_tsquery` 那句话已过期：T139 起查询侧早就换成了 `to_tsquery`（Python 切好、PG 只匹配），而那句「24 条真语料上全文召回 8/10（漏的两条是 `plainto_tsquery` 的 AND 语义）」是 8/30 在旧路径上的读数 | 那是 8/30 的**历史实测记录**，数字本身没错，但读的人会以为今天还走 `plainto_tsquery`。**没改**（铁律 3：历史读数改了就是篡改证据；而本轨改的是它下面那一节的判据说法） | 归 Wave H 的文档轨：在那句旁边补一行「这是 T139 换 `to_tsquery` 之前的读数」，**不要动数字** |
## task-t143（结果面第二档 + 四判据接上 DAG，2026-09-12）

**本轨办结三条老账**（原条目留在原处、一个字未改，按跨轨契约 §A.3 只在这里注明出处）：

- 办结 `## task-t114（单案例端到端证据束，2026-09-11）` 里「`handle_resolve` 把 `resolution_kind`
  写死成 `CP.RESOLUTION_SETTLED`」那条 —— `/resolve` 加了 `--not-settled` 第二档。**形状与当初
  建议的末位可选参数不同**（选了紧跟工单号的开关位），理由记在 `docs/DECISIONS.md ## task-t143` 第 1 行。
- 办结 `## task-t122` 里「`outcome._receipt_source()` 读观察回执的**顶层**」那条 ——
  改成 `detail.gateway` 先读、顶层两个键后读，`ManualReceiptAdapter` 与码表一个字节没动。
- 办结 `## task-t129` 里「`CaseOutcomeComputed` 事件只挂得上 `plan_id`」那条 ——
  `record_case_outcome` 加了 keyword-only 的 `trace_id` / `task_id`，三个入站函数一起透传；
  钉现状的那条反向测试改判成正向（新名字
  `test_case_outcome_computed_now_carries_the_whole_trace_triple`，判据是查库）。
  连带补齐 `## 整合期 p10-c（Wave C 五轨合并，2026-09-11）` 里「三个事件的 `trace_id` /
  `task_id` 是空的」那条的最后一格 —— 另两个事件 T129 已补，只剩这一个。

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | 🔴 **证据束里那条 `CaseOutcomeComputed` 仍然只挂 `plan_id`**。本轨补的 trace 三件套只在**房间命令面**填得上（`/resolve` `/confirm` `/complain` 经 `router._command_extras` 拿到三件套）；证据束走的是另一条路 —— `PlanFinalizer.poll` → `maos/kb/promotion.py::promote_case`（`:176`）与 `scripts/make_case_bundle.py:587`，两处都只传 `plan_id` | 评委在 trace 树里看的是**证据束**，而束里那条事件照旧接不上 DAG —— 本轨的目标在房间面达成了，在证据面没有。实测（重产 `--all-paths` 之后）：`evidence/case-real-01/{happy,gateway_fail}/event-chain.json` 里 `CaseOutcomeComputed` 的 `trace_id` / `task_id` 仍是空串。**连带一条好消息**：证据面因此不受本轨影响，本轨不需要为自己重产束 | **本轨一个字没改**：`maos/kb/**` 是 T145 独占、`scripts/make_case_bundle.py` 是 T144 独占。改法 3–5 行：`promote_case` 手上有 `plan_id`，照 `router._command_extras` 的写法取 `store.get_plan(plan_id)["trace_id"]` 与那条 `-payment` 任务，透传给 `record_case_outcome`（两个参数已经在了，keyword-only + 缺省空串）。归整合期或 Wave H |
| 2026-09-12 | p10 | **`/compensate` 两处回帖里的「关单」示范只给缺省形状**（`outcome_commands.py` 的「不用补开」与「已补开」两段都印 `关单：/resolve <单号> <渠道流水号> <线下凭证摘要>`），没提第二档 | 不是缺陷：那一行给的是最常见的走法，第二档在 `/help`（router.USAGE）与 `/resolve` 参数不合法时回的 USAGE 里都看得到。但房间里的人最先看到的是这一行，真跑日若撞上「确实没退成」那种单，他可能要先翻一次 `/help` | 本轨没改（派单只点名两处 USAGE 必须同步，改这两行属范围外）。要加就一行字：`（没退成加 --not-settled）`。归下一个动 `outcome_commands.py` 回帖模板的轨 |
| 2026-09-12 | p10 | **`docs/real-run-runbook.md` 与材料面里 `/resolve` 的用法仍只写缺省形状**（runbook 里那条真跑日照抄的命令行没有 `--not-settled`） | 真跑日照 runbook 敲命令时，「线下核对过、这笔确实没退成」这一档在纸面上不存在 —— 而这一档正是本轨为真跑日补出来的 | 本轨白名单里没有那些文件，**一个字都没改**。归 Wave H：`real-run-runbook.md`（T151）与 demo-script / defense-brief（材料面）各刷一句。回执里已单列 |
| 2026-09-12 | p10 | `compensation_record` 一个案子有两行（`kind=refund_request_revoked` 与 `kind=manual_ticket`），而 `_correction_of` 只看「有没有补偿记录」就判 `compensated` | 今天不误判（有工单的案子判 compensated 是对的），但那条判据的分母其实是「两类补偿记录之一」。将来若出现只作废请求、不开工单的路径，它也会被判成「人工纠错=compensated」，而那一档本该是 `overridden` 或 `none` | 记一笔免得下一个人以为分母是工单。真要收窄得先定义「哪几类 compensation_record 算人工纠错」，那是判据面的改动，不是一行 |

## task-t144（失败路径的客户通知 + 正文可查，2026-09-12）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **`reject` 那条路的通知仍挂不上 business_ref**：`custom_case.py:931` 的驳回分支按 docstring 有意传空 `task_id`（那条通知不是任何一个 DAG 任务跑出来的），于是 `notify.py:127` 跳过 `attach_business_ref` | 实测 `reject` 束的 `business_ref_missing` 里仍有 `notification`、`case_outcome.evidence_complete` 恒 `false`。**今天不是 bug**：它如实反映「这条通知没有归属任务」，但读束的人分不清「没发通知」与「发了但挂不上引用」—— 而这两件事差得远 | 要么给 business_ref 一种「挂在 plan 上、不挂任务」的形态，要么在束里把这一格写成「已通知，无任务归属」。两条都要动 `objects.attach_business_ref` 或 `case_pack.ref_coverage`，**本轨白名单外**。复赛之后 |
| 2026-09-12 | p10 | **跑过证据束之后 `test_render_trace::test_committed_report_is_in_sync` 必红**，而它的报错只说「跑 `python3 scripts/render_trace.py` 重新生成」，不说「你只是把工作树跑脏了」 | 任何一轨只要在验收里跑了 `make_case_bundle.py` 又忘了还原 evidence，全量 pytest 就会多出这一条红 —— 而派单给的期望是「只许 1 条 failed」，于是它看起来像回归。本轨查了一轮才排除 | 在那条测试的报错里补一句「先确认 `git status evidence/` 干净」。`test_render_trace.py` 与 `render_trace.py` 归 Wave H 的 T146，本轨不动 |
| 2026-09-12 | p10 | **补偿路径的通知文案与房间那条共用同一个产出处，但「什么时候发」的判据在两处各写了一遍**：`router.py:1974` 判 `KIND_DONE + CMD_RESOLVE`，`make_case_bundle.py::_compensate()` 判三步都 ok | 措辞不会漂（都走 `notify.customer`），但「该不该发」会。今天两处口径一致，没有症状 | 与 T143 的接缝一起看：`/resolve` 第二档落地后两条路要不要分档发。归整合期判断，见本轨回执 |

## task-t145（知识层不再 import 退款域 + 补偿收口不当范本，2026-09-12）

> 本轨办结三条旧账。按契约 §A.3 不回头改中间那几行，在这里记：
>
> 1. **办结 `docs/BACKLOG.md` 里「`maos/kb/promotion.py:54` 是模块级 import 退款域」那条**
>    （`## task-t130` 小节，2026-09-11 记）—— 改成函数体内 `_refund()` / `_objects()`
>    局部 import，9 个函数各取各的，调用点一个字没动。判据也从「只管 `plan_advice.py`
>    一个文件」扩到扫 `maos/kb/*.py` 全片（`test_no_kb_module_puts_a_business_domain_on_its_import_graph`）。
> 2. **办结「已补偿的案子会被晋升成『成功范本』」那条**（`## task-t114` 小节，2026-09-11 记）——
>    `_classify_by_outcome` 正例分支加 `status not in FAILED_BIZ_STATUS`，复用现成常量。
> 3. **办结「铁律 9 在 `plan_advice.py` 上实际有例外、没写进权威文本」那条**
>    （`## 整合期 p10-e` 小节，2026-09-12 记）—— `CLAUDE.md` 铁律 9 加一句括号说明，
>    `maos/kb/__init__.py` 的「依赖方向」段同口径展开。

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **`maos/runtime/plan_finalizer.py:123-126` 那个 `except ImportError` 分支现在多半走不到了**。它 try 的是 `from maos.kb.promotion import promote_plan`，注释写着「域未合入时软降级」—— 而退款域缺席现在由 `promotion._has_table()` 收（实测：域被 meta_path 拦掉时 `import maos.kb.promotion` 照样成功，`promote_plan` 返回 `[]`） | 不是缺陷：那条分支仍拦得住「`maos/kb/promotion.py` 这个文件本身不在」的部署，只是它原本要挡的那件事已经在上游解决了。留着的代价是一条没有判据、也没有现役触发路径的分支 | `plan_finalizer.py` 本轨不在白名单，没动。下一个动它的轨决定：要么把注释改成实况（「模块文件缺席时软降级」），要么连同那一层 try 一起去掉。**别顺手删**——`promote_plan` 下面那个 `except Exception` 才是现役兜底，两层作用不同 |
| 2026-09-12 | p10 | **`scripts/make_case_bundle.py:128 RESOLUTION_KIND = "not_settled"` 现在可以换回 `settled` 了**。当初改成 `not_settled` 正是为了绕开「补偿收口被晋升成成功范本」那个洞（`## task-t114` 小节那条末尾写着「做完可以换回」），护栏今天落地了 | 换回去那条路径就能演「人工线下退成了」的闭环，而不是只演「核对过、确实没退成」。不换也不错，只是少演一档 | `make_case_bundle.py` 是 Wave G 的 **T144 独占**，本轨不代改。换回之后 `case-real-01/gateway_fail` 束的 `kb` 那一族产物会变（那一单从 `history_case/success` 落到 `failure_hint/failed`），要连带重产并核对 verify 第 5、7 项的分母 —— 交整合期 |
| 2026-09-12 | p10 | **`docs/domain-portability.md:458-461` 与跨轨契约 §C 的口径仍然相反**：那段说「`flows/` 与 `kb/` **本来就是按域写的**（演示流程与知识语料），不在『内核零改动』的主张范围内」，而契约 §C 与本轨落地的判据说的是「`maos/kb/**` 是领域无关内核，取值可以局部 import + 兜底、断言不行」 | 两处措辞打架，评委任取一处都能质疑另一处。本轨把 `kb/` 那一半做实了（模块级 domain import 全片归零，有 AST 判据钉着），文档那句话现在**落后于代码** | 归 Wave H 的 **T152**（它独占那一族文档的漂移清单）。建议措辞：把「`flows/` 与 `kb/`」拆开写 —— **「`flows/` 本来就是按域写的演示流程，不在主张范围内；`kb/` 是领域无关的检索内核，模块级不 import 任何业务域（判据 `test_plan_advice.py::test_no_kb_module_puts_a_business_domain_on_its_import_graph`），已知例外只有『函数体内局部 import 取值 + 兜底』一个形状，见 `maos/kb/__init__.py` 的依赖方向段」**。两处的行数统计（`kb/` +1568 / +1885）不用改，那说的是改动量不是依赖方向 |
## task-t147

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-12 | p10 | **全仓第四处裸 sandbox 调用**：`maos/skills/builtin/code_repo_patch.py:267` 的 `sandbox_git_apply(patch, workdir or "", check_only=True)`（自修复前的预检干跑）。BACKLOG 2026-09-01 那条只数到三处，没数到它。本轨收掉的是 `control_plane.py` / `gate.py` / `flows/common.py` 那三处 | 与另外三处同因：绕开 `invoke_tool` 就没有 `ToolInvoked` 审计行，`scripts/verify.py:327` 的 hash-integrity 对这条路径是空的；将来换掉 `sandbox.*` 的 `entry` 时它不跟着换、也不报错 —— 静默失效。今天无害（不是 agent 发起的调用） | `code_repo_patch.py` 不在本轨白名单，**按铁律 4 没当场改**。归下一个独占该文件的轨。收法与本轨三处相同：`invoke_tool(GIT_APPLY_PORT, {...}, store=ctx.store, extras=ctx.extras)` —— 那个文件 `:198` 已经有一处走 `invoke_tool(GIT_MCP_PORT, ...)` 的现成姿势可抄 |
| 2026-09-12 | p10 | **`IllegalTransition` 走 bus 时会被吞成「表面正常」**：`InMemoryEventBus.drain()` 把 handler 异常 catch 住当 nack（`eventbus.py:69`）并重投，而重投时 `result:<task_id>:<attempt>` 幂等键**已被第一次调用消费**，第二次直接短路 return —— 不抛、不重投、连死信都进不去（`dead_letters` 是空的） | 两层掩盖叠起来的效果是：第一次调用里排在异常点**后面**的代码永远不执行，而屏幕上一片正常。本轨修的 `_fail_plan` 就是活例 —— 它抛出去会让 `on_task_result` 末尾那段销租约跑不到，`claim_lease` 表里从此躺着一条不会被销的租约。**这也让「走 bus 的测试」验不出异常**：本轨测试第一版走 bus，把护栏整个拆掉照样全绿（变异验证当场抓到），改成直调 `on_task_result` / `on_review_verdict` 才真断言得到 | 本轨**没改** `eventbus.py`（不在白名单，且那个类自述是「两个后端等价性证明的基准侧，改了结论就自我循环」）。建议下一个动可观测面的轨评估：handler 抛异常后幂等键要不要回滚，或者至少让「异常 + 键已消费」这一格留一条可查的痕（今天它连日志都只有 warning 级的一行重投记录） |


## 整合期 p10-g（Wave G 五轨合并，2026-09-13）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-13 | p10 | **补偿干跑闸在真链路上从来没跑过。** `_review`（`gate.py:263`）按 `a["version"] == task["attempt"]` 过滤产物后才把列表递给 `_gate_compensation`，而 `_attach_compensation` 写入的 compensation 产物恒是 `COMPENSATION_VERSION = 0`（`control_plane.py:186`），attempt 恒 ≥1 —— `comps` 永远为空，`_dry_run_reverse` 从不被调到。**`control_plane.py:184-185` 那条「⚠️ 合并期核对项：C 轨的 `_gate_compensation` 必须在全量 `list_artifacts(task_id)` 里按 kind 找，不能在按 version 过滤后的列表里找，否则找不到」写的正是这件事，而它从来没被执行** | 这道闸自述「补偿不可执行这件事，不干跑就永远发现不了」，而它自己一次都没干跑过 —— 一道形同虚设的安全闸比没有更坏，因为 `gate_results` 里它恒显示 `pass`。实证：scenario-3 是唯一有 compensation 产物的场景，它的 `gate_results.compensation` 是 `"pass"`，整个证据束没有一条来自干跑的 `sandbox.git_apply`。连带：T147 那处 `invoke_tool` 改写在这一处的审计覆盖是 **0 条**，verify 的 hash-integrity 分母涨的 6 条全部来自 `flows/common.py` | **不是 T147 引入的，是基线 bug**，整合期按铁律 4 没当场改（见 DECISIONS 本页末行）。修法：`_gate_compensation` 自己去 `store.list_artifacts(task["task_id"])` 拿全量再按 kind 挑，不用过滤后的 `artifacts` 形参。**修完要连带核**：场景 3 的 `gate_results` 会不会从 pass 变 fail、证据束的事件数会不会涨、干跑失败时补偿路径怎么走。建议单独开一轨，别夹在别的改动里 |
| 2026-09-13 | p10 | `maos/core/control_plane.py:1330` 回滚失败时那条 `ToolInvoked` 的 `status` 仍是 `"ok"`（`invoke_tool` 只按是否抛异常判 status，`port.py:52-56`），而 `sandbox_git_apply` 返回的是 `{"ok": False}` | 「证明这一步真在沙箱里跑过」的那条审计行会给读者相反印象。真实结果另记在 `CompensationExecuted` 里，所以不是事实缺失，是审计面的口径缺失 | `port.py` 不在本波任何轨的白名单。下一个动 ToolPort 的轨评估：`invoke_tool` 要不要认返回值里的 `ok` 字段（注意别把「工具正常返回了一个失败结论」与「工具自己炸了」压成一格） |
| 2026-09-13 | p10 | `maos/runtime/gate.py:518-520` 的 extras 用 `task.get("plan_id") or ""` 兜底，而同一轨 `control_plane.py:1332` 用的是直接下标 `task["trace_id"]`；两处姿势不一致 | 今天 task 行永远带 plan_id，落不到空串。一旦生效就是一条 plan_id 为空的 `ToolInvoked` → `trace.py:1047` 判成游离事件 → verify 第 4 项 warn | 与上一条同轨处理。今天无害，别单独为它开工 |
| 2026-09-13 | p10 | `maos/kb/experiment.py:1178-1181` 的注释因 T147 而过期：它写着「再对它们 decide 一次，`_fail_plan` 会撞 `FAILED -> FAILED` 的非法迁移」—— 护栏下沉后不会再撞了 | 纯注释失真，行为无影响。但它是一条会误导下一个读者的「已知限制」 | 下一个动 `kb/experiment.py` 的轨顺手改。该文件不在本波白名单 |
| 2026-09-13 | p10 | `scripts/polardb_smoke.py` 第 4 档对 `ConnectionResetError` 断言「不是白名单 … 不会回一个立刻的 RST」，而同一份 docstring 把链路 B 的白名单症状描述为「握得上、随即被断」 | 诊断 socket 只做裸 connect 不收发数据，绝大多数情况落到第 3 档，所以今天不算错；但若 SLB 在握手期就 RST，第 3/4 档会给出相反结论 | 与 `## task-t142` 那条「链路 B 未复现」同一风险，留到 **9/18 现场**核。别在没有真实例的情况下改措辞 |
| 2026-09-13 | p10 | `maos/tests/test_polardb_smoke_t142.py:369` 的 `test_egress_ip_wording_matches_the_preflight_probe` 只比两个函数 docstring 里有没有 `-4` / `208.67.222.222` 字样，不是行为判据 | 不算假绿（同文件的 `test_both_scripts_classify_digs_own_timeout_the_same` 真比了行为），只是这一条本身价值有限 | 复赛之后。不值得现在动 |
| 2026-09-13 | p10 | `docs/BACKLOG.md` 的 `## task-t143` 第 2 条漏了 `maos/ingress/router.py:1441` —— `/reject` 那张卡（真跑日人类**最先看到**的一处）里同样印着旧的 `/resolve` 用法 | 下一轨照那个条目去改会漏掉最要紧的一处，并撞上 `maos/tests/test_room_transcript_capture.py:530` 那条逐字冻结的断言 | 补在这里而不是改那条（两本账都是追加制）：**要改 `router.py:1441` 的用法行，必须同时改 `test_room_transcript_capture.py:530`**，那个文件本波谁都不许改，所以 T143 没动是对的 |
| 2026-09-13 | p10 | `CORRECTION_OVERRIDDEN` 被 T143 修好之后，在任何真实路径上仍然不可达：人工回执唯一生产者 `compensation_close` 先 `CP.require_ticket` ⇒ `compensations` 恒非空 ⇒ `_correction_of`（`outcome.py:178`）先命中 `compensated` | T143 的改动本身是对的（拆掉了一个恒假分支，两条新测试也真），但派单想要的「人工纠错演得出两档」并未达成 —— 演出来的还是只有 `compensated` 一档 | **材料面别把它当演示点。** 真要演两档，得先想清楚哪条真实路径会产出「有人工回执、但没有补偿记录」的案子 —— 那是设计问题不是代码问题，留到复赛之后 |
| 2026-09-13 | p10 | `maos/domain/refund/outcome.py::close_complaint` 的 trace 三件套透传没有测试，且全仓无调用方传它 | 纯前瞻参数。今天不会错，但也没有判据钉住它 | 等第一个真调用方出现时一起补。别为它单独写测试 |
| 2026-09-13 | p10 | `scripts/make_case_bundle.py:672` 把 `content` 加进 detail 白名单是**全局生效**的，不限事件类型 | 今天四条路径里只有 `CustomerNotified` 带这个键（已逐条扫过确认），但将来任何一个 detail 里放了 `content` 的事件（产物正文、模型回复）都会原样落进给评委看的 `event-chain.json`。需求是窄的，口子是宽的 | 下一个动 `make_case_bundle.py` 的轨收窄成「按事件类型给键」。注意本波已把身份红线的扫描面扩到 `trace.json`（见 DECISIONS 本页），收窄白名单时别把那条判据一起弱化了 |
| 2026-09-13 | p10 | `maos/skills/builtin/refund/notify.py:137` 的 `_log_notified` 落在 `INSERT notification` 与 `attach_business_ref` **之后**且不兜异常 | `append_event_log` 一旦抛，`run()` 往外传 → 房间 `/resolve` 会告诉操作员「通知没发出去」、`_compensate` 把整束判失败，而通知行其实已经落库了。一次留痕写失败被报成一次业务失败，方向与「观察 ≠ 权威事实」相反。概率很低（本地 SQLite insert） | 复赛之后。修法是把 `_log_notified` 包进 try 并记 warning —— 但要先想清楚「留痕失败」本身该不该有痕 |
| 2026-09-13 | p10 | `docs/DECISIONS.md` 的 `## task-t144` 那行写「`make_case_bundle.py` 那侧照 `_compensate` 现有的局部 import 块办」，实际那句 `from maos.flows.custom_case import notify_customer` 放在函数体中部、没并进开头的 import 块 | 纯文字与代码对不上，行为无影响 | 两本账追加制，不改旧行。下一个动那个函数的轨顺手并进去即可 |
| 2026-09-13 | p10 | `maos/kb/guardrails.py:556` 与 `scripts/verify.py::check_history_case` 之间仍留一条窄缝：护栏只挡 `FAILED_BIZ_STATUS`（`rejected` / `compensated`），而 verify 要求 `biz_status in AUTHORITATIVE_STATES`（退款域只有 `settled`）。`test_promotion.py:231` 还反过来把 `processing` 与 `case_row=None` 钉成「仍晋升成 history_case」 | 这类文档一旦进束，verify 第 7 项照样判负。可达性低：`payment.observe` 在 `processing` 上会把状态推到 `settled`；只有人工回执落在 `submitted` / `approved` / `gateway_accepted` 三档才留得下「settled 观察 + 非 settled 状态」，而房间 `/resolve` 必须先有工单、工单必经 `refund.compensate` 置 `compensated` | 口径是 T145 派单定死的（复用 `FAILED_BIZ_STATUS`、明令不许一刀切成「只认 settled」），子会话没越界。真要收口得先统一护栏与 verify 两边的状态集合定义 —— 那是一次口径重构，复赛之后做 |
| 2026-09-13 | p10 | `maos/tests/test_plan_advice.py:659` 的 `test_promotion_still_asks_the_refund_domain_from_inside_its_functions` 断的是**原始文本子串**：那一行被注释掉照样绿，把 import 拆成两行或改 alias 会误红 | 口径与同文件既有的 `test_the_domain_import_is_local_not_absent` 一致，不算跑偏。本波已把 `CLAUDE.md` 那句「两条 AST 测试」改成实况，所以不再有措辞失真 | 复赛之后。要改就两条一起改成 AST 判据（找 `FunctionDef` 体内的 `ImportFrom`），别只改一条 |
| 2026-09-14 | p10 | `README.md` §1 场景 7 那段引用的 `补偿记录 : 2 行 ['manual_ticket', 'refund_request_revoked']` 与现证据对不上：`evidence/scenario-7/business-objects.json` 里 `compensation_record` 只有 **1 条**（kind `manual_ticket`）。同段「四个 Agent 全部回复「完成」」也易被读成四个任务全 DONE，而 `result.json` 里 `task-s7-payment` 终态是 **FAILED** | 给评委/客户看的第一段里有一个数字对不上证据，正撞铁律 3 自己立的规矩。T148 的入口页已按实读绕开（见 DECISIONS 本页），但 README 原文未动 | `README.md` 归 **T151**，不在 T148 白名单，本轨不改。T151 刷这一段时按 `evidence/scenario-7/` 实读取数；那段代码块若原本是某条命令的真实输出，要连命令一起复跑再贴 |


## task-t149（双平台一键脚本，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **`RUN-ME.bat` 没有 Windows 真机验证。** 本机是 macOS，`.bat` 跑不了。做到的只有静态检查：CRLF 行尾（`file` 报 `with CRLF line terminators`）、纯 ASCII 无 BOM（`non-ascii: 0`、`has BOM: False`）、`goto` 的四个目标都有对应 label（`:eof` 是 cmd 内建）、探测表达式两档解释器实跑（3.11 -> exit 0、3.9 -> exit 1）、表达式里零个 cmd 元字符（`<` `>` `|` `&` `^`），以及 `scripts/check_docs.py` 判据 F 只扫 `*.md`、不会误伤 `.bat` | 未验的是 cmd.exe 的真实解析行为：`%~dp0` 在带空格路径下的展开、`call :probe` 子程序的 `%*` 作用域、`endlocal & exit /b %RC%` 的变量展开时机、App Execution Alias 存根被探到时会不会弹 Microsoft Store。这些都是「写对了看不出、写错了当场死」的地方 | **找一台 Windows 真机跑一遍**，最便宜的验法是先 `RUN-ME.bat` 在有 Python 与无 Python 两种机器上各双击一次。在真机验过之前，客户文档不许写「Windows 已验证」 |
| 2026-09-14 | p10 | **客户解压包跑 `verify.py` 是 `RESULT: 9/10 PASS`、exit 1，失败项 `provenance`** —— 与 T151 派单里说的现象同一件事，本轨复现并定位到根因：`scripts/make_release.sh` 用 `git clone --depth 1`，包里只有 1 条 commit；而 `evidence/` 下 19 个束自称出处 `6ecc10a`、1 个自称 `4a53a5c`，这些 sha 在浅克隆里**根本不存在**（实测 `git cat-file -e 6ecc10a` 报 `Not a valid object name`，同一个 sha 在完整仓库里是 `fb2100b` 的父提交）。于是 provenance 判「该 sha 不在本仓库历史里，谁也 checkout 不出来」 | 逐条点名的是 `case-real-01-pg/happy`、`case-real-01-pg/reject`、`contrast-R8`、`domains`（`room` 那三条是 `warn:`，不判负）。客户重跑 `make_evidence.py` 只重写 8 束 `scenario-*`，其余 12 束的出处刷不到 —— **所以这条与客户跑不跑一键脚本无关，是包本身的性质**。第 1 层的第 4 步会如实报失败并把 `RESULT: 9/10 PASS` 印在标题里（没有为它加特殊分支：那会掩盖一个真问题，且违铁律 4） | **归 T153（在修 9/10）与 T150（`make_release.sh` 归它）**。两条修法各自成立：① 打包前把全部束重产到同一个 HEAD（`make_evidence.py` + `--domains` + `--contrast` + `make_case_bundle.py --all-paths`，`room` 束无生成器要人工采集）；② 或让 `make_release.sh` 不用 `--depth 1`（代价是 `.git` 体积）。**别只改我这一侧的判据** —— 把第 4 步放宽成「只认 exit code」或「容忍 9/10」，等于让客户跑出一个我们自己知道不对的结果还显示成功 |
| 2026-09-14 | p10 | **macOS Gatekeeper 拦 `RUN-ME.command` 这件事，脚本侧解决不了，只能靠文档告知。** 实测：`xattr -w com.apple.quarantine "0081;0;;" RUN-ME.command` 之后 `open RUN-ME.command`（等同访达双击）阻塞两分多钟后回 `_LSOpenURLsWithCompletionHandler() failed with error -128`（`userCanceledErr`），**脚本一行都没执行**；清掉该属性后同一条命令 `exit=0` 立即返回、终端窗口正常起来 | 绕法只有客户自己动手：右键点 -> 选「打开」-> 弹窗里再点一次「打开」（**必须走右键菜单**，直接双击的那个弹窗上没有「打开」按钮）。已写进 `RUN-ME.command` 顶部注释，但客户多半不会去读一个打不开的文件里的注释 —— 所以真正的载体是客户文档 | **归 T151**（客户版说明书的「常见问题」）。另注：权限位这一路**不用担心**，实测 git 记 `100755` -> `git clone` -> `zip`（存 `unx -rwxr-xr-x`）-> `unzip` / `ditto`（访达用的就是它）全程保留可执行位，所以 `chmod +x` 那条兜底说明可以写成「万一遇到才用」，不必放在正文显眼处 |


## task-t152（客户路径验收，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **Windows 真机行为一条都没验过，也验不了。** `RUN-ME.bat` 在本机只做了静态检查（行尾 CRLF、含 `%~dp0`、Python 探测含 `py -3`、包根条目名全 ASCII 且打了 UTF-8 标志位），这四条判的是文件的字节。真机上还有五件事本机**永远**产生不了真实读数：Windows 自带解压器会不会吞掉可执行位 / 压平目录层级、对非 UTF-8 条目名实际解成什么；GBK 控制台下中文提示的显示效果与 `chcp 65001` 之后的行为；`py -3` 在没装 launcher 的机器上的落点、以及 `python` 撞进微软商店 stub 时的表现；双击 `.bat` 时 SmartScreen / 组策略的拦截；从网络下载的 zip 带 Mark-of-the-Web 时脚本被阻止执行 | `docs/client-smoke-report.md` 按铁律 3 把三栏分开写了，**任何一处都没有声称「Windows 已验证」**。但这意味着客户路径有一整条腿是盲的 —— 交付物里 `RUN-ME.bat` 是两个一键入口之一 | **需要一台真 Windows 机器**（或一次虚拟机 / 借机）。复赛 9/22-23 之前找一次机会跑：解压 → 双击 `RUN-ME.bat` → 看是否走到 `RESULT` 行；再把 Python 卸了 / 改名一次，看失败提示是否说人话并指向 `START-HERE.html`。跑完把读数补进 `docs/client-smoke-report.md` 的 §4.2，并把该项从「静态检查通过」挪进「已实测」 |
| 2026-09-14 | p10 | **`scripts/make_release.sh` 的密钥自查在当前主干上误报，exit 1，打不出一个 exit 0 的包。** B 层（已知密钥形态正则）的 `sk-[A-Za-z0-9]{20,}` 命中 `maos/tests/test_model_routing.py` 第 37 行 —— 那是一个**明确标注为假**的测试夹具，它自己的注释原文是「故意构造的假 key，只在第 4 组用例里出现。它不是任何真实凭证」。`git log -S` 指向 `38eca1a`（2026-09-01，Phase 9 的角色→模型路由表），**不是本波引入的** | A 层哨兵反查（拿本机环境变量真值反查）是 0 命中，所以**没有真实密钥泄露**；坏的是这道闸本身。复赛要交的就是这个包，而一道永远红的闸，人只会学会绕过它 —— 于是它哪天抓到真东西也没人信。从 9-01 到今天没有任何判据盯着这件事，因为打包脚本平时没人跑、跑的时候也没人看退出码 | 归 **T150**（本波唯一改 `make_release.sh` 的轨）。方向二选一，本轨不拍板：给 B 层加一个「文件内已声明为假夹具则豁免」的口径，或把假 key 的字面量拆成运行时拼接、让它不再长成密钥形态。**别直接把整个文件加进豁免名单** —— 那个文件将来还可能被塞进真东西。修完用 `python3 scripts/client_smoke.py` 复跑，A0 应当转绿 |
| 2026-09-14 | p10 | **Wave G 留给 T152 的那条文档漂移，交接落空了。** `## 整合期 p10-g` 小节那条写着「`docs/domain-portability.md:458-461` 与跨轨契约 §C 的口径仍然相反 … 归 Wave H 的 **T152**（它独占那一族文档的漂移清单）」，还给好了建议措辞。但 T152 的派单把职责改成了客户路径验收，白名单只有 `scripts/client_smoke.py` 与 `docs/client-smoke-report.md` 两个文件，**不含 `docs/domain-portability.md`** | 那条漂移仍在：文档说「`flows/` 与 `kb/` 本来就是按域写的，不在『内核零改动』主张范围内」，而契约 §C 与 `maos/tests/test_plan_advice.py` 的 AST 判据说的是「`maos/kb/**` 是领域无关内核」。两处措辞打架，评委任取一处都能质疑另一处 | 本轨按铁律 4 **没动**（不在白名单）。Wave G 给的建议措辞仍然适用，原文在 `## 整合期 p10-g` 小节末条。归整合期或下一个独占该文档的轨 —— 这条已经空转一波了，别再往下传 |
## task-t150（`make_release.sh --client` 裁剪模式，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **未裁剪的干净 clone 里 `scripts/check_docs.py` 报 54 条阻断、3 条把守测试红**，全部是 `review/**`（rtv-contracts.md 10 条、dispatch-R0.md 4 条……，共 30 个不同路径；这里刻意**不写成带反引号的完整路径** —— 写了就会给本条描述的那一堆再添两条）。根因：`review/` 靠 `.git/info/exclude` 退出射程，而 `info/exclude` 落在 `.git/` 里、**不随 `git clone` 复制**；`review/paste-*.md` 又全部未跟踪、clone 带不进来，于是引用它们的文档全成 E-missing | **交给评委的提交包 `dist/maos-runtime-<sha>.zip` 解压后 pytest 是 3 红**（`test_docs_guard::test_docs_structure_and_references_are_sound`、`test_rtv_sop_doc::test_two_docs_pass_check_docs`、`test_rtv_sop_doc::test_check_docs_cli_still_exits_zero`）。这与 provenance 9/10 是**两件独立的事**，把 provenance 修绿也不会让这三条转绿。它同时是 `check_docs.py` 模块头那条不变量（「判决不许因为跑在主仓还是跑在 worktree 里而变」）的反例：主仓与 worktree 确实同答案，**clone 里不同** | 本轨白名单只有 `scripts/make_release.sh` 与 `docs/client-manifest.txt`，按铁律 4 没当场改。两条修法：① 把 `review/` 从 `.git/info/exclude` 挪进 `.gitignore`（被跟踪、随 clone 走，一行解决，代价是改变主仓的忽略面）；② 打包时往包内 `.git/info/exclude` 写登记 —— `--client` 已经这么做（见 `make_release.sh` 抬头口径 4b），默认模式照抄即可。建议归 **T153**（它正在修同一片的 provenance），别与本轨的 `--client` 混在一起 |
| 2026-09-14 | p10 | `maos/tests/test_model_routing.py:37` 的 `FAKE_KEY_VALUE = "sk-FAKE0…0"` 会被 `make_release.sh` 密钥自查 B 层的形态正则 `sk-[A-Za-z0-9]{20,}` 判负。实测：本轨修之前，`--client` 与默认模式都报 `[FAIL] 密钥形态命中 1 个文件` | 自 `38eca1a`（p9 角色→模型路由表）起，**两种包的密钥自查恒定 [FAIL]** —— 一条恒红的判据等于没有判据，真密钥混进来时屏幕上看不出区别 | 本轨已在 `make_release.sh` 里加 `FAKE_LITERALS` 登记（按**字面量**登记而非文件名、每条写理由、剔掉几处照样打印计数）解决。留档是因为**根因在测试侧**：那个桩值故意长得像真 key。要从源头解决，可把它拼成 `"sk-" + "FAKE" + "0" * 36`，那样连登记都不需要 —— 但那是 `test_model_routing.py` 的事，不在本轨白名单 |
| 2026-09-14 | p10 | 客户包若不带 `evidence/**/maos.db`，`verify.py` 仍然 10/10 PASS，但**分母掉一半**：hash-integrity 179→114、business-ref 136→61、trace-tree 77→29、case-outcome 51→6（实测） | 解压后那一步只跑 `make_evidence.py`，它只重建 `scenario-*` 那 8 束的库；`case-real-01` / `domains` / `contrast-R8` 三组没有库就只能少核。「RESULT: 10/10 PASS」在两种包里说的不是同一句话 | 已在 `--client` 里处理：带上 `evidence/` 下的库，并把 `.db` 判据从「一个都不许有」换成「只许出现在 `evidence/` 下」。留档是为了让后来者知道 —— **看 RESULT 行之前先看分母** |


## task-t151（客户包里 README 正文会变成假话的 9 处，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **README 正文有 9 处指着客户包里不存在的证据束说话。** T150 的 `--client` 裁掉 `evidence/room/`（无生成器、人工采集）、`evidence/case-real-01/happy-live/`（重产要 API key）、`evidence/case-real-01-pg/`（重产要 PostgreSQL）、`legacy-ts/`，而 README 正文逐一点名了它们：`README.md` 的 125 / 129 / 132 / 250 / 318 / 323 / 370 / 408 / 484 行（本条写入时的行号，会漂；不漂的定位是 `grep -n 'happy-live\|evidence/room\|legacy-ts' README.md`）。九处逐行核过，T150 报的清单属实 | **一条都不会让 `scripts/check_docs.py` 变红** —— 裸文本不算链接，带反引号的那几处因为 T150 把被裁路径登记进了包内 `.git/info/exclude`、按 git 划的射程整个不进。所以这是纯粹的「文档说的和包里有的对不上」：技术读者解压客户包照着 §3 / §6 去找 `evidence/room/` 会扑空，而没有任何守卫会提前告诉他 | **T151 白名单只有 `README-CLIENT.md` 与 `README.md` 顶部导流，正文硬约束明令「一个字不许改」，按铁律 4 没当场改。** 也不归 T150（其白名单只有 `scripts/make_release.sh` 与 docs/client-manifest.txt）—— 本波没有任何一轨拥有 README 正文。**三束要分开判，不能一刀切**（据 T150 2026-09-14 实测，本行已据此重写一次，旧版把「确认没有 key 残留」当成前置条件，是找错了卡点）：三束的密钥形态命中与哨兵反查**都是 0**，密钥不是卡点；卡点是 `scripts/verify.py` 第 9 项 provenance。`evidence/room/`（出处 27c9e18，另有一份 f4ffa76-dirty）与 `evidence/case-real-01-pg/`（出处 6ecc10a）自报 scripted、**进 provenance 的分母**，而判据是「每束自称的出处 == 当前 HEAD」，客户包的 HEAD 是打包时新生成的 orphan commit ⇒ 必红；两束又都重产不出来（room 无生成器、pg 要一台装着 PostgreSQL 的机器）。**留 = 客户一跑核验就从 10/10 掉回 9/10，等于把 T153 正在修的那件事原样搬进客户包 ⇒ 不可留。** `evidence/case-real-01/happy-live/` 自报 model_mode 为 live，verify 按 T128 口径**整束不进十项分子**（真模型每跑一次说的话都不一样，拿它当重放基准第 4 项必红），所以出处旧不影响判定 ⇒ **可留**，代价是包 +188K，且 `scripts/make_release.sh` 里那条「证据出处全部 == 包内 HEAD」自查要放宽成「live 束除外」（T150 的文件，其自称不难）。它也是三束里对业务客户最有说服力的一束：真模型真的打了网络。**结论：happy-live 可留，room / pg 不可留** —— 于是 9 处里指着 room / pg / `legacy-ts/` 的那 6 处仍然无解，只剩「开一轨独占 README 正文，把这几处改成不点名具体路径」一条路，改动面大且会动到 §8 对外口径。**不建议「客户包里另起一份精简 README」**（T150 已撤回该提议）—— 两份 496 行文档必然漂，且 `README-CLIENT.md` 已占该生态位 |
| 2026-09-14 | p10 | **README §3 / §4 承诺的「①② 跑完 RESULT 全绿」只在客户包里成立。** T149 与 T150 两轨独立实测，读数一致而非矛盾：`scripts/make_release.sh` **默认模式**出的包 verify `9/10`（根因是 `--depth 1` 浅克隆里没有证据束自称的出处 sha，provenance 判负，T149 已记 `## task-t149`），**`--client` 模式**出的包 verify `10/10 PASS` exit 0 | README 正文 §3 那段真实输出贴的是 `RESULT: 10/10 PASS`，§4 写「`python3 scripts/verify.py` ② RESULT 行全绿，exit=0」。评委若拿默认模式的提交包去跑，看到的是 9/10 —— 这正是 T151 派单 §5 要求客户文档不许承诺 10/10 的那件事，只是它同样适用于 README 正文 | 根因归 T153（已在修：把 `scripts/make_release.sh` 的 `--depth 1` 改成 `--depth 50`，已与 T150 对过，其实测 depth 50 下客户包的裁剪链结果完全不变）。**修好之前别动 README 正文这两处**：数字本身不假（主仓真跑就是 10/10），假的是打包路径。T153 落地后要连着核一遍：默认包、客户包、主仓三处 verify 读数是否归一。`README-CLIENT.md` 这一侧已按派单 §5 处理，全文不出现任何分子分母，第 1 层只写「跑完会输出一份逐项核验结果并自动打开报告页」 |

## task-t153（全新 clone 核验 9/10 的根因 = 浅克隆，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **交付包里 pytest 恒红 3 条**：`test_docs_guard.py::test_docs_structure_and_references_are_sound`、`test_rtv_sop_doc.py::test_two_docs_pass_check_docs`、`test_rtv_sop_doc.py::test_check_docs_cli_still_exits_zero`。根因（T150 查实，比「文件不入库」更准）：`check_docs.py` 的射程按 git 划（`out_of_scope()` 走 `git check-ignore`），而 `review/` 是靠 **`.git/info/exclude`** 退出射程的 —— 那个文件落在 `.git/` 里，**不随 `git clone` 复制**。副本里规则没了，`review/` 回到射程内，而 `review/paste-*.md` 本就未跟踪、clone 带不进来，于是引用它们的文档全判 `E-missing`（**54 条阻断全部是 `review/**`，30 个不同路径**）。这也正是 `check_docs.py` 模块头那条不变量的反例：「判决不许因为跑在主仓还是 worktree 而变」在主仓与 worktree 之间成立（共享 common dir 的 `info/exclude`），**在 clone 里不成立**。worktree 里全绿，只在 clone / 解压包里红 | `scripts/make_release.sh` 第 2 条口径（解压后 pytest 全绿）过不了，**非 0 退出、产不出可提交的包**。T153 把 provenance 修好之后，这条是打包路上仅剩的两块拦路石之一 | 复赛前必须处理，不属 T153 白名单。**优先照 T150 已实测的那条做**：打包时往包内 `.git/info/exclude` 写登记 —— 它在 `make_release.sh` 的 `--client` 模式里已经实现（该脚本抬头口径 4b），默认模式照抄即可，实测登记后包内判决与主仓逐条一致（44 条全提示、阻断 0），源文档一个字不用改。次选：把 `review/` 从 `.git/info/exclude` 挪进被跟踪的 `.gitignore`（一行解决，但改变主仓忽略面，要先确认没有别处依赖「`review/` 只在主仓被忽略」）。**别直接删文档里的引用** —— 那几处指向的是真的内部契约文档，删掉等于丢线索。完整记录见本文件 `## task-t150` 第 1 条 |
| 2026-09-14 | p10 | `scripts/make_release.sh` 的密钥自查（第 3 条口径）报「密钥形态命中 1 个文件：`maos/tests/test_model_routing.py`」。同一次自查的**哨兵反查是 0 命中** —— 本机真实密钥没进包，命中的是文件里长得像密钥的测试用假值 | 同上：`make_release.sh` 非 0 退出，包打不出来 | 同上，复赛前处理。修法二选一：把那个假值改成一眼看得出是假的形态（如 `sk-TESTONLY-…`），或给正则加一条「测试文件里的显式假值」豁免。**别直接放宽正则** —— 它是铁律 6 的最后一道闸 |
| 2026-09-14 | p10 | `scripts/make_release.sh` 第 2 条口径里那句 `[FAIL] 解压后生成的证据带 -dirty`（实测恒 3 个）**名不副实**：它数的是 `grep -rl '^# generated at.*-dirty' evidence/`，即包内**所有**带 `-dirty` 首行的文件，而那 3 个都是入库时就带着的历史产物、不是这次解压后生成的。**但三者成因不同，修法要分开**：`evidence/capability-matrix.json`（`e0541e9-dirty`）**有生成器** （`scripts/gen_capability_matrix.py`），只是按既定口径不参与每波重产（`fb2100b` 的 commit message 明写「p9 独立维护的产物，上一波重产也没动它」）—— 把它加进重产清单就能消掉 1 个（T150 在客户包那侧已实测：重产后 198 份证据零 `-dirty`）；`evidence/room/roundtable-four-outcomes.json` 与 `.txt`（`f4ffa76-dirty`）则是**真的没有生成器**（`evidence/room/README.md` 开头：「采集人手写，不是脚本产物」），剩这 2 个才是口径打架的部分。**这句只在主干语境成立** —— 客户包（T150 的 `--client`）把 `evidence/room/` 整束裁掉（其客户包裁剪清单第 9 条；该清单文件随 T150 一并合入，本轨尚不存在故不写死路径），于是三个 `-dirty` 各自以不同方式归零：capability-matrix 靠重产、room 两份靠裁剪，合起来才是 T150 实测的「198 份零 `-dirty`」。两边读数都对，差的是语境（本轨实测：只走裁剪链不裁束 -> 剩 2 个） | 与 `verify.py` 口径打架：第 9 项对这两类是**认下的**（room 在 `_MANUAL_BUNDLES` 里 warn 豁免，`capability-matrix.json` 压根不进 provenance 分母），打包脚本却拿同一批文件判负。后果是 `make_release.sh` 非 0 退出，**且这条恒红**——fb2100b 那次跑也是同样的 3 个。三条 FAIL 里这是第三条 | 复赛前处理，不属 T153 白名单。分两步：① 重产清单补上 `scripts/gen_capability_matrix.py`（顺带核一下 `render_trace.py` 有没有漏，两条都是 T150 点名的必带项）；② 判据收窄成「**本次生成的**束带 `-dirty`」，对齐 `verify.py::provenance_anchors` 的束级口径与 `_MANUAL_BUNDLES` 豁免，而不是扫整个 `evidence/`。**别直接把阈值从 0 改成 3** —— 那会让真正的「解压后工作区不干净」再也报不出来 |
| 2026-09-14 | p10 | **上一条那个坑的跨轨版本**：在 md 里用**反引号**写一条别轨尚未合入的路径，`scripts/check_docs.py` 当场判 `E-missing`、本轨 `maos/tests/test_docs_guard.py` 变红。判据 E 只认反引号里的内容，同一条路径写成裸文本就不命中。两个实测实例：① 本轨初稿在 BACKLOG 里用反引号引了 T150 客户包的裁剪清单（那个文件随 T150 才进仓库），提交前跑守卫当场红；② T150 的 `## task-t150` 第 1 条在描述「指向内部派单目录的引用在干净 clone 里全判 E-missing」时，自己用反引号写了那个目录下的两条路径，给它描述的那一堆又添了 2 条（54 -> 56），已于其轨 commit 修掉、实测回到 54 零增长 | 会让**整合前的单轨自检出现看似回归的红**，而它既不是代码问题、也不是谁改坏了。两轨各撞一次说明这不是偶发：只要还在多轨并行、两本账又是追加制，每一轨写账时都可能踩 | **整合期判据（T150 明确让给本条记，不在其轨重复记一遍——同一件事记两处，将来没人知道该信哪条）：凡在 md 里用反引号写别轨尚未合入的路径，本轨的文档守卫必红；合并后自然转绿，合并前不算回归。** 反过来同样要用它防误判：合并后这类红若**没有**转绿，那才是真问题——说明那条路径合并后仍然不存在。写账时的规避办法只有一个字：该路径尚未合入就写裸文本，别加反引号 |


## integrate-p10-h（Wave H 整合期，2026-09-14）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-14 | p10 | **提交包（`make_release.sh` 默认模式）里的 4 个中文文件名，在「按 ZIP 规范解压」的一侧会变乱码，连带核验判负。** macOS 自带 zip 是 Info-ZIP 2.x，不支持 `-UN=UTF8`（实测报 `short option 'N' not supported`），产出的条目 `flag=0x0000`、UTF-8 位未设；按规范解压方须用 CP437 解码，Python 的 `zipfile` 与 Windows 自带解压器都照办，于是 `artifacts/` 下那 4 个名字解成 `maos-σ£║µÖ»Θô╛Φ╖»Θ¬îΦ»ü.html` 一类乱码，git 视原文件为已删除。仓库里的非 ASCII 文件名**只有这 4 个**（判据：`git -c core.quotePath=false ls-files \| LC_ALL=C grep '[^ -~]'`） | 工作区于是在 `evidence/` 之外脏 4 行 -> `make_evidence.py::git_sha()` 写出 `<sha>-dirty` -> `verify.py` 第 9 项 provenance 判负。**整合期实测**：Python zipfile 解压的包 `provenance 7/8`、`RESULT: 9/10`；`unzip` 解压同一个包 8/8、10/10（Info-ZIP 有自己的编码启发式）。也就是说它**只在 Windows 那一侧发作**，而那一侧本机永远验不了 —— 评委若在 Windows 上解压提交包再跑 README 的 ①②，看到的会是 9/10，正是 T153 刚修好的那个数字又以另一个根因回来了 | **客户包已在本次整合期解掉**：`docs/client-manifest.txt` 新增第三节裁掉 `artifacts/`，客户包里一个非 ASCII 文件名都不剩。**提交包没解，也不该照搬这个解法** —— 那个包必须含提交件。三条候选：① 把那 4 个文件改成 ASCII 名（最彻底，但 `artifacts/README.md` 与 `docs/ppt-outline.md` 的引用要跟着改）；② 打包改用 Python `zipfile`（默认写 UTF-8 位，实测 `flag=0x0800`，但它**不保留权限位**，`RUN-ME.command` 的 0755 会丢，需要手设 `external_attr`，风险不小）；③ 在提交说明里写死「用 `unzip` 解压，别用图形界面」（最弱，赌评委照做）。**推荐 ①**。复赛 2026-09-22/23，建议在此之前定。**【2026-09-14 已按 ① 解决】**：四个文件改名为 `maos-proposal.html` / `.pdf` 与 `maos-scenario-trace.html` / `.pdf`，共 14 处引用同步（`artifacts/README.md` 11、`docs/ppt-outline.md` 2、本文件 1）；全仓非 ASCII 文件名归零（判据同本条左列）。约定与原委钉在 `artifacts/README.md` 抬头，防下一个人改回中文名。客户包那侧不受影响：`artifacts/` 仍按「非客户内容」裁掉 |

## task-t161（Shopify 订单适配器 + webhook 验签，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | Shopify 真凭据实跑本轮未做：`maos/tests/test_commerce_shopify.py` 末尾那条实跑用例在缺 SHOPIFY_SHOP_DOMAIN / SHOPIFY_ACCESS_TOKEN / SHOPIFY_WEBHOOK_SECRET 时 skip | 查单与验签目前只有 FakeTransport + 官方文档样例的证据，没有真 dev store 往返 | 人类给出 Partner dev store 凭据后。本机 Python.framework 3.11 缺根证书，UrllibTransport 会报 CERTIFICATE_VERIFY_FAILED，要人类在自己的 shell 里先 export SSL_CERT_FILE="$(python3 -m certifi)" 再跑 |
| 2026-09-23 | p11 | Shopify REST Admin API 已是 legacy（2024-10-01 起；2025-04-01 起新公开应用只许用 GraphQL Admin API），本轨按契约走 REST —— GraphQL 迁移待定 | 若日后要上架 Shopify App Store，查单须迁 GraphQL；GraphQL 查不到单回 200 + null，与基座 404 抛 KeyError 的形状不同 | 决定上架公开应用之前；属契约事件，要总管拍板改基座与契约。出处：shopify.dev REST Admin API 2026-07 / Order 页顶部公告 |
| 2026-09-23 | p11 | `maos/tests/test_shop_callback.py` 的 autouse fixture 在每条用例前后清空**全部**平台的 verifier 登记表 | 整合后按字母序排在它后面的平台测试（tiktok、woocommerce 等）若只靠模块 import 时的那次登记，全量里会拿到 no_verifier / 400；本轨已在自己的 fixture 里只重登记 shopify 规避 | 整合期：要么每个平台测试都自带重登记 fixture，要么把基座样板改成只清它自己登记的 demo（基座文件不归平台轨改） |
| 2026-09-23 | p11 | 规则表只能正向命中：「已发货 + partially_refunded」的 Shopify 单归 shipped，退款维度被发货维度盖住 | 退过款的单只有在未发货时才会抛出去、转人工；已发货的只剩版本漂移兜底，快照若取在退款之后就兜不住 | 下一轮或整合期：让 StatusRule 支持「否决」规则（命中即抛），要改 `maos/tools/commerce/mapping.py`，属契约事件 |
| 2026-09-23 | p11 | verify_shopify 缺 SHOPIFY_WEBHOOK_SECRET 时抛 CredentialMissing，而 ingest() 只接 VerifyError，这个异常会直接冒出 ingest，那条回调不落库 | 凭据没配时 webhook 端点回 500 且没有取证行；七个平台的验签器都是这个形状 | 整合期统一定：给 verify_result 加一档，或在装配处兜住并落库 |
| 2026-09-23 | p11 | Shopify webhook 投递体按订阅时配置的 API 版本序列化（X-Shopify-API-Version 头），与查单用的 `API_VERSION` 常量互相独立 | 订阅版本与 2026-07 不一致时，refunds/create 等投递体的形状可能与本轨核过的不同 | 装配期配置 webhook 订阅时钉同一版本 |

## task-t162（WooCommerce 订单适配器 + webhook 验签，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | **Woo 建 webhook 时发的 ping 不带签名、不带 Delivery-ID，且要求恰好回 200。** 源码 WC_Webhook::deliver_ping()（woocommerce.github.io/code-reference 的 class-wc-webhook）：body 是 `'webhook_id=' . $this->get_id()`，`$args` 里没有 headers 键，于是没有任何 X-WC-Webhook-* 头；响应码 200 以外一律返回 WP_Error（`if ( 200 !== $response_code )`），`set_pending_delivery( false )` 不会执行。文档（woocommerce.com/document/webhooks/）：「The first time you save a webhook with the Active status, WooCommerce sends a ping to the Delivery URL.」 | 现在的 ingest() 对 ping 回 400（没签名头 → VerifyError → bad_signature），商家在后台保存 webhook 时会看到 Delivery URL 报错 | 整合期接 HTTP 路由时，在调 ingest() 之前识别 ping（无签名头、body 形如 webhook_id=N）并回 200；本轨不改 shop_callback.py |
| 2026-09-23 | p11 | **Delivery-ID 重投时会变，且同一秒内会撞车。** 源码 `get_new_delivery_id()` 返回 `wp_hash( $this->get_id() . strtotime( 'now' ) )`，每次 `deliver()` 调用现算。文档没提自动重投机制，只写了连续失败后自动停用（开发者文档 REST API / Webhooks：After 5 consecutive failed deliveries ... the webhook is disabled；woocommerce.com Webhooks 页：more than five consecutive delivery failures，失败指 2xx / 301 / 302 以外的响应） | ① 任何重投（含后台手工重发）都拿到新 Delivery-ID，按它去重挡不住，只会多回源一次（多一次查单，可接受）；② 分辨率是秒：同一个 webhook 在同一秒内的两次不同投递（大促时同一单一秒改两次）Delivery-ID 相同，第二次被判 duplicate、不回源，漏的这次由下一次执行前 order.query 的漂移判据兜底 | 整合期评估是否改用 Delivery-ID 加 body 摘要的复合去重键；这要改 shop_callback 的去重键组成，不在本轨 |
| 2026-09-23 | p11 | **date_modified_gmt 只到秒。** mapping.py 选毫秒的理由是「大促同一秒内两次改单」，Woo 这边平台只给到秒，适配器补不出来 | 同一秒内两次改单派生出同一个 version，漂移判据会漏报这一次（本轨未查到 Woo 订单有更细的修改时间字段） | 整合期 TΩ 写进各平台能力差异表；有需要再另找字段 |
| 2026-09-23 | p11 | **非 HTTPS 站点、会剥掉 Authorization 头的主机都接不进来。** 文档 REST API v3 / Introduction / Authentication：纯 HTTP 要 OAuth 1.0a；「Occasionally some servers may not parse the Authorization header correctly」时给的退路是把 key/secret 放 query 参数，本轨故意不走（见 DECISIONS 同名小节） | HTTP 站点查单直接抛 ValueError；剥头主机会收到 401（CommerceError，平台码通常是 consumer key 缺失一类） | 有真实商家撞上再做：OAuth 1.0a 签名单开一轨；剥头主机优先让商家修服务器配置 |
| 2026-09-23 | p11 | **REST API 要求站点开启 pretty permalinks。** 文档 REST API v3 / Introduction / Requirements：「Pretty permalinks in Settings > Permalinks so that the custom endpoints are supported. Default permalinks will not work.」 | 用默认固定链接的站点，wp-json 路径会 404，被基座读成 KeyError「订单不存在」—— 排查方向是反的 | 整合期写进客户接入说明的前置条件；或接入自检先探一次站点的 wp-json 根 |
| 2026-09-23 | p11 | **故意不映射的五个状态（pending / on-hold / failed / refunded / trash）谁来定。** 理由见 DECISIONS 同名小节 | 这些状态的订单查单会抛 UnmappedOrderStatus，退款流程停下转人工；refunded 最可能被问到。四态里没有合适的目标，想加第五态违反铁律 9 | 整合期由用户 / TΩ 拍板；要改就是往规则表补带 source 的条目，同时改测试里「不映射」那组参数 |

## task-t163（eBay 订单适配器 + 通知验签，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | **本机未装 cryptography，正向 ECDSA 用例（本轨 K=2 条里的那两条：test_verify_valid_ecdsa_signature_accepted、test_verify_tampered_signature_rejected）本机从未跑过。** `maos/tools/commerce/ebay.py` 里的 load_ecdsa_backend（真正调 cryptography 验签的那一段）在本机一次都没执行过；本机跑通的只有结构判错、缺依赖拒绝、替身验签三条路径 | 真 ECDSA 这条路（公钥 DER 加载、SHA1 + ECDSA、DER 签名）目前只有代码审阅、没有实跑证据。装了 cryptography 的机器上那两条会从 SKIPPED 变成必须 PASSED，写错了才第一次暴露 | M2 接线前：用户授权 python3 -m pip install 'maos[ingress]' 后在本机重跑 `maos/tests/test_commerce_ebay.py`，两条须为 PASSED；装依赖属 CLAUDE.md「必须问」第 3 类，本轮没装 |
| 2026-09-23 | p11 | **本轨的 eBay 平台事实全部来自 developer.ebay.com 的检索摘要，没读到任何一页原文**：WebFetch 打该域下每一页都是 HTTP 403。只靠摘要定下的有：CancelStateEnum 除 NONE_REQUESTED 以外的取值（没核到，所以没写进规则表）、getOrder 对不存在单号回 404 还是 400 + errorId 32100、ORDER_CONFIRMATION 与 ITEM_MARKED_SHIPPED 的 payload 里订单号的路径、X-EBAY-SIGNATURE 信封里 alg 与 digest 字面的大小写 | 任何一条摘要转述有偏差，就是「测试全绿、真打 eBay 才错」：订单号路径错 → 验签过了但 order_ref 为空、永不回源；404 口径错 → 查无此单抛成 CommerceError 而不是 KeyError | M2 接 sandbox 时逐条复核：①getOrder 喂一个不存在的单号看状态码；②用 Notification API 的 subscription test 方法收一条真通知，核对 notificationId、topic、订单号路径与签名头字面；③读 CancelStateEnum 原文页，若有表示已取消的取值，再补一条带 source 的规则（排在付款前） |
| 2026-09-23 | p11 | **「买家已申请取消、卖家还没处理」的单在四态里没有表达。** 规则表只认 cancelledDate，cancelState 处于申请中的单会落到发货 / 付款维度，被判成 shipped 或 paid | 退款链路可能对一笔正在走取消流程的单按「已付款」往下走。四态里没有一个能诚实表达「取消申请中」，补规则等于硬塞；要表达就得加第五态或在别处拦，那是铁律 9 要停下来问的事 | 整合期（TΩ）或 M2 与 T161 的 Shopify 一起定：是否需要一个「有未决取消申请就转人工」的前置判据（不走 StatusRule，放在 order.query 之后） |
| 2026-09-23 | p11 | **退过款但已发货的单被判成 shipped。** 顺序是取消 > 发货 > 付款，所以 FULFILLED + FULLY_REFUNDED / PARTIALLY_REFUNDED 在发货那条就命中了，退款维度被盖住 | 与基座参照表同形（Shopify 的 refunded + fulfilled 也是 shipped），不是本轨独有；但对退款链路来说，「已经退过一部分」恰恰是最该让上层知道的事 | 整合期由 T166 的跨平台一致性视角统一定：规则表只能「命中即返回」，表达不了「这个值出现就抛」，要改得动基座（`maos/tools/commerce/mapping.py`），属停手问人类的事 |
| 2026-09-23 | p11 | **Notification API 里没找到订单取消 / 退款类的 topic**（本轮核到的订单相关 topic 只有 ORDER_CONFIRMATION 与 ITEM_MARKED_SHIPPED） | 订单被取消、被退款这两类变化收不到推送，只能靠执行前 order.query 主动拉；两次执行之间发生的取消 MAOS 看不见 | M2 接线时定：是否另接 Post-Order API 的取消查询做轮询，或接受「只在执行前拉一次」 |
| 2026-09-23 | p11 | **两种令牌都是短命的，本轨只读、不负责续期。** EBAY_APP_TOKEN（client credentials 应用令牌）与 EBAY_OAUTH_TOKEN（用户令牌）按官方说明都是约两小时过期；用户令牌要靠 refresh token 续 | 凭据到位后跑不了多久就会 401（查单）或取公钥失败（通知全判 bad_signature）。后者尤其隐蔽：通知照常落库，只是 verify_result 全是 bad_signature | M2 装配时定谁来签发与刷新（client id / secret 与 refresh token 属新增凭据，同样只许走环境变量）；EBAY_APP_TOKEN 本身也不在契约 §D.2 表里，要人拍板 |
| 2026-09-23 | p11 | **契约表里的 EBAY_VERIFICATION_TOKEN 本轨没用上。** 它服务的是通知端点的 challenge 握手（eBay 建订阅时先 GET 一次端点、要求回一个哈希），那是 HTTP 路由那一侧的事 | 没有握手，eBay 那边建不成推送订阅，本轨的验签器在生产上收不到任何一条通知 | M2 加 HTTP 路由时一并实现握手，本轨不做（派单「不做什么」明列） |

## task-t164（Amazon SP-API 订单适配器，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | T164 Amazon SP-API 是 B 档、没有真凭据，适配器只在 `FakeTransport` 上验过（请求构造、LWA 换令牌、字段映射、错误读法） | 请求头取舍（x-amz-date / user-agent）、LWA 错误体的实际措辞、429 时平台实际给的响应头都还没见过真货 | 凭据到位后：装配处注入默认 transport，用一笔真单跑一次 `query()`，核五字段与 LWA 换新时序 |
| 2026-09-23 | p11 | T164 SP-API 订单通知（Notifications API 投 SQS / EventBridge）未接 | Amazon 的订单变化只能在查单时读到，没有推送入口。将来接的话，verifier 必须在自己测试的 fixture 里重新注册：`maos/tests/test_shop_callback.py` 的 autouse fixture 每条用例前后都 reset_verifiers，依赖导入期注册会拿到 VERIFY_NO_VERIFIER | 需要订单推送时另开一轨 |
| 2026-09-23 | p11 | T164 Orders API v0 已宣布弃用：官方 SP-API Deprecations Schedule 写明 getOrder 等六个 v0 操作 2026-01-28 弃用、**2027-03-27 起调用失败**，接替者 Orders API v2026-01-01 | `maos/tools/commerce/amazon_sp.py` 按 v0 写（路径与字段名），到期后 Amazon 查单全部失败 | 2027-03-27 之前迁到 v2026-01-01：路径、响应形状、字段名都要按新模型 orders_2026-01-01.json 重核，规则表 source 同步改；跨轨契约 §B 表里 Amazon 那一句也要跟着核 |
| 2026-09-23 | p11 | T164 已取消的单可能读不出来：官方模型里 OrderTotal 不是 required；Pending 单不返回定价是写明了的，Canceled 单文档没写，但官方仓 selling-partner-api-models 讨论区 #3657 有开发者实报 Canceled 单缺 OrderTotal | `query()` 先 map_status 后 normalize_amount：这类单状态能映射成 cancelled，却会在金额一步抛 ValueError —— 「订单已被取消」这个恰恰最该让退款停下的事实读不出来，上层只看到「读单失败」 | 基座层面的后果，本轨不修（改基座越界）。整合期或基座轨定：cancelled 单缺金额时要不要也能交出去，需用户拍板 |
| 2026-09-23 | p11 | 基座 `maos/tools/commerce/base.py:110-112` 的注释说「Amazon SP-API 用 400 + InvalidInput 表达找不到单」，与官方模型不符（getOrder 的 404 =「The resource specified does not exist.」，400 是参数错） | 只是注释失实，行为没错（缺省 404）；但后来人照注释把 Amazon 的 not_found_status 扩成 400 + 404，会把参数错误判成「订单不存在」 | 下次动基座时改注释（基座不在 T164 白名单） |
| 2026-09-23 | p11 | 派单判据里 grep 数 PASSED / FAILED 的写法对彩色输出失效：会话环境带 FORCE_COLOR=3 时 pytest 给状态词包 ANSI 码，T164 的 C6 首跑把 32 条通过数成 0 | 判据会在「全过」时报「全缺」，或在有红时报 0 条 FAILED —— 两个方向都失去区分力 | 下一版派单模板：判据里的 pytest 命令加 `--color=no`，或前缀 `env -u FORCE_COLOR` |

## task-t165（TikTok Shop + Shopee 订单适配器，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | **TikTok webhook 的签名头落库时会被抹掉**：`maos/ingress/shop_callback.py:256` 的 `_SECRET_HEADERS` 含 `authorization`，而 TikTok Shop 把 webhook 签名放在 `Authorization` 头里（Webhooks Overview 原文「TikTok Shop sends the webhook signature in the HTTP Authorization header」）。同一模块 `maos/ingress/shop_callback.py:254-255` 写着「签名头要留，那是复算验签的必要材料，也是攻击取证的核心」，两处冲突 | `shop_callback` 表里 TikTok 回调的签名一律是 `<redacted>`，验签失败的那几行（攻击证据）再也复算不出来。Shopee Push 若证实也用 `Authorization`，同样中招 | 基座文件，本轨不改。整合期或基座 owner 定：按平台登记「这个平台的 Authorization 是签名不是凭据」的豁免，或把签名另起一列存。改之前先确认没有平台真把凭据放在 Authorization 里推过来 |
| 2026-09-23 | p11 | 跨轨契约 §E.5（主仓 review/p11-commerce-contracts.md，不在本 worktree 里）说「HMAC-SHA256 + base64 三家共用（Shopify / Woo / TikTok）」，TikTok 这一项不对：官方文档写的是小写十六进制，两份官方样例本机复算一致。`maos/ingress/shop_callback.py` 里 `hmac_sha256_base64` 的 docstring 也点名了 TikTok Shop | 照契约写的人会用 `hmac_sha256_base64` 验 TikTok，所有真回调都判 `bad_signature`；T166 的一致性测试若按契约断言「TikTok 走 base64」，会红在本轨上 | 整合期更正契约与那条 docstring。本轨已按文档写 hex，理由见 `docs/DECISIONS.md` 的 `## task-t165` |
| 2026-09-23 | p11 | **Shopee Push 验签没做**（本轨 blocked 的那一半）：推送的签名规则、签名串含不含回调 URL、有没有逐条投递 id 都写在开发者指南里，而 `https://open.shopee.cn/developer-guide/32` 等指南页是 JS 空壳、数据接口的参数没摸出来；`open.shopee.com` 对抓取工具直接拒绝 | Shopee 回调进不来：`ingest` 对 `shopee` 会判 `no_verifier` 并回 400。查单不受影响 | 找能开浏览器的会话或人工读 `https://open.shopee.cn/developer-guide/16`（签名）与 `https://open.shopee.cn/developer-guide/32`（Push），核实后另开一轨补 verifier、登记和三条 webhook 用例；签名串若含回调 URL，再新增 `SHOPEE_PUSH_CALLBACK_URL`。顺带核实查单签名串「不加分隔符」那条 |
| 2026-09-23 | p11 | Shopee 适配器**不会报 `paid`**：`READY_TO_SHIP` / `PROCESSED` 因货到付款单在这两态尚未付钱而没进表 | Shopee 上「已付款未发货」的单查回来一律抛 `UnmappedOrderStatus`，下游转人工 | 若要收：要么确认目标市场不开 COD，要么让规则能按「`order_status` 且 `cod` 为 false」组合命中 —— 后者要改 `maos/tools/commerce/mapping.py`，是基座改动，得先停下来讨论 |
| 2026-09-23 | p11 | TikTok 两处状态拼写不一致：查单接口 Get Order Detail (202309) 的取消态是 `CANCELLED`，而 webhook「(1) Order status change」页列出的取值是 `CANCEL` | 现在不影响：规则表只用于查单响应，webhook 只触发回源、不读状态。日后若有人拿 webhook 的 `order_status` 直接走规则表，取消单会抛 `UnmappedOrderStatus` | 真要读 webhook 状态时再处理，届时先拿真回调核实实际拼写 |
| 2026-09-23 | p11 | 后台会话（`claude --bg`）带 `FORCE_COLOR=3`，pytest 输出带 ANSI 码；派单模板里按行首 FAILED 计数、按「名字 PASSED」计数的 grep 会**静默数到 0**，看起来像「没有失败」或「一条都没过」 | 派单判据在后台会话里失真，而且不报错 | 下次写派单时在 pytest 命令前统一加 `NO_COLOR=1`（本轨已这样跑，见 `docs/DECISIONS.md` 的 `## task-t165`） |
| 2026-09-23 | p11 | `docs/expected-metrics.json` 的采集总数要刷：本轨新增 47 条测试（TikTok 26 + Shopee 21，skip 0），`maos/tests/test_expected_metrics.py` 的 `test_collected_count_matches_source_of_truth` 在本轨恰好红这一条、差 +47 | 本轨按派单不改该文件，单轨全量因此恒有 1 条红 | 整合轨 TΩ 合并六轨后统一刷 |

## task-t166（跨平台一致性守卫；Lazada 适配器待授权，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | **一致性测试在本 worktree 里只扫得到 0 个真实适配器。**`maos/tests/test_commerce_consistency.py` 的判据 1–8 对真包是空跑的（发现结果为空，空集合上断言成立），它们的牙只由本文件的合成用例与派单的两个一次性探针（撞名、导入期读凭据）证明过 | 真对象上的第一次判定发生在整合期：六轨合流后才第一次有平台模块被扫。**判据 6 的扩展禁单（aiohttp / urllib3 / urllib.request / http.client）与判据 8（导入期不读凭据 / 环境变量）是本轨在契约之外加的**，别轨派单没写、无从预知，整合期因这两条红的概率最高；判据 3 的口径也只在合成用例上验过。另：判据 1 要求**每个**被发现的子类都声明 platform 与 status_rules，平台文件里若另写一个不带 platform 的抽象中间类，也会被判 1 点名 | 整合轨 TΩ：① 另加「发现数 == 7」的精确断言（本轨按派单不许加下限，只有整合期能加）；② 一致性测试若红，按报错点名的模块**退回所属轨**修，**不许在一致性测试里加豁免** —— TΩ 没有白名单去改别轨文件；退回时点明判据 6 的扩展禁单与判据 8 是契约外判据，免得对方以为是自己漏看了契约 |
| 2026-09-23 | p11 | 派单里「grep PASSED 计数」「grep ^FAILED 计数」这类判据，在环境变量 FORCE_COLOR 非空的会话里恒为 0：pytest 即使输出重定向到文件也照样着色，颜色码插在 test id 与 PASSED / FAILED 之间。本轮 T166 会话实测 FORCE_COLOR=3 | 照原文跑的子会话会把一次全绿读成「0 条 PASSED」、把「恰好 1 个 FAILED」读成 0，按派单停机或报假警；本轮本轨已按 DECISIONS 同日一行处理（加 `--color=no`） | 下一轮派单模板：凡把 pytest 输出落日志再 grep 的命令，一律带 `--color=no`（或在命令前加 NO_COLOR=1）；起草员在自己会话里实跑取期望值时也要带上，否则起草环境与子会话环境着色不同会出现两边读数对不上 |

## integrate-p11（p11 整合期：跨境电商六轨合入 integrate/p11，2026-09-23）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-23 | p11 | **Lazada 适配器没做**（task-t166 派单的第二件事，该轨小节标题记为「Lazada 适配器待授权」）：开放平台文档只能经 g.alicdn.com 渲染，本仓库会话一律不开阿里系域名；用户 09-23 在总管会话拍板延后（「Integrate 6, defer both」） | 七个平台缺一；整合期的精确断言按 6 个平台判，Lazada 不在其中是拍板后的预期 | 用户另行授权读文档的途径之后另开一轨，同时把整合断言改成 7 |
| 2026-09-23 | p11 | **Shopee 推送验签没做**：推送的签名规则写在读不到的开发者指南里（JS 空壳页、抓取被拒），只交了查单。见 ## task-t165 第 3 条 | `ingest` 对 shopee 判 no_verifier、回 400；Shopee 的订单变化只能靠执行前查单拉取 | 同上条，文档可读之后另开一轨补 verifier、登记与 webhook 用例 |
| 2026-09-23 | p11 | **eBay 正向 ECDSA 用例 2 条恒 skip**：本机没装 cryptography（可选依赖 maos[ingress]），真 ECDSA 验签这条路从没实跑过。见 ## task-t163 第 1 条 | 全量 skipped 的 106 条里含这 2 条；装了依赖的机器上它们必须 PASSED，写错了才第一次暴露 | 装依赖属 CLAUDE.md「必须问」第 3 类，用户授权 pip install 之后在本机重跑 `maos/tests/test_commerce_ebay.py` |
| 2026-09-23 | p11 | **`maos/tests/test_shop_callback.py` 的 autouse fixture 每条用例前后调 reset_verifiers()，会清掉平台模块导入期登记的 verifier**；整合后 `maos/tests/test_commerce_ebay.py` 与 `maos/tests/test_commerce_woocommerce.py` 自己的 autouse fixture 前后也调它（清的是全部平台）。有 verifier 的四个平台测试都在自己的 fixture 里重登记本平台，所以整合后全量与反序跑都无红；基座层面没修。见 ## task-t161 第 3 条、## task-t164 第 2 条 | 以后新增的平台测试若只靠导入期那次登记，排在这几份之后跑就会拿到 no_verifier / 400，且按字母序的全量不一定看得出来 | 下次动基座样板时改成只清它自己登记的 demo；在那之前，新平台轨的派单照本轮写法要求自带重登记 fixture、不许清别人的登记 |
| 2026-09-23 | p11 | **真凭据实跑一次都没做**，且本机 Python.framework 3.11 缺根证书：拿真凭据跑时 UrllibTransport 会报 CERTIFICATE_VERIFY_FAILED，要人类在自己 shell 里先 export SSL_CERT_FILE 指向 certifi 的证书包。见 ## task-t161 第 1 条、## task-t164 第 1 条、## task-t163 第 2 条 | 六个平台的查单（以及其中四个平台的验签）目前只有 FakeTransport + 官方文档样例的证据，eBay 连原文页都没读到、只有检索摘要 | 人类给出任一平台的沙箱 / dev store 凭据后，逐平台跑一次真往返；本轮不许设置任何凭据 |
| 2026-09-23 | p11 | **六个适配器都还没装配**：register_order_system 的调用点、HTTP 入口（按平台分的 callback/shop 路由）、「平台来一笔退款就自动启动流程」都不在本轮。同属路由侧的还有 Woo 建 webhook 时的无签名 ping（## task-t162 第 1 条）与 eBay 建订阅时的 challenge 握手（## task-t163 第 7 条） | 适配器目前只能在测试里被调用，真实平台的回调进不来、退款流程也不会被自动拉起 | 下一里程碑 M2 |
| 2026-09-23 | p11 | **规则表只能「命中即返回」，表达不了「这个值出现就转人工」**：已发货又退过款的单被判 shipped（## task-t161 第 4 条、## task-t163 第 4 条），买家已申请取消、卖家未处理的单落到发货 / 付款维度（## task-t163 第 3 条） | 对退款链路来说「已经退过一部分」「正在走取消」恰恰最该让上层知道 | 要改 `maos/tools/commerce/mapping.py`（否决规则或前置判据），属契约事件，先停下来问用户 |
| 2026-09-23 | p11 | **验签器缺凭据时抛 CredentialMissing，而 ingest() 只接 VerifyError**，异常直接冒出去、那条回调不落库。见 ## task-t161 第 5 条 | 凭据没配时回调端点回 500 且没有取证行，有 verifier 的平台都是这个形状 | M2 装配时统一定：给 verify_result 加一档，或在装配处兜住并落库 |
| 2026-09-23 | p11 | **签名放在 Authorization 头的平台，签名落库时会被抹成 redacted**（`maos/ingress/shop_callback.py` 的敏感头名单含 authorization），TikTok 已确认如此；另跨轨契约 §E.5 对 TikTok 签名编码的说法与官方文档不符（官方是小写十六进制）。见 ## task-t165 第 1、2 条 | 验签失败的那几行（攻击证据）复算不出来；照契约写 TikTok 验签会全判 bad_signature | 基座 owner 定：按平台登记豁免或另起一列存签名；契约与 hmac_sha256_base64 的 docstring 一并更正 |
| 2026-09-23 | p11 | **平台 API 版本与弃用**：Shopify REST Admin API 已是 legacy、webhook 投递体按订阅版本序列化（## task-t161 第 2、6 条）；Amazon Orders API v0 2027-03-27 起调用失败（## task-t164 第 3 条） | 到期或上架公开应用时查单整片失效 | Amazon 须在 2027-03-27 之前迁 v2026-01-01；Shopify 在决定上架 App Store 之前定 GraphQL 迁移（契约事件） |
| 2026-09-23 | p11 | **各平台状态映射的留白待用户拍板**：Woo 五个状态不映射（## task-t162 第 6 条）、Shopee 不报 paid（## task-t165 第 4 条）、TikTok 取消态拼写两处不一（## task-t165 第 5 条） | 这些状态的单查回来一律抛 UnmappedOrderStatus、转人工 | 用户逐条拍板；要收就往规则表补带 source 的条目，组合条件要改基座 |
| 2026-09-23 | p11 | **平台接入的前置条件与推送面缺口**：Woo 的 Delivery-ID 与修改时间都只到秒、要 HTTPS 与 pretty permalinks（## task-t162 第 2–5 条）；eBay 没有取消 / 退款类通知、两种令牌约两小时过期（## task-t163 第 5、6 条）；Amazon 订单通知未接、已取消单可能缺 OrderTotal、基座 base.py 那段注释与官方模型不符（## task-t164 第 2、4、5 条） | 分别影响去重精度、接入门槛、推送覆盖与读单成功率，详见各条 | 客户接入说明与 M2 装配时逐条处理；改基座的部分先问 |
| 2026-09-23 | p11 | **派单模板：后台会话带 FORCE_COLOR=3，pytest 日志带颜色码，按 PASSED / FAILED 计数的 grep 静默为 0**。见 ## task-t164 第 6 条、## task-t165 第 6 条、## task-t166 第 2 条 | 判据在「全过」时报「全缺」，或有红时报 0 条 FAILED | 本轮整合派单的 pytest 命令已一律带 --color=no；下一版派单模板照此写 |
| 2026-09-23 | p11 | **已办（记一笔以便对账）**：## task-t165 第 7 条（真源条数待刷）本轮已刷到收集 4379；## task-t166 第 1 条（一致性判据在真对象上的第一次判定）本轮合流后判据 1–8 对 6 个真平台全绿，精确断言按用户拍板为 6 而非该条建议的 7 | 无 | 无 |

## p12-contracts（p12 契约期：客服前台读图时查到的存量问题，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **外部渠道的命令路径外泄内部内容**：wechat_kf 上 /refund 回内部预检卡（含 /approve 提示与审批岗）、/help 回内部 USAGE、命令出错时附 USAGE、其它异常把异常原文回给客户（router.py 的 handle 命令分支）；p12 的前台只接非命令文本，这条路没动 | 客户看得到内部命令、审批岗与规则编号 | p13 退款预检桥（外部渠道的命令改走前台） |
| 2026-09-24 | p12 | **scripts/run_ingress.py serve 的库恒为 :memory:**，docstring 提到的 --db 选项并不存在 | 进程一重启，会话、转人工卡片、入站幂等键全丢；企微客服游标也只在内存里，只剩 KF_MAX_AGE 挡重放 | p13（前台真上线前） |
| 2026-09-24 | p12 | **scripts/make_evidence.py 的密钥名正则漏掉 *_AES_KEY / *_ENCRYPT_KEY / CORP_ID**（MAOS_WECHAT_KF_AES_KEY、MAOS_WECOM_AES_KEY、MAOS_FEISHU_ENCRYPT_KEY） | 这些值既不脱敏也不进扫描哨兵（铁律 6） | 下一次渠道配置可能进证据束之前 |
| 2026-09-24 | p12 | **客户侧状态措辞在 projection 之外还有两个出口**：notify.customer 在 projection 返回空串时回落 _INTERNAL_SAID（其中 settled 就是「退款已到账」），payload 的 content 能整段绕过 projection | 与「对外措辞唯一产出处是 projection.py」的口径不一致 | p13 前台接退款状态时一并收 |
| 2026-09-24 | p12 | **企微客服取图未实现；附件入库对外部渠道没有渠道闸** | 客户发图进不来；将来进来了会进内部证据缓冲 | p13 |
| 2026-09-24 | p12 | **企微客服会话转接（kf/service_state/trans）没封装** | 转人工只能发内部卡片，微信侧会话仍挂在机器人上，人工要自己去客服工作台接 | p13 / p14 |
| 2026-09-24 | p12 | **obs/trace.py 与 scripts/verify.py 没有 cs 树族**，cs: 前缀的 event_log 行进证据束就是游离事件 | CS 行只能留在 cs 自己的库里，不进证据束 | p14（与 verify 新项同批） |
| 2026-09-24 | p12 | **kb_doc 的 kind CHECK 是 IF NOT EXISTS**，加 cs_script 到不了已存在的持久库（同 task-t118 那条） | 旧 PG / 文件库写 cs_script 会撞 CHECK | 前台接持久库之前 |
| 2026-09-24 | p12 | **IngressServer 只有一个工作线程；企微客服 sync_msg 在 HTTP 线程里拉、不按 has_more 翻页** | 前台慢会拖住内部 /approve；积压超过一页的消息会漏 | p13 |
| 2026-09-24 | p12 | **WhatsApp 没有 adapter**（方案 §9-2 的跨境候选） | 跨境方向的首发渠道缺位 | 用户定跨境渠道时 |
| 2026-09-24 | p12 | **docs/ops/ORCHESTRATION.md 停在 2026-08-29**，p10 起没更新 | 编排现状要去 phase 文档与两本账里拼 | 编排总管有空时（本会话无权写它） |

## task-t167（会话对象与新表，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **_dbport.DomainConn 的 sqlite 事务深度记在实例上**：atomic 块里再 DomainConn.open 一个新实例执行写入（例如块内调某域 objects.execute），新实例不知道自己在事务里、当场 commit，保存点被提前提交；块内随后抛错时 ROLLBACK TO 报 no such savepoint，已写的行全部留下（本轨 scratchpad 探针实测：块内两行、抛错后两行都在） | 在事务块里调各域 execute / query 薄壳的代码会静默失去原子性；cs 的 transaction 目前靠 docstring 约束「块内只用 yield 出来的那个连接」 | 下一次有域需要嵌套事务之前；_dbport 是共享底座，改之前先问 |
| 2026-09-24 | p12 | **cs 会话事实与 Cs* 审计行不同事务**（见 DECISIONS 本节第 1 条）：进程在「会话表已提交、append_event_log 还没落」之间崩掉，会留下一轮没有 CsTurnRecorded 的 cs_turn | event_log 条数与 cs_turn 行数可能差一，审计对账会差 | p14 做 cs 树族与 verify 新项时加一条「cs_turn 行数 = CsTurnRecorded 条数」的对账 |
| 2026-09-24 | p12 | **STAGE_EXTRA_REASONS 目前只有 human_released**（maos/domain/cs/conversation.py）：p12 只走 active → handed_off（原因取 HANDOFF_REASONS），handed_off → active、→ closed 两条路径 p12 没有调用方 | 以后接这两条路径时若传别的原因字眼，审计行里只剩 sha256 摘要、读不出原因（不泄原文，但不可读） | p13/p14 接人工交还 / 结束会话时，把用到的原因字眼追加进 STAGE_EXTRA_REASONS 并补测试 |
| 2026-09-24 | p12 | **反向验证脚本会读到上一个变异的旧 .pyc**：变异脚本用 shutil.copy2 还原源文件（连原 mtime 一起还原），两个长度相同的变异又在同一秒内写入时，Python 按「mtime 秒 + 文件大小」判 .pyc 新鲜，第二个变异直接用了第一个的字节码。本轨复核第二轮实测：N3（change_stage 忽略 now）与 N4（mark_handoff_delivery 忽略 now）报出一模一样的失败清单；每个变异单独设 PYTHONPYCACHEPREFIX 之后，N4 才红在它该红的那条上。上一轮 10 个变异隔离后重跑，结论不变 | 各轨与复核的反向验证若也这么写，「变异红」可能是上一个变异红的，「变异绿」可能是上一个变异绿的，判负结论不可信 | 下次派单写反向验证要求时，在契约 §3 测试口径里加一句「每个变异单独设 PYTHONPYCACHEPREFIX（或每次跑前删掉被变异模块的 .pyc）」 |

## task-t168（话术 kind 与话术库，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | 话术库 holdout 实测：57 条改写里 2 条排错（「偏远地区邮费另算吗」排到 LOG-002、「发票在哪里开呀」排到 RET-002），另有 6 条判对但分数低于 MIN_SCRIPT_SCORE（如「能指定发中通快递吗」0.153、「买一件包不包邮」0.208） | 这些说法到前台会走兜底，连续两轮就转人工；T170 的评测句若落在同类说法上，intent / route 准确率到不了 1.0 | W-B（T169 合流后按契约只许补同义词与例句，不许抄评测句；补完重跑 scripts/gen_cs_kb.py 与 maos/tests/test_cs_kb_t168.py） |
| 2026-09-24 | p12 | GEN-003（人工客服的服务时间）与转人工触发词撞面：客户问「人工客服几点上班」会先命中 requested 触发词（契约 §1.4 判定顺序第 3 步），走不到检索 | GEN-003 只有不带「人工客服 / 转人工 / 找人工 / 真人」的问法才答得到；评测若拿带触发词的句子考 GEN-003 会判成 handoff | W-B（T169 定触发词表时、T170 出题时各看一眼） |
| 2026-09-24 | p12 | cs 的 KbRetrieved 里 detail.docs[].score 是重排分，而 channels 是知识层四通道信号，两者对不上（加权和 ≠ score） | 将来按 channels × weights 复算分数的读者（trace、verify 第 5 项若加这类校验）会在 cs 行上判不一致 | p14（建 cs 树族与 verify 新项时，给 cs 行单列口径或在 detail 里加重排说明字段） |
| 2026-09-24 | p12 | 复核轮重测 holdout（本节第 1 行的数字作废）：76 条改写里 13 条到前台会兜底或答错 —— 「偏远地区邮费另算吗」以 0.400 排到 LOG-002（过门槛，会答成配送范围的话术）、「谢了哈」一篇都检不出；另 11 条判对但低于 0.25（能指定发中通快递吗 0.077、买一件包不包邮 0.104、运费是怎么收的呀 0.143、包邮不亲 0.205、改个地址 0.223、能刷卡么 0.145、为啥付了钱订单还是没付款 0.209、付不了钱咋办 0.190、发票在哪里开呀 0.146、哈喽在不在 0.248、行那先这样拜拜啦 0.129） | 短问法和只对上一个实词二元组的说法落兜底，连续两轮就转人工；偏远地区邮费那条是过门槛的错答；T170 的评测句若落在同类说法上，intent / route 准确率到不了 1.0 | W-B（T169 合流后按契约只许补同义词与例句，不许抄评测句；LOG-003 先补「偏远地区邮费 / 邮费另算」，GEN-002 补「谢了」；补完重跑 scripts/gen_cs_kb.py 与 maos/tests/test_cs_kb_t168.py） |
| 2026-09-24 | p12 | 纯应答（好的 / 知道了 / 没有了 / 可以）现在低于门槛、走兜底 | 客户每回一句「好的」都算一轮兜底，连着两句就触发 repeated_fallback 转人工 | W-B（T169 定前台判定顺序时考虑应答轮：单独回一句客气话或不计入 fallback_streak；契约 §1.4 的判定顺序若要动，由人拍板） |
| 2026-09-24 | p12 | 「我不想活了」这类自伤表达不在任何触发词里，检索侧现在给 0 分、走兜底 | 高风险表达拿到的是「没听懂」式的兜底，而不是转人工 | W-B（触发词表是契约 §1.4 冻结的五类，要加一类需人拍板；T169 / T170 各看一眼） |
| 2026-09-24 | p12 | 重排的虚词字表是手写闭集，近域 holdout 上无关句最高 0.208、门槛 0.25，余量约 0.04；带两个实词二元组的售前问句仍可能过门槛 | 真实流量里的近域说法比 holdout 多，误命中率只能靠采样量出来 | p14（接真实会话后按周抽样误命中的回合，回灌 holdout 再调） |
| 2026-09-24 | p12 | 问状态的句子里实词全是政策话术的说法、「问状态」只靠虚字「了」表达时，字符二元组分不开：「我的钱退到卡里了吗」→ PAY-001 0.521（PAY-003 0.439），过门槛 | 前台照「原路退回」的政策话术回，而不是转人工查单；契约 §0 要求看具体订单的一律转人工，T170 的 handoff_recall 门槛是 1.0 | W-B（T169 定前台时可考虑一条确定性规则：句尾是「了吗 / 了没 / 没有」的问句命中不转人工的话术时，降为兜底或改走 needs_order_lookup；动契约 §1.4 判定顺序要人拍板）或 p13 意图模型 |
| 2026-09-24 | p12 | holdout status_lookup 里 2 条判给了另一篇查单话术：钱给我退了没 → RET-005 0.724、退款有结果了吗 → RET-005 0.676（期望 PAY-003）；3 条落兜底：我的货发了吗 0.210、东西给我寄了吗（一篇都没检出）、我的单子发货没呀 0.122 | 前两条路由对（handoff / needs_order_lookup）但 intent 是 return_exchange 而不是 refund_payment，评测按 intent 比会扣分；后三条兜底一轮，连续两轮就转人工 | W-B（只许补同义词与例句：PAY-003 补「给我退了没」类，LOG-004 补「货发了吗 / 寄了吗」类短问法；补完重跑 scripts/gen_cs_kb.py 与 maos/tests/test_cs_kb_t168.py） |
| 2026-09-24 | p12 | cs 行 KbRetrieved.detail.weights 记的是 MAOS_KB_WEIGHTS 配置值，话术召回用的却是 maos/domain/cs/scripts.py 点名给的一套 | 按 weights × channels 复算分数的读者在 cs 行上对不上（与本节 docs[].score 口径那一条同源） | p14（建 cs 树族与 verify 新项时，给 cs 行单列口径） |

## task-t170（后置校验、静态守卫与评测集，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **check_reply 把「已提出退款 / 支付处理中 / 已驳回」也当状态字眼**（见 DECISIONS task-t170 第 1 条）；T168 在 W-A 里看不到 claims.py，只能照 STATUS_PATTERNS 自查话术，话术或前台常量若含这三串（例如「如您已提出退款申请」）在空观察下会被拦 | 整合期真前台跑评测时这类话术会走 unverified_claim 转人工，answer 轮对不上 | W-A 合流时把 T168「每篇 script 空观察下过 check_reply」那条换成调 maos/domain/cs/claims.py 的真 check_reply |
| 2026-09-24 | p12 | **cs 包 __init__ 的说明写「本包不 import maos/contracts/**」，契约 §2.3 禁表里却没有 maos.contracts**；静态守卫照契约不判 | 这一条只是口头约定，没有机器判据 | p12 整合期由主会话决定要不要把 maos.contracts 加进 §2.3 禁表（改契约 + 守卫一处常量） |
| 2026-09-24 | p12 | **静态守卫只认字面量**：拼接出来的字符串（"payment." + "execute"）、用变量传给 import_module / getattr 的都判不到 | 防得住误用，防不住蓄意绕过 | p14 若要更严，补运行时判据（前台 identity 的 allowed_tools 为空 + SkillInvoker 的授权拒绝） |
| 2026-09-24 | p12 | **评测集 silent / tenant_unmapped 轮的期望 intent = unknown 是契约空白处的推断**（DECISIONS task-t170 第 4 条） | T169 若在这两步也做意图判断，真前台跑评测 intent_accuracy 掉到 1 以下 | W-B 开工前由主会话确认口径 |
| 2026-09-24 | p12 | **check_reply 的补充模式扩大了整合面**（DECISIONS task-t170 复核 L3-1 那条）：话术或前台常量里只要出现「已寄出 / 已退回」（包括「如您已寄出退货」这类条件句），或「48 小时内发货」「1-3 个工作日原路退回」这类时限承诺，空观察下都会被拦 | W-A 合流后真前台跑评测，这几轮会走 unverified_claim，对应的 answer / needs_order_lookup 期望对不上 | W-A 合流时用真的 check_reply 把话术库全量过一遍（T168 在 W-A 里看不到 claims.py） |
| 2026-09-24 | p12 | **补充模式是启发式**：只收「已 / 已经 + 完成态动词」「被驳回了」「预计 + 时长」「时长 + 到账 / 发货类动词」；不带「已」的完成态说法（「到账了」「钱退给您了」「今天就能到」「快递在路上」）判不到，条件句里的「已 X」会误拦 | p12 出门的都是静态话术与常量，问题在整合期能看到；p13 有了观察、开始用模型组稿以后，这道校验就是唯一的闸门 | p13 引入观察与模型组稿之前，由主会话决定：状态字眼口径升级成语义判定，还是扩 types.STATUS_PATTERNS |
| 2026-09-24 | p12 | **借身份只堵了已知入口**：AGENT_POOL、maos.capability、造 identity 时的字面量 skill 名。不在禁表、也不在允许清单里的模块没判（maos.ingress.classify 内部用 RefundIntakeAgent 的身份调模型，maos.core.roster 枚举全池）；allowed_skills 用变量传的判不到；不包禁工具、但会建工单或发通知的 skill 名（refund.intake、notify.customer、kb.sink、finance.settle 等）不在禁常量里。另外，本节第 3 条建议的运行时兜底（前台 identity 零工具 + SkillInvoker 授权拒绝）挡不住借身份：maos/skills/invoker.py 只看调用方传进来的 identity | 契约「禁表 + 允许清单」之外是空白；防得住误用，防不住蓄意绕过 | 由主会话决定：cs 扫描范围改成失败即关的口径（import 白名单 + 只许出现 cs.* skill 名，要改契约 §2.3）；或 p14 在 invoker 侧加运行时判据（核对 identity 的注册来源） |
| 2026-09-24 | p12 | **静态守卫修复轮二之后仍判不到的**（复核攻击矩阵里剩下的 b01–b03、b05–b08、b42、b21、b43）：拼接 / f-string / join / strip / lower / 切片算出来的字符串；运行时对象图上的可达性（store._conn 直接写 SQL、实例属性、闭包里的对象）；dict 这类实例被再导出（没有可信出处）；既不在禁表也不在允许清单里的模块（maos.core.roster 等） | 防得住误用，防不住蓄意绕过；静态守卫这条路基本到头 | 由主会话决定：cs 扫描范围的 maos.* import 改成失败即关的白名单（改契约 §2.3），或 p14 在 invoker / store 侧加运行时判据 |
| 2026-09-24 | p12 | **invoker 不核对 identity 的来路**：maos/skills/invoker.py 用 getattr 取调用方传进来的 identity 上的 allowed_skills，不管它是不是真正的、登记过的 AgentIdentity。守卫这轮把 allowed_skills 的写法堵到「求不出字面量就判」，但前台只要造得出一个对象就能绕 | 前台借身份的闸只有静态守卫一道 | p14：invoker 侧校验 identity 的类型与登记来源（例如只认 AGENT_POOL 里的与 CS_FRONT_DESK_IDENTITY） |
| 2026-09-24 | p12 | **check_reply 规则 3：一条有效 obs claim 撑住它的 literal 在正文里的每一处**（DECISIONS task-t170 复核 L2-3 那条）。p13 有了观察以后，「您的订单已发货。另一笔订单也已发货」只挂一条 ('已发货', obs:观察甲) 就能放行，第二处没有依据 | p12 观察恒为空，没有实际影响；p13 起就是一条编造状态的路 | p13 引入观察之前由主会话定口径：一条 claim 只撑一处（要匹配规则或给 Claim 加偏移，改契约），或维持现状 |
| 2026-09-24 | p12 | **守卫比契约 §2.3 严的部分又多了一批**（动态加载失败即关、按真实出处判再导出、identity 字段静态求值）；T169 在 W-A 里看不到 maos/tests/test_cs_guard_t170.py | T169 若用了 importlib、对导入的模块做 getattr、用参数或运行时拼出来的集合传 allowed_skills，W-B 会在这里变红 | W-B 开工前由主会话把判据摘要同步进契约 §2.3，或让 T169 开工第一步先跑一遍这份守卫测试 |

## integrate-p12-w-a（p12 整合期：W-A 三轨合流时看到的问题，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **cs 静态守卫对 maos.* 的 import 仍是黑名单 + 按真实出处补判**，解析失败时放行；鸭子类型伪身份判不到 | 外部渠道零授权靠守卫 + 复核两道，守卫一道不完备 | p13 校验轨：改失败即关白名单 |
| 2026-09-24 | p12 | **反向验证的变异脚本会吃到旧 .pyc**（同秒写入、长度相同的两个变异体，第二个跑的是第一个的字节码），T167 复核实测撞到 | 变异「存活」的结论可能是假的 | 派单模板的测试口径加一句：每个变异体用独立 PYTHONPYCACHEPREFIX |

## task-t169（前台编排与挂钩，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **问人工服务时间的句子被当成要人工**：「人工客服几点上班」含契约最低触发词「人工客服」，而触发词在检索之前，于是 GEN-003 只有不带「人工」二字的问法才答得到 | 客户问服务时间会被直接转人工（方向安全，但 GEN-003 形同半篇） | p13 意图模型接手时给「人工」加上下文判断（问时间 vs 要人工），要改契约 §1.4 的词表口径 |
| 2026-09-24 | p12 | **「钱退回来一般要几天」被 PAY-003 压过 PAY-001**（复核轮撤掉抄来的同义词后 0.723 对 0.415；首版靠抄评测句拉到 0.749 对 0.723）：T168 给 PAY-003 写的「钱退回来没」与这句只差句尾，字符二元组分不开「要几天」（问政策）与「没」（问这一单）；评测 CS12-044#1 因此不过 | 政策问题被转了人工（方向安全，不编状态）；反方向（问状态被政策话术答掉）已由 T168 的测试钉住，这个方向没有 | 主会话裁（见回执 open_issues）；长期 p13 意图 / 槽位模型时收，或检索侧给「几天 / 多久」一类问政策的词加权（T168 口径，不归本轨） |
| 2026-09-24 | p12 | **「催发货」一类说法覆盖不够**：评测 CS12-014#1「…还没动静，帮我催一下发货」对 LOG-004 只有 0.18（门槛 0.25），落兜底；能拉过门槛的补法都是照抄评测句 | 客户这一句拿到兜底话术，再说一句答不上才转人工（连续兜底兜得住） | 主会话裁（见回执 open_issues）；或 T168 口径下另写一批不看评测集的「催单」说法再测 |
| 2026-09-24 | p12 | **知识层全文通道与话术重排的开销随句长近似平方增长**（复核 L3-2 实测：4000 字 1.6 s、12000 字 13 s，大头在 maos/kb/retriever.py 的 _local_fts_rows） | 本轨已在前台把检索句截到 200 字（DECISIONS task-t169）；别的调用方（退款规划检索）若把长文本直接当关键词仍会慢 | 知识层归属方在 p14 性能轮给 retrieve 的 keyword 加长度上限或改分词查询 |
| 2026-09-24 | p12 | **冻结的 HANDOFF_REASONS 没有「前台内部出错」**，内部异常只能记成 unverified_claim（DECISIONS task-t169） | 转人工原因的分布里，内部出错与真被后置校验拦下的轮混在一起 | p13 改契约时加一个原因值（types.py 与契约测试同改） |
| 2026-09-24 | p12 | **router 既有的 _reply 失败日志打全量 chat_id 与回帖原文**；微信客服上 chat_id 就是 external_userid | 契约 R5「日志里客户标识用 mask_customer」在这一处不成立（本轨只许动 §1.8 那几处，没改） | p13 外部渠道收口时一并改 |
| 2026-09-24 | p12 | **转人工卡片在 webhook 处理线程里同步投递** | 内部渠道 API 抖动会拖慢给客户的回话（去重兜得住平台重推，但客户等得久）；投递失败只回写 failed、不重投 | p14：投递挪到后台，按 list_handoffs 捞 delivery=failed 的卡片重投 |
| 2026-09-24 | p12 | **gen_docs 把 cs.answer / cs.handoff 的域标成「软件交付域」**（docs/skill-catalog.md 的域列） | 生成文档的域列误导读者 | 整合期在 scripts/gen_docs.py 的域推断里认 maos/skills/builtin/cs（白名单外，本轨没动） |
| 2026-09-24 | p12 | **claims.turn_kb_doc_ids 每轮按 plan_id 读回整段会话的 event_log 再按 task_id 过滤**；前台每轮要调一到两次 | 长会话每轮开销随会话事件数线性涨；p12 规模无感 | p14 性能轮：按 task_id 过滤读（Store 接口与 claims.py 都不归本轨） |
| 2026-09-24 | p12 | **问候里带客套话的说法检不到 GEN-001**：评测 CS12-045#1「您好，打扰问一下」对 GEN-001 只有 0.156（门槛 0.25），落兜底；GEN-001 基线说法里没有「打扰 / 麻烦」一类，本轨一轮补的「打扰了」是看着这句评测补的，复核二轮撤回 | 客户这一句拿到兜底话术（请他直接说问题），不编、不转人工；评测少一轮 | 主会话裁（见回执 open_issues）；或 T168 口径下不看评测集另写一批问候客套说法再测 |
| 2026-09-24 | p12 | **转人工卡片的渲染只挡已知的几种平台标记**（尖括号、@、方括号、星号、波浪号换全角，换行压平），不是按投递目标平台做的完备转义；飞书文本消息的实际渲染本容器连不上开放平台、没真机验过 | 以后某个平台新认一种标记，客户原文里的同类写法会在内部房间被解释（客户仍造不出新的一行、@ 不了人） | p13 外部渠道收口时按投递目标平台各写一份转义，并在飞书真机上验一次 |

## integrate-p12-evalfix（p12 整合期：开发集三轮缺口的通用补法，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **PAY-001 / PAY-003 在「钱退回来 + 问时长」上分不开**（开发集 CS12-044#1，补完通用的问时长说法后 0.621 对 0.723）：重排的虚词表把「一般 / 要几 / 没 / 了吗」都压到 0.1，两篇共享钱退 / 退回 / 回来 / 几天这几个实词二元组，区分问时长与问到没到的只剩虚词 | 客户问退款一般要几天会被转人工查单（方向安全，但多占一次人工）；T169 满分那条仍 xfail，KNOWN_GAPS_T169 剩这一轮 | p13 检索轨（或主会话批准改 maos/domain/cs/scripts.py）：给重排加时长词（多久 / 几天 / 多长时间）与状态词（没 / 了吗 / 到没到）的意图线索，或政策篇与查单篇分差很近时的判别规则；改后摘 xfail、清空 KNOWN_GAPS_T169，并回归 T168 的 status_lookup 与 KNOWN_STATUS_MISROUTES_T168 |
| 2026-09-24 | p12 | **CS12-014#1 过门槛的余量只有 0.013**（LOG-004 0.263，门槛 0.25） | 门槛或重排权重稍一动，这一轮就回到兜底；开发集上对上不代表催促类句子普遍过线 | 盲写留出集落地后看催促类的实际过线率，再定要不要补说法或调重排 |
| 2026-09-24 | p12 | **gen_cs_kb.py 的 _PROVENANCE 与模块头仍写「话术、同义词、例句均由 task-t168 自写」**，integrate-p12-evalfix 已补 23 条 | 产物 JSON 的出处描述不准（数据仍全部合成） | 下次改生成器时一并改 _PROVENANCE 与模块头，重产 cs_scripts.json |
| 2026-09-24 | p12 | **p12_cases.json 的 _note / _provenance 没标它已改作开发集** | 后来人照文件自述把它当留出集报泛化指标 | 盲写留出集合入时，由评测集归属轨在文件里标注开发集身份 |
| 2026-09-24 | p12 | **与订单无关的催促会按查单转人工**：客户说「能不能快点回复我」「能不能快点处理」时，这句会命中 LOG-004 的「能不能快点发」（重合的是「能快 / 快点」两个实词二元组），被按 needs_order_lookup 转人工，而在 d96dd26 上是兜底；改成「能不能快点发货」实测挡不住（0.446） | 方向安全（多转一次人工），但转人工的原因和意图都记错了 | 盲写留出集落地后，看这类句子出现得多不多；多的话在重排里给「催促 + 对象」加判据（p13 检索轨），或者撤回这一条 |
| 2026-09-24 | p12 | **没带第二处问候的纯客套开场仍然兜底**：GEN-001 的客套增补收到只剩「请问 / 打扰」以后，「不好意思，打扰一下」「打扰了亲」「麻烦问一下哈」「你好，请问一下」都落兜底，单说「打扰了」刚好卡在 0.25；「你好 / 您好 + 打扰 + 真问题」时，问候篇的分会贴近该答的那篇（例：「你好呀，打扰了，坏了能换吗」RET-003 0.424 对 GEN-001 0.345） | 客户只说客套话时拿到的是兜底（请他直接说问题，不编、不转人工），连续两次兜底才转人工；「两处问候 + 很短的真问题」以后可能被问候篇挤掉 | p13 检索轨（重排不归本轨）：让重排认出客套前缀，用去掉前缀后剩下的内容给其他篇打分，剩下的内容在别篇没有实词命中时才由问候篇接；改完以后 GEN-001 可以放心补整句客套说法，POLITE_OPENER_PROBES_T169 继续当守卫 |

## integrate-p12-holdout（p12 整合期：盲写留出评测集，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | evaluate 的 shortfalls 对门槛文件里没写的 cite_accuracy 按 1.0 要求，而留出集预登记门槛只有四个键 | 留出集量不到编号级命中（同意图内选错话术判不到）：要么写 cite 背一条隐性 1.0 门槛，要么不写；本集选了不写 | 主会话启用留出集时定：预登记一条 cite_accuracy 门槛后把 tags 的 scheme:编号@轮 转成 cite，或维持现状 |
| 2026-09-24 | p12 | 契约没写 tenant_unmapped 的会话之后进不进 handed_off | 评测集只能出单轮，映射不到的客户第二句话前台怎么走没有判据 | p13 |
| 2026-09-24 | p12 | GEN-003（人工客服的服务时间）的自然问法很可能带「人工客服」，而它是 requested 的必认触发词，按 §1.4 顺序先转人工 | 这篇话术在真实流量里可能很少命中；留出集照契约顺序出题 | p13 触发词与意图轨 |
| 2026-09-24 | p12 | 真前台跑留出集那条测试靠 MAOS_CS_HOLDOUT_RUN 环境变量跳过 | 合流时忘了删 skipif，这条就永远是 skip，留出集形同虚设 | 合流 p12-holdout 时删 skipif 并记实测四个指标 |
| 2026-09-24 | p12 | 不重合棘轮在测试运行时读当时树上的开发集与话术库；合流 p12-evalfix 以后，evalfix 新增的说法与留出句共享 ≥6 字片段的话，这条棘轮会红 | 合流后留出集的棘轮测试会红；要是为了变绿去放宽门槛，或者改留出句去迁就调优后的话术库，留出集就不再独立 | 合流 p12-holdout 与 p12-evalfix 时：不许改棘轮或门槛；撞上的说法由主会话裁定撤回，或者请没看过开发集与话术库的人重写那一轮；同时向 evalfix 会话确认没读过 scenarios/cs/eval/p12_holdout_cases.json |
| 2026-09-24 | p12 | 棘轮只比开发集各轮与话术库的 examples、synonyms，不比话术正文（script、principle、title），也不比 T168 的检索泛化用例 | 检索要是也拿正文打分，留出句跟正文共享的片段一样会抬分，这部分重合棘轮判不到 | p13 检索轨定下检索实际用哪些字段以后，把这些字段加进棘轮参照 |

## integrate-p12（p12 整合收尾时留下的事，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p12 | **证据束没有按 p12 重产**（用户拍板留给 Mac）。p12 没改任何证据生成路径，但 provenance 按 sha 判 | 本分支上 verify 第 9 项在重产前判负 | 用户在 Mac 上按 scripts/make_release.sh 客户模式那一串重产（make_evidence、--domains、make_case_bundle --all-paths、kb.experiment --r8、gen_capability_matrix、render_trace，外加 PG 束），作为 p12 最后一个提交 |
| 2026-09-24 | p12 | **留出集泛化差**：intent 0.6857 / route 0.6429 / handoff 0.7368，预登记门槛 0.85 / 0.80 / 0.90 | 客户换个说法就落兜底（安全但体验差） | p13 理解层（意图示例、槽位）与前台编排：以预登记门槛为验收，在留出集上量 |
| 2026-09-24 | p12 | **触发词按子串匹配会误伤复合词**（留出集里一例：商品名里含情绪词的两个字，被判成情绪激烈转人工） | 误转人工（方向安全，但错） | p13 触发词接手轨：词边界 / 否定语境 / 复合词白名单 |
| 2026-09-24 | p12 | **问规则时长与查某一笔进度的说法共享实词**（开发集 CS12-044#1：PAY-001 被 PAY-003 抢走） | 该答政策的被转人工查单 | p13 理解层：把时长词与进度词当意图线索，或在政策篇与查单篇分数相近时加裁决 |
| 2026-09-24 | p12 | **留出集要保持盲**：p13 起所有实现轨不许读 p12_holdout_cases.json 与它的测试；重合棘轮只管话术库与开发集一侧 | 看过的人写的补法会让留出集失效 | p13 派单里写死；复核专查 |

## p13-contracts（p13 契约期：骨架时看到的问题，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p13 | **本期没有新的存量问题**：骨架只做了枚举增长与端口定义，读到的问题都已在 integrate-p12 小节 | 无 | 无 |

## task-t173（理解层：语种、槽位、意图，2026-09-24）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-24 | p13 | **理解层够不到 p12 留出集**：契约 §2 第 5 步「没注入端口 → 走 p12 的第 4–6 步，逐字节同 p12」，而 p12 留出集按 p12 的形状跑（不注入端口）；照此实现，本轨的 intent_hint、意图线索、示例表一样都进不了那条路，能影响留出集的只剩 triggers.py（复合词白名单、英文触发词） | 主会话在整合期量 p12 留出集时，类别 (1)(3) 的改进看不到；本轨实测接上提示后：开发集 52 → 53 / 53，T168 holdout 改写句过门槛且判对 63 → 68 / 76，问自己那一单状态的句子判对 41 → 44 / 47、被政策篇答掉 1 → 0，无关句最高分不变（0.208） | W-B 派单前由主会话裁定：第 5 步是否也把 understand 的意图当 intent_hint 传给 match_scripts（传了 CS12-044#1 会对上，T169 的 KNOWN_GAPS_T169 那条测试随之变红、按其注释清空） |
| 2026-09-24 | p13 | **英文句的 understand 意图不是 unknown**（开发集 CS12-041「how long does shipping usually take」→ logistics），而 p12 期望英文政策问题是 fallback / unknown | T174 若把 understand 的意图直接写进 DeskResult.intent，p12 开发集那一轮的 intent 期望就对不上 | T174（W-B）：英文兜底轮的 DeskResult.intent 按 p12 口径给 unknown，或主会话改 p13 口径 |
| 2026-09-24 | p13 | **绑定核验精确匹配，而 order_no 槽位做了规整**（NFKC、字母转大写、保留「#」） | 种子文件里 display_no 若是小写或全角，客户照着打也核验不过 → identity_unverified | T171 / T174：种子与测试夹具的 display_no 一律写大写半角；或 BindingVerifier 比对前做同样的规整 |
| 2026-09-24 | p13 | **类别 (1)「换个说法就落兜底」在不注入端口的路径上只能靠话术库补说法**，而 T169 的增补棘轮只许补主会话点名的三类缺口（催促发货 / 客套开场 / 问退款时长）、且只许补给对应那一篇 | 其余 16 篇的泛化缺口没有合规的补法 | 主会话裁定是否新开缺口类别（改 maos/tests/test_cs_eval_p12_t169.py 的 GAP_CLASSES_T169）；或按上面第 1 条让第 5 步也走提示模式 |
| 2026-09-25 | p13 | **错别字不在理解层的词表里**（开发集 CS12-043「你们包由吗」理解层意图 unknown；话术检索靠例句里登记的错写答对）。按字补错写就是拿开发集调词表 | 客户的同音错字（包由 / 退宽 / 发获）只有话术库例句里登记过的才认得；理解层的意图、诉求、提示对它们无效 | p14：统一的同音错字规整（拼音近音表，数据驱动），理解层与检索共用；不在本轨按句补 |
| 2026-09-25 | p13 | **「他妈的」恒判 anger**：「给孩子他妈的外套」这种所有格写法会误转人工（上下文写法只放过后面不跟「的」的「孩子他妈」） | 这一类会多转一次人工（宁可多转） | 需要真实客服日志看频率再定；频率高就加「(孩子 / 老公)他妈的 + 名词」的写法 |
| 2026-09-25 | p13 | **不带分隔符的座机号**（057188886666，0 开头 10–12 位纯数字）仍当纯数字单号认；手机号与 400 / 800 已排除 | 客户只报座机号时会被存成 order_no 槽位、送去身份核验（核验失败即关，查不到单） | T174 / p14：有真实平台单号样本后再定「0 开头纯数字」要不要排除（排了会误杀以 0 开头的平台单号） |
| 2026-09-25 | p13 | **骂人句型之外的说法会漏、句型之内的所有格会误转**（r3 把滚 / 妈的 / 他妈 / 气炸 / 去死 / 什么破改成只认句型之后）：「这快递妈的物流」这类妈的后面直接跟名词的骂法、「爱卖不卖滚」这类不带标点的句末「滚」不认；「她妈的生日礼物」「宝妈的真好用吗」这类所有格、「滚 筒洗衣机」这种拆开打的商品名会误判 anger | 前一类漏转人工（handoff recall 往下掉），后一类多转一次人工；句型只按作者写得出的说法定，真实分布没量过 | 主会话整合期量 p12 留出集时看 anger 那一类的召回；p14 拿真实客服日志补句型 |
| 2026-09-25 | p13 | **撤回诉求给的是 other**（「不用退款了」→ request=other），而 other 在本轨的诉求词表里本来是改地址、取消订单这类「其他要办的事」 | T174 若把 other 当成「要看具体订单」的一种，就会为一句撤回去查单；本轨测试只钉了 other 覆盖掉上一轮的 refund | T174（W-B）：other 不进契约 §2 第 6 步那几种诉求（与现行契约一致）；或主会话在 p14 给 ports.REQUEST_VALUES 加一个「撤回 / 无」取值（冻结面，要主会话改） |
| 2026-09-25 | p13 | **「在哪」不算物流线索**（「我的东西现在在哪」意图 unknown）：「在哪」同时是问入口的说法（退款入口在哪），放进物流词表会把问入口的句子拉成物流 | 这一类问包裹位置的说法落兜底或交给模型 | p14：按「东西 / 包裹 / 快递 + 在哪」这种主语 + 在哪的组合补，要先有真实日志看两种说法的比例 |

## task-t173（理解层：语种、槽位、意图（主会话裁定收敛轮），2026-09-25）

| 日期 | Phase | 现象 | 影响 | 建议处理时机 |
|---|---|---|---|---|
| 2026-09-25 | p13 | **R1 地板让 p12 的误伤回来了**：称谓 + 妈的（宝妈的奶粉、孩子他妈）、去死海、缓解恶心、什么破壁机、防泄漏水杯、熊猫滚滚、真人实拍图这类句子照 p12 转人工；唯一的豁免手段是封闭名词表，而这些词里的触发词不完全落在任何名词里 | 这一类普通咨询多转一次人工（安全方向）；p12 留出集的「复合词误伤」类别只有名词表覆盖到的那部分能改善 | 整合期主会话量留出集时对照；要再收窄只能逐条往名词表里加审过的名词，或在 p14 用真模型路径做二次判断 |
| 2026-09-25 | p13 | **型号与短单号字面上分不开**：「I'd like to order the MX5000」这类没有品类名词跟着的型号会被当成 order_no | 装了端口时这一轮核验不过 → identity_unverified 转人工（多转，不涉隐私）；不装端口时 p12 路径不看槽位，无影响 | T174：只在意图是查单 / 退换货诉求时才核验与查单；绑定表是最终裁判（只有绑定过的号才是单号） |
| 2026-09-25 | p13 | **p13 开发集有 3 轮理解层给不出查单意图**：「H8008 现在发到哪了」「帮我查查它现在到哪了」（进度线索要求句中有订单话题词，单号与上一轮的单号不算）、「Hi, could you check where my order A5001 is?」（check where my order 不在英文词表） | T174 若只按理解层的意图决定走不走查单，这 3 轮会落兜底 | T174 接线时：本轮或会话里已有 order_no 时把进度线索视为订单话题；或下一轮在理解层补「有单号即订单话题」与 check (on / where) my order 这一类说法 |
