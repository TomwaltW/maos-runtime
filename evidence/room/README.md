# generated at 2026-08-29T09:34:18+00:00 from 27c9e18ee736e19102c561796dde9b45ddc84d4b

# `evidence/room/` —— 房间侧人机交互证据

采集时间与 git sha 见首行。**采集人手写，不是脚本产物** —— 房间证据的载体是截图，
没有生成器可言（对比 `evidence/scenario-*/` 由 `scripts/make_evidence.py` 与
`maos.kb.experiment` 自动产出并自动写头）。首行仍按库内惯例保留，记的是**采集**
时刻与 sha，不是生成时刻。这条判断已记 `docs/DECISIONS.md` 的 `## task-C4`。

例外是同目录的 `transcript.md`：它**是**脚本产物 —— 从房间消息历史整份拉下来直接落盘，
中间没有人手编辑。出处命令写在那份文件的抬头。

重跑手册：`docs/matrix-room-runbook.md`。

---

## ✅ 当前状态：五张图已采集（T 轮，2026-08-29）

房间真接通了，三条路径全部在**真 Matrix 房间**里跑通，五张图与逐字副本齐。

采集环境（值一律不入库，见「脱敏口径」）：

```
homeserver : <redacted>           # 本机自建 Synapse v1.159.0，宿主机口径
room_id    : <redacted>           # 名「MAOS 审批」，非加密房
bot        : @maos-bot:maos.local
approver   : @boss:maos.local     # MAOS_APPROVERS 名单内
outsider   : @intern:maos.local   # 名单外
入口       : ~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case {approve,reject}
```

开跑前逐条验过的前置（全部实测，不是推理）：

```
cat ~/.maos-matrix/STATUS                                   READY 2026-08-29T05:52:11Z
docker ps                                                   maos-synapse / maos-element 均 healthy
GET /_matrix/client/versions                                HTTP 200
GET /_matrix/client/v3/account/whoami                       HTTP 200  user_id=@maos-bot:maos.local
GET /rooms/<room>/state/m.room.encryption                   HTTP 404  M_NOT_FOUND  ← 未加密，可用
GET /rooms/<room>/joined_members                            @boss / @intern / @maos-bot 三人在房
```

两轮实跑的终态：

| 用例 | 入口 | 终态 | 房间消息 |
|---|---|---|---|
| R1 顺利路径 | `--case approve` | `task=DONE  plan=DONE`，exit=0 | 23 条 |
| R2 失败路径 | `--case reject` | `task=FAILED  plan=FAILED`，exit=0 | 18 条 |
| 越权用例 | 含在 R1 那一轮里 | 两次 `无审批权限`，闲聊零回复 | 见 `05` |

🔴 **必须用 `~/.maos-matrix/venv/bin/python`**。系统 `python3` 没装 matrix-nio，
`_NioChannel` 构造即 `ImportError` → 静默降级 log-only，终端照常刷「房间消息」而房间里
一条都没有。这是本目录最容易被伪造的一步，理由见下一节。

---

## 🔴 为什么这份证据可信：它不是从终端抄的

`hiclaw.matrix_bus` 的降级模式（`log_only=True`）会把「本该发进房间的每一条消息」
**原文**打到 stdout。截那个终端窗口、或把那份输出抄进 `transcript.md`，
形态与真房间证据**一模一样、无法分辨**。

上一轮（C 轮）房间没接通，本目录当时**主动拒绝**了这么干，宁可交一份「手册齐、图待补」。
那条口径本轮完整继承，只是结论反过来了 —— 现在有真房间，所以交的是真的：

- **截图**：Element web（`http://localhost:8080`）里 `@boss` 的真实视图，
  房间是 `MATRIX_ROOM_ID` 那间非加密房。不是终端窗口。
- **逐字副本**：`transcript.md` 从 Matrix client-server API 的
  `/rooms/<room>/messages?dir=b` 整份拉下来，**不是** stdout。出处命令写在那份文件抬头。
- **审批命令**：`@boss` / `@intern` 两个真实 Matrix 账号发的真实房间消息，
  经 `RoomApprovalBridge` 的名单校验后真的改了任务状态。

判据一句话：**降级模式下 `transcript.md` 根本拉不出来** —— 没有房间就没有房间历史。
这份文件能存在，本身就是「消息真进了房间」的证明。

---

## 截图清单

每张图在下表里有且只有一句话，说清它证明 `docs/EXECUTION.md` Phase 4 验收的哪一条。
**证明不了任何一条的图不进本目录。**

