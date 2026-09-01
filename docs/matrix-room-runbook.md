# Matrix 房间演示 runbook

从零到截图的完整重跑手册。目标读者是**没跑过这套东西的人**：照着做一遍，
应当拿到 `evidence/room/` 下那五张图，以及与之逐字对应的 `transcript.md`。

- 适用版本：基线 `f42ea83`（C 轮）写就；**`27c9e18`（T 轮）照本手册真跑过一遍**，
  与实跑对不上的步骤已按实跑改正，逐条见文末「修订记录」
- 依赖三轨：C-1 出房间与凭证、C-2 修 `hiclaw/matrix_bus.py` 的真连通、C-3 出 `hiclaw/room_demo.py`
- 本机没有 `python` 命令，一律 `python3`

## 🔴 开跑前必读：用哪个 python 决定了这一整轮是真是假

**跑 `room_demo` 必须用 `~/.maos-matrix/venv/bin/python`，不是系统 `python3`。**

系统 `python3` **没装 matrix-nio**，`_NioChannel` 构造即 `ImportError`，上游当场降级
log-only —— 于是场景照跑、exit=0、终端上「房间消息」一条不落地全打出来，
而房间里一条都没有。这是 §0 那张表的第一行，且是本手册最容易踩、后果最贵的一步：
截那个终端窗口当证据，形态与真房间证据**无法分辨**。

T 轮实测（同一份 env，只换解释器）：

```bash
$ python3                        <check_wiring>   matrix-nio = 不可导入 -> No module named 'nio'
$ ~/.maos-matrix/venv/bin/python <check_wiring>   matrix-nio = 可导入
```

⚠️ `MatrixBusConfig.from_env()` 两边都报 `log_only=False` —— **配置对不等于通道通**。
`log_only` 只看四个 env 齐不齐，装没装 nio 它不知道。别拿这个字段当「接通了」的判据，
判据是**房间里真出现了消息**。

✅ **这一条现在会自己拦下来**（`926aa7b` 之后改的，见文末第二份修订记录）。
`room_demo` 开工先打一行自检；且「`MATRIX_*` 配齐了却没接通房间」不再跑完降级流程，
直接 **exit 4**（`EXIT_NO_ROOM`）：

```text
$ python3 -m hiclaw.room_demo --case approve --auto-approve
[自检] 解释器 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
       matrix-nio 不可导入（ModuleNotFoundError: No module named 'nio'）—— 进不了真房间
ERROR maos.matrix  Matrix 房间没接通：MatrixDepMissing: 当前解释器没装 matrix-nio：…
[没进房间] 当前解释器没装 matrix-nio，房间根本没接通。
  改用装了它的那个重跑：/Users/shensikai/.maos-matrix/venv/bin/python -m hiclaw.room_demo ...
exit=4
```

真要做无房间自检，显式加 `--allow-degraded`，退出码回到 0。
`log_only` 那条判据仍然成立，只是它不再是**唯一**的防线。

---

## 0. 先读这一节：五种「安静地什么都没发生」

这套链路**所有的失败都是静默降级**，没有一个会打红字、没有一个会卡住。
屏幕上看起来一切正常，只是 Element 房间里一条消息都不出现。
不知道这一点的人会以为程序挂了，去按 Ctrl-C —— 那会把一次好好的运行掐掉。

| 症状 | 真实原因 | 怎么确认 | 见 |
|---|---|---|---|
| 房间里**什么都没有**，终端刷 `[没进房间]`、**exit=4** | 房间开了端到端加密（E2EE）。`_NioChannel._verify_room()` 查到 `m.room.encryption` 状态事件就抛 `RoomEncrypted`，上游降级 log-only | `[没进房间]` 那段的「原因：」一行写着 `RoomEncrypted`；房间设置里「加密」是开的 | §7.1 |
| 房间里**什么都没有**，终端照常跑完 **exit=0** | 四个必填 env 漏了任意一个。这一档**不拦**：它是明确的降级自检意图 | 终端首行 `WARNING maos.matrix  Matrix 配置缺 <变量名>，降级 log-only（不进房间，行为等同进程内总线）` | §7.2 |
| 房间有消息，但 `/reject` 回的是**「审批未生效」** | `MAOS_SANDBOX_WORKDIR` 没设。补偿执行器**硬失败** | 回执原文含 `审批未生效：<task_id> —— ` | §7.3 |
| 终端刷 `[没进房间] 当前解释器没装 matrix-nio`、**exit=4** | 用的是**系统 `python3`**。`_NioChannel` 构造即 `ModuleNotFoundError`，被翻成 `MatrixDepMissing` | 开工第一行 `[自检]` 就写着解释器路径与 `matrix-nio 不可导入` | 抬头 |
| 房间里消息**齐的**，终端却打 `Matrix 镜像超时（RoomSendTimeout: …）` | **虚警**。Synapse 限流 429，nio 退避重试拖过 send 超时（30s），而协程仍在后台把消息送达 | 告警原文自带「不要重跑」与「未计入失败次数」；去房间里数消息，一条不少 | §7.6 |

