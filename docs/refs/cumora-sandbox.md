# cumora 解析 · 沙箱与凭据边界 （T42 · 基线 cumora@1e883f6 / MAOS@926aa7b）

> 本文里带 `cumora:` 前缀的引用指的是**外部仓 cumora**（@1e883f6）里的文件，本仓永远
> 不会有它们；不带前缀的路径才是本仓。`scripts/check_docs.py` 对前缀形式只校验写法、
> 不校验存在性（口径见该脚本文件头的「外部仓引用」一节）。

## 1. 它是怎么做的

BYOA 要解决的是一句很不舒服的话：**让别人的 Claude Code 跑在别人的 Mac 上，服务端既要指挥它、
又不能持有它的 provider 凭据、还不能让它越权碰宿主。** cumora 的答案不是「一层沙箱」，
而是三道方向不同的 fail-closed 闸，加一条把凭据整个搬出模型进程的绕行通道。
文档把这四件事收在 `cumora:docs/BYOA.md:486` 的 `## Boundaries` 一节，代码在
`server/src/agents/computer/engine.ts` 与 `daemon.ts`。值得注意的是那一节里**没有一条是
「告诉模型不要做 X」** —— 全是机制。

在三道闸之前还有一道**准入闸**，决定「谁有资格跑」。`SANDBOXED_ENGINE_IDS` 只有 claude 和
codex 两个（`engine.ts:329`），其余五个引擎默认不是「在 UI 里藏起来」，而是**从可运行清单里
被删除**：`runnableEngineIds()`（`engine.ts:344`）直接过滤，所以服务端就算给这台机器派了个
grok 的活也执行不了（`cumora:docs/BYOA.md:272`）。白名单之上再叠一道版本闸 ——
`SECURE_ENGINE_MIN_VERSIONS` 钉死 claude ≥ 2.1.248、codex ≥ 0.138.0（`engine.ts:331`），
`evaluateRunnableEngines()`（`engine.ts:381`）实探本机版本，低于最低版就拉黑。
理由写在 `engine.ts:379`：旧版本会**静默忽略** cumora 依赖的那几个边界开关 ——
光看二进制叫 claude 不算数。Linux 上还要 `bwrap` 和 `socat` 都在场，缺一个就拉黑
（`engine.ts:371`）。「能力探不到就当没有」这条判据，比白名单本身更值钱。

**第一道闸：文件系统。** Claude 路径靠 `--restricted` 把内建文件工具锁在 cwd（即 agent home）
里，再叠一份显式 `--settings` JSON（`engine.ts:1293` 的 `claudeSecureSettings`）。这里有个
非平凡的判断：Claude 的命令沙箱**默认是写受限、读放行**的，所以 cumora 先 `denyRead` 掉整个
用户数据根（darwin 上是 `~/`、`/Users`、`/Volumes`；Linux 上是 `/home` `/root` `/proc` `/sys`
一长串，`engine.ts:1302`），再把 agent home 和 PATH 上的目录**单独挖回来**（`engine.ts:1305`
起）；`/bin` `/usr` `/lib` 这些系统运行时路径保持可读，否则桥接程序起不来。Codex 路径换成
权限档：`permissions.cumora.filesystem` 只给 `:minimal` 读加 workspace 写
（`engine.ts:1621`），并且在 **CLI 优先级**上把 agent home 标成 `trust_level="untrusted"`
（`engine.ts:1640`）—— 目的很明确：不让模型在自己可写的 home 里种一个更宽松的 `.codex`
配置层，留给下一次 one-shot 用。

**第二道闸：命令网络。** Claude 是 `network: { allowedDomains: [], strictAllowlist: true }`
（`engine.ts:1330`），空白名单加严格模式等于全拒；Codex 是
`permissions.cumora.network.enabled=false`（在 `engine.ts:1584` 起的
`CODEX_SECURE_CONFIG_ARGS` 里）。工具层再补一刀：`WebFetch` / `WebSearch` 进 deny
（`engine.ts:1319`）。**这道闸反过来决定了 IPC 的传输形态** —— `daemon.ts:803` 明写：
用文件而不是 TCP / Unix socket 是刻意的，因为每个受支持引擎都能让工具子进程网络全拒，
而文件协议在 macOS / Linux / 原生 Windows / WSL 上都不用在这道闸上开洞。这是整份代码里
最漂亮的一处因果：安全边界反向约束了通信机制的选型，而不是反过来给边界开例外。

