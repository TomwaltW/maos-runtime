# Matrix 房间演示 runbook

从零到截图的完整重跑手册。目标读者是**没跑过这套东西的人**：照着做一遍，
应当拿到 `evidence/room/` 下那五张图，以及与之逐字对应的 `transcript.md`。

- 适用版本：基线 `f42ea83`（C 轮）
- 依赖三轨：C-1 出房间与凭证、C-2 修 `hiclaw/matrix_bus.py` 的真连通、C-3 出 `hiclaw/room_demo.py`
- 本机没有 `python` 命令，一律 `python3`

---

## 0. 先读这一节：三种「安静地什么都没发生」

这套链路**所有的失败都是静默降级**，没有一个会打红字、没有一个会卡住。
屏幕上看起来一切正常，只是 Element 房间里一条消息都不出现。
不知道这一点的人会以为程序挂了，去按 Ctrl-C —— 那会把一次好好的运行掐掉。

| 症状 | 真实原因 | 怎么确认 | 见 |
|---|---|---|---|
| 房间里**什么都没有**，终端照常跑完 exit=0 | 房间开了端到端加密（E2EE）。`_NioChannel._verify_room()` 查到 `m.room.encryption` 状态事件就抛 `RoomEncrypted`，上游当场降级 log-only | 终端有一行 `WARNING maos.matrix`；房间设置里「加密」是开的 | §7.1 |
| 同上 | 四个必填 env 漏了任意一个 | 终端首行 `WARNING maos.matrix  Matrix 配置缺 <变量名>，降级 log-only（不进房间，行为等同进程内总线）` | §7.2 |
| 房间有消息，但 `/reject` 回的是**「审批未生效」** | `MAOS_SANDBOX_WORKDIR` 没设。补偿执行器**硬失败** | 回执原文含 `审批未生效：<task_id> —— ` | §7.3 |

**记住这条判据**：`log_only=True` 时行为**等同进程内总线**，场景照跑、退出码照样是 0。
「跑通了」和「进房间了」是两件事，退出码只证明前者。

---

## 1. 起服务

**不在这里抄一遍步骤** —— 抄了就会和 C-1 的实际产物漂移。照 `deploy/synapse/README.md` 执行。

跑完应当拿到（三样都在**仓库外**，`chmod 600`，永不入库）：

```
~/.maos-matrix/STATUS      # 一行状态；这里必须是 READY <ISO8601>
~/.maos-matrix/room.env    # 七个键，可 source
~/.maos-matrix/creds.txt   # boss / intern 的 Element 登录口令
```

`room.env` 的键名（值一律由 C-1 现取，**不许占位符**）：

| 键 | 用途 | 缺了会怎样 |
|---|---|---|
| `MATRIX_HOMESERVER` | 宿主机跑 python 时是 `http://localhost:8008` | 降级 log-only |
| `MATRIX_USER` | `@maos-bot:maos.local` | 降级 log-only |
| `MATRIX_TOKEN` | bot 的 access token | 降级 log-only |
| `MATRIX_ROOM_ID` | `!xxxx:maos.local` | 降级 log-only |
| `MAOS_APPROVERS` | `@boss:maos.local`，逗号分隔 | **不降级**，但所有审批命令都被拒 |
| `MAOS_MATRIX_OUTSIDER` | `@intern:maos.local`，越权用例用 | 只影响 §5 那一步 |

⚠️ `MATRIX_HOMESERVER` 有两个口径且**都对**：宿主机跑是 `http://localhost:8008`，
容器内跑是 `http://host.docker.internal:8008`。用错场合的症状就是 §0 第一行 —— 静默降级。

**开跑前先确认房间没加密**：

```sh
. ~/.maos-matrix/room.env
curl -s -H "Authorization: Bearer $MATRIX_TOKEN" \
  "$MATRIX_HOMESERVER/_matrix/client/v3/rooms/$MATRIX_ROOM_ID/state/m.room.encryption"
# 期望 M_NOT_FOUND —— 查不到才是「未加密」。查得到就是加密房，本轨不装 matrix-nio[e2e]，必须重建一个非加密房
```

🔴 这条命令会把 token 打进 scrollback。**跑完立刻 `clear`，再开始截图**（见 §6）。

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

---

## 3. 登 Element

用 **boss** 账号（口令在 `~/.maos-matrix/creds.txt`），加入 C-1 建的那间房。

🔴 **不要打开设置页 / 账号页** —— 那里有 access token，一截屏就进了 git 历史，
而 PNG 是二进制，`make_evidence.py::scan_for_secrets` 一个字都扫不到（见 §6）。

---

## 4. R1 顺利路径（`--case approve`）

```sh
~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case approve --timeout 300
```

### 房间里依次出现什么

每条镜像消息都是 **`m.notice`**（不触发推送提醒），形态是「一行人话摘要 + 折叠的 Envelope JSON」。
折叠是必需的：一个 plan 跑几十条事件，不折叠的话人翻不到那条要审批的。

首行摘要的**逐字形态**（下面四行是 `hiclaw.matrix_bus.summarize` 的真实输出）：

```
[task-s7-payment] TaskAssignment → plan.tasks attempt=1 role=payment
[task-s7-payment] TaskResult → task.result attempt=1 status=FAILED
[task-s7-payment] ReviewVerdict → review.verdict attempt=1 verdict=REWORK
[task-s7-payment] Rework → plan.rework attempt=2 reason=gateway_unavailable
```

展开折叠块后是完整 Envelope（`render_mirror` 真实输出，注意 `api_key` 已被出口脱敏成 `***`）：

