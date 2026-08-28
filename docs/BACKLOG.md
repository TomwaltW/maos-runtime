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