**第三道闸：子进程凭据。** Claude 侧是「白名单反推 deny 清单」：`TOOL_ENV_ALLOWLIST`
（`engine.ts:1286`）只有 PATH / HOME / USER / TMPDIR / LANG 之类十几个再加两个
`CUMORA_AGENT_*`，然后遍历当前 env 的**全部** key，凡不在白名单里的逐个生成
`{name, mode:'deny'}` 塞进 `sandbox.credentials.envVars`（`engine.ts:1294`）。Codex 侧更直接：
`shell_environment_policy.inherit="none"` 之后再 `set={...}` 显式给一份非密环境
（`engine.ts:1604`），其中 `HOME` 被替换成 agent home 而不是宿主的（`engine.ts:1610`）。
但真正让这道闸变便宜的是上一层的决定：**安全档下 Claude 根本不给 `Bash` / `PowerShell` 工具**
（`engine.ts:1319` 的 deny 与 `engine.ts:1357` 的 `--disallowedTools`）。没有 shell 工具就没有
「模型生成的命令」，凭据 deny 只是第二保险 —— 从源头掐，比在出口拦便宜得多。

**凭据绕行通道。** 模型进程拿到的 env 里只有三个 `CUMORA_*` 变量（`daemon.ts:1753` 的
`engineEnv()`），**没有服务端 URL，没有 bearer token**。agent 要在 cumora 里做事，
只能调一个固定的 MCP 工具 `cli(argv)`（`daemon.ts:883` 的 `CUMORA_MCP_SHIM`），
它把 argv 写成一个 JSON 文件丢进 `.runtime-cli-ipc/<agentId>/requests/`；daemon 侧的
`RuntimeCliBroker`（`daemon.ts:972`）用**原子 rename 认领**到自己的私有目录之后才解析，
再由 daemon 附上内存里的短期 JWT POST `/runtime/cli`（`daemon.ts:1707` 的
`invokeRuntimeCli`）。桥接可执行文件和 IPC 目录都刻意**放在模型可写的 home 之外**
（`daemon.ts:1488`、`daemon.ts:1492`），所以模型改不了桥、也没法直接往 responses 目录塞东西。
broker 那一段的细节很密，堵的全是同一类攻击 —— 「模型预先摆一个符号链接，把 daemon 的
特权写重定向出去」：只认 `^[0-9a-f-]{36}\.json$` 形状的文件名、认领后再 `lstat` 且拒绝
符号链接（`daemon.ts:1094`）、`O_NOFOLLOW` 打开并限长 32MB（`daemon.ts:1101`）、
响应先写私有暂存再原子 rename 到位。

**PATH 影子这条要单独讲**，因为它最容易漏：agent home 是模型可写的，而引擎自己要从 PATH 上
解析 `claude` / `codex` 二进制。所以安全模式下 `engineProcessPath()`（`daemon.ts:1158`）
**不是**把 `<home>/bin` 前置，而是把整条 PATH 过一遍，只保留「绝对路径、且不在可写 home
之下」的条目 —— 空条目和相对条目一并滤掉，因为它们相对 cwd（即 agent home）解析，
同样是影子路径。只有兼容模式才走 `prependAgentBinToPath()`（`daemon.ts:1151`）。
另外 runner 启动时会主动删掉老版本遗留在 `<home>/bin/.runtime-token` 的活 bearer token，
并拒绝符号链接化的目录（`daemon.ts:1632`）—— 连**升级路径上的凭据残留**都按边界问题处理。

**危险档的形状。** 以上全部的逃生口是一个环境变量 `CUMORA_BYOA_ALLOW_UNSANDBOXED=1`
（`engine.ts:330`、`engine.ts:340`）。它有三点值得抄：一是默认不是「关掉的开关」而是
「不存在的能力」（不安全引擎被从清单里删除）；二是它**顺带**解锁 `CUMORA_*_ARGS` 整串 argv
覆盖 —— 理由写在 `engine.ts:1017`：cumora 无法证明一串不透明参数保住了沙箱，所以安全档下
**默认忽略**它们（这和 MAOS 守卫对 `$(...)` 判「不透明载荷」是同一个 idiom）；
三是文档里用大写 HIGH RISK 三行注释标注，并明说只该在你信得过的外部容器 / VM 里用
（`cumora:docs/BYOA.md:277`）。与之配套的是 `sandbox.failIfUnavailable: true`（`engine.ts:1323`）：
**OS 沙箱起不来就让这一轮失败，不降级。**

