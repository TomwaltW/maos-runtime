# generated at 2026-08-29T05:12:40.744383+00:00 from f42ea83f9f8d9b40525d6e793cb4ddf293a46f4d

# `evidence/room/` —— 房间侧人机交互证据

采集时间与 git sha 见首行。**采集人手写，不是脚本产物** —— 房间证据的载体是截图，
没有生成器可言（对比 `evidence/scenario-*/` 由 `scripts/make_evidence.py` 与
`maos.kb.experiment` 自动产出并自动写头）。首行仍按库内惯例保留，记的是**采集**
时刻与 sha，不是生成时刻。这条判断已记 `docs/DECISIONS.md` 的 `## task-C4`。

重跑手册：`docs/matrix-room-runbook.md`。

---

## 🔴 当前状态：截图待补

**卡在 C-1** —— 房间与凭证尚未交付。实测依据（本 sha 下）：

```
$ cat ~/.maos-matrix/STATUS
PENDING —— C-1 尚未交付房间凭证

$ ls ~/.maos-matrix/
README   STATUS   venv/          # 没有 room.env，没有 creds.txt

$ ls deploy/
docker-compose.yml  sandbox.Dockerfile     # 没有 synapse/

$ python3 -c "import importlib.util as u; print(u.find_spec('hiclaw.room_demo'))"
None                                        # C-3 的入口尚未交付
```

所以本目录**一张图都没有**，这是有意的。

🔴 **本轮明确拒绝了一件事**：`hiclaw.matrix_bus` 的降级模式（`log_only=True`）会把
「本该发进房间的每一条消息」原文打到 stdout，截那个终端窗口看起来和真房间证据
很像。**没有截，也不许截。** 降级模式的行为**等同进程内总线**，它证明的是
「事件流对」，**不是**「消息真进了 Matrix 房间、真人在房间里打了 `/approve`」——
而后者才是 Phase 4 验收要的东西。宁可交一份「手册齐、图待补」，
也不要交一份看起来完整但假的证据束。

房间接通后按 `docs/matrix-room-runbook.md` §4–§7 补齐五张图与 `transcript.md`，
并把本节整节换成实际采集记录。

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

🔴 **截图不许放 `evidence/scenario-R1/` 或 `evidence/scenario-R2/`**，
哪怕手册第 499/502 行是那么写的。理由是实测出来的，见 `docs/DECISIONS.md`
与 `docs/BACKLOG.md` 的 `## task-C4`：`verify.py::load_cases` 会把 `evidence/` 下
**任何** `scenario-` 开头的目录当成证据束，逐个要求 `maos.db` + `trace.json` +
`result.json`；放一张图进去，整个 `verify.py` 当场 `exit=2` 进不去核验 ——
「7/7 PASS」这条头号卖点会一起没掉。本目录名 `room` 不以 `scenario-` 开头，
因此对 `verify.py` 完全透明。

---

## 截图清单（待补）

每张图在下表里有且只有一句话，说清它证明 `docs/EXECUTION.md` Phase 4 验收的哪一条。
**证明不了任何一条的图不进本目录。**

| 文件 | 状态 | 证明哪一条验收 |
|---|---|---|
| `01-approval-card.png` | ⬜ 待补 | 「Element 里看到全过程」—— 审批卡形态：一行人话摘要 + 折叠的 Envelope JSON，镜像进房间这件事成立 |
| `02-transitions.png` | ⬜ 待补 | 同上，且证明状态迁移是**逐条**镜像的（含 `RUNNING → AWAITING_REVIEW`），不是跑完补一条总结 |
| `03-approve-effect.png` | ⬜ 待补 | 「发 `/approve` → DONE」—— 回执 `已批准 <task_id>（操作人 @boss:…）`，任务放行，Plan 终态 DONE |
| `04-reject-compensation.png` | ⬜ 待补 | 「`/reject` → 补偿」—— 回执 `已驳回 …`，补偿执行，Plan FAILED(human_reject)，`biz_status='compensated'` 且从未 `settled` |
| `05-denied-outsider.png` | ⬜ 待补 | 「只接受 `MAOS_APPROVERS` 名单内用户，其余回『无审批权限』并记 event_log」—— intern 账号被拒的房间回执 |

---

## 脱敏口径

🔴 **PNG 扫不到。** `scripts/make_evidence.py::scan_for_secrets` 只扫文本，
截图里的 token 它一个字都发现不了；而图一旦进了 git 历史就取不出来。
所以脱敏必须在**按快门那一刻**做完，事后补不了。逐条要求见
`docs/matrix-room-runbook.md` §7，摘要：

- 截图前 `clear` 终端 —— §1 的房间加密探测命令会把 `Bearer <token>` 留在 scrollback
- Element 的设置页 / 账号页一律不开（access token 在那里）
- 只截房间正文区，不截整屏；地址栏、标签页标题、书签栏里的内网地址按需裁掉

`transcript.md` 里 token 一律写成 `<redacted>`。

补图后必跑，输出附进本节：

```sh
grep -rIl . evidence/room/                                    # 只应命中 .md，PNG 不该出现
grep -rn "syt_\|Bearer \|access_token" evidence/room/*.md     # 应无输出
git status --short                                            # 不许有 room.env / creds.txt / 任何 token
python3 scripts/verify.py 2>&1 | tail -2                      # 报错原文不许出现 scenario-R1 / scenario-R2 / room
```

## `transcript.md` 的作用

PNG 不能 grep，评委没法在图里搜一个 `task_id`。`transcript.md` 是这些截图的
**可检索镜像** —— 房间消息的逐字文本副本。**两者必须一致**：图里有的话文本里就得有，
文本里有的话图里得能找到。不一致的那一刻，两份证据互相拆台，都不算数。
