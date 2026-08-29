# 提交自查单

复赛三件套：**方案 PPT + 仓库 + Demo 视频**。全部打勾才提交。

> ⚠️ **两处口径不在仓库里，需要人类照官方通知补**（本文件不编）：
> ① 「评审四维」的官方名称与权重 —— 仓库任何文件都没有，
> 本文件的对照表按手册附 C 的**十三条评委要求**组织，拿到官方四维后按它重排即可；
> ② 视频的**官方规格**（时长上限、分辨率、格式、大小、是否要字幕） —— 同样没有，
> 下面的视频段只列了手册明说的部分，其余标了「待确认」。

---

## A. 仓库自查

### A-1 机器验收（逐条跑，全绿才算）

```bash
python3 -m pytest maos/tests -q          # □ 全绿（基线 455 passed）
python3 run.py                           # □ exit=0（场景 1-6）
python3 run.py --scenario 7              # □ exit=0（失败路径，不在缺省序列里）
python3 scripts/gen_docs.py --check      # □ 退出 0：三份生成文档与代码一致
python3 scripts/make_evidence.py         # □ 7 场景落盘，0 场景缺模块
python3 -m maos.kb.experiment            # □ scenario-R5 落盘
python3 scripts/verify.py                # □ 7/7 PASS，exit=0
git diff --stat maos/contracts/          # □ 空（冻结契约未被动过）
```

- [ ] **新克隆冒烟**：`git clone` 到全新目录，严格按 README 从零跑到 `verify.py` 7/7，
      **掐表 ≤ 15 分钟**。过不了就改 README 直到过 —— 改 README，不是改口径。
- [ ] 冒烟用的是**没有任何 API key** 的环境（评审多半没有 key）。

### A-2 证据束

- [ ] `evidence/` 下每个文件首行都是 `# generated at <ISO8601> from <git sha>`。
- [ ] 那个 sha **不带 `-dirty` 后缀**（带了说明生成时工作区是脏的，重跑）。
- [ ] `INDEX.json` 里的 `git_sha` 与提交的 commit 一致。
- [ ] 全部 `*.db` **没有**入库（`.gitignore` 挡着，`git status` 里不该看见它们）。
- [ ] 证据束里 grep 不到任何真密钥（生成脚本已做出口脱敏 + 哨兵反查，
      但**提交前再人肉扫一遍** `MAOS_LLM_API_KEY` / `MATRIX_TOKEN` 的值）。

### A-3 文档

- [ ] README 第一屏就能看到 `python3 scripts/verify.py`。
- [ ] README 里所有命令都是 `python3`（评委在 macOS 上敲 `python` 会 command not found）。
- [ ] 七份文档齐：`architecture` / `domain-portability` / `authoritative-facts` /
      `agentteams-mapping` / `agent-identity` / `skill-catalog` / `toolport-contract`。
- [ ] 三份**代码生成**的文档是重新生成过的，不是手改的（`--check` 绿即可）。
- [ ] `docs/BACKLOG.md` 与 `docs/DECISIONS.md` **保留在仓库里**，不要为了好看删掉 ——
      「知道自己哪里没做完」本身是可信度的一部分。

### A-4 口径一致性（最容易被问穿的地方）

逐条确认材料里**没有**把下面任何一条说过头：

| 事实 | 只能这么说 | 不许这么说 |
| :-- | :-- | :-- |
| 政策数据与历史案例 | 「按行业惯例构造的合成数据」 | 「真实企业政策」 |
| 支付网关 | 「错误码与异步时序对齐支付宝开放平台公开规范；演示用模拟实现」 | 「接入了支付宝」 |
| Matrix 房间 | 「镜像层已实现，降级路径实测等价，真房间待接通」 | 「全过程在 Element 里跑通」 |
| StorePort / PolarDB | 「有地基、未接线；PG 后端是空壳且拒绝回落」 | 「后端已可插拔切 PolarDB」 |
| AutoGen | 「可插拔内核之一，未在复赛演示中启用」 | 「基于 AutoGen 构建」 |
| replan 换渠道 | 按合并状态如实说（见 `docs/BACKLOG.md ## task-W6` 第 1 条） | 「网关失败会自动换渠道重试到上限」 |
| 场景覆盖 | 「`run.py` 无参跑 1–6，失败路径需 `--scenario 7`」 | 「一条命令跑全部七个场景」 |

---

## B. 方案 PPT ↔ 评委要求逐条对照