### 哪些是真边界，哪些是软约束

派单要求把这条分清楚，因为「提示词里写了不许」不是边界。

| 层 | 具体机制 | 真边界？ | 绕过成本 |
| :-- | :-- | :-- | :-- |
| OS | Claude `--restricted` 沙箱 / Codex 权限档的文件系统与网络限制（`engine.ts:1293`、`engine.ts:1621`） | **是** | 需要一次 OS 沙箱逃逸（Seatbelt / bubblewrap）。前提是引擎 CLI 真的落到了 OS 机制上 —— cumora 只能传参数，验证不了，所以才有版本闸兜底 |
| 运行时（引擎进程） | 不给 `Bash`/`PowerShell`/`WebFetch`/`WebSearch` 工具（`engine.ts:1357`）；只暴露一个结构化 `cli(argv)` MCP 工具 | **是** | 模型没有可执行任意命令的工具面，只能在给定 schema 内提参数 |
| 运行时（daemon 进程） | token / URL / Authorization / 重定向 / 刷新全部 daemon 私有（`daemon.ts:1707`）；桥与 IPC 在 home 之外（`daemon.ts:1488`） | **是** | 要先拿到 daemon 进程的内存或写权限 —— 已经不是「模型越权」而是「宿主被攻陷」 |
| 进程启动 | PATH 清洗、`.runtime-token` 残留清理、拒绝符号链接目录（`daemon.ts:1158`、`daemon.ts:1632`） | **是**（加固性质） | 单独看不构成边界，是在堵前三道的绕行入口 |
| 配置优先级 | Codex `trust_level="untrusted"` 写在 CLI 层（`engine.ts:1640`）；Claude `--strict-mcp-config` + 忽略用户/项目设置 | **是** | 模型在 home 里写什么配置都不生效，因为 CLI 参数优先级更高 |
| 提示词 | standing prompt、persona、`GLANCE_YIELD_RULES`、「发言前先扫一眼」这类纪律（`cumora:docs/BYOA.md:162`） | **否，软约束** | 模型不照做即失效。cumora 自己也没把它写进 `## Boundaries` —— 那一节全是机制 |

一句话判据：**`## Boundaries`（`cumora:docs/BYOA.md:486`）里列出来的都是真边界，
standing prompt 里写的都不是。** cumora 把这两类东西放在文档的不同章节，
这本身就是一个可抄的编排。

## 2. MAOS 的对应物

