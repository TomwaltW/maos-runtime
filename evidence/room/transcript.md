# generated at 2026-08-29T05:12:40.744383+00:00 from f42ea83f9f8d9b40525d6e793cb4ddf293a46f4d

# 房间消息逐字副本

`evidence/room/*.png` 的**可检索镜像**。PNG 不能 grep，评委没法在图里搜一个
`task_id`；本文件把房间正文逐字抄下来，token 一律写成 `<redacted>`。
**与截图必须一致** —— 图里有的话这里就得有，这里有的话图里得能找到。

---

## 🔴 空 —— 房间未接通，无逐字副本可抄

卡在 C-1（`~/.maos-matrix/STATUS` 为 `PENDING`，无 `room.env` / `creds.txt`；
`deploy/synapse/` 与 `hiclaw/room_demo.py` 均未交付）。实测依据见
`evidence/room/README.md`。

**本文件保持空白，不填任何示例内容。**

`hiclaw.matrix_bus` 的降级模式会把「本该发进房间的每一条消息」原文打到 stdout，
把那份输出抄进来，形态与真副本**一模一样、无法分辨**。所以这里一个字都不抄 ——
一份看起来完整的假副本，比一份诚实的空文件危险得多。

消息的**预期形态**写在 `docs/matrix-room-runbook.md` §4–§6（那里逐字列出了
`summarize` / `render_mirror` / `RoomApprovalBridge` 的真实渲染输出，
并在原地标明「是渲染器输出，不是房间截图，不能当证据」）。
房间接通后按 §7 的清单采集，届时把下面的骨架填满并删掉本节。

---

## 采集骨架（房间接通后填）

采集环境（补图时逐字填真实值，token / room id 打码）：

```
homeserver : <redacted>
room_id    : <redacted>
bot        : @maos-bot:maos.local
approver   : @boss:maos.local
outsider   : @intern:maos.local
采集时间   : <ISO8601>
git sha    : <sha>
```

### R1 顺利路径（`--case approve`，对应 `01`–`03`）

```
[尚未采集]
```

### R2 失败路径（`--case reject`，对应 `04`）

```
[尚未采集]
```

### 越权用例（intern 打 `/approve`，对应 `05`）

```
[尚未采集]
```