**记住这条判据**：`log_only=True` 时行为**等同进程内总线**，场景照跑。
「跑通了」和「进房间了」仍然是两件事 —— 只是在 `room_demo` 这个入口上，
退出码现在**能把两者分开**：想进房间却没进成 = 4，从没打算进 = 0。
别把这条推广到别的入口，那边 `log_only` 照旧只是一行 WARNING。

**还有一条反向判据**（T 轮加的）：终端打了 `房间回话失败` 也**不**代表消息没进房间。
两个方向都不能只看终端 —— 唯一算数的判据是**去房间里数消息**。

---

## 1. 起服务

**不在这里抄一遍步骤** —— 抄了就会和 C-1 的实际产物漂移。照 `deploy/synapse/README.md` 执行。

跑完应当拿到（三样都在**仓库外**，`chmod 600`，永不入库）：

```text
~/.maos-matrix/STATUS      # 一行状态；这里必须是 READY <ISO8601>
~/.maos-matrix/room.env    # 八个键，可 source
~/.maos-matrix/creds.txt   # boss / intern 的 Element 登录口令
```

`room.env` 的键名（值一律由 C-1 现取，**不许占位符**）。
⚠️ 早期版本写「七个键」，T 轮实测是 **8 个** —— 后来补了 `MATRIX_ROOM_ID_ENCRYPTED`：

| 键 | 用途 | 缺了会怎样 |
|---|---|---|
| `MATRIX_HOMESERVER` | 宿主机跑 python 时是 `http://localhost:8008` | 降级 log-only |
| `MATRIX_USER` | `@maos-bot:maos.local` | 降级 log-only |
| `MATRIX_TOKEN` | bot 的 access token | 降级 log-only |
| `MATRIX_ROOM_ID` | `!xxxx:maos.local`，**非加密**的那间，演示用的就是它 | 降级 log-only |
| `MAOS_APPROVERS` | `@boss:maos.local`，逗号分隔 | **不降级**，但所有审批命令都被拒 |
| `MAOS_MATRIX_OUTSIDER` | `@intern:maos.local`，越权用例用 | 只影响 §6 那一步 |
| `MAOS_ELEMENT_URL` | `http://localhost:8080`，截图时开的那个地址 | 只影响人，代码不读 |
| `MATRIX_ROOM_ID_ENCRYPTED` | 一间**特意开了 E2EE** 的房，用来验「撞加密房会当场降级」这条 | 只影响那条针对性测试 |

🔴 **别把 `MATRIX_ROOM_ID_ENCRYPTED` 当演示房。** 它存在的意义正好相反 ——
拿它跑 `room_demo` 的结果就是 §0 第一行：终端一切正常、房间里一条消息都没有。

⚠️ `MATRIX_HOMESERVER` 有两个口径且**都对**：宿主机跑是 `http://localhost:8008`，
容器内跑是 `http://host.docker.internal:8008`。用错场合的症状就是 §0 第一行 —— 静默降级。

**开跑前先确认房间没加密**：

```sh
. ~/.maos-matrix/room.env
curl -s -H "Authorization: Bearer $MATRIX_TOKEN" \
  "$MATRIX_HOMESERVER/_matrix/client/v3/rooms/$MATRIX_ROOM_ID/state/m.room.encryption"
# 期望 M_NOT_FOUND —— 查不到才是「未加密」。查得到就是加密房，本轨不装 matrix-nio[e2e]，必须重建一个非加密房
```

T 轮实测输出（`MATRIX_ROOM_ID` 这间）：

```text
HTTP 404  {"errcode":"M_NOT_FOUND","error":"Event not found."}
```

顺带把另外两条也验了（都不回显 token，可以放心截）：

```text
GET $MATRIX_HOMESERVER/_matrix/client/versions            -> HTTP 200
GET $MATRIX_HOMESERVER/_matrix/client/v3/account/whoami   -> HTTP 200  user_id=@maos-bot:maos.local
```

`whoami` 这条值得单跑：它是**唯一**能当场分清「token 失效」和「房间不对」的探针。
两者的最终症状都是降级，但一个该去换 token，一个该去查房间 id。

🔴 这条命令会把 token 打进 scrollback。**跑完立刻 `clear`，再开始截图**（见 §7）。

---

## 2. 装环境变量

```sh
. ~/.maos-matrix/room.env
export MAOS_SANDBOX_WORKDIR=/private/tmp/maos-room-demo
mkdir -p "$MAOS_SANDBOX_WORKDIR"
```

🔴 **`MAOS_SANDBOX_WORKDIR` 必须在跑 `/reject` 之前设好，且目录必须真实存在。**

这不是可选项，也不是 bug。取不到这个变量时补偿执行器抛 `ValueError`，
房间回执会变成「审批未生效」而不是「已驳回」，整个 R2 用例的证据当场作废。
缺省值曾经是 `"."`（仓库根），合并期由人类裁决改成**必填**，
理由与回归守卫见 `docs/BACKLOG.md` 的 `## merge-p2` 第 3 条 ——
守卫是 `test_missing_workdir_env_raises_instead_of_guessing`，谁把缺省加回来它立刻红。

按 C-3 的约定，`room_demo --case reject` 在**启动时**就检查这个 env，缺了当场报错退出，
不会让人在 Element 里打完 `/reject` 才发现。