| cumora 的机制 | MAOS 的对应物（含文件路径） | 关系 |
| :-- | :-- | :-- |
| 引擎白名单：不安全引擎从可运行清单删除（`engine.ts:329`、`engine.ts:344`） | `AgentIdentity.allowed_tools` 白名单 + `check_tool()` 越权即抛 `PermissionDenied`（`maos/agents/base.py:60`、`maos/agents/base.py:133`） | **同构** —— 都是「不在名单上就不存在」，且都用异常而不是返回值表达越权 |
| 能力实探 + 版本闸：低版本静默忽略开关，故拉黑（`engine.ts:331`、`engine.ts:381`） | 无。`_docker_ready()` 只探 daemon 与镜像在不在（`maos/tools/sandbox.py:147`），不探隔离参数是否真生效 | **MAOS 没有** |
| 文件系统闸：OS 沙箱 + denyRead 用户数据根、allowRead 挖回 home（`engine.ts:1293`、`engine.ts:1302`） | 容器主路径 `--read-only` + 只挂 workdir 到 `/w`（`maos/tools/sandbox.py:498`）；降级路径无任何文件系统隔离 | **形似神不同** —— 容器路径更强（整个根只读），降级路径为零 |
| 无对应 | 补丁落盘前三重路径校验：受保护目录分段相等 / `conftest.py` 任意层级禁改 / workdir 内含性（`maos/tools/sandbox.py:326`） | **MAOS 独有** —— cumora 的模型直接编辑 home 内文件，没有「补丁审查」这一层 |
| 命令网络闸：空 allowlist + strict（`engine.ts:1330`）、Codex `network.enabled=false`（`engine.ts:1584`） | 容器 `--network none`（`maos/tools/sandbox.py:498` 的 docker run 参数）；降级路径无 | **形似神不同**（同上，降级路径为零） |
| 子进程凭据闸：白名单反推 deny 清单（`engine.ts:1286`、`engine.ts:1294`）；`HOME` 换成 agent home（`engine.ts:1610`） | `_clean_env()` 白名单重建 env，只放行 PATH / LANG，`HOME` 指向一次性空目录（`maos/tools/sandbox.py:80`、`maos/tools/sandbox.py:116`） | **同构** —— 连「按名字放行而不是按名字拦截」的理由都一样（MAOS 的 docstring 在 `sandbox.py:123` 写了同一条） |
| 不给 shell 工具，只给结构化 `cli(argv)`（`engine.ts:1357`、`daemon.ts:883`） | 模型不执行任意命令，产出只有补丁与被 pytest 收集的代码；工具面是九要素 `ToolPort`（`maos/tools/port.py:22`） | **同构**（结论一致，路径不同：cumora 靠关掉工具，MAOS 靠压根没有这个工具） |
| 凭据绕行：token / URL / 重定向全部 daemon 私有，模型进程 env 无 token（`daemon.ts:1707`、`daemon.ts:1753`） | `ModelClient` 持 key，Agent 只能调 `ask()`（`maos/model/client.py:183`、`maos/agents/base.py` 的 `ask`） | **形似神不同** —— MAOS 是同进程，key 与模型输出在同一地址空间；靠的是「不给模型执行面」，不是进程边界 |
| 重定向不出 daemon（`daemon.ts:1707` 的 "identity, URL, Authorization, redirects … remain daemon-owned"） | `_SameOriginRedirectHandler` 只放行同 origin 的 3xx，换 scheme / 主机 / 端口一律拒（`maos/model/client.py:148`、`maos/model/client.py:161`） | **同构**，MAOS 这一处更细（把 https→http 降级也算换 origin） |
| 沙箱不可用即失败：`failIfUnavailable: true`（`engine.ts:1323`） | 容器不可用即**自动降级**为裸 subprocess，`log.warning` + 写进 `sandbox_mode` / `degraded_reason`（`maos/tools/sandbox.py:591`、`maos/tools/sandbox.py:600`） | **方向相反** —— cumora fail-closed，MAOS fail-open 但**响亮**（降级进报告，不只进日志） |
| per-agent 持久 home，模型可写（`daemon.ts:54`、`cumora:docs/BYOA.md:313`） | 每次任务一个一次性 workdir（`maos/tools/sandbox.py:188` 的 `prepare_sandbox_workdir`），无 per-agent 持久 home | **形似神不同** —— MAOS 的 agent 隔离是**能力隔离**（`allowed_tools` / `allowed_skills` / `write_scope`），不是文件系统隔离 |
| 桥接可执行文件与 IPC 目录放在模型可写面之外（`daemon.ts:1488`、`daemon.ts:1492`） | `ToolPort.entry` 是进程内函数，不存在「模型可写面」这个概念（`maos/tools/port.py:26`） | **MAOS 没有**（当前也不需要；MCP 迁移之后才有落点，见 `docs/toolport-contract.md:95`） |
| PATH 清洗：滤掉可写目录、空条目、相对条目（`daemon.ts:1158`） | 降级路径把宿主 `PATH` 原样透传（`maos/tools/sandbox.py:80`） | **形似神不同**（评估见第 3 节 #7 —— MAOS 这里不构成新增风险） |
| 三层凭据：company 配对码 → 设备 token（sha256 存 `credential_hash`）→ 每 agent 2h JWT（`registry.ts:231`、`registry.ts:233`、`registry.ts:500`） | 无。单机演示，无设备配对面 | **MAOS 没有** |
| 撤销即抹哈希：`revoked_at` 与 `credential_hash = NULL` 同时写（`registry.ts:753`） | 无 | **MAOS 没有** |
| 令牌不是权威：JWT 里的租户只是提示，每次回查 `participants` 活行（`authorization.ts:4`、`authorization.ts:9`） | `AgentIdentity` 是 `frozen=True` dataclass 常量（`maos/agents/base.py:60`），铸造即快照，无回查 | **MAOS 没有**（单进程下 Identity 就是代码常量，暂无此需求；但这条与铁律 8「不持有权威事实」是同一个道理，见第 3 节 #4） |
| 每 agent 一个 wake-stream，云端 pod + Go FUSE 挂服务端工作区（`agent-fuse/main.go:1`） | 无 | **MAOS 没有**（见第 4 节反向清单 #1） |
| `npx cumora` 单文件分发 + `--install-service` + `--doctor`（`cumora:docs/BYOA.md:460`、`cumora:docs/BYOA.md:480`） | `python3 run.py` 薄入口 + `scripts/verify.py` 收口证据 | **形似神不同** —— MAOS 无分发面，但 `--doctor` 那个「开跑前一问」的位置是空的 |
| 每次工具调用的服务端审计 | `invoke_tool()` 无论成败落一条 `ToolInvoked` event_log 行，入参过 sha256 摘要（`maos/tools/port.py:43`） | **不可比** —— cumora 的 `/runtime/cli` 服务端侧不在本轨读单里，我没读，不下结论（见第 5 节） |