| 文件 | 状态 | 证明哪一条验收 |
|---|---|---|
| `01-approval-card.png` | ✅ 已采集 | 「Element 里看到全过程」—— 审批卡形态：一行人话摘要 + **展开的** Envelope 折叠块（可见 `effect_risk: "H"`、`state: "BLOCKED"`、`title: "变更生产环境配置"`），末尾逐字列出 `/approve` `/reject` 两条可用指令 |
| `02-transitions.png` | ✅ 已采集 | 同上，且证明状态迁移是**逐条**镜像的：`PENDING → DISPATCHED → RUNNING → AWAITING_REVIEW → BLOCKED` 五条各自一条消息，不是跑完补一条总结 |
| `03-approve-effect.png` | ✅ 已采集 | 「发 `/approve` → DONE」—— boss 发 `/approve task_5a1469c54bbe`，回执 `已批准 …（操作人 @boss:maos.local）`，`BLOCKED → DONE`，Plan `RUNNING → DONE` |
| `04-reject-compensation.png` | ✅ 已采集（**名不副实，见下**） | 「`/reject` → 驳回生效」—— 回执 `已驳回 …，原因：渠道回执异常，转人工`，`BLOCKED → FAILED`，Plan `RUNNING → FAILED` |
| `05-denied-outsider.png` | ✅ 已采集 | 「只接受 `MAOS_APPROVERS` 名单内用户，其余回『无审批权限』并记 event_log」—— intern **两次**被拒（一次带 task_id、一次缺参数），中间 boss 的闲聊**零回复** |

### 🔴 `04` 的文件名比它能证明的东西大

文件名写着 `compensation`，但**房间里拍不到补偿**。实测依据：

- `CompensationExecuted` 走的是 `store.append_event_log()`，**从不 publish**，
  因此永远不进房间镜像（房间只镜像状态迁移与总线事件）。
- 成功路径上**一行日志都不打**，终端同样看不见。
- `ok=true` 和 `ok=false` 的退出码**都是 0**。

所以这张图证明的是「驳回生效 + Plan 落 FAILED」，**不是**「补偿执行了」。
更要紧的一条：照 `docs/matrix-room-runbook.md` §2 建一个空 workdir 跑，
实测那条 `CompensationExecuted` 是 **`ok=false`**（`stage=apply`，
`auth/session.py: No such file or directory`）—— 因为 `room_demo` 从头到尾没把
正向补丁打进那个目录过，反向应用无从谈起。

**写材料时别把这张图说成补偿的证据。** 补偿的证据在 `evidence/scenario-7/`
（机器侧、有库、进 `verify.py` 七项核验）。

文件名 T 轮**没有改** —— `docs/EXECUTION.md:499/502` 与本文件都按这个名字引它，
改名要一起动三处，而那两处不在 T 轮可改面内。已记 `docs/BACKLOG.md` 的 `## task-T4`。

### 采集窗口：房间里还有 14 条不属于本次的消息

边界 event_id `$KI0ij47tDAxI68MYTdosnSmyygg1jjOEq2eiXLflyxg` **及其之前的 14 条**，
是上一轮中断运行的遗留（审批卡发出后无人审批，进程被结束）。
`transcript.md` 只收边界之后的 41 条；五张截图取的也都在边界之后
（认 `task_5a1469c54bbe` / `task_02695e4aac86` 两个 id 即可区分）。

那 14 条**没有删**。房间历史不该为了让证据好看而被修剪 —— 删了反而要多解释一次。

---

## 命名对照：R1/R2 与 `scenario-6/7` 是**互补**，不是缺了两个目录

`docs/EXECUTION.md` 的 Phase 4 用 R1 / R2 指代两条退款路径，容易让人以为
`evidence/` 下少了 `scenario-R1/` 和 `scenario-R2/` 两个目录。**没有少。**

| 手册里的代号 | 实际场景号 | 说的是什么 | **数据**证据在哪 | **房间**证据在哪 |
|---|---|---|---|---|
| R1 | `--scenario 6` | 退款顺利路径 | `evidence/scenario-6/` | 本目录 `01`–`03` |
| R2 | `--scenario 7` | 退款失败路径（渠道异常 → replan → 驳回 → 补偿） | `evidence/scenario-7/` | 本目录 `04` |

两侧证明的是不同的事，缺一不可：

- `evidence/scenario-6,7/` 是**机器侧**证据 —— `maos.db` + `trace.json` + `result.json`，
  由 `scripts/make_evidence.py` 自动生成、带出处头、进 `scripts/verify.py` 的七项核验
- `evidence/room/`（本目录）是**人机交互侧**证据 —— 事件真的镜像进了 Matrix 房间，
  真人在房间里打了 `/approve` / `/reject` 并且判定真的生效了

后者是前者**证不了**的：`trace.json` 里能看到一条 `human_reject`，但看不出那个
决定是从一间真实房间里、由一个真实账号、在名单校验通过之后做出的。