### 🔴 但「目录存在」只够过启动检查，不够让补偿真成功

T 轮实测：照上面这三行做（`mkdir -p` 一个**空目录**），`/reject` 之后
`event_log` 里那条 `CompensationExecuted` 是 **`ok=false`**：

```json
{"mode": "reverse", "ok": false, "files": 1,
 "workdir": "/private/tmp/maos-room-demo",
 "error": {"stage": "apply", "path": "auth/session.py", "hunk": null,
           "message": "error: auth/session.py: No such file or directory"}}
```

原因不难懂：补偿是**把正向补丁反着打一遍**（`git apply -R`），而 `room_demo`
从头到尾**没有把正向补丁打进这个目录**过 —— 它用的是 `seed_scripted_report`
预置报告，不走 `verify_patch_in_sandbox`。空目录里没有 `auth/session.py`，
反向应用当然失败。

**这不是配置错了，是 `room_demo` 本来就到不了 `ok=true`。** 已记
`docs/BACKLOG.md` 的 `## task-T4`。

对照实测：把 workdir 备成「靶场基线 + 已打正向补丁」，同一条驳回路径就是 `ok=true`：

```json
{"mode": "reverse", "ok": true, "files": 1, "error": null}
```

（这一条是**离线台架**量的，不是 `room_demo` 的行为 —— 台架自己先
`prepare_sandbox_workdir()` 再 `sandbox_git_apply(patch_set, workdir)` 打了正向补丁。
写在这里是为了说明「ok=false 不是补偿坏了」，别照抄成演示步骤。）

🔴 **最要命的是这件事没有任何人看得见**：`ok` 是 `true` 还是 `false`，
终端不打、房间不显、退出码一样是 0。见 §5 与 §7 对 `04` 那张图的说明。

---

## 3. 登 Element

用 **boss** 账号（口令在 `~/.maos-matrix/creds.txt`），加入 C-1 建的那间房。

🔴 **不要打开设置页 / 账号页** —— 那里有 access token，一截屏就进了 git 历史，
而 PNG 是二进制，`make_evidence.py::scan_for_secrets` 一个字都扫不到（见 §7）。

---

## 4. R1 顺利路径（`--case approve`）

```sh
~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case approve --timeout 300
```

### 房间里依次出现什么

每条镜像消息都是 **`m.notice`**（不触发推送提醒），形态是「一行人话摘要 + 折叠的 Envelope JSON」。
折叠是必需的：一个 plan 跑几十条事件，不折叠的话人翻不到那条要审批的。

🔴 **T 轮更正**：本节原先逐字列的是 `task-s7-payment` 那一串（`role=payment`、
`status=FAILED`、`verdict=REWORK`、`amount_claimed`、`api_key`）。那是**退款域场景 7**
的形态，`room_demo` 根本不产 —— 它建的是一个 `role=coding`、标题「变更生产环境配置」、
id 形如 `task_<12位hex>` 的任务。照原样去房间里找 `task-s7-payment` 会一无所获。
下面全部换成 T 轮从**真房间历史**抄回的逐字输出（完整 41 条见
`evidence/room/transcript.md`）。

一次 `--case approve` 房间里依次出现 23 条。开头三条是总线自己的订阅回执，
不是业务事件，别当成漏跑：

```text
订阅 maos.task.result（group=control-plane）
订阅 maos.review.verdict（group=control-plane）
订阅 maos.task.assignment（group=worker-w1）
```

首行摘要的**逐字形态**（`hiclaw.matrix_bus.summarize` 的真实输出）：

```text
[task_5a1469c54bbe] TaskAssignment → maos.task.assignment attempt=1 role=coding
[task_5a1469c54bbe] TaskResult → maos.task.result attempt=1 status=ok
[task_5a1469c54bbe] ReviewVerdict → maos.review.verdict attempt=1 verdict=pass
[task_5a1469c54bbe] StateTransition → AWAITING_REVIEW → BLOCKED attempt=1
[task_5a1469c54bbe] HumanApprovalRequired → 待人工审批 attempt=1
```

中间还夹着几条 `drain 处理 N 条事件` —— 同样是总线回执，不是业务事件。

展开折叠块后是完整 Envelope（`render_mirror` 真实输出，注意 `api_key` 已被出口脱敏成 `***`）：

````text
[task_5a1469c54bbe] TaskAssignment → maos.task.assignment attempt=1 role=coding
```json
{
  "event_type": "TaskAssignment",
  "plan_id": "plan_a9e5af33ed5b",
  "task_id": "task_5a1469c54bbe",
  "idempotency_key": "assign:task_5a1469c54bbe:1",
  "payload": {
    "role": "coding",
    "inputs": {
      "repo": "demo/app"
    },
    "acceptance": [
      "build 通过"
    ],
    "risk_level": "M",
    "rework_findings": []
  },
  "event_id": "evt_da6cc029962e",
  "trace_id": "trace_975966814b80",
  "attempt": 1,
  "occurred_at": "2026-08-29T09:00:00.487693+00:00"
}
```
````