## 3. 可移植清单

| # | cumora 的做法 | 出处 `文件:行` | MAOS 现状 | 形态 | 落点 | 成本 | 判断 |
|---|---|---|---|---|---|---|---|
| 1 | 沙箱起不来就让这一轮**失败**，绝不降级运行（`failIfUnavailable: true`） | `server/src/agents/computer/engine.ts:1323` | `_docker_ready()` 一返回 False 就**无条件**降级到裸 subprocess，只 `log.warning` + 写 `degraded_reason`；没有任何配置能让它「宁可不跑也不裸跑」（`maos/tools/sandbox.py:591`、`maos/tools/sandbox.py:600`） | 抄思想 | 新增插件（`maos/tools/sandbox.py` 加一个 `MAOS_SANDBOX_REQUIRE_CONTAINER` 档，走 `maos.config` 配置面，与现有 `MAOS_SANDBOX_TIMEOUT` 同一读法） | 0.5 人天 | **赛前做** —— 评审/演示档下「容器隔离」这句话要么成立要么这一轮不跑；测试与 CI 的 `MAOS_SANDBOX_FORCE_SUBPROCESS=1` 行为一个字不改，两条路径都仍被测到 |
| 2 | `--doctor`：开跑前一条命令端到端探大脑、小脑与 wake 全链路，绿了才认为真实唤醒能跑 | `cumora:docs/BYOA.md:480` | 有 `scripts/verify.py` 在**跑完之后**收口证据，但没有「这台机器上容器档能不能走」的**开跑前一问**；现在要等报告出来看 `degraded_reason` 才知道 | 抄思想 | 新增插件（`scripts/` 下新增一个 preflight 脚本，不碰 `maos/`） | 0.5 人天 | **赛前做** —— 现场演示前一条命令回答「容器档能不能走」，比跑完看报告早一步；与 #1 配套（#1 决定失败，#2 让人提前知道会失败） |
| 3 | 能力**实探**而非存在性判断：版本低于最低版即拉黑，理由是旧版会静默忽略边界开关 | `server/src/agents/computer/engine.ts:331`、`engine.ts:381` | `_docker_ready()` 只回答「daemon 在不在、镜像有没有」（`maos/tools/sandbox.py:147`），不回答「`--network none` 这一次是否真生效」 | 抄思想 | 新增插件（`maos/tools/sandbox.py` + `deploy/sandbox.Dockerfile` 里加一条自证探针） | 1 人天 | **复赛后** —— 靶场里已有 `test_no_network` 这类探针间接证明；再加一层开跑前自证，收益在演示可信度上，但要动镜像（属「改 Docker」，须先问人），三周内不值得 |
| 4 | **令牌不是权威，活行才是**：JWT 里的租户只是铸造时的快照，每次鉴权都回查 `participants` 活行，所以移走或离职一个 agent 会让之前铸出的所有令牌立刻失效 | `server/src/agents/runtime/authorization.ts:4`、`authorization.ts:9` | `AgentIdentity` 是 `frozen=True` dataclass 常量（`maos/agents/base.py:60`），`check_tool` / `check_risk` / `check_write` 三查都对着这份快照判（`maos/agents/base.py:133`、`:140`、`:148`） | 抄思想 | **动内核**（`maos/agents/**` 的 Identity 取值路径 + `maos/core/**` 的活行来源） | 3 人天 | **复赛后** —— 单进程下 Identity 就是代码里的常量，不存在「撤权后旧令牌还能用」这个窗口，现在抄是解决不存在的问题；等 Identity 落库、多进程之后这条才成立。**但它与铁律 8 是同一条道理的另一个应用面**（权威事实不在自己手里的东西，不许缓存成终态），值得整合轮记一笔 |
| 5 | 模型可写面与桥接/IPC **物理分离**：桥可执行文件和 rendezvous 目录都在 agent home 之外，模型改不了桥也写不进 responses | `server/src/agents/computer/daemon.ts:1488`、`daemon.ts:1492` | `ToolPort.entry` 是进程内函数（`maos/tools/port.py:26`），没有「模型可写面」这个概念；模型产出只有补丁，越界由 `_check_path` 的内含性校验拦（`maos/tools/sandbox.py:326`） | 抄思想 | 新增插件（`maos/tools/**`，仅体现在 workdir 与工具通道的布局上） | 1 人天 | **复赛后** —— 只有等 `docs/toolport-contract.md:95` 说的 MCP 迁移真发生、`entry` 变成跨进程 stub 时，「桥放哪」才成为一个真问题；现在没有落点 |
| 6 | 子进程凭据白名单反推 deny 清单（遍历当前 env，非白名单逐个 deny） | `server/src/agents/computer/engine.ts:1286`、`engine.ts:1294` | `_clean_env()` 已经是同构白名单，且 docstring 写了同一条理由：按名字放行而不是按名字拦截，新增变量时不需要有人记得去补拦截清单（`maos/tools/sandbox.py:116`） | 抄思想 | 不适用 | 0 | **不做** —— MAOS 已经是这个形状，抄过来是重复。记在这里是为了让整合轮知道：这一处**已经对齐了生产级做法**，不必再改 |
| 7 | PATH 清洗：滤掉模型可写目录、空条目、相对条目（空/相对条目相对 cwd 解析，同样是影子路径） | `server/src/agents/computer/daemon.ts:1158` | 降级路径把宿主 `PATH` 原样透传（`maos/tools/sandbox.py:80`） | 抄代码 | 不适用 | 0 | **不做** —— 结论与直觉相反：降级路径**本来就在以宿主 uid 执行模型产出的 Python**（pytest collection 阶段就会 import 补丁写进 workdir 的文件），PATH 影子不增加任何新能力。真正该修的是 #1「要么隔离要么不跑」，不是给一条已经敞开的路加锁 |
| 8 | 撤销即抹掉匹配条件本身：`revoked_at` 与 `credential_hash = NULL` 同一条 UPDATE 写下去，`resolveDevice` 的 `WHERE credential_hash = $1` 从此永远匹配不上 | `server/src/agents/computer/registry.ts:753`（对照 `registry.ts:484`） | 无设备配对面 | 抄思想 | 不适用 | 0 | **不做** —— MAOS 单机无配对。记下来是因为这个 idiom 很便宜：**撤销要让匹配条件失效，而不是再加一个 `WHERE revoked_at IS NULL`** —— 后者漏一处查询就等于没撤销。将来若做审批名单撤销可直接用 |
| 9 | 兼容档逃生口本身（`CUMORA_BYOA_ALLOW_UNSANDBOXED=1` 保留一条「危险但可用」的路） | `server/src/agents/computer/engine.ts:330` | MAOS 无存量用户、无向后兼容包袱 | —— | 不适用 | 0 | **不做** —— 见第 4 节 #5。#1 抄的是 `failIfUnavailable` 的**方向**，不是连这个逃生口一起抄；给自己造一个「危险但保留」的档位是纯负债 |

