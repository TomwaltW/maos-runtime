# cumora 解析 · 沙箱与凭据边界 （T42 · 基线 cumora@1e883f6 / MAOS@926aa7b）

## 1. 它是怎么做的

BYOA 要解决的是一句很不舒服的话：**让别人的 Claude Code 跑在别人的 Mac 上，服务端既要指挥它、
又不能持有它的 provider 凭据、还不能让它越权碰宿主。** cumora 的答案不是「一层沙箱」，
而是三道方向不同的 fail-closed 闸，加一条把凭据整个搬出模型进程的绕行通道。
文档把这四件事收在 `docs/BYOA.md:486` 的 `## Boundaries` 一节，代码在
`server/src/agents/computer/engine.ts` 与 `daemon.ts`。值得注意的是那一节里**没有一条是
「告诉模型不要做 X」** —— 全是机制。

在三道闸之前还有一道**准入闸**，决定「谁有资格跑」。`SANDBOXED_ENGINE_IDS` 只有 claude 和
codex 两个（`engine.ts:329`），其余五个引擎默认不是「在 UI 里藏起来」，而是**从可运行清单里
被删除**：`runnableEngineIds()`（`engine.ts:344`）直接过滤，所以服务端就算给这台机器派了个
grok 的活也执行不了（`docs/BYOA.md:272`）。白名单之上再叠一道版本闸 ——
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
（`engine.ts:1319` 的 deny 与 `engine.ts:1356` 的 `--disallowedTools`）。没有 shell 工具就没有
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
（`docs/BYOA.md:277`）。与之配套的是 `sandbox.failIfUnavailable: true`（`engine.ts:1323`）：
**OS 沙箱起不来就让这一轮失败，不降级。**

### 哪些是真边界，哪些是软约束

派单要求把这条分清楚，因为「提示词里写了不许」不是边界。

| 层 | 具体机制 | 真边界？ | 绕过成本 |
| :-- | :-- | :-- | :-- |
| OS | Claude `--restricted` 沙箱 / Codex 权限档的文件系统与网络限制（`engine.ts:1293`、`engine.ts:1621`） | **是** | 需要一次 OS 沙箱逃逸（Seatbelt / bubblewrap）。前提是引擎 CLI 真的落到了 OS 机制上 —— cumora 只能传参数，验证不了，所以才有版本闸兜底 |
| 运行时（引擎进程） | 不给 `Bash`/`PowerShell`/`WebFetch`/`WebSearch` 工具（`engine.ts:1356`）；只暴露一个结构化 `cli(argv)` MCP 工具 | **是** | 模型没有可执行任意命令的工具面，只能在给定 schema 内提参数 |
| 运行时（daemon 进程） | token / URL / Authorization / 重定向 / 刷新全部 daemon 私有（`daemon.ts:1707`）；桥与 IPC 在 home 之外（`daemon.ts:1488`） | **是** | 要先拿到 daemon 进程的内存或写权限 —— 已经不是「模型越权」而是「宿主被攻陷」 |
| 进程启动 | PATH 清洗、`.runtime-token` 残留清理、拒绝符号链接目录（`daemon.ts:1158`、`daemon.ts:1632`） | **是**（加固性质） | 单独看不构成边界，是在堵前三道的绕行入口 |
| 配置优先级 | Codex `trust_level="untrusted"` 写在 CLI 层（`engine.ts:1640`）；Claude `--strict-mcp-config` + 忽略用户/项目设置 | **是** | 模型在 home 里写什么配置都不生效，因为 CLI 参数优先级更高 |
| 提示词 | standing prompt、persona、`GLANCE_YIELD_RULES`、「发言前先扫一眼」这类纪律（`docs/BYOA.md:169` 一带） | **否，软约束** | 模型不照做即失效。cumora 自己也没把它写进 `## Boundaries` —— 那一节全是机制 |