> 上面这段是 T 轮从真房间抄回的**其中一条**，用来告诉你「该长什么样」。
> **它本身不是证据** —— 证据只认 `evidence/room/` 下的真实截图与
> `evidence/room/transcript.md`（那份是从房间消息历史整份拉的，不是从终端抄的）。

⚠️ 注意 `risk_level` 是 **M** 而 `effect_risk` 才是 **H**。停在 BLOCKED 靠的是后者：
Agent 产出补丁是 M 级、在其授权内，但这个补丁**合进生产**是 H 级，必须人工放行。
去房间里按 `risk_level=H` 找审批卡是找不到的。

### 在哪一步打命令

等到房间里出现那条 `HumanApprovalRequired → 待人工审批` 的审批卡
（卡片正文里 `effect_risk` 是 `H`，末尾逐字写着可用指令），
**从卡片里复制 `task_id`**，在房间发：

```text
/approve task_5a1469c54bbe        # ← 你那一轮的 id 不是这个，每次运行都重新生成
```

不要手打 task_id。打错的那条命令**不会**被猜成相近的任务 ——
`parse_approval_command` 认不出就返回 `None`，一律不猜，因为审批不可逆。

### 期望回执与终态

| 你发的 | 房间回你（逐字） |
|---|---|
| `/approve <task_id>` | `已批准 <task_id>（操作人 @boss:maos.local）` |
| `/approve`（缺参数） | `用法：/approve <task_id>  或  /reject <task_id> [原因]` |
| 任何非 `/approve`、`/reject` 开头的闲聊 | **一声不吭**（机器人不该给房间发用法提示） |

终态：任务放行继续执行，Plan 走到 **DONE**。

T 轮实测（终端最后一行 + 房间最后三条）：

```text
终态: task=DONE  plan=DONE  （镜像发出 7 条迁移）
exit=0
```

```text
[task_5a1469c54bbe] StateTransition → BLOCKED → DONE attempt=1
已批准 task_5a1469c54bbe（操作人 @boss:maos.local）
[plan_a9e5af33ed5b] PlanTransition → RUNNING → DONE attempt=1
```

⚠️ **回执与迁移的先后不固定**。上面这一轮里 `BLOCKED → DONE` 排在
`已批准 …` 前面 —— 因为判定先落库、镜像线程再把迁移推进房间，
而回话本身走的是另一次 send。截 `03` 那张图时把这三条一起框进去，
别只截「已批准」一条然后纳闷终态在哪。

---

## 5. R2 失败路径（`--case reject`）

```sh
# §2 的 MAOS_SANDBOX_WORKDIR 必须已经设好，否则这条命令启动即报错退出
~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case reject --timeout 300
```

审批卡出现后，在房间发（**原因写清楚，它会进回执也进审计**）：

```text
/reject task-s7-payment 渠道回执异常，转人工
```

| 你发的 | 房间回你（逐字） |
|---|---|
| `/reject <task_id> <原因>` | `已驳回 <task_id>（操作人 @boss:maos.local），原因：渠道回执异常，转人工` |
| 同上但 `MAOS_SANDBOX_WORKDIR` 没设 | `审批未生效：<task_id> —— MAOS_SANDBOX_WORKDIR 未设置` ← **这就是 §0 第三行，证据作废，回 §2 重来** |

### 🔴 期望终态 —— 本节整段被 T 轮推翻重写

原先这里写着三条 `sqlite3 <db> …` 判据（`refund_case.biz_status='compensated'`、
`payment_observation` 里 `settled` 计数为 0、`event_log` 里 `CompensationExecuted > 0`）。
**三条对 `room_demo` 一条都跑不了**，逐条说明为什么：

| 原判据 | 为什么跑不了 |
|---|---|
| `select … from refund_case` | **这张表不存在**。`room_demo` 跑的是 `role=coding` 的软件域任务，`refund_case` 是退款域（场景 6/7）的表。T 轮实测 `no such table: refund_case` |
| `select … from payment_observation` | 同上，`no such table: payment_observation` |
| `sqlite3 <db> …` 这个动作本身 | **压根没有 `<db>` 这个文件**。`flows/common.py::build()` 用的是 `SqliteStore()`，缺省路径 `":memory:"` —— 进程一退，库就没了 |

那三条判据说的是 **`python3 run.py --scenario 7`** 的事，不是这里的事。
`docs/submission-checklist.md` 的 P10 那一行也是这么挂的（「`run.py --scenario 7`
→ Plan 终态 FAILED、`biz_status=compensated`、`settled` 观察 0 条」）。
两件事被混成一件，是本手册 C 轮写就时的错，T 轮改正。

### `--case reject` 真正能核的终态

```text
终态: task=FAILED  plan=FAILED  （镜像发出 7 条迁移）
exit=0
```

房间里最后三条（T 轮逐字）：

```text
已驳回 task_02695e4aac86（操作人 @boss:maos.local），原因：渠道回执异常，转人工
[task_02695e4aac86] StateTransition → BLOCKED → FAILED attempt=1
[plan_546534bf2ccc] PlanTransition → RUNNING → FAILED attempt=1
```

终端里那一跳的原因逐字是 `BLOCKED -> FAILED (human_reject)`。