**落点分布**：新增插件 4（#1 #2 #3 #5）／动内核 1（#4）／动冻结契约 **0**。
判断分布：赛前做 2 ／ 复赛后 3 ／ 不做 4。

## 4. 反向清单 —— 它做了但 MAOS 不该抄

判据一句话：*这个设计在解决我也有的问题，还是在解决它的用户量 / 多租户 / 向后兼容才有的问题？*

1. **Go FUSE 把服务端工作区挂成文件系统**（`agent-fuse/main.go:1`）。它的设计说明自己写清楚了
   动机：托管 PG（Cloud SQL / RDS）不让几百个 pod 各开连接，所以把连接池收在服务端，
   pod 只拿一个 JWT 走 HTTP `/runtime/fs/*`。这是**几百个 pod** 才有的问题。MAOS 是单进程
   单机，workdir 就是本地目录 —— 抄这一层等于给自己造一个不存在的多租户问题，还多出
   490 行 Go 和一个跨语言构建。

2. **三层凭据链 + 设备配对 + 心跳/离线扫描**（`registry.ts:28` 的 `COMPUTER_STALE_MS`、
   `registry.ts:233`、`registry.ts:484`、`registry.ts:500`）。它存在是因为 cumora 有
   company / owner_user / 用户的 N 台机器这条链。一人公司没有「别人的机器接进来」这件事，
   抄一套配对协议是纯负债 —— 而且每一层凭据都是一处新的泄漏面，要配套写撤销、轮转、
   过期测试。**唯一值得单独摘出来的是 #8 那个撤销 idiom，不是整条链。**