````
[task-s7-payment] TaskAssignment → plan.tasks attempt=1 role=payment
```json
{
  "event_type": "TaskAssignment",
  "plan_id": "plan_4d0fc98be697",
  "task_id": "task-s7-payment",
  "idempotency_key": "idem-s7-payment-1",
  "payload": {
    "role": "payment",
    "risk_level": "H",
    "effect_risk": "H",
    "goal": "对渠道发起退款付款",
    "amount_claimed": "6800.00",
    "api_key": "***"
  },
  "event_id": "evt_27e61487b1c3",
  "trace_id": "trace_088bd7d82beb",
  "attempt": 1,
  "occurred_at": "2026-08-29T05:11:44.714392+00:00"
}
```
````

> 上面这段是**渲染器输出**，用来告诉你「该长什么样」，
> **不是房间截图，也不能当证据**。证据只认 `evidence/room/` 下的真实截图。

### 在哪一步打命令

等到房间里出现那条高风险任务的审批卡（`risk_level=H` / `effect_risk=H`），
**从卡片里复制 `task_id`**，在房间发：

```
/approve task-s7-payment
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

---

## 5. R2 失败路径（`--case reject`）

```sh
# §2 的 MAOS_SANDBOX_WORKDIR 必须已经设好，否则这条命令启动即报错退出
~/.maos-matrix/venv/bin/python -m hiclaw.room_demo --case reject --timeout 300
```

审批卡出现后，在房间发（**原因写清楚，它会进回执也进审计**）：

```
/reject task-s7-payment 渠道回执异常，转人工
```

| 你发的 | 房间回你（逐字） |
|---|---|
| `/reject <task_id> <原因>` | `已驳回 <task_id>（操作人 @boss:maos.local），原因：渠道回执异常，转人工` |
| 同上但 `MAOS_SANDBOX_WORKDIR` 没设 | `审批未生效：<task_id> —— MAOS_SANDBOX_WORKDIR 未设置` ← **这就是 §0 第三行，证据作废，回 §2 重来** |

### 期望终态（三条都要核，缺一条这个用例就不成立）

```sh
sqlite3 <db> "select biz_status from refund_case where case_id='<R2 的 case>'"          # compensated
sqlite3 <db> "select count(*) from payment_observation where observed_state='settled'"  # 0
sqlite3 <db> "select count(*) from event_log where event_type='CompensationExecuted'"   # >0
```

- Plan 终态 **FAILED**，原因 `human_reject`
- `refund_case.biz_status = 'compensated'`
- **从未进入 `settled`** —— 这条是铁律 8 的现场证明：没问出终态就一条 settled 观察都不该有

---

## 6. 越权用例

用 **intern** 账号（`$MAOS_MATRIX_OUTSIDER`，**不在** `MAOS_APPROVERS` 名单里）在同一间房发：

```
/approve task-s7-payment
```

期望房间回执（逐字）：

```
无审批权限：@intern:maos.local 不在 MAOS_APPROVERS 名单内
```

同时 `event_log` 落一行：

```sh
sqlite3 <db> "select event_type, reason, detail from event_log where event_type='ApprovalDenied'"
# ApprovalDenied | sender 不在 MAOS_APPROVERS 名单内 | {"sender": "@intern:...", "command": "approve", "task_id": "..."}
```

> 判定顺序是**先认命令词、再查名单、最后校参数**，三步不可换序。
> 所以名单外的人哪怕把参数打错了，也照样记一条越权证据 ——
> 先校参数会把越权尝试降级成一句用法提示，那条证据就没了。
> 想验这一点：用 intern 发一条**缺 task_id** 的 `/approve`，回执仍应是「无审批权限」，不是用法提示。

---

## 7. 截图清单与命名

五张，落 `evidence/room/`（**不是** `evidence/scenario-R*/`，理由见该目录 README 与 `docs/DECISIONS.md` 的 `## task-C4`）。

| 文件名 | 截什么 | 证明 `docs/EXECUTION.md` Phase 4 验收的哪一条 |
|---|---|---|
| `01-approval-card.png` | 审批卡：一行人话摘要 + 展开的 Envelope 折叠块 | 「Element 里看到全过程」——镜像进房间这件事成立 |
| `02-transitions.png` | 状态迁移轨迹，含 `RUNNING → AWAITING_REVIEW` | 同上；且证明迁移是逐条镜像的，不是跑完补一条总结 |
| `03-approve-effect.png` | `/approve` 后的回执 + 放行 + 终态 DONE | 「发 `/approve` → DONE」 |
| `04-reject-compensation.png` | `/reject` 回执 → 补偿执行 → Plan FAILED(human_reject) | 「`/reject` → 补偿」 |
| `05-denied-outsider.png` | intern 打 `/approve` 被拒的房间回执 | 「只接受 `MAOS_APPROVERS` 名单内用户，其余回『无审批权限』并记 event_log」 |

**证明不了任何一条的图就别放。**

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
python3 scripts/verify.py 2>&1 | tail -2
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

  ```
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
- 机器人不听自己的回声（`listen` 里跳过 `sender == self._client.user`），
  所以用 bot 账号自己发命令是没反应的 —— 必须用 boss 账号

### 7.5 前几条消息进了房间，后面突然没了

- **原因**：连续镜像失败 `MAX_MIRROR_FAILURES = 3` 次后**永久降级**，不再重试
- 这是有意的：房间挂一整场时不该把控制台刷成告警墙，那会淹掉真正的业务日志
- **下一步**：翻终端最早那条镜像失败的 WARNING，它才是根因；后面没有告警不代表恢复了