### 补偿：跑了，但你**看不见**它跑没跑

`human_decision(approved=False)` 确实先调 `_execute_compensation` 再改状态
（`maos/core/control_plane.py:728-731`，「先回滚再改状态」）。它也确实往 `event_log`
写了一条 `CompensationExecuted`。但是：

- **终端不打。** 成功路径上一行日志都没有，只有 `sandbox_unavailable` 那条老分支会 warn。
- **房间不显。** 房间镜像只镜像状态迁移与总线事件；`CompensationExecuted` 走的是
  `append_event_log`，从不 publish，因此永远不会出现在房间里。
- **退出码一样。** `ok=true` 和 `ok=false` 都是 exit=0。

所以照本手册跑一遍，你对补偿唯一能说的话是「它被调用了」，
**不能**说「它成功了」。想看 `ok` 值只有一条路：进程内把 `event_log` 拉出来看
（T 轮是拿离线台架量的，读数见 §2）。已记 `docs/BACKLOG.md` 的 `## task-T4`。

---

## 6. 越权用例

用 **intern** 账号（`$MAOS_MATRIX_OUTSIDER`，**不在** `MAOS_APPROVERS` 名单里）在同一间房发：

```text
/approve task-s7-payment
```

期望房间回执（逐字）：

```text
无审批权限：@intern:maos.local 不在 MAOS_APPROVERS 名单内
```

T 轮实测，房间回执与上面一字不差；终端同步打 `[房间回执] 无审批权限：…`。

同时 `event_log` 落一行 `ApprovalDenied`
（`hiclaw/matrix_bus.py::RoomApprovalBridge._record_denied`）：

```text
ApprovalDenied | sender 不在 MAOS_APPROVERS 名单内 |
  {"sender": "@intern:maos.local", "command": "approve", "task_id": "…"}
```

⚠️ **别照 `sqlite3 <db> "select … from event_log"` 去查** —— 本手册原先是这么写的，
但 `room_demo` 的 store 是 `":memory:"`，没有库文件（同 §5 那张表）。
这一条落库在**进程内**成立，进程一退就查不到了。
房间回执是它在演示现场唯一可核的外化形态，截图截的就是这个。

> 判定顺序是**先认命令词、再查名单、最后校参数**，三步不可换序。
> 所以名单外的人哪怕把参数打错了，也照样记一条越权证据 ——
> 先校参数会把越权尝试降级成一句用法提示，那条证据就没了。
> 想验这一点：用 intern 发一条**缺 task_id** 的 `/approve`，回执仍应是「无审批权限」，不是用法提示。

T 轮把这一条真验了。房间里逐字（`transcript.md` 第 15–19 条）：

```text
@intern:maos.local   /approve task_5a1469c54bbe
@maos-bot:maos.local  无审批权限：@intern:maos.local 不在 MAOS_APPROVERS 名单内
@intern:maos.local   /approve                       ← 缺参数
@boss:maos.local     hello 大家好，这条是闲聊，机器人不该回
@maos-bot:maos.local  无审批权限：@intern:maos.local 不在 MAOS_APPROVERS 名单内
```

三件事一次坐实：**缺参数的越权仍判越权**（不是用法提示）、
**闲聊零回复**（boss 那条至今没有回音）、
以及回执**不按发言顺序紧跟**（第 19 条回的是第 17 条，中间隔着第 18 条）——
最后这点是 429 限流导致的，截 `05` 时别以为自己截漏了。

---

## 7. 截图清单与命名

五张，落 `evidence/room/`（**不是** `evidence/scenario-R*/`，理由见该目录 README 与 `docs/DECISIONS.md` 的 `## task-C4`）。

| 文件名 | 截什么 | 证明 `docs/EXECUTION.md` Phase 4 验收的哪一条 |
|---|---|---|
| `01-approval-card.png` | 审批卡：一行人话摘要 + 展开的 Envelope 折叠块 | 「Element 里看到全过程」——镜像进房间这件事成立 |
| `02-transitions.png` | 状态迁移轨迹，含 `RUNNING → AWAITING_REVIEW` | 同上；且证明迁移是逐条镜像的，不是跑完补一条总结 |
| `03-approve-effect.png` | `/approve` 后的回执 + 放行 + 终态 DONE | 「发 `/approve` → DONE」 |
| `04-reject-compensation.png` | `/reject` 回执 → `BLOCKED → FAILED` → Plan `RUNNING → FAILED` | 「`/reject` → 驳回生效、Plan 落 FAILED」。**注意它证不了「补偿执行」**，见下 |
| `05-denied-outsider.png` | intern 打 `/approve` 被拒的房间回执（两条：带参数的与缺参数的） | 「只接受 `MAOS_APPROVERS` 名单内用户，其余回『无审批权限』并记 event_log」 |

**证明不了任何一条的图就别放。**

### 🔴 `04` 这个文件名比它能证明的东西大

文件名叫 `04-reject-compensation.png`，但**房间里拍不到补偿**：`CompensationExecuted`
只落 `event_log`、从不 publish，因此永远不进房间镜像（详见 §5 末节）。
这张图能证明的是「驳回生效 + Plan 落 FAILED」，**不是**「补偿执行了」。