3. **`FOR SHARE` / `FOR UPDATE` 锁序把撤权与副作用线性化**（`authorization.ts:24`、
   `authorization.ts:49`）。这是「一边有人在撤 agent 的权、一边这个 agent 正在写观测数据」
   的并发竞态，还要给并发批次统一锁顺序防死锁。MAOS 单进程串行执行，没有这个竞态；
   抄进来只会得到一堆无法被触发、因而也无法被验证的代码。

4. **七引擎适配器矩阵**（`engine.ts:320` 的 `EngineId` 七个取值，各自一套 Adapter/Session，
   `engine.ts:1364`/`1934`/`2374`/`2767`/`3161`/`3734`/`4182`）。向后兼容包袱：cumora 早期
   支持了这些引擎，现在删不掉，只能降级成 compatibility opt-in。MAOS 没有存量用户，
   `ModelClient` 一个就够。**这条同时是个警告**：cumora 为这七个引擎付出的代价，
   在 `engine.ts` 里是 4597 行中的大部分。

5. **兼容档本身**（`engine.ts:330` 的 `CUMORA_BYOA_ALLOW_UNSANDBOXED`）。它存在是因为
   「已经有人在用老引擎跑生产」。MAOS 没有存量，直接只留安全档就行。
   这条要点出来是因为它很容易被误抄：看到「危险选项要显式且有成本」这个漂亮设计，
   顺手就把逃生口一起抄了 —— **该抄的是 `failIfUnavailable` 的方向，不是逃生口。**

6. **每 agent 一个持久 home + 引擎原生 memory/skills 目录**（`cumora:docs/BYOA.md:313`）。
   它解决的是「引擎 CLI 有自己的记忆与技能格式（`CLAUDE.md` / `AGENTS.md` / `.claude/skills/`），
   要顺着它走」。MAOS 的 Skill 层和 kb 是自己的，agent 是同进程对象 —— 给每个 agent 造一个
   磁盘 home，会凭空多出一个需要清理、需要隔离、需要备份、还需要在证据束里脱敏的状态面。
   MAOS 现在的「一次性 workdir」（`maos/tools/sandbox.py:188`）在这件事上是更好的默认。

## 5. 我没看懂 / 没时间看的

- **`/runtime/cli` 的服务端侧**（路由、argv 解析、每次调用的审计与限流）不在本轨读单里，
  我没读。所以「cumora 有没有 MAOS `ToolInvoked` 那样的逐次调用审计」这个问题我答不上来，
  第 2 节末行标了「不可比」而不是「MAOS 更强」。