每一行都要能在 PPT 里指出**哪一页**，并且那一页给得出**可核验证据**。
（对照表原文见 `docs/EXECUTION.md` 附 C；README §8 是同一张表的仓库版。）

| # | 评委要求 | PPT 页 | 证据 | ✓ |
| :-- | :-- | :-- | :-- | :-- |
| 1 | 一条脱敏真实退款需求的可执行纵向切片 | ___ | `evidence/scenario-6,7/` | ☐ |
| 2 | AgentTeams 事件链 | ___ | `docs/agentteams-mapping.md` + `trace.json` | ☐ |
| 3 | 关键 Skill 的真实调用 | ___ | `event_log` 的 `SkillInvoked` | ☐ |
| 4 | 返工 / HITL Trace | ___ | `evidence/scenario-2,3,5,7/trace.json` | ☐ |
| 5 | Evidence Bundle | ___ | `verify.py` 7/7 | ☐ |
| 6 | 业务对象关联到同一案例 | ___ | verify 第 2 项 | ☐ |
| 7 | 外部系统保留权威事实，区分已提出/处理中/已到账 | ___ | verify 第 3 项 + 越权拒绝单测 | ☐ |
| 8 | RAG 面向 workflow 规划 | ___ | `kb-hits.json` + `dag-diff.json` | ☐ |
| 9 | 先结构化过滤再组合召回（评委给的字段顺序） | ___ | `maos/kb/retriever.py` + 跨租户不召回单测 | ☐ |
| 10 | 减少遗漏财务复核 / 错误套用政策 / 无限重试 | ___ | 第六道闸 + 政策版本锁定 + `MAOS_MAX_REPLAN` | ☐ |
| 11 | 历史流程不能替代当前订单事实和人工授权 | ___ | `maos/kb/guardrails.py` 三条断言 + 护栏单测 | ☐ |
| 12 | 以到账 / 客户确认 / 人工纠错验证 DAG | ___ | `result.json` 的 `business_outcome` | ☐ |
| 13 | 只有证据完整且外部结果明确的案例进默认知识层 | ___ | verify 第 7 项 | ☐ |

另外三条是**评委三段反馈的诊断**，PPT 必须正面回应：

- [ ] **没有可执行制品和运行证据** → 指向 `verify.py` 一条命令。
- [ ] **现实业务锚点不足** → 指向退款域纵切（不是软件域自证式 demo）。
- [ ] **「所有 Agent 都回复完成」≠ 业务成功** → 指向场景 7 的
      `biz_status=compensated` + `settled` 观察 0 条。

### PPT 逐页自查

- [ ] 每一页「我们做到了 X」的断言，都能在仓库里指出**文件 + 行号**或**一条命令**。
- [ ] 架构图与 `docs/architecture.md` 的图**一致**（别是两个版本）。
- [ ] 数据口径页：合成数据 / 真实规范 / 模拟实现三者分得清清楚楚（见 A-4）。
- [ ] 没有一页写着仓库里不存在的能力。

---

## C. Demo 视频

- [ ] 按 [`docs/demo-script.md`](demo-script.md) 录，**失败路径为主线**。
- [ ] 录制前跑完该文件的「录制前置」六条命令，全绿。
- [ ] 录制前确认该文件末尾的**三件事**（Element 是否接通 / replan 是否合并 /
      审批由谁驱动），按现状调整念词 —— **不许演不存在的功能**。
- [ ] 时长 3–5 分钟（手册口径；官方上限**待确认**）。
- [ ] 终端字号够大，后排能看清；窗口 ≥ 100 列。
- [ ] 画面里没有露出任何 key、token、homeserver 地址
      （房间设置页、浏览器开发者工具都别开着录）。
- [ ] 最后一屏是「同一个内核，两个域」+ `git diff --stat` 的空输出。
- [ ] 分辨率 / 格式 / 大小上限 / 是否要字幕：**待确认**，照官方通知补。

---

## D. 提交前最后一遍

- [ ] commit 全部落盘，工作区干净（`git status` 空）。
- [ ] **推送由人类手动做**（仓库纪律：Claude 只许本地 commit，禁止 push）。
- [ ] 仓库链接可访问，且落地页就是根 README。
- [ ] 三件套的版本对得上：PPT 里引用的行号、视频里跑出的数字、仓库当前 HEAD
      是同一个状态。**改了代码就要回来重录那一镜、重生成那三份文档。**