文件名 T 轮**没有改** —— `docs/EXECUTION.md:499/502` 与
`evidence/room/README.md` 的截图清单都按这个名字引它，改名要一起动三处，
而那两处不在本轨可改面内。已记 `docs/BACKLOG.md` 的 `## task-T4`。

**写材料时别把这张图说成「补偿的证据」。** 补偿的证据在
`evidence/scenario-7/`（机器侧、有库、进 `verify.py` 七项核验），不在这里。

### 🔴 脱敏：必须在按快门那一刻做掉

`scripts/make_evidence.py::scan_for_secrets` **只扫文本**。PNG 里的 token 它一个都发现不了。
图一旦进 git 历史就出不来了，事后补不了。所以按下截图键之前，逐条清干净：

1. 终端 scrollback 里任何 `curl … Bearer <token>`、`echo $MATRIX_TOKEN`、`cat room.env` 的回显
   —— §1 那条 encryption 探测命令就会留下 token，**先 `clear` 再截图**
2. Element 的设置页 / 账号页**一律不开**（access token 在那里）
3. 浏览器地址栏、标签页标题、书签栏里的内网地址按需裁掉
4. **优先只截 Element 房间正文区，不要截整屏**

落盘后自查：

```sh
grep -rIl . evidence/room/          # 只应命中文本文件（README.md / transcript.md），PNG 不该出现
grep -rn "syt_\|Bearer \|access_token" evidence/room/*.md   # 应无输出
git status --short                  # 不许出现 room.env / creds.txt / 任何 token
```

### `transcript.md` 为什么是必需的

PNG 不能 grep，评委没法在图里搜一个 `task_id`。
`transcript.md` 是这些图的**可检索镜像**：房间消息的逐字文本副本，token 一律写成 `<redacted>`。
**两者必须一致** —— 图里有的话，文本里就得有；文本里有的话，图里得能找到。

---

## 8. 收尾

```sh
bash deploy/synapse/down.sh
```

然后确认 `python3 scripts/verify.py` **仍能开始核验**：

```sh
python3 scripts/make_evidence.py            # ← 这一行不能省，理由见下
python3 scripts/verify.py 2>&1 | tail -3
# 期望 RESULT: 8/8 PASS，exit=0
```

🔴 **`make_evidence.py` 这一行是判据成立的前提，不是可选的。**
干净检出里没有 `evidence/scenario-*/maos.db`（`.gitignore` 挡着），
直接跑 `verify.py` 会在第一个证据束上就 `[FAIL] 无法开始核验：缺数据库 …`、`exit=2`。
报错里当然也不会出现 `room` 字样 —— 于是这条自检**永远通过、却什么都没验到**，
恰恰漏掉它唯一要防的那件事。这条是 H 轮记在 `docs/BACKLOG.md` 里的账，T 轮补上。

跑完记得把证据束还原，别把重跑产物混进提交：

```sh
git checkout -- evidence/scenario-1 evidence/scenario-2 evidence/scenario-3 \
                evidence/scenario-4 evidence/scenario-5 evidence/scenario-6 \
                evidence/scenario-7 evidence/scenario-R5
```

报错里出现 `scenario-R1` / `scenario-R2` / `room` 任意一个，说明有人把截图放错了目录 ——
`verify.py::load_cases` 把 `evidence/` 下**任何** `scenario-` 开头的目录都当证据束，
逐个要求 `maos.db` + `trace.json` + `result.json`，缺一个就整体 `exit=2` 进不去核验。
详见 `docs/DECISIONS.md` 的 `## task-C4`。

---

## 9. 跑不通怎么办

**先回 §0 那张表**：这套链路的失败绝大多数是静默的，不要先怀疑程序卡住。

### 7.1 房间被建成加密房

- **症状**：终端跑完 exit=0，房间里一条消息都没有
- **原因**：`m.room.encryption` 状态事件存在 → `_verify_room()` 抛 `RoomEncrypted` → 上游降级 log-only。
  本轨不装 `matrix-nio[e2e]`，加密房是**当场**降级，不是等 send 失败才降
- **下一步**：`curl` 那条 encryption 查询（§1），确认返回不是 `M_NOT_FOUND` → **重建一个非加密房**。
  Element 建房时把「加密」关掉；Synapse 的房间加密默认值也要一并确认

### 7.2 四个必填 env 漏了一个

- **症状**：同上，安静地什么都没有
- **原因**：`MatrixBusConfig.from_env()` 缺任一必填项就 `log_only=True`，**不抛异常**
- **下一步**：看终端第一行 WARNING，它会**逐字点名缺哪个变量**（只打变量名不打值）：

  ```text
  WARNING maos.matrix  Matrix 配置缺 MATRIX_TOKEN，降级 log-only（不进房间，行为等同进程内总线）
  ```

  然后 `. ~/.maos-matrix/room.env` 重来。注意 `MAOS_APPROVERS` **不在**这四个必填里 ——
  它缺了不降级，只是所有审批命令都被拒，症状变成「命令发了回『无审批权限』」

### 7.3 `MAOS_SANDBOX_WORKDIR` 没设