- **`--restricted` 与 Codex 权限档在引擎 CLI 内部怎么落到 OS 机制上**（Seatbelt / bubblewrap /
  seccomp）我没有验证。我只看到 cumora 这一侧传了什么参数，没看到参数真的生效。
  所以第 1 节表里的「绕过成本」是按**设计意图**给的，不是实测 —— cumora 自己也意识到这一点，
  版本闸（`engine.ts:331`）正是为「参数被静默忽略」准备的兜底。
- **`engine.ts` + `daemon.ts` 共 8595 行**，按派单只读了沙箱与凭据相关的函数。
  进程树终止那一组（`engine.ts:144` `terminateWindowsTree` / `engine.ts:162` `terminatePosixTree` /
  `engine.ts:222` `terminateEngineTree`）只扫了签名和注释，没细读；它与「模型 spawn 的
  子进程逃出进程组」这个边界问题相关，但归属更接近 T40/T44 的运行时面。
- **`agent-fuse/main.go` 490 行只读了头 20 行的设计说明和 HTTP 客户端壳**，
  文件系统语义（一致性、缓存、并发写、错误映射）完全没看。反向清单 #1 的判断是基于
  它的**动机**（写在头部注释里），不是基于它的实现质量。
- **我没跑过 cumora 的任何测试。** 所有测试相关的结论都来自读断言文本，例如
  `server/src/__tests__/agents-computer-engine.test.ts:157`（`Claude secure mode is
  fail-closed and strips tool credentials`）在 `:221` 断言了 `OPENAI_API_KEY` 出现在 deny 列表里、
  在 `:222` 断言了 settings 文本里不含哨兵字符串。这些断言的存在证明了**意图**，
  不证明运行时行为 —— 我没有 Node 环境去实跑。
- **`cli-version.ts` 只读了头部与导出清单**（`server/src/agents/computer/cli-version.ts:26` 起），
  各引擎的版本解析细节（`parseCursorAbout` / `parseGrokCheck` 等）没看，
  因为它们服务的是那七个兼容引擎，已进反向清单 #4。

## 附录 A · 顺手发现的 MAOS 问题

1. **降级路径没有 fail-closed 档。** `sandbox_pytest_run` 在 `_docker_ready()` 返回 False 时
   **无条件**降级（`maos/tools/sandbox.py:591`、`maos/tools/sandbox.py:600`），没有任何配置能
   表达「宁可不跑，也不裸跑」。降级本身被记进 `sandbox_mode` / `degraded_reason` 并进
   `summary`，这比只 `log.warning` 好很多（docstring 在 `sandbox.py:574` 一带解释了为什么），
   但**没有人必须同意**。对照 `engine.ts:1323`。→ 可移植清单 #1。

2. **`security_boundary` 的措辞容易被读成「降级也隔离了」。** 现文（`docs/toolport-contract.md:91`，
   源在 `maos/tools/sandbox.py:678` 一带的 `PYTEST_RUN_PORT` 声明）写的是「降级路径裸
   subprocess，env 按白名单重建（只放行 PATH/LANG，HOME 指向一次性空目录）」—— 每个字都对，
   但**没说的那半句**是：降级路径没有任何文件系统与网络隔离，模型产出的 Python 在 pytest
   collection 阶段就以宿主 uid 执行，能读该 uid 可读的一切**绝对路径**（换掉 `HOME` 不影响
   `/Users/<name>/.ssh` 这种硬编码路径被读到）。这是**文档口径问题，不是代码 bug** ——
   `security_boundary` 是「评审会逐条对」的字段（`docs/toolport-contract.md:19`），
   建议在 ⑦ 里补一句明说降级路径隔离为零。注意该文件由 `scripts/gen_docs.py` 生成，
   要改得改代码里的声明。

3. **`ENV_PASSTHROUGH` 原样透传宿主 `PATH`**（`maos/tools/sandbox.py:80`）。我核过了，
   **这条不构成新增风险**，理由见可移植清单 #7（降级路径本来就在执行模型产出的代码，
   PATH 影子不增加能力）。写在这里只为一件事：让整合轮不必再把这条推导重来一遍，
   也不要因为「cumora 做了 PATH 清洗」就顺手给 MAOS 加一个没有收益的加固。