一句话判据：**`## Boundaries`（`docs/BYOA.md:486`）里列出来的都是真边界，
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
| 子进程凭据闸：白名单反推 deny 清单（`engine.ts:1286`、`engine.ts:1294`）；`HOME` 换成 agent home（`engine.ts:1610`） | `_clean_env()` 白名单重建 env，只放行 PATH / LANG，`HOME` 指向一次性空目录（`maos/tools/sandbox.py:80`、`maos/tools/sandbox.py:116`） | **同构** —— 连「按名字放行而不是按名字拦截」的理由都一样（MAOS 的 docstring 在 `sandbox.py:126` 一带写了同一条） |
| 不给 shell 工具，只给结构化 `cli(argv)`（`engine.ts:1356`、`daemon.ts:883`） | 模型不执行任意命令，产出只有补丁与被 pytest 收集的代码；工具面是九要素 `ToolPort`（`maos/tools/port.py:22`） | **同构**（结论一致，路径不同：cumora 靠关掉工具，MAOS 靠压根没有这个工具） |
| 凭据绕行：token / URL / 重定向全部 daemon 私有，模型进程 env 无 token（`daemon.ts:1707`、`daemon.ts:1753`） | `ModelClient` 持 key，Agent 只能调 `ask()`（`maos/model/client.py:183`、`maos/agents/base.py` 的 `ask`） | **形似神不同** —— MAOS 是同进程，key 与模型输出在同一地址空间；靠的是「不给模型执行面」，不是进程边界 |
| 重定向不出 daemon（`daemon.ts:1707` 的 "identity, URL, Authorization, redirects … remain daemon-owned"） | `_SameOriginRedirectHandler` 只放行同 origin 的 3xx，换 scheme / 主机 / 端口一律拒（`maos/model/client.py:148`、`maos/model/client.py:161`） | **同构**，MAOS 这一处更细（把 https→http 降级也算换 origin） |
| 沙箱不可用即失败：`failIfUnavailable: true`（`engine.ts:1323`） | 容器不可用即**自动降级**为裸 subprocess，`log.warning` + 写进 `sandbox_mode` / `degraded_reason`（`maos/tools/sandbox.py:591`、`maos/tools/sandbox.py:600`） | **方向相反** —— cumora fail-closed，MAOS fail-open 但**响亮**（降级进报告，不只进日志） |
| per-agent 持久 home，模型可写（`daemon.ts:54`、`docs/BYOA.md:313`） | 每次任务一个一次性 workdir（`maos/tools/sandbox.py:188` 的 `prepare_sandbox_workdir`），无 per-agent 持久 home | **形似神不同** —— MAOS 的 agent 隔离是**能力隔离**（`allowed_tools` / `allowed_skills` / `write_scope`），不是文件系统隔离 |
| 桥接可执行文件与 IPC 目录放在模型可写面之外（`daemon.ts:1488`、`daemon.ts:1492`） | `ToolPort.entry` 是进程内函数，不存在「模型可写面」这个概念（`maos/tools/port.py:26`） | **MAOS 没有**（当前也不需要；MCP 迁移之后才有落点，见 `docs/toolport-contract.md:95`） |
| PATH 清洗：滤掉可写目录、空条目、相对条目（`daemon.ts:1158`） | 降级路径把宿主 `PATH` 原样透传（`maos/tools/sandbox.py:80`） | **形似神不同**（评估见第 3 节 #7 —— MAOS 这里不构成新增风险） |
| 三层凭据：company 配对码 → 设备 token（sha256 存 `credential_hash`）→ 每 agent 2h JWT（`registry.ts:231`、`registry.ts:233`、`registry.ts:500`） | 无。单机演示，无设备配对面 | **MAOS 没有** |
| 撤销即抹哈希：`revoked_at` 与 `credential_hash = NULL` 同时写（`registry.ts:753`） | 无 | **MAOS 没有** |
| 令牌不是权威：JWT 里的租户只是提示，每次回查 `participants` 活行（`authorization.ts:4`、`authorization.ts:9`） | `AgentIdentity` 是 `frozen=True` dataclass 常量（`maos/agents/base.py:60`），铸造即快照，无回查 | **MAOS 没有**（单进程下 Identity 就是代码常量，暂无此需求；但这条与铁律 8「不持有权威事实」是同一个道理，见第 3 节 #5） |
| 每 agent 一个 wake-stream，云端 pod + Go FUSE 挂服务端工作区（`agent-fuse/main.go:1`） | 无 | **MAOS 没有**（见第 4 节反向清单 #1） |
| `npx cumora` 单文件分发 + `--install-service` + `--doctor`（`docs/BYOA.md:460`、`docs/BYOA.md:480`） | `python3 run.py` 薄入口 + `scripts/verify.py` 收口证据 | **形似神不同** —— MAOS 无分发面，但 `--doctor` 那个「开跑前一问」的位置是空的 |
| 每次工具调用的服务端审计 | `invoke_tool()` 无论成败落一条 `ToolInvoked` event_log 行，入参过 sha256 摘要（`maos/tools/port.py:43`） | **不可比** —— cumora 的 `/runtime/cli` 服务端侧不在本轨读单里，我没读，不下结论（见第 5 节） |