- **症状**：房间有消息，`/reject` 回的是「审批未生效：<task_id> —— ...」
- **原因**：补偿执行器硬失败。这是**有意设计**，不是 bug（`## merge-p2` 第 3 条）
- **下一步**：回 §2 设好 env 并 `mkdir -p`，重跑。
  **不要**去把 `"."` 那个缺省值加回来 —— 有回归测试守着，加回来立刻红

### 7.4 房间有消息但审批命令没反应

- 先确认发命令的账号在 `MAOS_APPROVERS` 里（`echo $MAOS_APPROVERS`，**这条不回显 token，可以截**）
- 再确认命令是 `/approve` / `/reject` 开头且**带 task_id**：缺参数只回用法，不落任何决策
- 机器人不听自己的回声（`should_deliver` 里跳过 `sender == whoami 回来的 mxid`），
  所以用 bot 账号自己发命令是没反应的 —— 必须用 boss 账号
- **现在它会出声**：被回声过滤丢掉的那条如果长得像审批命令，终端会打

  ```text
  WARNING maos.matrix  忽略了一条 bot 自己发的审批命令（@maos-bot:maos.local）——
  机器人不听自己的回声。请换一个**人类**账号（MAOS_APPROVERS 里的那个）在 Element 里发
  ```

  自己发的**普通回执**照旧悄悄丢，不打这条 —— 每条都喊一句就成了刷屏

### 7.5 前几条消息进了房间，后面突然没了

- **原因**：连续镜像失败 `MAX_MIRROR_FAILURES = 3` 次后**永久降级**，不再重试
- 这是有意的：房间挂一整场时不该把控制台刷成告警墙，那会淹掉真正的业务日志
- **下一步**：翻终端最早那条镜像失败的 WARNING，它才是根因；后面没有告警不代表恢复了

### 7.6 终端打 `房间回话失败（）`，可房间里消息一条不少（T 轮新增）

- **症状**：终端出现若干条 `WARNING maos.matrix  房间回话失败（），判定已生效`，
  括号里**是空的**。但去房间里数，回执与迁移一条不差。
- **原因**：**虚警**，而且是两个东西叠出来的：
  1. Synapse 默认 `rc_message` 限流。演示开头那一串镜像是**连发**的，
     直接打穿限流，nio 打出 `Got 429 response (ratelimited), sleeping for 4854ms`
     并自行退避重试（T 轮一次 approve 跑出 4 条 429）。
  2. `_NioChannel._await()` 用的是 `run_coroutine_threadsafe(...).result(self._timeout)`，
     而 `_timeout` 缺省 **10.0 秒**。一次 send 被退避拖过 10 秒，这里就抛
     `concurrent.futures.TimeoutError` —— 而**它的 `str()` 恰好是空字符串**，
     于是 `log.warning("房间回话失败（%s）", exc)` 打出来就是一对空括号。
  3. 关键在于：协程还在私有事件循环上**继续跑**，退避结束后消息照样送达。
     调用方已经放弃，消息其实成功了。
- **怎么确认**：数房间，不看终端。T 轮 R1 那一轮打了 3 条这个警告，
  房间里 23 条消息一条不少。
- **不要**因为看到这条警告就重跑 —— 重跑只会再撞一次限流，并且把房间灌得更满。
- ✅ **本轮已修**（`926aa7b` 之后），三件事一起改的：
  1. 所有异常日志过 `describe_exc()`，类名一律带上、消息为空补 `<该异常没有消息>`，
     超时类再追一句「不要重跑」的处置口径。**空括号不会再出现**。
  2. send 单独一档超时（`DEFAULT_SEND_TIMEOUT = 30s`），与构造期的 10s 分开 ——
     构造连不上要早知道，发消息要经得起退避。限流本身照旧由 nio 的
     `Got 429 response (ratelimited)` 打出来，**没有被盖住**。
  3. 超时抛的是 `RoomSendTimeout`，`MatrixEventBus._mirror` 里**不计入**
     `MAX_MIRROR_FAILURES`。这条最要紧：原来撞一次限流就够 3 次、直接触发
     §7.5 那个永久降级 —— 那之后房间里是真的一条都没有了，**一次虚警被自己
     亲手做实成了真故障**。
- 判据不变：唯一算数的仍然是去房间里数消息。`mirror.mirrored` 那个条数在超时时
  **不加一**，所以它是个下界，不是等号。

### 7.7 跑完退出时刷一屏 asyncio 报错（T 轮新增）

- **症状**：终态已经打出来了、`exit=0`，屏幕上却还刷出
  `RuntimeError: Event loop is closed`、`Task was destroyed but it is pending!`、
  `Exception ignored in: <coroutine object AsyncClient.sync_forever …>`
- **原因**：收口时私有事件循环先关，`sync_forever` 那条常驻协程后死。
  发生在**终态之后**，不影响任何判定，也不影响退出码。
- ✅ **本轮已修**（`926aa7b` 之后）。`_NioChannel.close()` 改成有序收口六步：
  停 sync -> `cancel()` 掉 `sync_forever` 并**等它落地** -> 关客户端 ->
  `shutdown_asyncgens()` -> 停循环 -> `join()` 线程 -> `loop.close()`。
  常驻协程和 aiohttp 连接池都在循环还活着的时候收干净，GC 就没有东西可以去碰了。