⚠️ **但也别把对照表读过头**：本目录的截图跑的是 `room_demo`，那是个 `role=coding`
的软件域任务（标题「变更生产环境配置」），**不是**退款域场景 6/7 本身。
它证明的是「审批闸这套人机交互成立」，退款域的业务终态仍以 `scenario-6,7/` 为准。

🔴 **截图不许放 `evidence/scenario-R1/` 或 `evidence/scenario-R2/`**，
哪怕手册第 499/502 行是那么写的。理由是实测出来的，见 `docs/DECISIONS.md`
与 `docs/BACKLOG.md` 的 `## task-C4`：`verify.py::load_cases` 会把 `evidence/` 下
**任何** `scenario-` 开头的目录当成证据束，逐个要求 `maos.db` + `trace.json` +
`result.json`；放一张图进去，整个 `verify.py` 当场 `exit=2` 进不去核验 ——
「7/7 PASS」这条头号卖点会一起没掉。本目录名 `room` 不以 `scenario-` 开头，
因此对 `verify.py` 完全透明。

---

## 脱敏口径

🔴 **PNG 扫不到。** `scripts/make_evidence.py::scan_for_secrets` 只扫文本，
截图里的 token 它一个字都发现不了；而图一旦进了 git 历史就取不出来。
所以脱敏必须在**按快门那一刻**做完，事后补不了。逐条要求见
`docs/matrix-room-runbook.md` §7，本轮实际做到的：

- 截图**只截浏览器视口**，不截整屏 —— 地址栏、标签页标题、书签栏、桌面一概不在画面里
- Element 的设置页 / 账号页**一次都没开**（access token 在那里）
- 五张图里没有任何终端窗口，因此不存在 scrollback 泄漏 token 的问题
- 画面里出现的标识只有 `@boss` / `@intern` / `maos-bot` 三个显示名与房间名「MAOS 审批」，
  这三者在 `docs/hiclaw-probe.md` 里本来就是明文

`transcript.md` 里 token 一律写成 `<redacted>`；生成脚本内置五类模式的脱敏自检，
扫到真值就拒绝落盘（本轮实跑：五类全部无命中）。

落盘后自查（本轮实跑输出附在每条后面）：

```sh
grep -rIl . evidence/room/
#   evidence/room/transcript.md
#   evidence/room/README.md          ← 只命中两个 .md，5 个 PNG 都没出现，符合预期

grep -rnE "syt_[A-Za-z0-9_-]{6,}|Bearer [A-Za-z0-9_-]{8,}" evidence/room/*.md
#   （无输出）

git status --short                   # 不许出现 room.env / creds.txt / 任何 token
```

⚠️ 上一版这里写的是 `grep -rn "syt_\|Bearer \|access_token" evidence/room/*.md   # 应无输出`。
那条**恒有输出** —— 本文件和 `transcript.md` 的正文里就带着 `Bearer $MATRIX_TOKEN`
这样的**占位符**和 `access_token` 这个词本身。判据永远红，看的人只会学会忽略它。
上面换成了只认**真值**的形态（`syt_` 后面跟真串、`Bearer` 后面跟真串）。

补图后必跑：

```sh
python3 scripts/make_evidence.py                   # ← 不能省，理由见下
python3 scripts/verify.py 2>&1 | tail -3           # 期望 RESULT: 8/8 PASS，exit=0
                                                   # 报错原文不许出现 scenario-R1 / scenario-R2 / room
```

⚠️ `make_evidence.py` 那一行是判据成立的前提。干净检出里没有
`evidence/scenario-*/maos.db`（`.gitignore` 挡着），直接跑 `verify.py` 会在第一个证据束上
`[FAIL] 无法开始核验：缺数据库 …`、`exit=2` —— 报错里当然也不会出现 `room` 字样，
于是这条自检**永远通过却什么都没验到**，恰恰漏掉它唯一要防的那件事。
这是 H 轮记在 `docs/BACKLOG.md` 里的账（当时本目录不在那一轨可改面内），T 轮补上。

跑完记得还原证据束，别把重跑产物混进提交：

```sh
git checkout -- evidence/scenario-1 evidence/scenario-2 evidence/scenario-3 \
                evidence/scenario-4 evidence/scenario-5 evidence/scenario-6 \
                evidence/scenario-7 evidence/scenario-R5
```

## `transcript.md` 的作用

PNG 不能 grep，评委没法在图里搜一个 `task_id`。`transcript.md` 是这些截图的
**可检索镜像** —— 房间消息的逐字文本副本，41 条全收，含每条的完整 Envelope JSON。
**两者必须一致**：图里有的话文本里就得有，文本里有的话图里得能找到。
不一致的那一刻，两份证据互相拆台，都不算数。

本轮的一致性靠这一条保证：**两者同源**。截图截的是 Element 渲染的房间时间线，
`transcript.md` 拉的是同一间房同一段时间的消息历史，中间没有第二份数据。