- 还看到这一屏的话，先确认跑的是改后的版本；仍无害，判定与退出码照旧在它上面几行。

---

## 修订记录（T 轮，基线 `27c9e18`）

照本手册真跑了三条路径（approve / reject / 越权），改了下面这些步骤。
**改动依据一律是实跑，不是推理。**

| 节 | 原来写的 | 改成什么 | 依据 |
|---|---|---|---|
| 抬头 | 未提解释器 | 新增「必须用 `~/.maos-matrix/venv/bin/python`」整节 | 系统 `python3` 无 matrix-nio，实测静默降级 |
| §0 | 三种静默症状 | 补两行：系统 python 降级、`房间回话失败（）`虚警 | 两条都在 T 轮真撞上了 |
| §1 | `room.env`「七个键」 | **八个键**，补 `MAOS_ELEMENT_URL` / `MATRIX_ROOM_ID_ENCRYPTED` 两行与「别拿加密房当演示房」 | `room.env` 实读 8 个 |
| §1 | 只给加密探测命令 | 补三条探针的**实测输出**（`M_NOT_FOUND` / `versions 200` / `whoami`） | 实跑 |
| §2 | `mkdir -p` 空目录即可 | 新增整节：空目录会让补偿 `ok=false`，且**无人看得见** | 实测 `CompensationExecuted.ok=false`，台架对照 `ok=true` |
| §4 | 逐字示例是 `task-s7-payment`（退款域） | 全换成 `room_demo` 真产的 `task_<hex>` / `role=coding`；补订阅与 drain 两类噪声行；补 `risk_level=M` vs `effect_risk=H` 的提醒 | 房间历史 |
| §4 | 无终态实测 | 补终端终态行与房间末三条，并说明回执与迁移**先后不固定** | 实跑 |
| §5 | 三条 `sqlite3 <db>` 判据 | **整段推翻**：两张表不存在、库是 `:memory:` 没有文件；那三条说的是 `run.py --scenario 7` | 实测 `no such table` + `SqliteStore()` 缺省 `":memory:"` |
| §5 | 「`/reject` → 补偿执行」 | 改成「补偿被调用了，但终端不打、房间不显、退出码相同 —— 只能说被调用，不能说成功」 | 代码路径 + 实跑 |
| §6 | `sqlite3 <db>` 查 `ApprovalDenied` | 标注同样查不到（`:memory:`），房间回执才是可核的外化形态；补两条越权的逐字实测 | 实跑 |
| §7 | `04` 证明「`/reject` → 补偿」 | 改成「驳回生效 + Plan FAILED」，并写明文件名比它能证明的东西大、为什么没改名 | 同 §5 |
| §8 | 直接跑 `verify.py` | 前面补 `make_evidence.py`，否则这条自检**永远通过却什么都没验到**；补还原命令 | H 轮已记账，T 轮补上 |
| §9 | 五条排错 | 补 7.6（429 + 10s 超时导致的空括号虚警）、7.7（退出时 asyncio 噪声） | 实跑 |


## 修订记录（2026-08-31，基线 `926aa7b`）

把 §0 那张表里的四行**从「文档提醒」改成「代码会响」**。改的是行为不是措辞 ——
这四条原来的共同点是：屏幕上一切正常、退出码 0、房间里一条都没有，
全靠读手册的人记得去查，而「记得去查」在 T 轮一次都没成立过。

| 坑 | 原来的形态 | 改成什么 | 落点 |
|---|---|---|---|
| 用系统 `python3` 跑（最贵的一步） | `ImportError` 与「连不上」共用一条降级分支，跑完 exit=0，终端形态与真房间无法分辨 | 单列 `MatrixDepMissing`；总线记 `degrade_reason`；入口对「想进房间却没进成」**exit 4**，并打一行 `[自检]` 写明解释器 | `matrix_bus.py`、`room_demo.py` |
| `房间回话失败（）` 空括号虚警 | `TimeoutError` 的 `str()` 是空串，告警说不出自己是什么；且计进 `MAX_MIRROR_FAILURES`，撞一次限流就永久降级 | 全部日志过 `describe_exc()`；send 单列 30s 超时并抛 `RoomSendTimeout`；**超时不计入**失败次数 | `matrix_bus.py`、`transition_mirror.py` |
| 退出刷一屏 `Event loop is closed` | 收口只 stop 循环，常驻协程与连接池后死 | `close()` 六步有序收口，等线程退出再 `loop.close()` | `matrix_bus.py` |
| bot 账号自己发命令没反应 | 回声过滤静默丢弃，房间里什么都不发生 | 丢掉的那条**是审批命令**时打一条 WARNING，点名要换人类账号 | `matrix_bus.py` |

判据：`maos/tests/test_matrix_bus.py` 第 8 节（19 条）+ `test_room_wiring.py` 第 4 节（6 条）。
四条修复逐个撤掉做过变异检验，每条都能让对应用例变红 —— 不是「写了测试」，是「测到了」。
全量 `python3 -m pytest maos/tests -q` → **1114 passed / 39 skipped**。
